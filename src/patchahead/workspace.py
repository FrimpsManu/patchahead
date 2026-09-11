"""Repository and isolated-workspace abstractions.

Two classes, with a deliberate asymmetry:

* :class:`Repository` is **read-only**. It is how PatchAhead sees the user's
  actual source tree. It has no write method, so no amount of later refactoring
  can accidentally introduce a path that mutates the user's files.
* :class:`Workspace` is a writable *copy*, created in a temporary directory.
  All patching and all test execution happen there.

This is the structural version of the safety promise. The prototype called
``Path(...).write_text()`` on the user's repository and relied on a fixture-only
restore function to undo it (``docs/assessment.md`` §2.4).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from patchahead.analysis import edits as edit_utils
from patchahead.analysis import index as repo_index
from patchahead.config import Config
from patchahead.domain.plan import TextEdit

log = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Raised when an isolated workspace cannot be created or used."""


class RepositoryError(Exception):
    """Raised when the target path is not a usable repository."""


def _is_within(child: Path, parent: Path) -> bool:
    """Whether ``child`` is inside ``parent`` after resolving symlinks."""
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


@dataclass
class Repository:
    """A read-only view of the repository the user pointed PatchAhead at."""

    root: Path
    config: Config

    @classmethod
    def open(cls, path: str | os.PathLike[str], config: Config | None = None) -> Repository:
        """Open a local repository, loading its PatchAhead configuration."""
        from patchahead import config as config_module

        root = Path(path).expanduser()
        if not root.exists():
            raise RepositoryError(f"no such directory: {root}")
        if not root.is_dir():
            raise RepositoryError(f"not a directory: {root}")
        root = root.resolve()
        return cls(root=root, config=config or config_module.load(root))

    @property
    def is_git_repo(self) -> bool:
        return (self.root / ".git").exists()

    def read(self, relative: str) -> str:
        """Read a file by repo-relative path.

        Refuses to read outside the repository root, so a crafted path in a
        change document cannot make PatchAhead read arbitrary files.
        """
        target = (self.root / relative).resolve()
        if not _is_within(target, self.root):
            raise RepositoryError(f"path escapes the repository root: {relative}")
        return target.read_text(encoding="utf-8")

    def index(self) -> repo_index.RepoIndex:
        """Discover and parse every Python file. Cached by the caller, not here."""
        return repo_index.build(self.root, self.config)

    def count_files(self) -> int:
        """Total files that would be copied into a workspace."""
        total = 0
        stack = [self.root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    relative = entry.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if repo_index.is_excluded(relative, self.config.exclude):
                    continue
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                else:
                    total += 1
        return total


@dataclass
class Workspace:
    """A writable temporary copy of a repository.

    Created by :meth:`materialize`, cleaned up by :meth:`cleanup` or by using it
    as a context manager. Never shares a path with the source repository.
    """

    root: Path
    source_root: Path
    config: Config
    #: relative path -> original contents, captured lazily on first write, so
    #: :meth:`restore` and :meth:`diff` always have a true baseline.
    _originals: dict[str, str] = field(default_factory=dict, repr=False)
    _cleaned_up: bool = field(default=False, repr=False)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def materialize(cls, repository: Repository, prefix: str = "patchahead-") -> Workspace:
        """Copy ``repository`` into a fresh temporary directory.

        A plain directory copy is used rather than ``git worktree`` for two
        reasons: it works on repositories that are not git repositories or have
        uncommitted work, and it captures exactly the state the user is looking
        at. ``git worktree`` would silently analyze committed state instead.
        """
        file_count = repository.count_files()
        limit = repository.config.max_workspace_files
        if limit and file_count > limit:
            raise WorkspaceError(
                f"repository has {file_count} files, above the "
                f"`max_workspace_files` limit of {limit}. Narrow the scope with "
                f"`source_dirs`/`exclude`, or raise the limit, in the "
                f"repository's PatchAhead configuration."
            )

        exclude = list(repository.config.exclude)

        def ignore(directory: str, names: list[str]) -> set[str]:
            try:
                relative_dir = Path(directory).resolve().relative_to(repository.root)
            except ValueError:
                return set()
            ignored = set()
            for name in names:
                relative = (relative_dir / name).as_posix().lstrip("./")
                if repo_index.is_excluded(relative, exclude):
                    ignored.add(name)
            return ignored

        temp_root = Path(tempfile.mkdtemp(prefix=prefix))
        destination = temp_root / repository.root.name
        try:
            shutil.copytree(
                repository.root,
                destination,
                ignore=ignore,
                symlinks=True,
                ignore_dangling_symlinks=True,
            )
        except OSError as exc:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise WorkspaceError(f"could not copy repository into a workspace: {exc}") from exc

        log.debug("materialized workspace at %s (%d files)", destination, file_count)
        return cls(root=destination, source_root=repository.root, config=repository.config)

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Delete the workspace. Safe to call twice; never touches the source."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self.root == self.source_root:  # pragma: no cover - defensive
            raise WorkspaceError("refusing to delete the source repository")
        shutil.rmtree(self.root.parent, ignore_errors=True)
        log.debug("cleaned up workspace %s", self.root)

    def keep(self) -> Path:
        """Leave the workspace on disk and return its path."""
        self._cleaned_up = True
        return self.root

    # -- file access -------------------------------------------------------

    def _resolve(self, relative: str) -> Path:
        target = (self.root / relative).resolve()
        if not _is_within(target, self.root):
            raise WorkspaceError(f"path escapes the workspace root: {relative}")
        return target

    def read(self, relative: str) -> str:
        return self._resolve(relative).read_text(encoding="utf-8")

    def write(self, relative: str, contents: str) -> None:
        """Write a file, recording its original contents the first time."""
        target = self._resolve(relative)
        if relative not in self._originals:
            try:
                self._originals[relative] = target.read_text(encoding="utf-8")
            except OSError:
                self._originals[relative] = ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        log.debug("wrote %s in workspace", relative)

    def apply_edits(self, relative: str, text_edits: list[TextEdit]) -> str:
        """Apply range edits to one file and write the result back."""
        original = self.read(relative)
        patched = edit_utils.apply_edits(original, text_edits)
        self.write(relative, patched)
        return patched

    def original(self, relative: str) -> str:
        """The contents a file had before this workspace modified it."""
        if relative in self._originals:
            return self._originals[relative]
        return self.read(relative)

    def index(self) -> repo_index.RepoIndex:
        return repo_index.build(self.root, self.config)

    # -- change tracking ---------------------------------------------------

    def changed_files(self) -> list[str]:
        """Files whose current contents differ from their originals."""
        changed = []
        for relative, original in sorted(self._originals.items()):
            try:
                current = self.read(relative)
            except OSError:
                changed.append(relative)
                continue
            if current != original:
                changed.append(relative)
        return changed

    def diff(self, context: int = 3) -> str:
        """A unified diff of everything this workspace changed."""
        entries = []
        for relative in self.changed_files():
            try:
                current = self.read(relative)
            except OSError:
                current = ""
            entries.append((relative, self._originals[relative], current))
        return edit_utils.combined_diff(entries, context=context)

    def restore(self, relative: str | None = None) -> None:
        """Undo modifications -- one file, or all of them."""
        targets = [relative] if relative else list(self._originals)
        for path in targets:
            if path not in self._originals:
                continue
            self._resolve(path).write_text(self._originals[path], encoding="utf-8")
            del self._originals[path]
        log.debug("restored %d file(s) in workspace", len(targets))

    # -- command execution -------------------------------------------------

    def run(
        self,
        command: str,
        timeout: int | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a shell command with the workspace as the working directory.

        This executes code from the repository under analysis. That is
        unavoidable for a tool whose central claim is "tests verify", and it is
        why ``docs/safety.md`` states plainly that PatchAhead must only be
        pointed at repositories and test commands the user trusts. PatchAhead
        does not sandbox the command; it runs with the caller's privileges.
        """
        env = dict(os.environ)
        # Keep the workspace importable the way a developer running tests from
        # the repository root would have it, and stop stray .pyc files from
        # being written into the copy.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        # Resolve bare `python`/`pytest` to the environment PatchAhead itself is
        # running in. Without this, a default `python -m pytest` finds whatever
        # `python` happens to be first on PATH -- often a system interpreter
        # with no pytest installed -- and every migration fails validation for a
        # reason that has nothing to do with the migration. Explicit paths and
        # other interpreters in a configured `test_command` still win, because
        # this only prepends a directory rather than rewriting the command.
        interpreter_dir = str(Path(sys.executable).parent)
        env["PATH"] = os.pathsep.join(
            [interpreter_dir] + ([env["PATH"]] if env.get("PATH") else [])
        )
        env.update(extra_env or {})

        log.debug("running in workspace: %s", command)
        return subprocess.run(
            command,
            shell=True,
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
