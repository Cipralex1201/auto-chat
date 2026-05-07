from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    ig_username: str
    ig_password: str
    watched_threads: list[str]

    firefox_profile_dir: Path
    geckodriver_path: Path | None
    headless: bool
    page_load_timeout_sec: int

    idle_min_sec: int
    idle_max_sec: int
    active_min_sec: int
    active_max_sec: int

    first_reply_min_sec: int
    first_reply_max_sec: int
    followup_reply_min_sec: int
    followup_reply_max_sec: int

    conversation_expire_min_sec: int
    conversation_expire_max_sec: int

    idle_browser_grace_sec: int
    force_browser_restart_sec: int

    enable_sending: bool
    skip_reply_probability: float
    dry_run_reply_text: str

    log_level: str
    log_file: Path | None

    # LLM responder settings (OpenAI-compatible)
    llm_enabled: bool
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_history_n: int
    llm_master_prompt_file: Path
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout_sec: int

    # Storage bound
    max_stored_messages_per_thread: int


def _get_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)


def _parse_threads(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_optional_path(raw: str | None) -> Path | None:
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return Path(stripped).expanduser()


def load_settings() -> Settings:
    load_dotenv()

    settings = Settings(
        ig_username=os.getenv("IG_USERNAME", "").strip(),
        ig_password=os.getenv("IG_PASSWORD", "").strip(),
        watched_threads=_parse_threads(os.getenv("INSTAGRAM_WATCHED_THREADS")),
        firefox_profile_dir=Path(os.getenv("FIREFOX_PROFILE_DIR", "./firefox-profile")).expanduser(),
        geckodriver_path=_parse_optional_path(os.getenv("GECKODRIVER_PATH")),
        headless=_get_bool("HEADLESS", "false"),
        page_load_timeout_sec=_get_int("PAGE_LOAD_TIMEOUT_SEC", 30),
        idle_min_sec=_get_int("IDLE_MIN_SEC", 120),
        idle_max_sec=_get_int("IDLE_MAX_SEC", 240),
        active_min_sec=_get_int("ACTIVE_MIN_SEC", 10),
        active_max_sec=_get_int("ACTIVE_MAX_SEC", 20),
        first_reply_min_sec=_get_int("FIRST_REPLY_MIN_SEC", 45),
        first_reply_max_sec=_get_int("FIRST_REPLY_MAX_SEC", 150),
        followup_reply_min_sec=_get_int("FOLLOWUP_REPLY_MIN_SEC", 8),
        followup_reply_max_sec=_get_int("FOLLOWUP_REPLY_MAX_SEC", 45),
        conversation_expire_min_sec=_get_int("CONVERSATION_EXPIRE_MIN_SEC", 480),
        conversation_expire_max_sec=_get_int("CONVERSATION_EXPIRE_MAX_SEC", 720),
        idle_browser_grace_sec=_get_int("IDLE_BROWSER_GRACE_SEC", 900),
        force_browser_restart_sec=_get_int("FORCE_BROWSER_RESTART_SEC", 14400),
        enable_sending=_get_bool("ENABLE_SENDING", "false"),
        skip_reply_probability=_get_float("SKIP_REPLY_PROBABILITY", 0.20),
        dry_run_reply_text=os.getenv("DRY_RUN_REPLY_TEXT", "Thanks! I saw your message.").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        log_file=_parse_optional_path(os.getenv("LOG_FILE", "./logs/bot.log")),

        llm_enabled=_get_bool("LLM_ENABLED", "false"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip(),
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_history_n=_get_int("LLM_HISTORY_N", 20),
        llm_master_prompt_file=Path(os.getenv("LLM_MASTER_PROMPT_FILE", "./master_prompt.txt")).expanduser(),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.2),
        llm_max_tokens=_get_int("LLM_MAX_TOKENS", 200),
        llm_timeout_sec=_get_int("LLM_TIMEOUT_SEC", 30),

        max_stored_messages_per_thread=_get_int("MAX_STORED_MESSAGES_PER_THREAD", 1000),
    )

    if settings.idle_min_sec > settings.idle_max_sec:
        raise ValueError("IDLE_MIN_SEC must be <= IDLE_MAX_SEC")
    if settings.active_min_sec > settings.active_max_sec:
        raise ValueError("ACTIVE_MIN_SEC must be <= ACTIVE_MAX_SEC")
    if settings.first_reply_min_sec > settings.first_reply_max_sec:
        raise ValueError("FIRST_REPLY_MIN_SEC must be <= FIRST_REPLY_MAX_SEC")
    if settings.followup_reply_min_sec > settings.followup_reply_max_sec:
        raise ValueError("FOLLOWUP_REPLY_MIN_SEC must be <= FOLLOWUP_REPLY_MAX_SEC")
    if settings.conversation_expire_min_sec > settings.conversation_expire_max_sec:
        raise ValueError("CONVERSATION_EXPIRE_MIN_SEC must be <= CONVERSATION_EXPIRE_MAX_SEC")
    if not (0.0 <= settings.skip_reply_probability <= 1.0):
        raise ValueError("SKIP_REPLY_PROBABILITY must be between 0 and 1")

    if settings.llm_history_n < 0:
        raise ValueError("LLM_HISTORY_N must be >= 0")
    if settings.llm_max_tokens < 1:
        raise ValueError("LLM_MAX_TOKENS must be >= 1")
    if settings.llm_timeout_sec < 1:
        raise ValueError("LLM_TIMEOUT_SEC must be >= 1")
    if not (0.0 <= settings.llm_temperature <= 2.0):
        raise ValueError("LLM_TEMPERATURE must be between 0 and 2")
    if settings.max_stored_messages_per_thread < 0:
        raise ValueError("MAX_STORED_MESSAGES_PER_THREAD must be >= 0")

    return settings
