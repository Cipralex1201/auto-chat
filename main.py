from __future__ import annotations

import logging
from pathlib import Path

from browser import BrowserManager
from config import load_settings
from scheduler import BotScheduler
from state_store import StateStore


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

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
