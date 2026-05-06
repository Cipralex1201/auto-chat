from __future__ import annotations

import hashlib
import logging
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
    message_fingerprint: str
    message_text: str
    latest_direction: str  # incoming | outgoing | unknown
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
    if lowered.startswith("seen") and len(lowered) <= 25:
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

    # Prefer scanning message rows first; fall back to a broader scan if none match.
    row_xpath = f"{MAIN_CONTAINER_XPATH}//*[@role='row']"
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

        LOGGER.debug("Row selector matched %d elements (attempt=%d)", len(rows), attempt + 1)

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
    latest_text, latest_direction = _extract_latest_message(driver)

    # Retry once if extraction was empty to avoid false EMPTY cycles.
    if not latest_text:
        LOGGER.debug("Empty extraction on first attempt, retrying once")
        try:
            driver.refresh()
            _wait_thread_ready(driver)
        except TimeoutException:
            LOGGER.warning("Retry refresh timed out for thread: %s", thread_url)
        time.sleep(1.0)
        latest_text, latest_direction = _extract_latest_message(driver)

    observed_at = datetime.now(timezone.utc).isoformat()

    if latest_text:
        content_hash = hashlib.sha256(f"{latest_direction}\n{latest_text}".encode()).hexdigest()[:16]
        fingerprint = f"msg:{latest_direction}:{content_hash}"
    else:
        fingerprint = "EMPTY"
        latest_direction = "unknown"
        LOGGER.warning(
            "No message text extracted after retry. url=%s current_url=%s title=%s",
            thread_url,
            driver.current_url,
            driver.title,
        )

    LOGGER.debug("Thread snapshot: url=%s fingerprint=%s text_len=%d", thread_url, fingerprint, len(latest_text))
    return ThreadSnapshot(
        thread_url=thread_url,
        message_fingerprint=fingerprint,
        message_text=latest_text,
        latest_direction=latest_direction,
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
