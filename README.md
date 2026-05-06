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
- `reply_llm.py`: placeholder reply generator (no external API yet)
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

## Timing Model

Configured in `.env` (defaults already match your requested ranges):

- Idle checks: `IDLE_MIN_SEC` to `IDLE_MAX_SEC` (120-240)
- Active checks: `ACTIVE_MIN_SEC` to `ACTIVE_MAX_SEC` (10-20)
- First reply delay: `FIRST_REPLY_MIN_SEC` to `FIRST_REPLY_MAX_SEC` (45-150)
- Follow-up delay: `FOLLOWUP_REPLY_MIN_SEC` to `FOLLOWUP_REPLY_MAX_SEC` (8-45)
- Conversation expiry for active mode: `CONVERSATION_EXPIRE_MIN_SEC` to `CONVERSATION_EXPIRE_MAX_SEC` (480-720)
- Optional skip probability: `SKIP_REPLY_PROBABILITY` (default 0.20)

## Browser Lifetime Strategy

To reduce memory leak accumulation while preserving responsiveness:

- Browser remains open in active mode.
- In idle mode, browser is closed after `IDLE_BROWSER_GRACE_SEC` inactivity.
- Browser is force-restarted every `FORCE_BROWSER_RESTART_SEC`.

## Important Notes

- Instagram UI selectors can change often; adjust XPath selectors in `dm_reader.py` and `sender.py` when needed.
- 2FA/checkpoint pages may require manual completion in visible browser.
- Keep `ENABLE_SENDING=false` until you verify reading/detection behavior.
