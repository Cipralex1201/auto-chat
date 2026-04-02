from __future__ import annotations

import logging
from typing import Final

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

LOGGER = logging.getLogger(__name__)

INSTAGRAM_HOME: Final[str] = "https://www.instagram.com/"
INSTAGRAM_LOGIN: Final[str] = "https://www.instagram.com/accounts/login/"


def is_logged_in(driver) -> bool:
    try:
        driver.get(INSTAGRAM_HOME)
    except TimeoutException:
        LOGGER.warning("Timeout while opening Instagram home, continuing with URL checks")

    current = (driver.current_url or "").lower()
    if "/accounts/login" in current:
        return False

    login_inputs = driver.find_elements(By.NAME, "username")
    return len(login_inputs) == 0


def login_if_needed(driver, username: str, password: str) -> None:
    if is_logged_in(driver):
        return

    LOGGER.info("Instagram session is not logged in, trying credential login")
    if not username or not password:
        raise RuntimeError("Missing IG_USERNAME or IG_PASSWORD in environment")

    driver.get(INSTAGRAM_LOGIN)

    username_input = driver.find_element(By.NAME, "username")
    password_input = driver.find_element(By.NAME, "password")

    username_input.clear()
    username_input.send_keys(username)
    password_input.clear()
    password_input.send_keys(password)

    # Click login button by form submit role to avoid localization-specific text matching.
    login_buttons = driver.find_elements(By.XPATH, "//button[@type='submit']")
    if not login_buttons:
        raise RuntimeError("Could not find Instagram login submit button")
    login_buttons[0].click()

    # Post-login challenges (2FA/checkpoint) may need manual completion.
    if not is_logged_in(driver):
        LOGGER.warning(
            "Still not logged in after submitting credentials. "
            "Complete 2FA/challenge manually in opened browser, then the next check cycle will continue."
        )
