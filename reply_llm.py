from __future__ import annotations


def generate_reply_placeholder(incoming_text: str, fallback: str) -> str:
    # This project uses env-configured text as the actual reply message.
    # Keep the signature for future LLM integration.
    _ = incoming_text
    reply = (fallback or "").strip()
    return reply if reply else "auto reply"
