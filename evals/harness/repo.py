"""Building throwaway repositories on disk for the executing suites.

The ``migrations`` and ``validation`` suites do not simulate a repository: they
write one to a temporary directory, run real ``pytest`` subprocesses against it,
and read the real gate results. A suite that mocks the thing it is measuring
measures the mock.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Makes a repository's own modules importable by its tests without packaging
#: it. Datasets that ship tests need this file; it is added automatically so
#: every case does not have to repeat it.
CONFTEST = (
    "import os\nimport sys\n\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
)


@contextmanager
def temporary_repo(files: dict[str, str], change: str = "") -> Iterator[tuple[Path, Path]]:
    """Materialize ``files`` as a repository; yield ``(repo_path, change_path)``.

    The directory is removed on exit whether or not the case passed. Keeping a
    failed case's tree around sounds useful and is not: the suites run dozens of
    cases, and the ones worth inspecting are reproducible from the dataset.
    """
    temp = Path(tempfile.mkdtemp(prefix="patchahead-eval-"))
    try:
        repo = temp / "repo"
        repo.mkdir()
        has_tests = any(path.startswith("tests/") or "test_" in path for path in files)
        if has_tests and "conftest.py" not in files:
            files = {"conftest.py": CONFTEST, **files}
        for relative, source in files.items():
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")

        change_path = temp / "change.md"
        change_path.write_text(change, encoding="utf-8")
        yield repo, change_path
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def changed_line_count(diff: str) -> int:
    """Added + removed lines in a unified diff, excluding its file headers."""
    return sum(
        1
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def touched_text(diff: str) -> str:
    """Only the added and removed lines of a diff.

    Minimality assertions check against this rather than the whole diff: a
    unified diff carries unchanged context lines too, and matching those would
    flag every correct patch as having touched text it should not have.
    """
    return "\n".join(
        line
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
