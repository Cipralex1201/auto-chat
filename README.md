# Instagram DM Selenium Bot (Logic v1)

This project implements the bot logic layer for Instagram DM automation using:

- Python
- Selenium
- Firefox + geckodriver
- Dedicated Firefox profile directory
- Single long-running scheduler process with idle/active timing model

LLM prompt assembly/integration is intentionally left for a later phase.

## Implemented Modules

- `browser.py`: starts Firefox with a fixed profile and handles restart/close lifecycle
- `session.py`: checks login state and performs credential login attempt
- `dm_reader.py`: opens watched DM thread URLs and snapshots latest message text
- `reply_llm.py`: reply generator using an OpenAI-compatible Chat Completions API
- `sender.py`: sends a message through DM composer when sending is enabled
- `state_store.py`: SQLite state persistence
- `scheduler.py`: idle/active mode scheduling, delays, randomization, browser recycle
- `main.py`: app entrypoint

## Setup

1. Create virtual environment and install dependencies.
2. Copy `.env.example` to `.env` and fill values.
3. Ensure Firefox is installed.
4. If geckodriver is on PATH (or Selenium Manager can resolve it), leave `GECKODRIVER_PATH` empty.
5. Set `GECKODRIVER_PATH` only when you want an explicit pinned binary override.
6. Create/use a dedicated Firefox profile path for `FIREFOX_PROFILE_DIR`.

## Run

```bash
python main.py
```

## Debug: view the assembled LLM prompt

Set `LLM_DEBUG_DUMP_PROMPTS=true` to write the exact JSON payload (system prompt + message history) to `./logs/llm_prompts/` right before the API call.

If you only want to validate prompt assembly (no network request), also set `LLM_DEBUG_DUMP_ONLY=true`.

## Guard: ignore thread caption text

Instagram sometimes re-renders the thread caption (username + display name) inside the message pane, which can be misread as a new message.

To hard-ignore those exact strings, set these in `.env`:

- `IG_IGNORE_EXACT_USERNAME` — the thread caption username/handle (with or without `@`)
- `IG_IGNORE_EXACT_FULLNAME` — the thread caption display name (full name)

## LLM network retries

If the LLM request fails due to transient network issues (DNS failure, timeout, no route to host), the bot will retry a couple times with exponential backoff before falling back to `DRY_RUN_REPLY_TEXT`.

Configure via:

- `LLM_RETRY_N` (default: 2) — number of extra retries after the first attempt
- `LLM_RETRY_BACKOFF_BASE_SEC` (default: 1) — base seconds for exponential backoff (1s, 2s, 4s, ...)

## Timing Model

Configured in `.env` (defaults already match your requested ranges):

- Idle checks: `IDLE_MIN_SEC` to `IDLE_MAX_SEC` (120-240)
- Active checks: `ACTIVE_MIN_SEC` to `ACTIVE_MAX_SEC` (10-20)
- First reply delay: `FIRST_REPLY_MIN_SEC` to `FIRST_REPLY_MAX_SEC` (45-150)
- Follow-up delay: `FOLLOWUP_REPLY_MIN_SEC` to `FOLLOWUP_REPLY_MAX_SEC` (8-45)
- Conversation expiry for active mode: `CONVERSATION_EXPIRE_MIN_SEC` to `CONVERSATION_EXPIRE_MAX_SEC` (480-720)
- Optional skip probability: `SKIP_REPLY_PROBABILITY` (default 0.20)

## Quiet Hours (Hard Idle Windows)

If you want the bot to be fully idle during certain times of day (no chat responses, no browser, no inbox checking), configure `IDLE_WINDOWS_LOCAL`.

Examples:

- Fully idle roughly from 1AM to 8AM (with some randomness):
	- `IDLE_WINDOWS_LOCAL=01:00-08:00`
	- `IDLE_WINDOWS_START_JITTER_MIN=20`
	- `IDLE_WINDOWS_END_JITTER_MIN=20`

During these windows the scheduler closes Selenium and sleeps efficiently until the window ends.

## Browser Lifetime Strategy

To reduce memory leak accumulation while preserving responsiveness:

- Browser remains open in active mode.
- In idle mode, browser is closed after `IDLE_BROWSER_GRACE_SEC` inactivity.
- Browser is force-restarted every `FORCE_BROWSER_RESTART_SEC`.

## Important Notes

- Instagram UI selectors can change often; adjust XPath selectors in `dm_reader.py` and `sender.py` when needed.
- 2FA/checkpoint pages may require manual completion in visible browser.
- Keep `ENABLE_SENDING=false` until you verify reading/detection behavior.
