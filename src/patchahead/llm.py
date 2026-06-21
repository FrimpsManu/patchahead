"""Optional Claude reasoning layer.

PatchAhead is deterministic by default so the demo is bulletproof and
offline. When ``PATCHAHEAD_USE_LLM=1`` and ``ANTHROPIC_API_KEY`` is set,
the parser / impact / patch / summary stages can call Claude for semantic
reasoning. Every call is wrapped so any failure transparently falls back
to the deterministic path -- the LLM proposes, tests verify, humans
approve.
"""

import os

_MODEL = os.environ.get("PATCHAHEAD_MODEL", "claude-opus-4-8")


def enabled() -> bool:
    return os.environ.get("PATCHAHEAD_USE_LLM", "0") == "1" and bool(
        os.environ.get("ANTHROPIC_API_KEY")
    )


def complete(system: str, user: str, max_tokens: int = 1024) -> str | None:
    """Return Claude's text response, or None if unavailable/failed."""
    if not enabled():
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    except Exception:
        return None
