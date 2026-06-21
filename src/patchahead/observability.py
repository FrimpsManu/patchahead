"""Observability for the PatchAhead workflow.

Every stage runs inside a span. If a ``SENTRY_DSN`` is configured and the
``sentry_sdk`` package is installed, spans are reported to Sentry as a
single ``patchahead.migration_run`` transaction. Otherwise we fall back
to pretty console traces so the workflow is still visibly instrumented
during the demo. Missing DSN/package never crashes the run.
"""

import contextlib
import os
import time

try:
    import sentry_sdk

    _HAS_SENTRY = True
except Exception:  # pragma: no cover - optional dep
    sentry_sdk = None
    _HAS_SENTRY = False

_DSN = os.environ.get("SENTRY_DSN", "").strip()
ENABLED = bool(_DSN) and _HAS_SENTRY

_GREY = "\033[90m"
_RESET = "\033[0m"
_NOCOLOR = bool(os.environ.get("NO_COLOR"))

# In-memory waterfall of the most recent run, so any surface (CLI, web
# dashboard) can show the trace even when no Sentry DSN is configured.
_RECORDED = []


def recorded_spans():
    return list(_RECORDED)


def _c(text, code=_GREY):
    if _NOCOLOR:
        return text
    return f"{code}{text}{_RESET}"


def init():
    if ENABLED:
        sentry_sdk.init(dsn=_DSN, traces_sample_rate=1.0, environment="hackathon")


def backend():
    return "sentry" if ENABLED else "console"


@contextlib.contextmanager
def transaction(name="patchahead.migration_run", **tags):
    init()
    _RECORDED.clear()
    if ENABLED:
        with sentry_sdk.start_transaction(op="task", name=name) as txn:
            for k, v in tags.items():
                txn.set_tag(k, v)
            yield txn
    else:
        print(_c(f"  ⎯ trace[{name}] started ({backend()} backend)"))
        yield None


@contextlib.contextmanager
def span(name, **data):
    start = time.time()
    try:
        if ENABLED:
            with sentry_sdk.start_span(op="patchahead.step", description=name) as sp:
                for k, v in data.items():
                    sp.set_data(k, v)
                try:
                    yield sp
                except Exception as exc:
                    sentry_sdk.capture_exception(exc)
                    raise
        else:
            yield None
    finally:
        dur = int((time.time() - start) * 1000)
        _RECORDED.append({"name": name, "ms": dur})
        if not ENABLED:
            print(_c(f"    ↪ span {name:<22} {dur:>5} ms"))


def set_tags(txn, **tags):
    if ENABLED and txn is not None:
        for k, v in tags.items():
            txn.set_tag(k, v)
