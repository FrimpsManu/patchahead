"""Test discovery and execution."""

from patchahead.testing import discovery, runner
from patchahead.testing.discovery import (
    has_any_tests,
    scoped_command,
    tests_by_file,
    tests_for_path,
    tests_for_paths,
)
from patchahead.testing.runner import run_tests

__all__ = [
    "discovery",
    "run_tests",
    "runner",
    "has_any_tests",
    "scoped_command",
    "tests_by_file",
    "tests_for_path",
    "tests_for_paths",
]
