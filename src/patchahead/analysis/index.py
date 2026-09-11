"""A parsed, cached view of one repository's Python source.

Each file is read and parsed at most once per run, no matter how many handlers
analyze it. The prototype re-read and re-scanned every file for every pattern of
every change; this does one pass and shares it.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from patchahead.analysis.python_ast import ModuleAnalysis, ParseError, analyze_source
from patchahead.config import Config

log = logging.getLogger(__name__)

#: Files larger than this are skipped. A multi-megabyte generated module is
#: never the hand-written integration code a migration targets, and parsing it
#: costs more than it can possibly return.
MAX_FILE_BYTES = 2_000_000


@dataclass
class RepoIndex:
    """Discovered and parsed Python modules under a repository root."""

    root: Path
    config: Config
    #: relative POSIX path -> parsed module
    modules: dict[str, ModuleAnalysis] = field(default_factory=dict)
    #: relative POSIX path -> why it was skipped
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.modules)

    def paths(self) -> list[str]:
        return sorted(self.modules)

    def get(self, path: str) -> ModuleAnalysis | None:
        return self.modules.get(path)

    def source_of(self, path: str) -> str:
        module = self.modules.get(path)
        return module.source if module else ""

    def test_paths(self) -> list[str]:
        """Paths that look like pytest test modules."""
        return [p for p in self.paths() if is_test_path(p)]

    def non_test_paths(self) -> list[str]:
        return [p for p in self.paths() if not is_test_path(p)]


def is_test_path(path: str) -> bool:
    """Whether a relative path looks like a pytest test module.

    Follows pytest's own default discovery rules (``test_*.py`` / ``*_test.py``),
    plus the near-universal convention of a ``tests/`` directory.
    """
    pure = PurePosixPath(path)
    name = pure.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part in ("test", "tests") for part in pure.parts[:-1])


def is_excluded(relative: str, patterns: list[str]) -> bool:
    """Whether a relative POSIX path is excluded by name or glob.

    A bare name like ``node_modules`` excludes any directory with that name at
    any depth; a pattern containing ``/`` or ``*`` is matched as a glob against
    the whole relative path.
    """
    parts = PurePosixPath(relative).parts
    for pattern in patterns:
        if "/" in pattern or "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(relative, pattern):
                return True
            if fnmatch.fnmatch(relative, pattern.rstrip("/") + "/*"):
                return True
        elif pattern in parts:
            return True
    return False


def discover_python_files(root: Path, config: Config) -> list[Path]:
    """Every analyzable ``.py`` file under ``root``, honouring the config.

    Directories are pruned during the walk rather than filtered afterwards, so
    a ``node_modules`` or ``.venv`` inside the repository costs nothing.
    """
    root = Path(root).resolve()
    roots: list[Path] = []
    if config.source_dirs:
        for entry in config.source_dirs:
            candidate = (root / entry).resolve()
            if not candidate.is_dir():
                log.warning("configured source_dir does not exist: %s", entry)
                continue
            if root not in candidate.parents and candidate != root:
                log.warning("ignoring source_dir outside the repository: %s", entry)
                continue
            roots.append(candidate)
        if not roots:
            log.warning("no configured source_dirs exist; scanning the whole repository")
            roots = [root]
    else:
        roots = [root]

    # When `source_dirs` narrows analysis, test files are still discovered from
    # the whole repository. `source_dirs` exists to stop PatchAhead proposing
    # edits outside a project's own source; it is not a statement about where
    # the tests live, and narrowing test discovery with it would silently
    # disable the targeted-test validation gate.
    scan_roots = list(roots)
    if config.source_dirs and root not in scan_roots:
        scan_roots.append(root)

    found: list[Path] = []
    seen: set[Path] = set()
    for base in scan_roots:
        restrict_to_tests = base == root and config.source_dirs
        stack = [base]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir())
            except OSError as exc:
                log.debug("cannot list %s: %s", directory, exc)
                continue
            for entry in entries:
                try:
                    relative = entry.resolve().relative_to(root).as_posix()
                except ValueError:
                    # A symlink pointing outside the repository. Following it
                    # could make PatchAhead read or propose edits to files the
                    # user did not point it at.
                    log.debug("skipping path outside the repository: %s", entry)
                    continue
                if is_excluded(relative, config.exclude):
                    continue
                if entry.is_symlink():
                    log.debug("skipping symlink: %s", relative)
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.suffix == ".py" and entry not in seen:
                    if restrict_to_tests and not is_test_path(relative):
                        continue
                    seen.add(entry)
                    found.append(entry)
    return sorted(found)


def build(root: Path, config: Config) -> RepoIndex:
    """Discover, read, and parse every Python file under ``root``.

    Unreadable and unparseable files are recorded in
    :attr:`RepoIndex.skipped` with a reason and reported to the user, rather
    than dropped. A repository with a syntax error in one module is still
    analyzable, but the user should know which module was not looked at.
    """
    root = Path(root).resolve()
    index = RepoIndex(root=root, config=config)

    for path in discover_python_files(root, config):
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError as exc:
            index.skipped[relative] = f"cannot stat: {exc}"
            continue
        if size > MAX_FILE_BYTES:
            index.skipped[relative] = f"file is {size} bytes (limit {MAX_FILE_BYTES})"
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            index.skipped[relative] = f"not valid UTF-8: {exc.reason}"
            continue
        except OSError as exc:
            index.skipped[relative] = f"cannot read: {exc}"
            continue
        try:
            index.modules[relative] = analyze_source(source, relative)
        except ParseError as exc:
            index.skipped[relative] = f"syntax error: {exc}"

    log.debug(
        "indexed %d module(s), skipped %d, under %s",
        len(index.modules),
        len(index.skipped),
        root,
    )
    for relative, reason in index.skipped.items():
        log.debug("skipped %s: %s", relative, reason)
    return index
