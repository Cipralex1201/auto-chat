from __future__ import annotations

import logging
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService

from config import Settings

LOGGER = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.driver: webdriver.Firefox | None = None
        self.started_at_monotonic: float | None = None

    def _build_driver(self) -> webdriver.Firefox:
        profile_dir: Path = self.settings.firefox_profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)

        options = FirefoxOptions()
        options.add_argument("-profile")
        options.add_argument(str(profile_dir))
        if self.settings.headless:
            options.add_argument("-headless")

        if self.settings.geckodriver_path is not None:
            service = FirefoxService(executable_path=str(self.settings.geckodriver_path))
            driver = webdriver.Firefox(service=service, options=options)
        else:
            # Let Selenium Manager / system PATH resolve geckodriver automatically.
            driver = webdriver.Firefox(options=options)
        driver.set_page_load_timeout(self.settings.page_load_timeout_sec)
        return driver

    def get_driver(self) -> webdriver.Firefox:
        if self.driver is None:
            LOGGER.info("Starting Firefox WebDriver")
            self.driver = self._build_driver()
            self.started_at_monotonic = time.monotonic()
        return self.driver

    def is_running(self) -> bool:
        if self.driver is None:
            return False
        try:
            _ = self.driver.current_url
            return True
        except WebDriverException:
            return False

    def should_force_restart(self) -> bool:
        if self.started_at_monotonic is None:
            return False
        return (time.monotonic() - self.started_at_monotonic) >= self.settings.force_browser_restart_sec

    def restart(self) -> webdriver.Firefox:
        LOGGER.info("Restarting Firefox WebDriver")
        self.close()
        return self.get_driver()

    def close(self) -> None:
        if self.driver is not None:
            LOGGER.info("Closing Firefox WebDriver")
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Error while closing Firefox WebDriver")
            finally:
                self.driver = None
                self.started_at_monotonic = None
