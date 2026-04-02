from __future__ import annotations

import logging

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

LOGGER = logging.getLogger(__name__)


def send_message(driver, text: str) -> bool:
    # Try a few selectors to find the composer textbox in DM thread view.
    candidates = [
        "//div[@role='textbox']",
        "//textarea[@placeholder='Message...']",
        "//textarea",
    ]

    composer = None
    for xpath in candidates:
        elements = driver.find_elements(By.XPATH, xpath)
        if elements:
            composer = elements[-1]
            break

    if composer is None:
        LOGGER.warning("Could not find message composer textbox")
        return False

    composer.click()
    composer.send_keys(text)
    composer.send_keys(Keys.ENTER)
    return True
