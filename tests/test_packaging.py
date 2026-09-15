"""The distribution has to carry the demo, not just the code that serves it.

`patchahead demo` is the first thing a new user runs, and it is the one command
whose dependencies are *files* rather than modules: a repository to migrate, a
set of release notes, and an HTML page. Python packaging does not ship those by
accident -- `orders-service` is not a valid identifier, so package discovery
skips that whole subtree and it travels only because `package-data` patterns say
so. A pattern that is one directory too shallow drops files silently, the build
stays green, and the demo breaks for everyone who installed rather than cloned.

So these tests build the real artifacts and look inside them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest

from patchahead import demo
from patchahead.web.server import index_path
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.slow


#: Everything the build backend reads. Copied to a scratch directory rather
#: than built in place, because setuptools reuses `build/` in the project root
#: and a stale one makes this whole module measure the *previous* build --
#: which it silently did, until that was noticed.
BUILD_INPUTS = ("pyproject.toml", "README.md", "LICENSE", "src")


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """Build a real sdist and wheel from a pristine copy of the source."""
    pytest.importorskip("build", reason="packaging checks need the `build` module")
    source = tmp_path_factory.mktemp("source")
    for name in BUILD_INPUTS:
        origin = REPO_ROOT / name
        if not origin.exists():  # pragma: no cover - LICENSE may be named differently
            continue
        if origin.is_dir():
            shutil.copytree(
                origin,
                source / name,
                # `*.egg-info` matters: an editable install leaves one in
                # `src/`, and setuptools will happily rebuild from its stale
                # SOURCES.txt instead of from the current `package-data`
                # patterns -- which makes this module assert nothing at all.
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".pytest_cache", "*.egg-info"
                ),
            )
        else:
            shutil.copy2(origin, source / name)

    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        # `--no-isolation` keeps this offline and deterministic; the CI
        # packaging job runs the isolated build that a real publish uses.
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(out), str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - surfaces a broken build
        pytest.fail(f"python -m build failed:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
    return out


def wheel_names(built: Path) -> set[str]:
    wheels = list(built.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as archive:
        return set(archive.namelist())


def sdist_names(built: Path) -> set[str]:
    sdists = list(built.glob("*.tar.gz"))
    assert len(sdists) == 1, sdists
    with tarfile.open(sdists[0]) as archive:
        # Strip the `patchahead-0.2.0/` prefix every sdist member carries.
        return {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}


def fixture_files_on_disk() -> set[str]:
    root = demo.fixtures_root()
    package = root.parents[1]
    return {
        str(path.relative_to(package))
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts
    }


class TestTheWheelCarriesTheDemo:
    def test_every_bundled_fixture_file_is_in_the_wheel(self, built):
        """The check that catches a `package-data` glob one level too shallow.

        Asserting on a handful of known filenames would pass while a new
        subdirectory of fixtures quietly failed to ship. This compares the whole
        tree.
        """
        names = wheel_names(built)
        missing = sorted(
            path for path in fixture_files_on_disk() if f"patchahead/{path}" not in names
        )

        assert not missing, f"these fixture files are not in the wheel: {missing}"

    def test_the_web_page_is_in_the_wheel(self, built):
        assert "patchahead/web/static/index.html" in wheel_names(built)

    def test_every_scenario_document_is_in_the_wheel(self, built):
        names = wheel_names(built)

        for scenario in demo.scenarios():
            assert f"patchahead/demo/fixtures/changes/{scenario.document}" in names, scenario.id

    def test_the_bundled_repository_ships_with_its_tests(self, built):
        """Without them there is nothing to go from red to green."""
        names = wheel_names(built)
        tests = {n for n in names if n.startswith("patchahead/demo/fixtures/orders-service/tests/")}

        assert tests, "the example repository's tests did not ship"

    def test_caches_are_not_shipped(self, built):
        names = wheel_names(built)

        assert not [n for n in names if "__pycache__" in n or ".pytest_cache" in n]


class TestTheSdistCarriesTheDemo:
    def test_every_bundled_fixture_file_is_in_the_sdist(self, built):
        names = sdist_names(built)
        missing = sorted(
            path for path in fixture_files_on_disk() if f"src/patchahead/{path}" not in names
        )

        assert not missing, f"these fixture files are not in the sdist: {missing}"

    def test_the_web_page_is_in_the_sdist(self, built):
        assert "src/patchahead/web/static/index.html" in sdist_names(built)


class TestNothingResolvesRelativeToTheWorkingDirectory:
    """The bug this guards against is invisible from a checkout.

    In a git clone, a path like `examples/orders-service` resolves from the
    repository root and everything looks fine -- right up until someone
    `pip install`s the package and runs the command from their own project.
    """

    def test_the_demo_paths_are_absolute_and_inside_the_package(self, tmp_path):
        import patchahead

        package = Path(patchahead.__file__).resolve().parent
        for path in (demo.fixtures_root(), demo.repo_root(), demo.changes_root(), index_path()):
            assert path.is_absolute()
            assert package in path.resolve().parents

    def test_the_demo_command_works_from_an_unrelated_directory(self, tmp_path):
        """Run it with a cwd that has nothing to do with the checkout."""
        result = subprocess.run(
            [sys.executable, "-m", "patchahead.cli", "demo", "--print-paths"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        printed = dict(line.split(None, 1) for line in result.stdout.strip().splitlines())
        assert Path(printed["repository"].strip()).is_dir()
        assert Path(printed["changes"].strip()).is_dir()

    def test_the_scenario_listing_works_from_an_unrelated_directory(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "patchahead.cli", "demo", "--list"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "field-rename" in result.stdout


class TestConsoleEntryPoint:
    def test_the_installed_script_exists_and_runs(self):
        script = Path(sysconfig.get_path("scripts")) / (
            "patchahead.exe" if sys.platform == "win32" else "patchahead"
        )
        if not script.exists():  # pragma: no cover - not installed in this environment
            pytest.skip("patchahead is not installed as a console script here")

        result = subprocess.run([str(script), "--version"], capture_output=True, text=True)

        assert result.returncode == 0
        assert "patchahead" in result.stdout


class TestTheExtrasStoryHoldsTogether:
    """`[web]` serves the UI; `[demo]` is what makes the demo *verify* anything.

    The distinction is easy to lose in a docstring and impossible to lose here.
    Without a test runner every bundled scenario reports, honestly and
    uselessly, that the test command could not start -- so pytest belongs in the
    extra the demo instructions name, and the demo instructions have to name
    that extra.
    """

    @staticmethod
    def optional_dependencies() -> dict[str, list[str]]:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10
            tomli = pytest.importorskip("tomli", reason="reading pyproject needs tomli on 3.10")
            tomllib = tomli
        with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
            return tomllib.load(handle)["project"]["optional-dependencies"]

    def test_the_demo_extra_brings_a_test_runner(self):
        demo_extra = " ".join(self.optional_dependencies()["demo"])

        assert "pytest" in demo_extra

    def test_the_demo_extra_includes_the_web_extra(self):
        demo_extra = " ".join(self.optional_dependencies()["demo"])

        assert "patchahead[web]" in demo_extra

    def test_the_web_extra_stays_the_ui_alone(self):
        """It is a legitimate install for someone pointing the UI at their own
        repository, which brings its own test runner. Adding pytest here would
        make the narrower extra pay for the demo's needs."""
        web_extra = " ".join(self.optional_dependencies()["web"])

        assert "fastapi" in web_extra
        assert "uvicorn" in web_extra
        assert "pytest" not in web_extra

    def test_every_extra_the_readme_names_exists(self):
        """A documented extra that is not declared is an install that fails."""
        declared = set(self.optional_dependencies())
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        named = set(re.findall(r"\.\[([a-z,]+)\]", readme))

        for group in named:
            for extra in group.split(","):
                assert extra in declared, f"README names a `{extra}` extra that does not exist"
