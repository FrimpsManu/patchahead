"""Shared fixtures.

The guiding rule (from ``docs/contributing.md``): build realistic repositories on
disk and run the real engine over them. The Anthropic API is the one *external
service* that is mocked, because it is remote, paid, and non-deterministic;
everything else -- filesystem workspaces, subprocess test runs, wheel builds --
happens for real.

A few tests in ``tests/test_demo.py`` do substitute functions this project owns
(the server start, the port probe, the browser launch) where the behaviour under
test is the wiring rather than the effect. No part of the migration pipeline is
ever substituted.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from patchahead import demo
from patchahead.analysis import analyze_source
from patchahead.analysis.index import RepoIndex
from patchahead.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The bundled example repository and change documents. They live inside the
#: package so `patchahead demo` works from a wheel install; the tests reach them
#: the same way the demo does rather than by a path relative to the checkout.
EXAMPLES = demo.fixtures_root()
EXAMPLE_REPO = demo.repo_root()
EXAMPLE_CHANGES = demo.changes_root()


@pytest.fixture
def config() -> Config:
    return Config()


def dedent(source: str) -> str:
    """Normalize an inline source fixture and guarantee a trailing newline."""
    return textwrap.dedent(source).lstrip("\n")


@pytest.fixture
def make_index():
    """Build a RepoIndex from inline sources, without touching the filesystem.

    For unit tests of analysis and planning, where a real directory would add
    IO without adding coverage.
    """

    def _make(files: dict[str, str], config: Config | None = None) -> RepoIndex:
        index = RepoIndex(root=Path("/nonexistent"), config=config or Config())
        for path, source in files.items():
            index.modules[path] = analyze_source(dedent(source), path)
        return index

    return _make


@pytest.fixture
def make_repo(tmp_path: Path):
    """Write a real repository to a temp directory and return its path."""

    def _make(files: dict[str, str], name: str = "repo") -> Path:
        root = tmp_path / name
        for path, source in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dedent(source), encoding="utf-8")
        return root

    return _make


@pytest.fixture
def write_change(tmp_path: Path):
    """Write a change document and return its path."""

    def _write(text: str, name: str = "change.md") -> Path:
        path = tmp_path / name
        path.write_text(dedent(text), encoding="utf-8")
        return path

    return _write
