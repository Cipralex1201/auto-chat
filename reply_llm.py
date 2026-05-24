from __future__ import annotations

import json
import hashlib
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from state_store import ThreadMessage

LOGGER = logging.getLogger(__name__)


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
    ts = (msg.observed_at_utc or "").strip()
    text = (msg.text or "").strip()
    if ts:
        return f"[{ts}] {text}"
    return text


def _to_chat_messages(history: list[ThreadMessage]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for msg in history:
        direction = (msg.direction or "").strip().lower()
        if direction == "incoming":
            role = "user"
        elif direction == "outgoing":
            role = "assistant"
        else:
            # v1: drop unknown direction messages to avoid confusing the model.
            continue
        out.append({"role": role, "content": _format_message_content(msg)})
    return out


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


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
    messages.extend(_to_chat_messages(history))

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
        result = _post_json(url, headers, payload, timeout_sec=timeout_sec)
        choice0 = (result.get("choices") or [{}])[0]
        msg = choice0.get("message") or {}
        content = (msg.get("content") or "").strip()
        if content:
            return content
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
