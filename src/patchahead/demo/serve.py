"""Starting the bundled demo: pick a port, print the URL, serve the UI.

Everything here is about removing steps between ``pip install`` and seeing the
product work. It contains no migration logic and no second execution path -- it
builds the same FastAPI app :mod:`patchahead.web.server` builds for any
repository, hands it the bundled fixtures, and runs it on the loopback
interface.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from patchahead import __version__
from patchahead.demo import DemoError, changes_root, repo_root, scenarios

log = logging.getLogger("patchahead.demo")

DEFAULT_PORT = 8000
#: How far to look for a free port before giving up. Twenty is enough to get
#: past a handful of other dev servers and small enough to fail quickly.
PORT_SEARCH_RANGE = 20
HOST = "127.0.0.1"


def port_is_free(port: int, host: str = HOST) -> bool:
    """Whether a server could bind this port right now.

    Asks the only question that matters -- "can I bind?" -- rather than probing
    for a listener, because a port can be unbindable for reasons other than
    something serving on it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def choose_port(preferred: int = DEFAULT_PORT, *, explicit: bool = False) -> int:
    """Return a bindable port, or explain why there is not one.

    When the user named a port, an occupied one is an error: silently serving
    somewhere else would send them to a URL they did not ask for, and in the
    worst case to a *different* server that is already there. When the port is
    just our default, moving on is the friendlier behaviour and is announced.
    """
    if port_is_free(preferred):
        return preferred
    if explicit:
        raise DemoError(
            f"port {preferred} is already in use. Pass a different --port, or stop "
            f"whatever is listening on {HOST}:{preferred}."
        )
    for candidate in range(preferred + 1, preferred + 1 + PORT_SEARCH_RANGE):
        if port_is_free(candidate):
            log.info("port %d was busy; using %d instead", preferred, candidate)
            return candidate
    raise DemoError(
        f"ports {preferred}-{preferred + PORT_SEARCH_RANGE} are all in use. "
        f"Free one, or pass --port."
    )


def browser_is_practical() -> bool:
    """Whether opening a browser would do something useful.

    On a headless machine -- a container, a CI runner, an SSH session --
    ``webbrowser.open`` can launch a terminal browser over the top of the
    output, which is worse than doing nothing. The URL is always printed, so
    declining here costs the user one click.
    """
    if os.environ.get("PATCHAHEAD_NO_BROWSER"):
        return False
    if sys.platform in ("darwin", "win32"):
        return True
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def _open_later(url: str, delay: float = 1.0) -> None:
    """Open the browser once the server has had a moment to start listening."""

    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception as exc:  # pragma: no cover - platform dependent
            log.debug("could not open a browser: %s", exc)

    timer = threading.Timer(delay, _open)
    timer.daemon = True
    timer.start()


def test_runner_available() -> bool:
    """Whether the bundled repository's test command can actually run.

    The demo's whole point is the difference between "patched" and "verified",
    and verification needs a test runner. Without pytest every scenario reports,
    honestly and uselessly, that the test command could not start -- so this is
    checked up front and the fix is printed, rather than leaving the viewer to
    conclude the tool is broken.
    """
    return importlib.util.find_spec("pytest") is not None


def banner(url: str, repo: Path, test_command: str, *, can_run_tests: bool = True) -> str:
    """The text printed before the server takes over the terminal."""
    lines = [
        "",
        f"  PatchAhead {__version__} — demo",
        "",
        f"  Open   {url}",
        "",
        f"  Repository   {repo}",
        f"  Scenarios    {len(scenarios())} bundled, including one PatchAhead refuses",
        "",
        "  This is the real engine. Migrating copies the bundled repository to a",
        f"  temporary directory, patches the copy, and runs `{test_command}` there.",
        "  The bundled repository is never modified. Serving on localhost only.",
        "",
        "  Ctrl-C to stop.",
        "",
    ]
    if not can_run_tests:
        lines[-1:] = [
            "  WARNING: pytest is not installed, so no scenario can be verified.",
            "  Every run will report that the test command could not start.",
            "  Fix:  pip install 'patchahead[demo]'",
            "",
        ]
    return "\n".join(lines)


def serve(
    port: int = DEFAULT_PORT,
    *,
    port_was_explicit: bool = False,
    open_browser: bool = True,
    scenario: str = "",
) -> int:
    """Run the demo server until interrupted. Returns a process exit code."""
    from patchahead.config import load as load_config
    from patchahead.web.server import create_app

    repo = repo_root()
    changes = changes_root()

    try:
        import uvicorn
    except ImportError:
        log.error(
            "the demo needs its extra dependencies: pip install 'patchahead[demo]' "
            "(FastAPI, uvicorn, and the pytest that verifies the migrations)"
        )
        return 2

    chosen = choose_port(port, explicit=port_was_explicit)
    app = create_app(repo, changes, scenarios())
    url = f"http://{HOST}:{chosen}"
    if scenario:
        url = f"{url}/?scenario={scenario}"

    print(
        banner(
            url,
            repo,
            load_config(repo).test_command,
            can_run_tests=test_runner_available(),
        )
    )
    if open_browser and browser_is_practical():
        _open_later(url)

    uvicorn.run(app, host=HOST, port=chosen, log_level="warning")
    return 0
