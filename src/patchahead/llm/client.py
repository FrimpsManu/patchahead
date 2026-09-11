"""A thin, honest wrapper over the Anthropic API.

Two things this does that the prototype did not:

**It distinguishes failure modes.** The prototype wrapped every call in
``except Exception: return None``, so a bad API key, a network outage, a
malformed response, and "the LLM is switched off" were indistinguishable
(``docs/assessment.md`` §2.6). Here each raises or returns a typed
:class:`LLMError` with a message a user can act on.

**It never sends more than it was given.** The caller assembles the prompt from
a named, bounded set of inputs. This module adds nothing, reads no files, and
logs no prompt content above debug level.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Default model. Overridable with ``PATCHAHEAD_MODEL``.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8000


class LLMError(Exception):
    """A structured LLM failure. Carries a message meant for the user."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMUnavailable(LLMError):
    """The LLM path cannot run at all: no SDK, or no credentials."""


@dataclass
class LLMResponse:
    """A completed model response."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


def available() -> tuple[bool, str]:
    """Whether the LLM path can run, and why not if it cannot."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, ("the `anthropic` package is not installed (pip install 'patchahead[llm]')")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False, "no ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is set"
    return True, ""


def model_name() -> str:
    return os.environ.get("PATCHAHEAD_MODEL", DEFAULT_MODEL)


class LLMClient:
    """Sends one request and returns the text, or raises :class:`LLMError`."""

    def __init__(self, model: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self.model = model or model_name()
        self.max_tokens = max_tokens
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        ok, reason = available()
        if not ok:
            raise LLMUnavailable(reason)
        import anthropic

        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:
            raise LLMUnavailable(f"could not construct the Anthropic client: {exc}") from exc
        return self._client

    def complete(self, system: str, user: str) -> LLMResponse:
        """Send one request. Raises :class:`LLMError` on any failure."""
        # `_ensure_client` first: it turns a missing package or missing
        # credentials into an LLMUnavailable the caller can report. Importing
        # `anthropic` before that check would raise ModuleNotFoundError straight
        # past every handler in the stack.
        client = self._ensure_client()
        import anthropic

        log.debug("llm request: model=%s, %d chars of input", self.model, len(user))

        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError(f"the Anthropic API rejected the credentials: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited by the Anthropic API: {exc}", retryable=True) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the Anthropic API: {exc}", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                f"the Anthropic API returned {exc.status_code}: {exc}",
                retryable=exc.status_code >= 500,
            ) from exc
        except Exception as exc:  # unexpected SDK-level failure
            raise LLMError(f"unexpected error calling the Anthropic API: {exc}") from exc

        if getattr(message, "stop_reason", "") == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", "") if details else ""
            raise LLMError(
                "the model declined to answer" + (f" (category: {category})" if category else "")
            )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        if not text.strip():
            raise LLMError(
                f"the model returned no text (stop_reason: "
                f"{getattr(message, 'stop_reason', 'unknown')})"
            )

        usage = getattr(message, "usage", None)
        response = LLMResponse(
            text=text,
            model=getattr(message, "model", self.model),
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            stop_reason=getattr(message, "stop_reason", "") or "",
        )
        log.debug(
            "llm response: %d in / %d out tokens, stop_reason=%s",
            response.input_tokens,
            response.output_tokens,
            response.stop_reason,
        )
        return response
