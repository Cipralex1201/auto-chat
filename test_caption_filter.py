import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dm_reader import ThreadSnapshot
from scheduler import BotScheduler
from state_store import ThreadMessage, ThreadState


class _DummyStore:
    def __init__(self, *, latest_in: ThreadMessage | None = None):
        self.latest_in = latest_in
        self.latest_out = None
        self.update_tail_calls: list[list[tuple[str, str]]] = []
        self.upserts: list[dict] = []

    def get_thread_state(self, thread_url: str) -> ThreadState:
        return ThreadState(
            thread_url=thread_url,
            last_seen_fingerprint=None,
            last_seen_text=None,
            last_activity_utc=None,
            first_reply_sent=0,
            last_replied_incoming_fingerprint=None,
            last_replied_incoming_text=None,
            last_reply_utc=None,
            last_replied_incoming_msg_id=None,
            last_attempt_incoming_fingerprint=None,
            last_attempt_utc=None,
            attempt_count=0,
            last_attempt_incoming_msg_id=None,
            pending_reply_incoming_msg_id=None,
            pending_reply_text=None,
            pending_reply_created_utc=None,
        )

    def update_history_from_tail(self, thread_url: str, tail_messages, observed_at_utc: str, *, max_per_thread=None) -> int:
        self.update_tail_calls.append(list(tail_messages))
        return 0

    def upsert_thread_state(self, **kwargs) -> None:
        self.upserts.append(dict(kwargs))

    def get_latest_incoming_message(self, thread_url: str):
        return self.latest_in

    def get_latest_outgoing_message(self, thread_url: str):
        return self.latest_out

    def get_recent_thread_messages(self, thread_url: str, limit: int):
        return []

    def append_thread_messages(self, thread_url: str, messages, *, max_per_thread=None) -> int:
        raise AssertionError("append_thread_messages should not be called in these tests")


class CaptionIgnoreTests(unittest.TestCase):
    def _make_scheduler(self, store: _DummyStore) -> BotScheduler:
        settings = SimpleNamespace(
            # Minimal settings used by _handle_snapshots
            llm_history_n=10,
            max_stored_messages_per_thread=1000,
            ig_username="myuser",
            dry_run_reply_text="I received your message.",
            enable_sending=False,
            skip_reply_probability=1.0,
            ig_ignore_exact_username="target_handle",
            ig_ignore_exact_fullname="Target Full Name",
        )
        return BotScheduler(settings=settings, browser=None, store=store)

    def test_filters_caption_texts_before_persisting_tail(self):
        store = _DummyStore(latest_in=None)
        sched = self._make_scheduler(store)

        snap = ThreadSnapshot(
            thread_url="https://example.test/direct/t/1/",
            message_fingerprint="win:1234",
            message_text="Target Full Name",
            latest_direction="incoming",
            latest_incoming_fingerprint=None,
            latest_incoming_text=None,
            latest_outgoing_text=None,
            tail_messages=[
                ("incoming", "target_handle"),
                ("incoming", "hello"),
                ("unknown", "Target Full Name"),
                ("incoming", "ok"),
            ],
            observed_at_utc="2026-05-26T00:00:00+00:00",
        )

        sched._handle_snapshots(driver=None, snapshots=[snap])

        self.assertEqual(len(store.update_tail_calls), 1)
        self.assertEqual(store.update_tail_calls[0], [("incoming", "hello"), ("incoming", "ok")])

    def test_ignored_caption_text_never_triggers_llm(self):
        latest_in = ThreadMessage(
            id=123,
            thread_url="https://example.test/direct/t/1/",
            direction="incoming",
            text="@target_handle",
            observed_at_utc="2026-05-26T00:00:00+00:00",
        )
        store = _DummyStore(latest_in=latest_in)
        sched = self._make_scheduler(store)

        snap = ThreadSnapshot(
            thread_url=latest_in.thread_url,
            message_fingerprint="win:1234",
            message_text="@target_handle",
            latest_direction="incoming",
            latest_incoming_fingerprint="in:aaaa",
            latest_incoming_text="@target_handle",
            latest_outgoing_text=None,
            tail_messages=[],
            observed_at_utc="2026-05-26T00:00:00+00:00",
        )

        with patch("scheduler.generate_reply", side_effect=AssertionError("LLM should not be called")):
            sched._handle_snapshots(driver=None, snapshots=[snap])


if __name__ == "__main__":
    unittest.main()
