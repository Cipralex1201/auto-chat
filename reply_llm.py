from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from state_store import ThreadMessage

LOGGER = logging.getLogger(__name__)


_LEADING_ISO_TS = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\]\s*"
)

_NOISE_AGE_LABEL = re.compile(r"^\d{1,3}[smhdw]$", re.IGNORECASE)
_NOISE_24H_TIME = re.compile(r"^\d{1,2}:\d{2}$", re.IGNORECASE)
_NOISE_12H_TIME = re.compile(r"^\d{1,2}:\d{2}\s*(?:am|pm|a\.?m\.?|p\.?m\.?)$", re.IGNORECASE)
_NOISE_DURATION = re.compile(
    r"^\d{1,3}\s*(sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours|day|days|week|weeks)$",
    re.IGNORECASE,
)

_USERNAME_LIKE = re.compile(r"^[a-z0-9._]{3,40}$", re.IGNORECASE)


def _is_noise_prompt_content(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return True

    lowered = normalized.lower()
    if lowered in {
        # UI headers/labels we've observed being extracted as messages.
        "unread",
        "new messages",
        "new message",
        # Delivery/status labels that are not part of the conversation.
        "seen",
        "seen just now",
        "sent",
        "delivered",
        "active",
        "message",
    }:
        return True

    if _NOISE_AGE_LABEL.fullmatch(normalized):
        return True
    if _NOISE_24H_TIME.fullmatch(normalized):
        return True
    if _NOISE_12H_TIME.fullmatch(normalized):
        return True
    if _NOISE_DURATION.fullmatch(normalized):
        return True

    return False


def _strip_leading_timestamp_prefix(text: str) -> str:
    return _LEADING_ISO_TS.sub("", text or "", count=1)


def _safe_thread_tag(thread_url: str) -> str:
    normalized = (thread_url or "").strip()
    if not normalized:
        return "thread_unknown"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"thread_{digest}"


def _dump_llm_payload(settings, payload: dict[str, Any], *, thread_url: str) -> None:
    if not (
        _as_bool(getattr(settings, "llm_debug_dump_prompts", False))
        or _as_bool(getattr(settings, "llm_debug_dump_only", False))
    ):
        return

    dump_dir = getattr(settings, "llm_debug_dump_dir", None)
    if not dump_dir:
        return

    try:
        os.makedirs(str(dump_dir), exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fname = f"llm_payload_{ts}_{_safe_thread_tag(thread_url)}_{time.time_ns()}.json"
        path = os.path.join(str(dump_dir), fname)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        LOGGER.info("LLM debug: dumped prompt payload to %s", path)
    except Exception:  # noqa: BLE001
        LOGGER.exception("LLM debug: failed to dump prompt payload")


def _read_text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception:  # noqa: BLE001
        LOGGER.exception("Failed to read master prompt file: %s", path)
        return ""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _format_message_content(msg: ThreadMessage) -> str:
    text = (msg.text or "").strip()
    return _strip_leading_timestamp_prefix(text)


def _to_chat_messages(history: list[ThreadMessage], *, own_username: str | None = None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen_assistant_long: set[str] = set()
    seen_user_short: set[str] = set()
    own_u = (own_username or "").strip().lstrip("@").lower()
    for msg in history:
        direction = (msg.direction or "").strip().lower()
        if direction == "incoming":
            role = "user"
        elif direction == "outgoing":
            role = "assistant"
        else:
            # v1: drop unknown direction messages to avoid confusing the model.
            continue

        content = _format_message_content(msg)
        if _is_noise_prompt_content(content):
            continue

        # Backstop: sometimes the thread header username leaks into history as a standalone
        # token. Avoid sending such rows to the LLM.
        if role == "user":
            compact = "".join((content or "").split())
            if own_u and compact.lower().lstrip("@") == own_u:
                continue
            if compact and _USERNAME_LIKE.fullmatch(compact) and ("_" in compact or "." in compact):
                continue

            # If DB history is polluted with rapid-poll duplicates, collapse repeated short
            # user texts to a single occurrence to keep prompts readable.
            if len(content) <= 40:
                if content in seen_user_short:
                    continue
                seen_user_short.add(content)

        # If DB history is already polluted with duplicated outgoing replies, keep only the
        # first occurrence of long assistant messages to avoid confusing the model.
        if role == "assistant" and len(content) >= 20:
            if content in seen_assistant_long:
                continue
            seen_assistant_long.add(content)

        out.append({"role": role, "content": content})
    return out


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def _is_retryable_network_error(exc: BaseException) -> bool:
    """Return True for transient connectivity-ish failures worth retrying.

    Intentionally does NOT retry HTTP status errors (HTTPError).
    """

    if isinstance(exc, urllib.error.HTTPError):
        return False

    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True

    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        if isinstance(reason, OSError) and reason.errno in {101, 110, 111, 113}:
            return True
        # DNS failures and other URL-related connection errors are usually safe to retry.
        return True

    if isinstance(exc, OSError) and exc.errno in {101, 110, 111, 113}:
        return True

    return False


def generate_reply(history: list[ThreadMessage], settings, *, fallback: str) -> str:
    """Generate a reply using an OpenAI-compatible Chat Completions API.

    - Reads a master prompt from settings.llm_master_prompt_file.
    - Sends the last N stored messages as chat history.
    - On any failure or when disabled, returns fallback.
    """

    fallback_text = (fallback or "").strip() or "auto reply"

    enabled = _as_bool(getattr(settings, "llm_enabled", False))
    debug_dump_prompts = _as_bool(getattr(settings, "llm_debug_dump_prompts", False))
    debug_dump_only = _as_bool(getattr(settings, "llm_debug_dump_only", False))
    want_payload_dump = debug_dump_prompts or debug_dump_only

    # If neither real LLM nor dumping is desired, fast path.
    if not enabled and not want_payload_dump:
        return fallback_text

    # Build the payload (so we can dump it) even if LLM is disabled.
    base_url = (getattr(settings, "llm_base_url", "") or "https://api.openai.com/v1").strip().rstrip("/")
    model_for_dump = (getattr(settings, "llm_model", "") or "").strip() or "(unset)"

    master_prompt_file_value = getattr(settings, "llm_master_prompt_file", "./master_prompt.txt")
    master_prompt_file = str(master_prompt_file_value).strip() or "./master_prompt.txt"
    system_prompt = _read_text_file(master_prompt_file)

    temperature = float(getattr(settings, "llm_temperature", 0.2))
    max_tokens = int(getattr(settings, "llm_max_tokens", 200))
    timeout_sec = int(getattr(settings, "llm_timeout_sec", 30))

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(_to_chat_messages(history, own_username=getattr(settings, "ig_username", None)))

    payload: dict[str, Any] = {
        "model": model_for_dump,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    thread_url = history[-1].thread_url if history else ""
    if want_payload_dump:
        _dump_llm_payload(settings, payload, thread_url=thread_url)

    # Dump-only is explicitly "no network request".
    if debug_dump_only:
        return fallback_text

    # If LLM is disabled, we only dump payload (above) and stop here.
    if not enabled:
        return fallback_text

    # Guard: don't call an LLM if we have no user messages.
    if not any(m.get("role") == "user" for m in messages):
        LOGGER.info("LLM: no user messages in history; using fallback")
        return fallback_text

    api_key = (getattr(settings, "llm_api_key", "") or os.getenv("LLM_API_KEY", "")).strip()
    if not api_key:
        LOGGER.warning("LLM enabled but LLM_API_KEY is empty; using fallback")
        return fallback_text

    real_model = (getattr(settings, "llm_model", "") or "").strip()
    if not real_model:
        LOGGER.warning("LLM enabled but LLM_MODEL is empty; using fallback")
        return fallback_text

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload["model"] = real_model

    try:
        retry_n = int(getattr(settings, "llm_retry_n", 2))
        backoff_base_sec = float(getattr(settings, "llm_retry_backoff_base_sec", 1.0))

        attempts_total = 1 + max(0, retry_n)
        result: dict[str, Any] | None = None
        last_exc: BaseException | None = None

        for attempt_idx in range(attempts_total):
            try:
                result = _post_json(url, headers, payload, timeout_sec=timeout_sec)
                last_exc = None
                break
            except Exception as e:  # noqa: BLE001
                if _is_retryable_network_error(e) and attempt_idx < attempts_total - 1:
                    delay = max(0.0, backoff_base_sec) * (2**attempt_idx)
                    LOGGER.warning(
                        "LLM network error (attempt %s/%s): %s; retrying in %.1fs",
                        attempt_idx + 1,
                        attempts_total,
                        repr(e),
                        delay,
                    )
                    if delay > 0:
                        time.sleep(delay)
                    continue
                last_exc = e
                break

        if last_exc is not None:
            raise last_exc
        if result is None:
            raise RuntimeError("LLM call failed with no exception")

        choice0 = (result.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        content = (msg.get("content") or "").strip()
        if content:
            return _strip_leading_timestamp_prefix(content)
        LOGGER.warning("LLM response missing content; using fallback")
        return fallback_text
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        LOGGER.warning("LLM HTTP error %s: %s", getattr(e, "code", "?"), body[:500])
        return fallback_text
    except Exception:  # noqa: BLE001
        LOGGER.exception("LLM call failed; using fallback")
        return fallback_text
