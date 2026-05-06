from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from browser import BrowserManager
from config import Settings
from dm_reader import ThreadSnapshot, read_watched_threads
from reply_llm import generate_reply_placeholder
from sender import send_message
from session import login_if_needed
from state_store import StateStore

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

LOGGER = logging.getLogger(__name__)


@dataclass
class ModeState:
    mode: str = "idle"
    last_new_message_monotonic: float | None = None
    active_expire_after_sec: int = 0
    last_activity_monotonic: float | None = None


class BotScheduler:
    def __init__(self, settings: Settings, browser: BrowserManager, store: StateStore) -> None:
        self.settings = settings
        self.browser = browser
        self.store = store
        self.mode_state = ModeState()

    def _rand_idle_interval(self) -> int:
        return random.randint(self.settings.idle_min_sec, self.settings.idle_max_sec)

    def _rand_active_interval(self) -> int:
        return random.randint(self.settings.active_min_sec, self.settings.active_max_sec)

    def _rand_first_reply_delay(self) -> int:
        return random.randint(self.settings.first_reply_min_sec, self.settings.first_reply_max_sec)

    def _rand_followup_reply_delay(self) -> int:
        return random.randint(self.settings.followup_reply_min_sec, self.settings.followup_reply_max_sec)

    def _rand_conversation_expiry(self) -> int:
        return random.randint(self.settings.conversation_expire_min_sec, self.settings.conversation_expire_max_sec)

    def _should_skip_reply(self) -> bool:
        return random.random() < self.settings.skip_reply_probability

    def _looks_like_bot_message(self, text: str) -> bool:
        normalized = text.strip().lower()
        if not normalized:
            return False
        if normalized.startswith("auto-reply placeholder:"):
            return True
        if normalized == self.settings.dry_run_reply_text.strip().lower():
            return True
        return False

    def _ensure_thread_open(self, driver, thread_url: str, timeout_sec: int = 10) -> None:
        current_url = (driver.current_url or "").lower().rstrip("/")
        target = (thread_url or "").lower().rstrip("/")
        if target and target not in current_url:
            LOGGER.debug("Navigating to thread before sending: %s", thread_url)
            driver.get(thread_url)
        try:
            WebDriverWait(driver, timeout_sec).until(
                EC.presence_of_element_located((By.XPATH, "(//main | //*[@role='main'])[1]"))
            )
        except TimeoutException:
            LOGGER.warning("Thread main area did not become ready in time before sending: %s", thread_url)

    def _enter_active_mode(self) -> None:
        self.mode_state.mode = "active"
        self.mode_state.last_new_message_monotonic = time.monotonic()
        self.mode_state.active_expire_after_sec = self._rand_conversation_expiry()
        LOGGER.info("Switched to ACTIVE mode; expiry in %s sec", self.mode_state.active_expire_after_sec)

    def _maybe_exit_active_mode(self) -> None:
        if self.mode_state.mode != "active":
            return
        if self.mode_state.last_new_message_monotonic is None:
            self.mode_state.mode = "idle"
            return
        elapsed = time.monotonic() - self.mode_state.last_new_message_monotonic
        if elapsed >= self.mode_state.active_expire_after_sec:
            self.mode_state.mode = "idle"
            LOGGER.info("Switched to IDLE mode (active conversation expired)")

    def _handle_snapshots(self, driver, snapshots: list[ThreadSnapshot]) -> None:
        saw_new_message = False

        for snap in snapshots:
            state = self.store.get_thread_state(snap.thread_url)

            if snap.message_fingerprint == "EMPTY":
                LOGGER.warning("Skipping thread %s this cycle: extraction returned EMPTY", snap.thread_url)
                continue

            has_new = state.last_seen_fingerprint != snap.message_fingerprint
            LOGGER.debug(
                "Thread %s: prev_fp=%s new_fp=%s text_len=%d has_new=%s",
                snap.thread_url,
                state.last_seen_fingerprint,
                snap.message_fingerprint,
                len(snap.message_text),
                has_new,
            )

            if has_new:
                saw_new_message = True
                LOGGER.info("New message detected in thread: %s", snap.thread_url)

                handled = False

                if self._looks_like_bot_message(snap.message_text):
                    LOGGER.info("Skipping reply in thread=%s because latest message looks bot-authored", snap.thread_url)
                    handled = True
                else:
                    first_reply = state.first_reply_sent == 0
                    delay_sec = self._rand_first_reply_delay() if first_reply else self._rand_followup_reply_delay()

                    if self._should_skip_reply():
                        # Intentionally do NOT mark as handled; we'll retry next cycle.
                        LOGGER.info("Skipping immediate reply for this cycle (will retry next poll): %s", snap.thread_url)
                    else:
                        LOGGER.info("Waiting %s sec before reply in thread: %s", delay_sec, snap.thread_url)
                        time.sleep(delay_sec)

                        # Ensure we're on the correct thread right before composing/sending.
                        self._ensure_thread_open(driver, snap.thread_url)

                        reply_text = generate_reply_placeholder(snap.message_text, self.settings.dry_run_reply_text)
                        if self.settings.enable_sending:
                            ok = send_message(driver, reply_text)
                            LOGGER.info(
                                "Send attempted for thread=%s success=%s text=%s",
                                snap.thread_url,
                                ok,
                                reply_text[:120],
                            )
                            if ok:
                                handled = True
                                state.first_reply_sent = 1
                            else:
                                handled = False
                                LOGGER.warning("Send failed; will retry next poll (thread=%s)", snap.thread_url)
                        else:
                            LOGGER.info("DRY RUN reply for thread=%s text=%s", snap.thread_url, reply_text)
                            handled = True
                            state.first_reply_sent = 1

                if handled:
                    self.store.upsert_thread_state(
                        thread_url=snap.thread_url,
                        last_seen_fingerprint=snap.message_fingerprint,
                        last_seen_text=snap.message_text,
                        last_activity_utc=snap.observed_at_utc,
                        first_reply_sent=state.first_reply_sent,
                    )
                else:
                    # Leave the previous fingerprint intact so the same inbound message
                    # remains "new" and we will retry on the next poll.
                    self.store.upsert_thread_state(
                        thread_url=snap.thread_url,
                        last_seen_fingerprint=state.last_seen_fingerprint,
                        last_seen_text=state.last_seen_text,
                        last_activity_utc=state.last_activity_utc,
                        first_reply_sent=state.first_reply_sent,
                    )
            else:
                # Keep state stable when no actual new message was detected.
                self.store.upsert_thread_state(
                    thread_url=snap.thread_url,
                    last_seen_fingerprint=state.last_seen_fingerprint,
                    last_seen_text=state.last_seen_text,
                    last_activity_utc=state.last_activity_utc,
                    first_reply_sent=state.first_reply_sent,
                )

        if saw_new_message:
            self.mode_state.last_activity_monotonic = time.monotonic()
            self._enter_active_mode()

    def _check_inbox_once(self) -> None:
        driver = self.browser.get_driver()

        if self.browser.should_force_restart():
            driver = self.browser.restart()

        login_if_needed(driver, self.settings.ig_username, self.settings.ig_password)

        if not self.settings.watched_threads:
            LOGGER.warning("No watched threads configured (INSTAGRAM_WATCHED_THREADS is empty)")
            return

        snapshots = read_watched_threads(driver, self.settings.watched_threads)
        self._handle_snapshots(driver, snapshots)

    def _maybe_close_browser_in_idle(self) -> None:
        if self.mode_state.mode != "idle":
            return
        if not self.browser.is_running():
            return

        if self.mode_state.last_activity_monotonic is None:
            LOGGER.info("Idle mode with no recent activity, closing browser")
            self.browser.close()
            return

        elapsed = time.monotonic() - self.mode_state.last_activity_monotonic
        if elapsed >= self.settings.idle_browser_grace_sec:
            LOGGER.info("Deep idle reached (%ss), closing browser", int(elapsed))
            self.browser.close()

    def run_forever(self) -> None:
        LOGGER.info("Scheduler started at %s", datetime.now(timezone.utc).isoformat())
        while True:
            self._maybe_exit_active_mode()

            try:
                self._check_inbox_once()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Inbox check failed")

            self._maybe_close_browser_in_idle()

            interval = self._rand_active_interval() if self.mode_state.mode == "active" else self._rand_idle_interval()
            LOGGER.info("Mode=%s, next inbox check in %s sec", self.mode_state.mode, interval)
            time.sleep(interval)
