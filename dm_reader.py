from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from selenium.webdriver.common.by import By

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreadSnapshot:
    thread_url: str
    message_fingerprint: str
    message_text: str
    observed_at_utc: str


def _extract_latest_message_text(driver) -> str:
    # Instagram UI changes frequently. We try a sequence of generic selectors.
    candidates = [
        "//div[@role='main']//div[contains(@class, 'x9f619') and @dir='auto']",
        "//div[@role='main']//span[@dir='auto']",
        "//div[@role='main']//div[@dir='auto']",
    ]

    texts: list[str] = []
    for xpath in candidates:
        for el in driver.find_elements(By.XPATH, xpath):
            text = (el.text or "").strip()
            if text:
                texts.append(text)
        if texts:
            break

    if not texts:
        return ""

    return texts[-1]


def read_thread_snapshot(driver, thread_url: str) -> ThreadSnapshot:
    driver.get(thread_url)

    latest_text = _extract_latest_message_text(driver)
    observed_at = datetime.now(timezone.utc).isoformat()

    fingerprint = f"{latest_text}|{observed_at[:16]}" if latest_text else f"EMPTY|{observed_at[:16]}"
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
