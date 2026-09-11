"""Workspace isolation -- the structural safety guarantee."""

from __future__ import annotations

import pytest

from patchahead.config import Config
from patchahead.domain.plan import TextEdit
from patchahead.workspace import Repository, RepositoryError, Workspace, WorkspaceError


@pytest.fixture
def repository(make_repo):
    root = make_repo(
        {
            "app/client.py": "def f():\n    return 1\n",
            "tests/test_client.py": "def test_f():\n    assert True\n",
            "README.md": "# example\n",
            ".venv/lib/junk.py": "syntax ( error\n",
            "__pycache__/x.pyc": "binary",
        }
    )
    return Repository.open(root)


class TestRepository:
    def test_repository_has_no_write_method(self):
        """Structural, not conventional: there is nothing to call by mistake."""
        assert not hasattr(Repository, "write")

    def test_rejects_a_missing_path(self, tmp_path):
        with pytest.raises(RepositoryError, match="no such directory"):
            Repository.open(tmp_path / "nope")

    def test_rejects_a_file(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("x")

        with pytest.raises(RepositoryError, match="not a directory"):
            Repository.open(path)

    def test_refuses_to_read_outside_the_root(self, repository):
        with pytest.raises(RepositoryError, match="escapes"):
            repository.read("../../etc/passwd")

    def test_indexing_skips_excluded_directories(self, repository):
        index = repository.index()

        assert set(index.paths()) == {"app/client.py", "tests/test_client.py"}
        assert index.skipped == {}, ".venv is excluded, so it is never even read"


class TestWorkspaceIsolation:
    def test_patching_never_touches_the_source_tree(self, repository):
        original = (repository.root / "app/client.py").read_text()

        with Workspace.materialize(repository) as workspace:
            workspace.write("app/client.py", "def f():\n    return 2\n")
            assert workspace.read("app/client.py") != original

        assert (repository.root / "app/client.py").read_text() == original

    def test_cleanup_removes_the_workspace(self, repository):
        workspace = Workspace.materialize(repository)
        root = workspace.root
        assert root.exists()

        workspace.cleanup()

        assert not root.exists()

    def test_cleanup_is_idempotent(self, repository):
        workspace = Workspace.materialize(repository)
        workspace.cleanup()
        workspace.cleanup()

    def test_keep_leaves_the_workspace_on_disk(self, repository):
        workspace = Workspace.materialize(repository)
        root = workspace.keep()
        try:
            workspace.cleanup()
            assert root.exists()
        finally:
            import shutil

            shutil.rmtree(root.parent, ignore_errors=True)

    def test_excluded_directories_are_not_copied(self, repository):
        with Workspace.materialize(repository) as workspace:
            assert not (workspace.root / ".venv").exists()
            assert not (workspace.root / "__pycache__").exists()
            assert (workspace.root / "README.md").exists()

    def test_refuses_to_write_outside_the_workspace(self, repository):
        with (
            Workspace.materialize(repository) as workspace,
            pytest.raises(WorkspaceError, match="escapes"),
        ):
            workspace.write("../escaped.py", "x")

    def test_refuses_a_repository_over_the_file_limit(self, make_repo):
        root = make_repo({f"f{i}.py": "x = 1\n" for i in range(6)})
        repository = Repository.open(root, Config(max_workspace_files=3))

        with pytest.raises(WorkspaceError, match="max_workspace_files"):
            Workspace.materialize(repository)


class TestChangeTracking:
    def test_tracks_changed_files_and_produces_a_diff(self, repository):
        with Workspace.materialize(repository) as workspace:
            workspace.write("app/client.py", "def f():\n    return 2\n")

            assert workspace.changed_files() == ["app/client.py"]
            diff = workspace.diff()
            assert "-    return 1" in diff
            assert "+    return 2" in diff

    def test_writing_identical_contents_is_not_a_change(self, repository):
        with Workspace.materialize(repository) as workspace:
            workspace.write("app/client.py", workspace.read("app/client.py"))

            assert workspace.changed_files() == []

    def test_restore_undoes_a_write(self, repository):
        with Workspace.materialize(repository) as workspace:
            original = workspace.read("app/client.py")
            workspace.write("app/client.py", "changed\n")

            workspace.restore("app/client.py")

            assert workspace.read("app/client.py") == original
            assert workspace.changed_files() == []

    def test_apply_edits_writes_through(self, repository):
        with Workspace.materialize(repository) as workspace:
            workspace.apply_edits("app/client.py", [TextEdit(2, 11, 2, 12, "9")])

            assert workspace.read("app/client.py") == "def f():\n    return 9\n"

    def test_original_returns_the_pre_patch_contents(self, repository):
        with Workspace.materialize(repository) as workspace:
            before = workspace.read("app/client.py")
            workspace.write("app/client.py", "x\n")

            assert workspace.original("app/client.py") == before


class TestCommandExecution:
    def test_runs_with_the_workspace_as_the_working_directory(self, repository):
        with Workspace.materialize(repository) as workspace:
            completed = workspace.run("pwd || cd")

            assert str(workspace.root) in completed.stdout

    def test_bare_python_resolves_to_the_running_interpreter(self, repository):
        """Otherwise a default `python -m pytest` finds a system interpreter."""
        import sys

        with Workspace.materialize(repository) as workspace:
            completed = workspace.run("python -c 'import sys; print(sys.executable)'")

            assert completed.stdout.strip() == sys.executable

    def test_a_failing_command_returns_its_code_rather_than_raising(self, repository):
        with Workspace.materialize(repository) as workspace:
            assert workspace.run("exit 3").returncode == 3
