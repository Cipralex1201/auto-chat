import tempfile
import unittest
from pathlib import Path

from state_store import StateStore


class TestStateStorePendingReply(unittest.TestCase):
    def test_pending_reply_persist_preserve_and_clear(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.sqlite3"
            store = StateStore(db_path)

            thread = "https://example.test/direct/t/1/"
            s0 = store.get_thread_state(thread)
            self.assertIsNone(s0.pending_reply_incoming_msg_id)
            self.assertIsNone(s0.pending_reply_text)
            self.assertIsNone(s0.pending_reply_created_utc)

            # Set pending reply.
            store.upsert_thread_state(
                thread_url=thread,
                last_seen_fingerprint=None,
                last_seen_text=None,
                last_activity_utc=None,
                first_reply_sent=0,
                pending_reply_incoming_msg_id=123,
                pending_reply_text="hello",
                pending_reply_created_utc="2026-05-31T00:00:00+00:00",
            )
            s1 = store.get_thread_state(thread)
            self.assertEqual(s1.pending_reply_incoming_msg_id, 123)
            self.assertEqual(s1.pending_reply_text, "hello")
            self.assertEqual(s1.pending_reply_created_utc, "2026-05-31T00:00:00+00:00")

            # Preserve pending reply when upserting unrelated fields.
            store.upsert_thread_state(
                thread_url=thread,
                last_seen_fingerprint="fp",
                last_seen_text="txt",
                last_activity_utc="2026-05-31T00:00:01+00:00",
                first_reply_sent=1,
            )
            s2 = store.get_thread_state(thread)
            self.assertEqual(s2.pending_reply_incoming_msg_id, 123)
            self.assertEqual(s2.pending_reply_text, "hello")

            # Clear explicitly.
            store.upsert_thread_state(
                thread_url=thread,
                last_seen_fingerprint="fp2",
                last_seen_text="txt2",
                last_activity_utc="2026-05-31T00:00:02+00:00",
                first_reply_sent=1,
                pending_reply_incoming_msg_id=None,
                pending_reply_text=None,
                pending_reply_created_utc=None,
            )
            s3 = store.get_thread_state(thread)
            self.assertIsNone(s3.pending_reply_incoming_msg_id)
            self.assertIsNone(s3.pending_reply_text)
            self.assertIsNone(s3.pending_reply_created_utc)

            store.close()


if __name__ == "__main__":
    unittest.main()
