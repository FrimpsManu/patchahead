"""Optional error reporting must never carry the repository's source code."""

from __future__ import annotations

import sys
import types

import pytest

from patchahead import observability


@pytest.fixture
def fake_sentry(monkeypatch):
    """A stand-in `sentry_sdk` that records what `init` was given."""
    calls: list[dict] = []
    module = types.ModuleType("sentry_sdk")
    module.init = lambda **kwargs: calls.append(kwargs)
    monkeypatch.setitem(sys.modules, "sentry_sdk", module)
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.invalid/1")
    monkeypatch.setattr(
        observability,
        "_sentry_state",
        {"initialized": False, "enabled": False, "reason": "not initialized"},
    )
    return calls


def test_frame_locals_are_turned_off_at_init(fake_sentry):
    """sentry-sdk attaches every frame's locals by default; here they are source code."""
    assert observability.init_error_reporting() is True

    assert fake_sentry[0]["include_local_variables"] is False
    assert fake_sentry[0]["send_default_pii"] is False


def test_frame_locals_are_stripped_from_an_event_anyway():
    frame = {"function": "apply_edits", "vars": {"source": "SECRET = 'proprietary'"}}
    event = {
        "exception": {"values": [{"stacktrace": {"frames": [frame]}}]},
        "threads": {"values": [{"stacktrace": {"frames": [dict(frame)]}}]},
    }

    scrubbed = observability._scrub_event(event, None)

    frames = [
        f
        for section in ("exception", "threads")
        for value in scrubbed[section]["values"]
        for f in value["stacktrace"]["frames"]
    ]
    assert frames and all("vars" not in f for f in frames)
    assert all(f["function"] == "apply_edits" for f in frames)


def test_an_event_without_stack_traces_passes_through():
    event = {"message": "hello", "contexts": {"env": {"HOME": "/home/x"}}}

    scrubbed = observability._scrub_event(event, None)

    assert scrubbed["message"] == "hello"
    assert "env" not in scrubbed["contexts"]
