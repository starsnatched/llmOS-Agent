from __future__ import annotations

__all__ = ["limit_chars"]


def limit_chars(text: str, limit: int = 10_000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text

    truncated = len(text) - limit
    return f"(output truncated, {truncated} characters hidden)\n{text[-limit:]}"


from .debug import debug_all
debug_all(globals())

