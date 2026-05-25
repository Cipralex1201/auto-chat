from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

LOGGER = logging.getLogger(__name__)


# Instagram frequently renders the main app container as either a <main> element
# or an element with role="main".
MAIN_CONTAINER_XPATH = "(//main | //*[@role='main'])[1]"


@dataclass(frozen=True)
class ThreadSnapshot:
    thread_url: str
    # Fingerprint of the observed tail window of messages (diagnostic / change detection).
    message_fingerprint: str

    # Best-effort latest bubble info (kept for backward compatibility).
    message_text: str
    latest_direction: str  # incoming | outgoing | unknown

    # Stable dedupe key for the most recent incoming message in the observed tail window.
    latest_incoming_fingerprint: str | None
    latest_incoming_text: str | None

    # For optional send verification/debug.
    latest_outgoing_text: str | None

    # Chronological tail window of messages for history persistence.
    tail_messages: list[tuple[str, str]]
    observed_at_utc: str


def _wait_thread_ready(driver, timeout_sec: int = 10) -> None:
    wait = WebDriverWait(driver, timeout_sec)
    wait.until(EC.presence_of_element_located((By.XPATH, MAIN_CONTAINER_XPATH)))


def _is_ignored_text(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return True

    lowered = normalized.lower()
    ignored_exact = {
        "seen",
        "seen just now",
        "sent",
        "delivered",
        "active",
        "typing...",
        "message",
        # Inbox/thread UI labels that are not actual messages.
        "unread",
        "new messages",
        "new message",
    }
    if lowered in ignored_exact:
        return True

    # Unicode ellipsis / locale variants.
    if lowered in {"typing…", "typing..", "typing."}:
        return True
    if lowered.startswith("typing") and ("..." in lowered or "…" in lowered) and len(lowered) <= 20:
        return True
    # Hungarian / common localizations (best-effort).
    # Examples: "gépel...", "gépel…"
    if lowered.startswith("gépel") and ("..." in lowered or "…" in lowered) and len(lowered) <= 20:
        return True
    if lowered.startswith("seen") and len(lowered) <= 25:
        return True

    # Common timestamp / age labels in the message list that are not actual messages.
    # Examples observed: "17w", and sometimes HH:MM.
    if re.fullmatch(r"\d{1,3}[smhdw]", lowered):
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}", lowered):
        return True
    # 12-hour clock labels sometimes appear as their own rows (e.g. "1:48 AM").
    if re.fullmatch(r"\d{1,2}:\d{2}\s*(?:am|pm|a\.?m\.?|p\.?m\.?)", lowered):
        return True
    if re.fullmatch(r"\d{1,3}\s*(sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours|day|days|week|weeks)", lowered):
        return True

    # Keep short real messages too (e.g., "ok"), but drop isolated UI glyph-like chars.
    if len(normalized) == 1 and not normalized.isalnum():
        return True

    # Typing indicator bubble often renders as just dots/ellipsis glyphs.
    # Examples observed across UIs: "...", "···", "…", "• • •".
    compact = re.sub(r"\s+", "", normalized)
    if compact and re.fullmatch(r"[.·•…]{1,8}", compact):
        return True

    # Inbox/thread-list preview prefix (not an actual message bubble).
    if lowered.startswith("you:") and len(lowered) <= 200:
        return True

    return False


def _safe_text(el) -> str | None:
    try:
        return (el.text or "").strip()
    except StaleElementReferenceException:
        return None


def _safe_rect(el) -> dict | None:
    try:
        return el.rect
    except StaleElementReferenceException:
        return None
    except Exception:  # noqa: BLE001
        return None


_USERNAME_LIKE = re.compile(r"^[a-z0-9._]{3,40}$")


def _looks_like_ig_handle(text: str) -> bool:
    """Return True if `text` looks like an Instagram handle (no '@').

    We keep this conservative so we don't accidentally treat display names
    (often Title Case) as handles.
    """

    norm = (text or "").strip()
    if not norm:
        return False

    compact = "".join(norm.split()).lstrip("@")
    if not compact:
        return False

    # Handles are typically lowercase in the header UI; treat mixed/upper as display-name.
    if compact != compact.lower():
        return False

    return _USERNAME_LIKE.fullmatch(compact) is not None


