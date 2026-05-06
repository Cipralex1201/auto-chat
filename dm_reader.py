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
    if re.fullmatch(r"\d{1,3}\s*(sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours|day|days|week|weeks)", lowered):
        return True

    # Keep short real messages too (e.g., "ok"), but drop isolated UI glyph-like chars.
    if len(normalized) == 1 and not normalized.isalnum():
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

    try:
        main = driver.find_element(By.XPATH, MAIN_CONTAINER_XPATH)
        main_rect = main.rect
        main_mid_x = float(main_rect.get("x", 0.0)) + float(main_rect.get("width", 0.0)) / 2.0
    except Exception:  # noqa: BLE001
        main_mid_x = 0.0

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

    def add_candidate(text: str, rect: dict) -> None:
        norm = _normalize_text(text)
        if not norm or _is_ignored_text(norm):
            return
        bottom = float(rect.get("y", 0.0)) + float(rect.get("height", 0.0))
        direction = _classify_direction(main_mid_x, rect)
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
            incoming_parts = parts[: last_in_idx + 1]
            latest_incoming_fingerprint = f"in:{_fingerprint_parts(incoming_parts)}"
            latest_incoming_text = tail[last_in_idx][1]

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
