"""Configuration loading, validation, and CLI precedence."""

from __future__ import annotations

import pytest

from patchahead import config as config_module
from patchahead.config import Config, ConfigError
from patchahead.domain.change import Confidence


def test_defaults_apply_with_no_config_file(tmp_path):
    config = config_module.load(tmp_path)

    assert config.test_command == "python -m pytest"
    assert config.min_confidence is Confidence.MEDIUM
    assert config.source_path == ""


def test_reads_tool_patchahead_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.patchahead]\ntest_command = "nox -s tests"\n'
        'source_dirs = ["src"]\nmax_changed_files = 3\n'
    )
    config = config_module.load(tmp_path)

    assert config.test_command == "nox -s tests"
    assert config.source_dirs == ["src"]
    assert config.max_changed_files == 3
    assert "pyproject.toml" in config.source_path


def test_dedicated_file_wins_over_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.patchahead]\ntest_command = "from-pyproject"\n')
    (tmp_path / ".patchahead.toml").write_text('test_command = "from-dedicated"\n')

    assert config_module.load(tmp_path).test_command == "from-dedicated"


def test_dedicated_file_also_accepts_a_tool_table(tmp_path):
    (tmp_path / ".patchahead.toml").write_text('[tool.patchahead]\ntest_command = "scoped"\n')

    assert config_module.load(tmp_path).test_command == "scoped"


def test_exclude_extends_the_defaults_rather_than_replacing_them(tmp_path):
    (tmp_path / ".patchahead.toml").write_text('exclude = ["vendor"]\n')
    config = config_module.load(tmp_path)

    assert "vendor" in config.exclude
    assert ".git" in config.exclude, "a project adding one exclusion must not lose the defaults"


def test_unknown_key_is_an_error_not_a_silent_noop(tmp_path):
    (tmp_path / ".patchahead.toml").write_text("max_changed_file = 3\n")

    with pytest.raises(ConfigError, match="max_changed_file"):
        config_module.load(tmp_path)


@pytest.mark.parametrize(
    "body, message",
    [
        ("test_command = 3\n", "test_command"),
        ("max_changed_files = -1\n", "must not be negative"),
        ('min_confidence = "certain"\n', "min_confidence"),
        ("allow_llm = 1\n", "allow_llm"),
        ("source_dirs = [1, 2]\n", "source_dirs"),
    ],
)
def test_bad_values_are_rejected_with_a_useful_message(tmp_path, body, message):
    (tmp_path / ".patchahead.toml").write_text(body)

    with pytest.raises(ConfigError, match=message):
        config_module.load(tmp_path)


def test_malformed_toml_is_reported_not_ignored(tmp_path):
    (tmp_path / ".patchahead.toml").write_text("this is not = = toml\n")

    with pytest.raises(ConfigError, match="not valid TOML"):
        config_module.load(tmp_path)


def test_cli_overrides_take_precedence_over_the_file():
    config = Config(test_command="pytest", max_changed_files=10)

    merged = config.merged_with_cli(test_command="nox", max_changed_files=2)

    assert merged.test_command == "nox"
    assert merged.max_changed_files == 2
    assert config.test_command == "pytest", "merging must not mutate the original"


def test_allow_llm_false_cannot_be_overridden_by_the_cli():
    config = Config(allow_llm=False)

    permitted, reason = config.llm_permitted(requested=True)

    assert permitted is False
    assert "allow_llm = false" in reason


def test_llm_not_requested_is_not_a_refusal():
    permitted, reason = Config(allow_llm=True).llm_permitted(requested=False)

    assert permitted is False
    assert reason == ""
