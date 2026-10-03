"""Reading breaking changes out of two versions of a library."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import zipfile

import pytest

from patchahead import apidiff, engine
from patchahead.apidiff import download as fetch_module
from patchahead.cli import EXIT_OK, EXIT_USAGE, main
from patchahead.domain.change import ChangeKind
from patchahead.ingest import parse_file
from patchahead.ingest.base import IngestError
from tests.conftest import dedent


def library(tmp_path, name, files):
    root = tmp_path / name
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(source), encoding="utf-8")
    return root


class TestReadingAnApi:
    def test_parameters_keep_their_kind_and_default(self, tmp_path):
        root = library(
            tmp_path,
            "lib",
            {"sdk/__init__.py": "def f(a, /, b, c=1, *args, d, e=2, **kw):\n    pass\n"},
        )

        member = apidiff.read(root).public["sdk.f"]

        assert [(p.name, p.kind, p.has_default) for p in member.params] == [
            ("a", "positional", False),
            ("b", "normal", False),
            ("c", "normal", True),
            ("args", "var_positional", False),
            ("d", "keyword", False),
            ("e", "keyword", True),
            ("kw", "var_keyword", False),
        ]

    def test_self_is_not_a_parameter_but_a_static_methods_first_argument_is(self, tmp_path):
        root = library(
            tmp_path,
            "lib",
            {
                "sdk/__init__.py": """
                    class C:
                        def m(self, x):
                            pass

                        @staticmethod
                        def s(x):
                            pass
                """
            },
        )

        surface = apidiff.read(root)

        assert [p.name for p in surface.public["sdk.C.m"].params] == ["x"]
        assert [p.name for p in surface.public["sdk.C.s"].params] == ["x"]

    def test_all_private_names_and_private_modules_are_respected(self, tmp_path):
        root = library(
            tmp_path,
            "lib",
            {
                "sdk/__init__.py": "__all__ = ['shown']\n\ndef shown():\n    pass\n\ndef hidden():\n    pass\n",
                "sdk/_impl.py": "def internal():\n    pass\n",
                "sdk/tools.py": "def _private():\n    pass\n\ndef public():\n    pass\n",
            },
        )

        assert sorted(apidiff.read(root).public) == ["sdk.shown", "sdk.tools.public"]

    def test_a_reexport_from_a_private_module_is_public(self, tmp_path):
        root = library(
            tmp_path,
            "lib",
            {
                "sdk/__init__.py": "from ._client import Client as Client\n",
                "sdk/_client.py": "class Client:\n    def get(self):\n        pass\n",
            },
        )

        surface = apidiff.read(root)

        assert surface.public["sdk.Client.get"].module == "sdk._client"

    def test_a_package_directory_itself_can_be_given(self, tmp_path):
        root = library(tmp_path, "lib", {"sdk/__init__.py": "def f():\n    pass\n"})

        assert "sdk.f" in apidiff.read(root / "sdk").public

    def test_a_module_that_does_not_parse_is_reported_not_fatal(self, tmp_path):
        root = library(
            tmp_path, "lib", {"sdk/__init__.py": "def f():\n    pass\n", "sdk/bad.py": "def (:\n"}
        )

        surface = apidiff.read(root)

        assert "sdk.f" in surface.public
        assert "sdk.bad" in surface.skipped


class TestUnpacking:
    def test_a_directory_is_read_in_place(self, tmp_path):
        assert apidiff.unpack(tmp_path, tmp_path / "out") == tmp_path

    def test_a_wheel_is_extracted(self, tmp_path):
        wheel = tmp_path / "sdk-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("sdk/__init__.py", "def f():\n    pass\n")

        root = apidiff.unpack(wheel, tmp_path / "out")

        assert "sdk.f" in apidiff.read(root).public

    def test_an_archive_that_writes_outside_its_folder_is_refused(self, tmp_path):
        wheel = tmp_path / "evil-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("../../escaped.py", "x = 1\n")

        with pytest.raises(apidiff.ApiDiffError, match="unsafe path"):
            apidiff.unpack(wheel, tmp_path / "out")
        assert not (tmp_path / "escaped.py").exists()


class FakeIndex:
    """Stands in for PyPI's JSON API and its file host."""

    def __init__(self, releases: dict[str, dict], files: dict[str, bytes]):
        self.releases, self.files = releases, files

    def __call__(self, url, timeout=None):
        if url.endswith("/json"):
            key = url.rsplit("/", 3)[-3] + "==" + url.rsplit("/", 2)[-2]
            if key not in self.releases:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return io.BytesIO(json.dumps(self.releases[key]).encode())
        return io.BytesIO(self.files[url])


