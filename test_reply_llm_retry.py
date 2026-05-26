import unittest
from unittest.mock import patch

import urllib.error

import reply_llm
from state_store import ThreadMessage


class _FakeSettings:
    llm_enabled = True
    llm_debug_dump_prompts = False
    llm_debug_dump_only = False
    llm_base_url = "https://api.example.test/v1"
    llm_model = "test-model"
    llm_api_key = "test-key"
    llm_master_prompt_file = "./does-not-exist.txt"
    llm_temperature = 0.0
    llm_max_tokens = 16
    llm_timeout_sec = 1
    llm_retry_n = 2
    llm_retry_backoff_base_sec = 1.0
    ig_username = "me"


class FakeResponse:
    def __init__(self, body_bytes: bytes):
        self._body = body_bytes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class TestLlmNetworkRetry(unittest.TestCase):
    def test_retries_twice_then_succeeds(self):
        settings = _FakeSettings()
        history = [
            ThreadMessage(
                id=1,
                thread_url="https://www.instagram.com/direct/t/123/",
                direction="incoming",
                text="hi",
                observed_at_utc="2026-01-01T00:00:00Z",
            )
        ]

        call_count = {"n": 0}
        response_body = (
            b'{"choices":[{"message":{"content":"hello from llm"}}]}'
        )

        def fake_urlopen(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise urllib.error.URLError("temporary dns failure")
            return FakeResponse(response_body)

        sleep_calls = []

        def fake_sleep(sec):
            sleep_calls.append(sec)

        with patch("reply_llm.urllib.request.urlopen", new=fake_urlopen), patch("reply_llm.time.sleep", new=fake_sleep):
            out = reply_llm.generate_reply(history, settings, fallback="fallback")

        self.assertEqual(out, "hello from llm")
        self.assertEqual(call_count["n"], 3)
        self.assertEqual(sleep_calls, [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