def _get_thread_header_info(driver) -> tuple[set[str], set[str], float | None]:
    """Return (header_texts_lower, handle_texts_lower, header_bottom_y).

    Instagram can render thread header/caption strings inside the same main pane
    and they can be picked up by broad bubble/text XPaths.

    We treat handle-like header strings (e.g. "adamgyory") as *always ignored*
    wherever they appear, because the UI may temporarily re-render them lower in
    the pane during active updates.
    """

    header_bottom_y: float | None = None
    try:
        header_el = driver.find_element(By.XPATH, f"{MAIN_CONTAINER_XPATH}//header")
        header_rect = _safe_rect(header_el)
        if header_rect is not None:
            header_bottom_y = float(header_rect.get("y", 0.0)) + float(header_rect.get("height", 0.0))
    except Exception:  # noqa: BLE001
        header_bottom_y = None

    xpaths = [
        f"{MAIN_CONTAINER_XPATH}//header//*[self::h1 or self::h2][normalize-space()]",
        f"{MAIN_CONTAINER_XPATH}//header//span[normalize-space()]",
        f"{MAIN_CONTAINER_XPATH}//header//div[normalize-space()]",
    ]

    header_texts: set[str] = set()
    header_handles: set[str] = set()

    for xp in xpaths:
        try:
            els = driver.find_elements(By.XPATH, xp)
        except Exception:  # noqa: BLE001
            els = []

        for el in els:
            text = _safe_text(el)
            if not text:
                continue
            norm = _normalize_text(text)
            if not norm:
                continue
            if _is_ignored_text(norm):
                continue

            lowered = norm.lower()
            # Keep header candidates reasonably short to avoid catching large blocks.
            if not (1 < len(lowered) <= 60):
                continue

            header_texts.add(lowered)
            if _looks_like_ig_handle(norm):
                header_handles.add(lowered.lstrip("@"))

        if header_texts:
            # Prefer earlier/stronger header signals; if we found any, that's enough.
            break

    return header_texts, header_handles, header_bottom_y


def _classify_direction(main_mid_x: float, bubble_rect: dict, margin_px: float = 30.0) -> str:
    try:
        bubble_mid_x = float(bubble_rect.get("x", 0.0)) + float(bubble_rect.get("width", 0.0)) / 2.0
    except Exception:  # noqa: BLE001
        return "unknown"

    if bubble_mid_x < (main_mid_x - margin_px):
        return "incoming"
    if bubble_mid_x > (main_mid_x + margin_px):
        return "outgoing"
    return "unknown"


