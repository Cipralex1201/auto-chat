import unittest

from reply_llm import _to_chat_messages
from state_store import ThreadMessage


class TestReplyLlmPromptDedupe(unittest.TestCase):
    def test_deduplicates_assistant_messages_in_prompt(self):
        thread = "https://example.test/direct/t/1/"
        history = [
            ThreadMessage(
                id=1,
                thread_url=thread,
                direction="incoming",
                text="hi",
                observed_at_utc="2026-05-26T00:00:00+00:00",
            ),
            ThreadMessage(
                id=2,
                thread_url=thread,
                direction="outgoing",
                text="OK",
                observed_at_utc="2026-05-26T00:00:01+00:00",
            ),
            ThreadMessage(
                id=3,
                thread_url=thread,
                direction="outgoing",
                text="OK",
                observed_at_utc="2026-05-26T00:00:02+00:00",
            ),
            ThreadMessage(
                id=4,
                thread_url=thread,
                direction="incoming",
                text="next",
                observed_at_utc="2026-05-26T00:00:03+00:00",
            ),
        ]

        msgs = _to_chat_messages(history)
        roles = [(m["role"], m["content"]) for m in msgs]

        # Should keep only one copy of the outgoing assistant message.
        self.assertEqual(
            roles,
            [
                ("user", "hi"),
                ("assistant", "OK"),
                ("user", "next"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
