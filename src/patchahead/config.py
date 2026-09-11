"""Project configuration.

Read from ``[tool.patchahead]`` in the target repository's ``pyproject.toml``,
or from a ``.patchahead.toml`` at its root. Every setting has a default that
works, so a repository with no configuration at all is fully supported.

Configuration belongs to the *repository being analyzed*, not to PatchAhead, so
it is loaded from the repo path rather than from the current directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from patchahead.domain.change import Confidence

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 only
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CONFIG_FILENAME = ".patchahead.toml"
PYPROJECT = "pyproject.toml"

#: Directories never walked when discovering source files. Keeps analysis fast
#: and stops PatchAhead from proposing edits to vendored or generated code.
DEFAULT_EXCLUDE = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    "build",
    "dist",
    "site-packages",
    ".eggs",
    ".patchahead",
)


class ConfigError(Exception):
    """Raised when a configuration file exists but cannot be used."""


@dataclass
class Config:
    """Effective configuration for one PatchAhead run."""

    #: Shell command used to run tests inside the isolated workspace.
    test_command: str = "python -m pytest"
    #: Directories to analyze, relative to the repo root. Empty means the
    #: whole repo minus ``exclude``.
    source_dirs: list[str] = field(default_factory=list)
    #: Directory names or glob patterns to skip.
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    #: A proposal touching more files than this fails the scope gate. Guards
    #: against a runaway rename or an LLM rewriting half the repository.
    max_changed_files: int = 10
    #: A proposal larger than this (added + removed lines) fails the scope gate.
    max_diff_lines: int = 400
    #: Findings below this confidence are reported but never patched.
    min_confidence: Confidence = Confidence.MEDIUM
    #: Whether this repository permits sending source code to an LLM. The CLI's
    #: ``--use-llm`` cannot override a ``false`` here.
    allow_llm: bool = True
    #: Where artifacts (diff, plan, report) are written, relative to cwd.
    output_dir: str = ".patchahead"
    #: Seconds before a test command is killed.
    test_timeout_seconds: int = 300
    #: Refuse to copy a repository with more files than this into a workspace.
    max_workspace_files: int = 20000

    #: Absolute path of the file this config came from, or "" for defaults.
    source_path: str = ""

    def merged_with_cli(
        self,
        *,
        test_command: str | None = None,
        output_dir: str | None = None,
        min_confidence: Confidence | None = None,
        max_changed_files: int | None = None,
    ) -> Config:
        """Apply CLI overrides on top of file configuration.

        Precedence is CLI > config file > defaults, except ``allow_llm``, which
        the CLI can only narrow (see :meth:`llm_permitted`).
        """
        updates: dict[str, Any] = {}
        if test_command:
            updates["test_command"] = test_command
        if output_dir:
            updates["output_dir"] = output_dir
        if min_confidence is not None:
            updates["min_confidence"] = min_confidence
        if max_changed_files is not None:
            updates["max_changed_files"] = max_changed_files
        return replace(self, **updates) if updates else self

    def llm_permitted(self, requested: bool) -> tuple[bool, str]:
        """Decide whether the LLM path may run, and say why if it may not.

        ``allow_llm = false`` in a repository's config is a policy statement by
        whoever owns that source code. ``--use-llm`` does not override it.
        """
        if not requested:
            return False, ""
        if not self.allow_llm:
            return False, (
                "LLM mode requested but `allow_llm = false` in this repository's "
                "PatchAhead configuration; refusing to send source code to an LLM"
            )
        return True, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_command": self.test_command,
            "source_dirs": self.source_dirs,
            "exclude": self.exclude,
            "max_changed_files": self.max_changed_files,
            "max_diff_lines": self.max_diff_lines,
            "min_confidence": self.min_confidence.value,
            "allow_llm": self.allow_llm,
            "output_dir": self.output_dir,
            "test_timeout_seconds": self.test_timeout_seconds,
            "max_workspace_files": self.max_workspace_files,
            "source_path": self.source_path,
        }


def _as_str_list(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"`{key}` must be a string or a list of strings, got {value!r}")


def _as_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"`{key}` must be an integer, got {value!r}")
    if value < 0:
        raise ConfigError(f"`{key}` must not be negative, got {value!r}")
    return value


def _as_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"`{key}` must be true or false, got {value!r}")
    return value


def from_mapping(data: dict[str, Any], source_path: str = "") -> Config:
    """Build a :class:`Config` from a parsed ``[tool.patchahead]`` table.

    Unknown keys are a hard error rather than a silent no-op: a typo'd
    ``max_changed_file`` that quietly does nothing is worse than a message.
    """
    config = Config(source_path=source_path)
    known = {
        "test_command",
        "source_dirs",
        "exclude",
        "max_changed_files",
        "max_diff_lines",
        "min_confidence",
        "allow_llm",
        "output_dir",
        "test_timeout_seconds",
        "max_workspace_files",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"unknown PatchAhead config key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )

    if "test_command" in data:
        value = data["test_command"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"`test_command` must be a non-empty string, got {value!r}")
        config.test_command = value.strip()
    if "source_dirs" in data:
        config.source_dirs = _as_str_list(data["source_dirs"], "source_dirs")
    if "exclude" in data:
        # Additive: a project extending the exclude list should not have to
        # restate the defaults, which exist to keep analysis correct and fast.
        extra = _as_str_list(data["exclude"], "exclude")
        config.exclude = list(DEFAULT_EXCLUDE) + [e for e in extra if e not in DEFAULT_EXCLUDE]
    if "max_changed_files" in data:
        config.max_changed_files = _as_int(data["max_changed_files"], "max_changed_files")
    if "max_diff_lines" in data:
        config.max_diff_lines = _as_int(data["max_diff_lines"], "max_diff_lines")
    if "min_confidence" in data:
        value = data["min_confidence"]
        try:
            config.min_confidence = Confidence(str(value).strip().lower())
        except ValueError:
            raise ConfigError(
                f"`min_confidence` must be one of high, medium, low; got {value!r}"
            ) from None
    if "allow_llm" in data:
        config.allow_llm = _as_bool(data["allow_llm"], "allow_llm")
    if "output_dir" in data:
        value = data["output_dir"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"`output_dir` must be a non-empty string, got {value!r}")
        config.output_dir = value.strip()
    if "test_timeout_seconds" in data:
        config.test_timeout_seconds = _as_int(data["test_timeout_seconds"], "test_timeout_seconds")
    if "max_workspace_files" in data:
        config.max_workspace_files = _as_int(data["max_workspace_files"], "max_workspace_files")
    return config


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:  # pragma: no cover - only on 3.10 without tomli
        raise ConfigError(
            f"cannot read {path}: no TOML parser available. "
            "Install `tomli` (Python 3.10) or use Python 3.11+."
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except Exception as exc:  # tomllib.TOMLDecodeError and friends
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def load(repo_root: Path) -> Config:
    """Load configuration for ``repo_root``.

    Looks for ``.patchahead.toml`` first (a dedicated file wins over a shared
    one), then ``[tool.patchahead]`` in ``pyproject.toml``. Returns defaults if
    neither exists. Raises :class:`ConfigError` if one exists but is unusable --
    a broken config is reported, never silently ignored.
    """
    repo_root = Path(repo_root)

    dedicated = repo_root / CONFIG_FILENAME
    if dedicated.is_file():
        data = _load_toml(dedicated)
        table = data.get("tool", {}).get("patchahead", data)
        if not isinstance(table, dict):
            raise ConfigError(f"{dedicated}: expected a table of settings")
        log.debug("loaded config from %s", dedicated)
        return from_mapping(table, source_path=str(dedicated))

    pyproject = repo_root / PYPROJECT
    if pyproject.is_file():
        data = _load_toml(pyproject)
        table = data.get("tool", {}).get("patchahead")
        if isinstance(table, dict):
            log.debug("loaded config from %s [tool.patchahead]", pyproject)
            return from_mapping(table, source_path=f"{pyproject} [tool.patchahead]")

    log.debug("no PatchAhead config found under %s; using defaults", repo_root)
    return Config()
