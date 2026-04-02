from __future__ import annotations


def generate_reply_placeholder(incoming_text: str, fallback: str) -> str:
    if incoming_text:
        return f"Auto-reply placeholder: I received your message ({incoming_text[:80]})."
    return fallback
