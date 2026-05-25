from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from browser import BrowserManager
from config import Settings
from dm_reader import ThreadSnapshot, read_watched_threads
from reply_llm import generate_reply
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

        def now_utc_iso() -> str:
            return datetime.now(timezone.utc).isoformat()

        def parse_iso(ts: str | None) -> datetime | None:
            if not ts:
                return None
            try:
                return datetime.fromisoformat(ts)
            except Exception:  # noqa: BLE001
                return None

        # Guardrails against accidental spam on DOM bounces.
        min_reply_gap_sec = 2
        attempt_backoff_sec = 10
        max_attempts_per_inbound = 3

        # Message history storage and LLM context.
        history_n = getattr(self.settings, "llm_history_n", 20)
        max_store = getattr(self.settings, "max_stored_messages_per_thread", 1000)

        for snap in snapshots:
            state = self.store.get_thread_state(snap.thread_url)

            # Persist tail window into message history (idempotent overlap update).
            try:
                self.store.update_history_from_tail(
                    snap.thread_url,
                    getattr(snap, "tail_messages", []) or [],
                    snap.observed_at_utc,
                    max_per_thread=max_store,
                )
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to persist message history (thread=%s)", snap.thread_url)

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

            # Always persist the latest observed fingerprint/text for diagnostics.
            # This must NOT control deduplication of replies.
            self.store.upsert_thread_state(
                thread_url=snap.thread_url,
                last_seen_fingerprint=snap.message_fingerprint,
                last_seen_text=snap.message_text,
                last_activity_utc=snap.observed_at_utc,
                first_reply_sent=state.first_reply_sent,
                last_replied_incoming_fingerprint=state.last_replied_incoming_fingerprint,
                last_replied_incoming_text=state.last_replied_incoming_text,
                last_reply_utc=state.last_reply_utc,
                last_attempt_incoming_fingerprint=state.last_attempt_incoming_fingerprint,
                last_attempt_utc=state.last_attempt_utc,
                attempt_count=state.attempt_count,
            )

            latest_in = None
            latest_out = None
            try:
                latest_in = self.store.get_latest_incoming_message(snap.thread_url)
                latest_out = self.store.get_latest_outgoing_message(snap.thread_url)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to load latest in/out messages for thread (thread=%s)", snap.thread_url)

            if latest_in is None:
                LOGGER.debug("No incoming candidate found in DB; no reply (thread=%s)", snap.thread_url)
                continue

            incoming_msg_id = int(latest_in.id)
            incoming_text = (latest_in.text or "").strip()

            own_u = (getattr(self.settings, "ig_username", "") or "").strip().lstrip("@").lower()
            if own_u and incoming_text.strip().lstrip("@").lower() == own_u:
                LOGGER.info("Skipping: latest incoming equals own username (UI leak) (thread=%s)", snap.thread_url)
                # Mark as replied-to so it cannot trigger reply spam.
                self.store.upsert_thread_state(
                    thread_url=snap.thread_url,
                    last_seen_fingerprint=snap.message_fingerprint,
                    last_seen_text=snap.message_text,
                    last_activity_utc=snap.observed_at_utc,
                    first_reply_sent=state.first_reply_sent,
                    last_replied_incoming_fingerprint=getattr(snap, "latest_incoming_fingerprint", None),
                    last_replied_incoming_text=incoming_text,
                    last_reply_utc=state.last_reply_utc,
                    last_replied_incoming_msg_id=incoming_msg_id,
                    last_attempt_incoming_fingerprint=None,
                    last_attempt_utc=None,
                    last_attempt_incoming_msg_id=None,
                    attempt_count=0,
                )
                continue

            if state.last_replied_incoming_msg_id == incoming_msg_id:
                LOGGER.debug("Incoming already replied-to (msg_id=%s); no reply (thread=%s)", incoming_msg_id, snap.thread_url)
                continue

            # Fallback dedupe: if the latest inbound text matches the last replied inbound text
            # and the last reply was recent, treat it as already-handled. This protects against
            # accidental duplicate inserts of the same inbound due to DOM/extraction overlap drift.
            last_reply_dt = parse_iso(state.last_reply_utc)
            if (
                incoming_text
                and state.last_replied_incoming_text
                and incoming_text == (state.last_replied_incoming_text or "").strip()
                and last_reply_dt is not None
            ):
                since = (datetime.now(timezone.utc) - last_reply_dt).total_seconds()
                if since <= 300:
                    LOGGER.info(
                        "Skipping: inbound text matches recently replied inbound (%.1fs) (thread=%s)",
                        since,
                        snap.thread_url,
                    )
                    self.store.upsert_thread_state(
                        thread_url=snap.thread_url,
                        last_seen_fingerprint=snap.message_fingerprint,
                        last_seen_text=snap.message_text,
                        last_activity_utc=snap.observed_at_utc,
                        first_reply_sent=state.first_reply_sent,
                        last_replied_incoming_fingerprint=getattr(snap, "latest_incoming_fingerprint", None),
                        last_replied_incoming_text=incoming_text,
                        last_reply_utc=state.last_reply_utc,
                        last_replied_incoming_msg_id=incoming_msg_id,
                    )
                    continue

            # Echo guard: if extraction misclassified our own outgoing as incoming,
            # avoid reply loops by detecting when latest incoming == very recent outgoing text.
            if latest_out is not None:
                out_text = (latest_out.text or "").strip()
                if out_text and incoming_text and out_text == incoming_text:
                    out_dt = parse_iso(getattr(latest_out, "observed_at_utc", None))
                    in_dt = parse_iso(getattr(latest_in, "observed_at_utc", None))
                    if out_dt is not None and in_dt is not None:
                        delta = abs((in_dt - out_dt).total_seconds())
                        if delta <= 300:
                            LOGGER.info(
                                "Skipping: latest incoming matches recent outgoing (echo guard, %.1fs) (thread=%s)",
                                delta,
                                snap.thread_url,
                            )
                            # Mark as replied-to to suppress further loops.
                            self.store.upsert_thread_state(
                                thread_url=snap.thread_url,
                                last_seen_fingerprint=snap.message_fingerprint,
                                last_seen_text=snap.message_text,
                                last_activity_utc=snap.observed_at_utc,
                                first_reply_sent=state.first_reply_sent,
                                last_replied_incoming_fingerprint=state.last_replied_incoming_fingerprint,
                                last_replied_incoming_text=incoming_text,
                                last_reply_utc=state.last_reply_utc,
                                last_replied_incoming_msg_id=incoming_msg_id,
                                last_attempt_incoming_fingerprint=None,
                                last_attempt_utc=None,
                                last_attempt_incoming_msg_id=None,
                                attempt_count=0,
                            )
                            continue

            if self._looks_like_bot_message(incoming_text):
                LOGGER.info("Skipping: latest incoming looks bot-authored (thread=%s)", snap.thread_url)
                # Mark as replied-to so we don't loop on our own message echoes.
                self.store.upsert_thread_state(
                    thread_url=snap.thread_url,
                    last_seen_fingerprint=snap.message_fingerprint,
                    last_seen_text=snap.message_text,
                    last_activity_utc=snap.observed_at_utc,
                    first_reply_sent=state.first_reply_sent,
                    last_replied_incoming_fingerprint=getattr(snap, "latest_incoming_fingerprint", None),
                    last_replied_incoming_text=incoming_text,
                    last_reply_utc=state.last_reply_utc,
                    last_replied_incoming_msg_id=incoming_msg_id,
                    last_attempt_incoming_fingerprint=None,
                    last_attempt_utc=None,
                    last_attempt_incoming_msg_id=None,
                    attempt_count=0,
                )
                continue

            # Minimum gap between replies per thread.
            last_reply_dt = parse_iso(state.last_reply_utc)
            if last_reply_dt is not None:
                gap = (datetime.now(timezone.utc) - last_reply_dt).total_seconds()
                if gap < float(min_reply_gap_sec):
                    LOGGER.info(
                        "Reply gap guard: last reply %.1fs ago; delaying (thread=%s)",
                        gap,
                        snap.thread_url,
                    )
                    continue

            # Attempt backoff / attempt limit per inbound.
            if state.last_attempt_incoming_msg_id == incoming_msg_id:
                last_attempt_dt = parse_iso(state.last_attempt_utc)
                if state.attempt_count >= max_attempts_per_inbound:
                    LOGGER.warning(
                        "Max attempts reached for inbound; suppressing further retries (thread=%s)",
                        snap.thread_url,
                    )
                    continue
                if last_attempt_dt is not None:
                    since = (datetime.now(timezone.utc) - last_attempt_dt).total_seconds()
                    if since < float(attempt_backoff_sec):
                        LOGGER.info(
                            "Backoff active (%.1fs < %ss); retry later (thread=%s)",
                            since,
                            attempt_backoff_sec,
                            snap.thread_url,
                        )
                        continue

            saw_new_message = True
            LOGGER.info(
                "New incoming to reply (thread=%s latest_dir=%s)",
                snap.thread_url,
                getattr(snap, "latest_direction", "unknown"),
            )

            first_reply = state.first_reply_sent == 0
            delay_sec = self._rand_first_reply_delay() if first_reply else self._rand_followup_reply_delay()

            if self._should_skip_reply():
                LOGGER.info("Skipping immediate reply this cycle (thread=%s)", snap.thread_url)
                continue

            # Record attempt before sleeping/sending to reduce chance of rapid duplicates.
            attempt_count = state.attempt_count + 1 if state.last_attempt_incoming_msg_id == incoming_msg_id else 1
            self.store.upsert_thread_state(
                thread_url=snap.thread_url,
                last_seen_fingerprint=snap.message_fingerprint,
                last_seen_text=snap.message_text,
                last_activity_utc=snap.observed_at_utc,
                first_reply_sent=state.first_reply_sent,
                last_replied_incoming_fingerprint=state.last_replied_incoming_fingerprint,
                last_replied_incoming_text=state.last_replied_incoming_text,
                last_reply_utc=state.last_reply_utc,
                last_attempt_incoming_fingerprint=getattr(snap, "latest_incoming_fingerprint", None),
                last_attempt_utc=now_utc_iso(),
                last_attempt_incoming_msg_id=incoming_msg_id,
                attempt_count=attempt_count,
            )

            LOGGER.info("Waiting %s sec before reply in thread: %s", delay_sec, snap.thread_url)
            time.sleep(delay_sec)

            self._ensure_thread_open(driver, snap.thread_url)
            history = []
            try:
                history = self.store.get_recent_thread_messages(snap.thread_url, limit=int(history_n))
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to load message history for LLM (thread=%s)", snap.thread_url)
            reply_text = generate_reply(history, self.settings, fallback=self.settings.dry_run_reply_text)

            if self.settings.enable_sending:
                ok = send_message(driver, reply_text)
                LOGGER.info(
                    "Send attempted for thread=%s success=%s text=%s",
                    snap.thread_url,
                    ok,
                    reply_text[:120],
                )
                if not ok:
                    LOGGER.warning("Send failed; will retry later (thread=%s)", snap.thread_url)
                    continue
            else:
                LOGGER.info("DRY RUN reply for thread=%s text=%s", snap.thread_url, reply_text)

            # Store outgoing reply in history so LLM context remains accurate even if UI lags.
            try:
                self.store.append_thread_messages(
                    snap.thread_url,
                    [("outgoing", reply_text, now_utc_iso())],
                    max_per_thread=max_store,
                )
            except Exception:  # noqa: BLE001
                LOGGER.exception("Failed to persist outgoing message history (thread=%s)", snap.thread_url)

            # Mark inbound as replied-to. Even if Instagram UI bounces, this prevents re-reply spam.
            state.first_reply_sent = 1
            replied_at = now_utc_iso()
            self.store.upsert_thread_state(
                thread_url=snap.thread_url,
                last_seen_fingerprint=snap.message_fingerprint,
                last_seen_text=snap.message_text,
                last_activity_utc=snap.observed_at_utc,
                first_reply_sent=state.first_reply_sent,
                last_replied_incoming_fingerprint=getattr(snap, "latest_incoming_fingerprint", None),
                last_replied_incoming_text=incoming_text,
                last_reply_utc=replied_at,
                last_replied_incoming_msg_id=incoming_msg_id,
                last_attempt_incoming_fingerprint=None,
                last_attempt_utc=None,
                last_attempt_incoming_msg_id=None,
                attempt_count=0,
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
