"""Logging, timing, and optional error reporting.

Three rules this module exists to enforce:

1. **Nothing fails silently.** Optional integrations degrade to a no-op, but the
   degradation itself is logged at debug level with the reason.
2. **Nothing sensitive is logged.** :func:`redact` scrubs values that look like
   credentials, and source code is never logged above debug level.
3. **Timing is measured, not claimed.** :class:`Timer` records real durations so
   performance statements come from instrumentation.

Sentry is entirely optional; PatchAhead has no runtime dependency on it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import time
from collections.abc import Iterator
from typing import Any

log = logging.getLogger("patchahead")

#: Environment variables whose values must never reach a log or an error report.
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|dsn|credential)"
)
_SECRET_VALUE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,})\b")


def redact(value: Any) -> Any:
    """Return ``value`` with anything credential-shaped replaced.

    Applied to every structured log field. Conservative by design: a redacted
    log line is a mild inconvenience, a leaked key is an incident.
    """
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if _SECRET_PATTERN.search(str(key)) else redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


class _RedactingFilter(logging.Filter):
    """Scrub credential-shaped substrings from every formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _SECRET_VALUE.sub("[REDACTED]", record.msg)
        if record.args:
            record.args = redact(record.args)
        return True


class _Formatter(logging.Formatter):
    """Plain, greppable output. Level prefix only when it matters."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            prefix = f"{record.levelname.lower()}: "
        elif record.levelno <= logging.DEBUG:
            prefix = f"debug [{record.name}] "
        else:
            prefix = ""
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{prefix}{message}"


def configure_logging(level: int = logging.INFO, stream: Any = None) -> None:
    """Install PatchAhead's log handler. Idempotent.

    Logs go to stderr so that ``--json`` output on stdout stays machine-readable
    even at debug verbosity.
    """
    logger = logging.getLogger("patchahead")
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(_Formatter())
    handler.addFilter(_RedactingFilter())
    logger.addHandler(handler)


class Timer:
    """Accumulates named wall-clock durations for one run.

    Used for the ``timings`` field on results, so any performance claim
    PatchAhead makes is backed by a measurement the user can see.
    """

    def __init__(self) -> None:
        self.durations: dict[str, int] = {}

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = int((time.perf_counter() - start) * 1000)
            self.durations[name] = self.durations.get(name, 0) + elapsed
            log.debug("stage %s took %dms", name, elapsed)

    def as_dict(self) -> dict[str, int]:
        return dict(self.durations)


# --------------------------------------------------------------------------
# Optional Sentry integration
# --------------------------------------------------------------------------

_sentry_state: dict[str, Any] = {
    "initialized": False,
    "enabled": False,
    "reason": "not initialized",
}


def init_error_reporting() -> bool:
    """Initialize Sentry if ``SENTRY_DSN`` is set and the SDK is installed.

    Returns whether reporting is active. Never raises: a broken optional
    integration must not break a migration run. The reason for any degradation
    is recorded and available from :func:`error_reporting_status`.
    """
    if _sentry_state["initialized"]:
        return bool(_sentry_state["enabled"])
    _sentry_state["initialized"] = True

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        _sentry_state["reason"] = "SENTRY_DSN not set"
        log.debug("error reporting disabled: %s", _sentry_state["reason"])
        return False
    try:
        import sentry_sdk
    except ImportError:
        _sentry_state["reason"] = "sentry-sdk not installed (pip install 'patchahead[sentry]')"
        log.debug("error reporting disabled: %s", _sentry_state["reason"])
        return False
    try:
        from patchahead import __version__

        sentry_sdk.init(
            dsn=dsn,
            release=f"patchahead@{__version__}",
            traces_sample_rate=float(os.environ.get("PATCHAHEAD_TRACES_SAMPLE_RATE", "0")),
            # Repository source is the user's proprietary code. Never attach it.
            send_default_pii=False,
            max_request_body_size="never",
            before_send=_scrub_event,
        )
    except Exception as exc:
        _sentry_state["reason"] = f"sentry_sdk.init failed: {exc}"
        log.warning("error reporting could not start: %s", _sentry_state["reason"])
        return False

    _sentry_state["enabled"] = True
    _sentry_state["reason"] = "active"
    log.debug("error reporting enabled")
    return True


def _scrub_event(event: dict[str, Any], _hint: Any) -> dict[str, Any]:
    """Strip environment variables and request bodies from outgoing events."""
    event.pop("request", None)
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        contexts.pop("env", None)
    extra = event.get("extra")
    if isinstance(extra, dict):
        event["extra"] = redact(extra)
    return event


def error_reporting_status() -> str:
    """Human-readable state of the optional error reporting integration."""
    if not _sentry_state["initialized"]:
        return "not initialized"
    return "active" if _sentry_state["enabled"] else f"disabled ({_sentry_state['reason']})"


def capture_exception(exc: BaseException, **context: Any) -> None:
    """Report an exception if Sentry is active. Always logs it either way."""
    log.debug("captured exception: %s", exc, exc_info=exc)
    if not _sentry_state.get("enabled"):
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for key, value in redact(context).items():
                scope.set_extra(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:  # pragma: no cover - reporting must never break a run
        log.debug("failed to report exception to Sentry", exc_info=True)
