from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from browser import BrowserManager
from config import load_settings
from scheduler import BotScheduler
from state_store import StateStore


def configure_logging(level: str, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3))

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_file)

    # Keep our bot logs readable even when LOG_LEVEL=DEBUG.
    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("selenium").setLevel(logging.INFO)
    logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(logging.INFO)

    store = StateStore(Path("./state.sqlite3"))
    browser = BrowserManager(settings)
    scheduler = BotScheduler(settings, browser, store)

    try:
        scheduler.run_forever()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Stopping bot by keyboard interrupt")
    finally:
        browser.close()
        store.close()


if __name__ == "__main__":
    main()
