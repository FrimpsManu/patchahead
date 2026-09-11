"""Finding the tests that exercise a given module.

Used for two things: telling a reviewer which tests cover the code being
changed, and giving the targeted-tests validation gate a narrow, fast subset to
run before the full suite.

The strategy is name-based and deliberately simple: a module ``app/client.py``
is matched to ``tests/test_client.py``, ``tests/app/test_client.py``,
``tests/test_app_client.py``, and so on. It does not trace imports, so it is
*best effort*. That is why the regression gate always runs the full suite as
well -- the targeted gate exists to fail fast and to tell a reviewer where to
look, not to replace running everything.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import PurePosixPath

from patchahead.analysis.index import RepoIndex, is_test_path

log = logging.getLogger(__name__)


def module_stem(path: str) -> str:
    """The importable stem of a module path: ``app/order_sync.py`` -> ``order_sync``."""
    return PurePosixPath(path).stem


def _candidate_names(source_path: str) -> set[str]:
    """Test-module basenames that would conventionally cover ``source_path``."""
    pure = PurePosixPath(source_path)
    stem = pure.stem
    if stem == "__init__":
        stem = pure.parent.name or stem

    names = {f"test_{stem}.py", f"{stem}_test.py"}
    # tests/test_app_client.py for app/client.py
    parts = [part for part in pure.parts[:-1] if part not in (".", "src")]
    if parts:
        names.add(f"test_{'_'.join(parts[-1:] + [stem])}.py")
        names.add(f"test_{'_'.join(parts + [stem])}.py")
    return names


def tests_for_path(index: RepoIndex, source_path: str) -> list[str]:
    """Test modules that look like they cover ``source_path``.

    Ordered by how specific the match is, so the most likely test comes first.
    """
    if is_test_path(source_path):
        return []

    candidates = _candidate_names(source_path)
    stem = module_stem(source_path)
    exact: list[str] = []
    fuzzy: list[str] = []

    for test_path in index.test_paths():
        name = PurePosixPath(test_path).name
        if name in candidates:
            exact.append(test_path)
        elif stem and stem in name:
            fuzzy.append(test_path)

    return exact + fuzzy


def tests_for_paths(index: RepoIndex, source_paths: list[str]) -> list[str]:
    """The union of tests covering several modules, order-preserving."""
    found: list[str] = []
    for source_path in source_paths:
        for test_path in tests_for_path(index, source_path):
            if test_path not in found:
                found.append(test_path)
    return found


def tests_by_file(index: RepoIndex, source_paths: list[str]) -> dict[str, list[str]]:
    """A ``source path -> test paths`` mapping, for the impact graph."""
    mapping: dict[str, list[str]] = {}
    for source_path in source_paths:
        tests = tests_for_path(index, source_path)
        if tests:
            mapping[source_path] = tests
    return mapping


def has_any_tests(index: RepoIndex) -> bool:
    return bool(index.test_paths())


def scoped_command(test_command: str, test_paths: list[str]) -> str:
    """Build a command that runs only ``test_paths``.

    Only attempted for pytest-shaped commands, where appending paths is a
    well-defined way to narrow a run. For any other runner the full command is
    returned unchanged and the caller reports the targeted gate as covering the
    whole suite -- guessing at another runner's path syntax would produce a gate
    that silently tests nothing.
    """
    if not test_paths:
        return test_command
    if "pytest" not in test_command:
        return test_command
    # Refuse to append to a command that already has shell plumbing; appending
    # after a pipe or redirect would change what the arguments apply to.
    if any(token in test_command for token in ("|", ">", "<", "&&", ";")):
        return test_command
    return " ".join([test_command] + [shlex.quote(path) for path in test_paths])