def wheel_bytes(source: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sdk/__init__.py", source)
    return buffer.getvalue()


class TestFetchingFromTheIndex:
    def release(self, data: bytes, digest: str | None = None, kind: str = "bdist_wheel"):
        return {
            "urls": [
                {
                    "packagetype": kind,
                    "filename": "sdk-1.0-py3-none-any.whl",
                    "url": "https://files.example/sdk-1.0-py3-none-any.whl",
                    "digests": {"sha256": digest or hashlib.sha256(data).hexdigest()},
                }
            ]
        }

    def test_a_wheel_is_downloaded_verified_and_read(self, tmp_path, monkeypatch):
        data = wheel_bytes("def f():\n    pass\n")
        index = FakeIndex(
            {"sdk==1.0": self.release(data)},
            {"https://files.example/sdk-1.0-py3-none-any.whl": data},
        )
        monkeypatch.setattr(fetch_module.urllib.request, "urlopen", index)

        root = apidiff.fetch("sdk", "1.0", tmp_path)

        assert "sdk.f" in apidiff.read(root).public

    def test_a_tampered_download_is_refused(self, tmp_path, monkeypatch):
        data = wheel_bytes("def f():\n    pass\n")
        index = FakeIndex(
            {"sdk==1.0": self.release(data, digest="0" * 64)},
            {"https://files.example/sdk-1.0-py3-none-any.whl": data},
        )
        monkeypatch.setattr(fetch_module.urllib.request, "urlopen", index)

        with pytest.raises(apidiff.ApiDiffError, match="digest"):
            apidiff.fetch("sdk", "1.0", tmp_path)

    def test_a_release_with_only_a_source_distribution_is_not_built(self, tmp_path, monkeypatch):
        index = FakeIndex({"sdk==1.0": self.release(b"", kind="sdist")}, {})
        monkeypatch.setattr(fetch_module.urllib.request, "urlopen", index)

        with pytest.raises(apidiff.ApiDiffError, match="no wheel"):
            apidiff.fetch("sdk", "1.0", tmp_path)

    def test_an_unknown_version_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fetch_module.urllib.request, "urlopen", FakeIndex({}, {}))

        with pytest.raises(apidiff.ApiDiffError, match="not found"):
            apidiff.fetch("sdk", "9.9", tmp_path)


OLD = {"sdk/__init__.py": "class Client:\n    def fetch_all(self, limit=10):\n        return []\n"}
NEW = {
    "sdk/__init__.py": """
        import warnings


        class Client:
            def list_all(self, limit=10):
                return []

            def fetch_all(self, limit=10):
                warnings.warn("use list_all instead", DeprecationWarning)
                return self.list_all(limit)
    """
}


class TestCommand:
    def test_writes_a_change_document_migrate_can_read(self, tmp_path, capsys):
        old, new = library(tmp_path, "old", OLD), library(tmp_path, "new", NEW)
        out = tmp_path / "changes.json"

        code = main(["api-diff", "--old", str(old), "--new", str(new), "--out", str(out)])

        assert code == EXIT_OK
        assert "method_rename" in capsys.readouterr().out
        [change] = parse_file(out)
        assert change.kind is ChangeKind.METHOD_RENAME
        assert (change.target.symbol, change.target.replacement) == ("fetch_all", "list_all")
        # The class is known; what callers name their instance is not.
        assert (change.target.owner, change.target.owner_is_explicit) == ("Client", False)

    def test_the_document_migrates_a_receiver_with_any_name(self, tmp_path, make_repo, capsys):
        old, new = library(tmp_path, "old", OLD), library(tmp_path, "new", NEW)
        out = tmp_path / "changes.json"
        main(["api-diff", "--old", str(old), "--new", str(new), "--out", str(out)])
        repo = make_repo({"app/a.py": "def go(api_client):\n    return api_client.fetch_all()\n"})

        run = engine.migrate(
            repo, out, engine.EngineOptions(run_tests=False, write_artifacts=False)
        )

        assert "api_client.list_all()" in run.results[0].diff

    def test_old_and_new_paths_go_together(self, tmp_path):
        assert main(["api-diff", "--old", str(tmp_path)]) == EXIT_USAGE

    def test_a_package_needs_both_versions(self):
        assert main(["api-diff", "sdk", "1.0"]) == EXIT_USAGE

    def test_nothing_to_migrate_writes_no_document(self, tmp_path, capsys):
        same = library(tmp_path, "same", OLD)
        out = tmp_path / "changes.json"

        assert main(["api-diff", "--old", str(same), "--new", str(same), "--out", str(out)]) == 0
        assert not out.exists()
        assert "no breaking changes" in capsys.readouterr().out


class TestOwnerExplicitInStructuredDocuments:
    def test_false_makes_the_owner_a_hint(self, write_change):
        document = write_change(
            json.dumps(
                {
                    "title": "t",
                    "kind": "method_rename",
                    "target": {
                        "symbol": "a",
                        "replacement": "b",
                        "owner": "Client",
                        "owner_explicit": False,
                    },
                }
            ),
            name="c.json",
        )

        assert parse_file(document)[0].target.owner_is_explicit is False

    def test_a_non_boolean_is_rejected(self, write_change):
        document = write_change(
            json.dumps(
                {
                    "title": "t",
                    "kind": "method_rename",
                    "target": {"symbol": "a", "replacement": "b", "owner_explicit": "no"},
                }
            ),
            name="c.json",
        )

        with pytest.raises(IngestError, match="owner_explicit"):
            parse_file(document)