def _extract_latest_message(driver) -> tuple[str, str]:
    """Return (latest_text, direction) where direction is incoming/outgoing/unknown.

    This is intentionally geometry-based to be resilient across DOM variants.
    """

    try:
        main = driver.find_element(By.XPATH, MAIN_CONTAINER_XPATH)
        main_rect = main.rect
        main_mid_x = float(main_rect.get("x", 0.0)) + float(main_rect.get("width", 0.0)) / 2.0
    except Exception:  # noqa: BLE001
        main_mid_x = 0.0

    # Prefer scanning message rows/list items first; fall back to a broader scan if none match.
    row_xpath = f"{MAIN_CONTAINER_XPATH}//*[@role='row']"
    listitem_xpath = f"{MAIN_CONTAINER_XPATH}//*[@role='listitem']"
    bubble_xpath = (
        ".//*[@dir='auto' or @dir='ltr'][normalize-space() and not(ancestor::*[@role='textbox'])]"
        " | .//span[normalize-space() and not(ancestor::*[@role='textbox'])]"
    )

    fallback_xpaths = [
        f"{MAIN_CONTAINER_XPATH}//*[@dir='auto' and normalize-space() and not(ancestor::*[@role='textbox'])]",
        f"{MAIN_CONTAINER_XPATH}//*[@dir='ltr' and normalize-space() and not(ancestor::*[@role='textbox'])]",
        f"{MAIN_CONTAINER_XPATH}//span[normalize-space() and not(ancestor::*[@role='textbox'])]",
    ]

    for attempt in range(2):
        best_text: str = ""
        best_rect: dict | None = None
        best_bottom: float | None = None
        seen: set[str] = set()

        try:
            rows = driver.find_elements(By.XPATH, row_xpath)
        except Exception:  # noqa: BLE001
            rows = []

        try:
            listitems = driver.find_elements(By.XPATH, listitem_xpath)
        except Exception:  # noqa: BLE001
            listitems = []

        LOGGER.debug(
            "Container selectors matched rows=%d listitems=%d (attempt=%d)",
            len(rows),
            len(listitems),
            attempt + 1,
        )

        def consider_candidate(text: str, rect: dict) -> None:
            nonlocal best_text, best_rect, best_bottom
            lowered = text.lower()
            if lowered in seen:
                return
            seen.add(lowered)

            bottom = float(rect.get("y", 0.0)) + float(rect.get("height", 0.0))
            if best_bottom is None or bottom >= best_bottom:
                best_bottom = bottom
                best_text = text
                best_rect = rect

        # Pass 1: row-based scan
        for row in rows:
            try:
                bubbles = row.find_elements(By.XPATH, bubble_xpath)
            except StaleElementReferenceException:
                continue
            except Exception:  # noqa: BLE001
                continue

            for el in bubbles:
                text = _safe_text(el)
                if text is None or _is_ignored_text(text):
                    continue
                rect = _safe_rect(el)
                if rect is None:
                    continue
                consider_candidate(text, rect)

        # Pass 1b: listitem-based scan (common alternative to role=row)
        if not best_text:
            for item in listitems:
                try:
                    bubbles = item.find_elements(By.XPATH, bubble_xpath)
                except StaleElementReferenceException:
                    continue
                except Exception:  # noqa: BLE001
                    continue

                for el in bubbles:
                    text = _safe_text(el)
                    if text is None or _is_ignored_text(text):
                        continue
                    rect = _safe_rect(el)
                    if rect is None:
                        continue
                    consider_candidate(text, rect)

        # Pass 2: broader fallback scan (in case role='row' isn't present)
        if not best_text:
            for xpath in fallback_xpaths:
                try:
                    elements = driver.find_elements(By.XPATH, xpath)
                except Exception:  # noqa: BLE001
                    elements = []
                LOGGER.debug("Fallback selector %s matched %d elements", xpath, len(elements))
                for el in elements:
                    text = _safe_text(el)
                    if text is None or _is_ignored_text(text):
                        continue
                    rect = _safe_rect(el)
                    if rect is None:
                        continue
                    consider_candidate(text, rect)

        if best_text and best_rect is not None:
            direction = _classify_direction(main_mid_x, best_rect)
            LOGGER.info(
                "Latest message extracted: direction=%s bottom=%.0f text=%s",
                direction,
                float(best_bottom or 0.0),
                best_text[:160],
            )
            return best_text, direction

        # If we got nothing, retry once after allowing the DOM to settle.
        time.sleep(0.15)

    return "", "unknown"


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def _fingerprint_parts(parts: list[tuple[str, str]]) -> str:
    payload = "\n".join([f"{d}\t{t}" for d, t in parts])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _extract_tail_window(driver, window_size: int = 8) -> tuple[str, list[tuple[str, str, float]]]:
    """Return (best_effort_latest_direction, candidates) where candidates are (direction, text, bottom).

    Candidates are deduped per-bubble using a coarse (text,bottom) key to avoid duplicate spans,
    but still allow repeated identical messages if they appear at different vertical positions.
    """

    # Determine the message pane midpoint using the composer position.
    # This helps us avoid extracting text from the left sidebar thread list.
    pane_mid_x = 0.0
    x_filter_min: float | None = None
    x_filter_max: float | None = None

    composer_xpaths = [
        f"{MAIN_CONTAINER_XPATH}//div[@role='textbox' and (@contenteditable='true' or @contenteditable='')]",
        f"{MAIN_CONTAINER_XPATH}//textarea",
    ]

    composer_rect: dict | None = None
    for cxp in composer_xpaths:
        try:
            els = driver.find_elements(By.XPATH, cxp)
        except Exception:  # noqa: BLE001
            els = []
        if not els:
            continue
        # Prefer the bottom-most composer (visually) in case there are multiple.
        best = None
        best_y = None
        for el in els:
            rect = _safe_rect(el)
            if rect is None:
                continue
            y = float(rect.get("y", 0.0))
            if best_y is None or y >= best_y:
                best_y = y
                best = rect
        if best is not None:
            composer_rect = best
            break

    if composer_rect is not None:
        cx = float(composer_rect.get("x", 0.0))
        cw = float(composer_rect.get("width", 0.0))
        pane_mid_x = cx + cw / 2.0
        # Allow some slack so bubbles just above/around the composer are included.
        # Keep the left slack tight to avoid accidentally reading the left sidebar.
        x_filter_min = cx - 80.0
        x_filter_max = cx + cw + 250.0
    else:
        try:
            main = driver.find_element(By.XPATH, MAIN_CONTAINER_XPATH)
            main_rect = main.rect
            pane_mid_x = float(main_rect.get("x", 0.0)) + float(main_rect.get("width", 0.0)) / 2.0
        except Exception:  # noqa: BLE001
            pane_mid_x = 0.0

    row_xpath = f"{MAIN_CONTAINER_XPATH}//*[@role='row']"
    listitem_xpath = f"{MAIN_CONTAINER_XPATH}//*[@role='listitem']"
    bubble_xpath = (
        ".//*[@dir='auto' or @dir='ltr'][normalize-space() and not(ancestor::*[@role='textbox'])]"
        " | .//span[normalize-space() and not(ancestor::*[@role='textbox'])]"
    )
    fallback_xpaths = [
        f"{MAIN_CONTAINER_XPATH}//*[@dir='auto' and normalize-space() and not(ancestor::*[@role='textbox'])]",
        f"{MAIN_CONTAINER_XPATH}//*[@dir='ltr' and normalize-space() and not(ancestor::*[@role='textbox'])]",
        f"{MAIN_CONTAINER_XPATH}//span[normalize-space() and not(ancestor::*[@role='textbox'])]",
    ]

    raw: list[tuple[str, str, float]] = []

    header_texts: set[str] = set()
    header_handles: set[str] = set()
    header_bottom_y: float | None = None
    try:
        header_texts, header_handles, header_bottom_y = _get_thread_header_info(driver)
    except Exception:  # noqa: BLE001
        header_texts, header_handles, header_bottom_y = set(), set(), None

    def add_candidate(text: str, rect: dict) -> None:
        norm = _normalize_text(text)
        if not norm or _is_ignored_text(norm):
            return

        lowered = norm.lower().lstrip("@")

        # Avoid treating the thread header username/handle as a message.
        # Robust behavior: ignore handle-like header strings wherever they appear,
        # because IG sometimes re-renders them lower in the pane during active updates.
        if header_handles and lowered in header_handles:
            return

        # For other header strings (e.g. display name), be conservative: ignore only
        # when they appear inside the header band.
        if header_texts and norm.lower() in header_texts:
            try:
                y = float(rect.get("y", 0.0))
            except Exception:  # noqa: BLE001
                y = 0.0
            threshold = (header_bottom_y + 40.0) if header_bottom_y is not None else 200.0
            if y <= threshold:
                return

        # If we found a composer, only accept candidates that visually live in the same pane.
        # This avoids accidentally reading thread previews from the left sidebar.
        if x_filter_min is not None and x_filter_max is not None:
            try:
                mid_x = float(rect.get("x", 0.0)) + float(rect.get("width", 0.0)) / 2.0
            except Exception:  # noqa: BLE001
                mid_x = None
            if mid_x is None or not (x_filter_min <= mid_x <= x_filter_max):
                return

        bottom = float(rect.get("y", 0.0)) + float(rect.get("height", 0.0))
        direction = _classify_direction(pane_mid_x, rect)
        raw.append((direction, norm, bottom))

    for attempt in range(2):
        raw.clear()

        try:
            rows = driver.find_elements(By.XPATH, row_xpath)
        except Exception:  # noqa: BLE001
            rows = []

        try:
            listitems = driver.find_elements(By.XPATH, listitem_xpath)
        except Exception:  # noqa: BLE001
            listitems = []

        for row in rows:
            try:
                bubbles = row.find_elements(By.XPATH, bubble_xpath)
            except Exception:  # noqa: BLE001
                continue
            for el in bubbles:
                text = _safe_text(el)
                if text is None:
                    continue
                rect = _safe_rect(el)
                if rect is None:
                    continue
                add_candidate(text, rect)

        if not raw:
            for item in listitems:
                try:
                    bubbles = item.find_elements(By.XPATH, bubble_xpath)
                except Exception:  # noqa: BLE001
                    continue
                for el in bubbles:
                    text = _safe_text(el)
                    if text is None:
                        continue
                    rect = _safe_rect(el)
                    if rect is None:
                        continue
                    add_candidate(text, rect)

        if not raw:
            for xpath in fallback_xpaths:
                try:
                    elements = driver.find_elements(By.XPATH, xpath)
                except Exception:  # noqa: BLE001
                    elements = []
                for el in elements:
                    text = _safe_text(el)
                    if text is None:
                        continue
                    rect = _safe_rect(el)
                    if rect is None:
                        continue
                    add_candidate(text, rect)

        if raw:
            # Deduplicate likely duplicates from nested spans: coarse bucket by (direction,text,bottom_bucket)
            seen_keys: set[tuple[str, str, int]] = set()
            deduped: list[tuple[str, str, float]] = []
            for direction, text, bottom in sorted(raw, key=lambda t: t[2]):
                key = (direction, text.lower(), int(bottom // 6))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append((direction, text, bottom))

            tail = deduped[-window_size:]
            latest_direction = tail[-1][0] if tail else "unknown"
            return latest_direction, tail

        time.sleep(0.15)

    return "unknown", []


def read_thread_snapshot(driver, thread_url: str) -> ThreadSnapshot:
    current_url = (driver.current_url or "").lower()
    normalized_target = thread_url.lower().rstrip("/")
    normalized_current = current_url.rstrip("/")

    if normalized_target not in normalized_current:
        LOGGER.debug("Navigating to watched thread: %s", thread_url)
        driver.get(thread_url)

    try:
        _wait_thread_ready(driver)
    except TimeoutException:
        LOGGER.warning("Thread main area did not become ready in time: %s", thread_url)

    # One short grace delay helps dynamic content settle after thread load.
    time.sleep(1.0)
    latest_direction, tail = _extract_tail_window(driver)

    # Retry once if extraction was empty to avoid false EMPTY cycles.
    if not tail:
        LOGGER.debug("Empty extraction on first attempt, retrying once")
        try:
            driver.refresh()
            _wait_thread_ready(driver)
        except TimeoutException:
            LOGGER.warning("Retry refresh timed out for thread: %s", thread_url)
        time.sleep(1.0)
        latest_direction, tail = _extract_tail_window(driver)

    observed_at = datetime.now(timezone.utc).isoformat()

    latest_text: str = tail[-1][1] if tail else ""

    tail_messages: list[tuple[str, str]] = [(d, t) for d, t, _ in tail] if tail else []

    if tail:
        parts = [(d, t) for d, t, _ in tail]
        observed_hash = _fingerprint_parts(parts)
        fingerprint = f"win:{observed_hash}"

        latest_incoming_fingerprint: str | None = None
        latest_incoming_text: str | None = None
        latest_outgoing_text: str | None = None

        last_in_idx: int | None = None
        last_out_idx: int | None = None
        for idx, (d, t, _bottom) in enumerate(tail):
            if d == "incoming":
                last_in_idx = idx
            elif d == "outgoing":
                last_out_idx = idx

        if last_in_idx is not None:
            # Stable per-bubble key: hash incoming text + coarse vertical bucket.
            # This stays stable across polls even if the tail window prefix changes.
            _d, _t, _b = tail[last_in_idx]
            bucket = int(float(_b) // 40)
            content_hash = hashlib.sha256(f"incoming\n{_t}\n{bucket}".encode()).hexdigest()[:16]
            latest_incoming_fingerprint = f"in:{content_hash}"
            latest_incoming_text = _t

        if last_out_idx is not None:
            latest_outgoing_text = tail[last_out_idx][1]
    else:
        fingerprint = "EMPTY"
        latest_direction = "unknown"
        latest_incoming_fingerprint = None
        latest_incoming_text = None
        latest_outgoing_text = None
        LOGGER.warning(
            "No message text extracted after retry. url=%s current_url=%s title=%s",
            thread_url,
            driver.current_url,
            driver.title,
        )

    LOGGER.debug(
        "Thread snapshot: url=%s fp=%s latest_dir=%s tail=%d latest_in_fp=%s",
        thread_url,
        fingerprint,
        latest_direction,
        len(tail),
        latest_incoming_fingerprint,
    )
    return ThreadSnapshot(
        thread_url=thread_url,
        message_fingerprint=fingerprint,
        message_text=latest_text,
        latest_direction=latest_direction,
        latest_incoming_fingerprint=latest_incoming_fingerprint,
        latest_incoming_text=latest_incoming_text,
        latest_outgoing_text=latest_outgoing_text,
        tail_messages=tail_messages,
        observed_at_utc=observed_at,
    )


def read_watched_threads(driver, thread_urls: list[str]) -> list[ThreadSnapshot]:
    snapshots: list[ThreadSnapshot] = []
    for thread_url in thread_urls:
        try:
            snapshots.append(read_thread_snapshot(driver, thread_url))
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to read thread: %s", thread_url)
    return snapshots
