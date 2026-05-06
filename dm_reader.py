from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from selenium.common.exceptions import TimeoutException
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
    observed_at_utc: str


def _wait_thread_ready(driver, timeout_sec: int = 10) -> None:
    wait = WebDriverWait(driver, timeout_sec)
    wait.until(EC.presence_of_element_located((By.XPATH, MAIN_CONTAINER_XPATH)))


def _extract_latest_message_text(driver) -> str:
    # Prefer selectors that stay inside the DM main area and avoid composer fields.
    candidates = [
        f"{MAIN_CONTAINER_XPATH}//*[@dir='auto' and normalize-space() and not(ancestor::*[@role='textbox'])]",
        f"{MAIN_CONTAINER_XPATH}//*[@dir='ltr' and normalize-space() and not(ancestor::*[@role='textbox'])]",
        # Fallbacks for DOM variants where message bubbles lack dir=auto/ltr.
        f"{MAIN_CONTAINER_XPATH}//*[@role='row']//*[normalize-space() and not(ancestor::*[@role='textbox'])]",
        f"{MAIN_CONTAINER_XPATH}//span[normalize-space() and not(ancestor::*[@role='textbox'])]",
    ]

    ignored_exact = {
        "seen",
        "sent",
        "delivered",
        "active",
        "typing...",
        "message",
    }

    # Instead of trusting DOM order, pick the *visually* newest message by Y coordinate.
    best_text: str = ""
    best_y: int | None = None
    seen: set[str] = set()
    for xpath in candidates:
        elements = driver.find_elements(By.XPATH, xpath)
        LOGGER.debug("Selector %s matched %d elements", xpath, len(elements))

        for el in elements:
            text = (el.text or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in ignored_exact:
                continue
            # Keep short real messages too (e.g., "ok"), but drop isolated UI glyph-like chars.
            if len(text) == 1 and not text.isalnum():
                continue
            if lowered in seen:
                continue
            seen.add(lowered)
            try:
                y = int(el.location.get("y", 0))
            except Exception:  # noqa: BLE001
                y = 0
            if best_y is None or y >= best_y:
                best_y = y
                best_text = text

        if best_text:
            break

    if not best_text:
        return ""

    LOGGER.debug(
        "Extracted latest message text by Y (y=%s, len=%d): %s",
        best_y,
        len(best_text),
        best_text[:120],
    )
    return best_text


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
    latest_text = _extract_latest_message_text(driver)

    # Retry once if extraction was empty to avoid false EMPTY cycles.
    if not latest_text:
        LOGGER.debug("Empty extraction on first attempt, retrying once")
        try:
            driver.refresh()
            _wait_thread_ready(driver)
        except TimeoutException:
            LOGGER.warning("Retry refresh timed out for thread: %s", thread_url)
        time.sleep(1.0)
        latest_text = _extract_latest_message_text(driver)

    observed_at = datetime.now(timezone.utc).isoformat()

    if latest_text:
        content_hash = hashlib.sha256(latest_text.encode()).hexdigest()[:16]
        fingerprint = f"text:{content_hash}"
    else:
        fingerprint = "EMPTY"
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
