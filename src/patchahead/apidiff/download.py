"""Getting one version of a library to read: a local tree, a wheel, or PyPI.

Only wheels are fetched. A wheel is a zip of files; reading it runs nothing. A
source distribution may have to be *built* before its files can be read, and
building runs the package's own setup code -- so refusing sdists is not an
optimization here, it is the safety property.

Wheels come straight from PyPI's JSON API with the standard library, rather
than through ``pip download``: that works in environments without pip (a
``uv``-made virtualenv has none), and the file is checked against the SHA-256
digest PyPI publishes for it.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


class ApiDiffError(Exception):
    """A version could not be fetched or read."""


#: The package index's JSON API. Overridable for a mirror that serves the same API.
INDEX_URL = os.environ.get("PATCHAHEAD_PYPI_URL", "https://pypi.org/pypi")
#: How long one request may take.
TIMEOUT_SECONDS = 60


def fetch(package: str, version: str, dest: Path) -> Path:
    """Download ``package==version`` as a wheel into ``dest``, unpack it, return the tree."""
    url = f"{INDEX_URL}/{urllib.parse.quote(package)}/{urllib.parse.quote(version)}/json"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            release = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ApiDiffError(f"{package}=={version} was not found on the package index") from None
        raise ApiDiffError(f"could not look up {package}=={version}: {exc}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiDiffError(f"could not look up {package}=={version}: {exc}") from None

    wheels = [f for f in release.get("urls", []) if f.get("packagetype") == "bdist_wheel"]
    if not wheels:
        raise ApiDiffError(
            f"{package}=={version} has no wheel. PatchAhead reads wheels only, because "
            f"building a source distribution runs the package's own code; pass a local "
            f"directory or .whl file with --old/--new instead"
        )
    # Any wheel carries the Python source; a pure one is simply the smallest.
    wheels.sort(key=lambda f: (not f["filename"].endswith("-none-any.whl"), f["filename"]))
    chosen = wheels[0]

    dest.mkdir(parents=True, exist_ok=True)
    target = dest / Path(chosen["filename"]).name
    try:
        with urllib.request.urlopen(chosen["url"], timeout=TIMEOUT_SECONDS) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiDiffError(f"could not download {chosen['filename']}: {exc}") from None
    expected = (chosen.get("digests") or {}).get("sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise ApiDiffError(f"{chosen['filename']} does not match the digest the index published")
    target.write_bytes(data)
    return unpack(target, dest / "unpacked")


def unpack(source: Path, dest: Path) -> Path:
    """A directory as is; a ``.whl`` or ``.zip`` extracted into ``dest``."""
    if source.is_dir():
        return source
    if source.suffix not in (".whl", ".zip") or not source.is_file():
        raise ApiDiffError(f"{source} is not a directory, a .whl, or a .zip")
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                target = (dest / member.filename).resolve()
                # A crafted archive can name `../../somewhere`; extract nothing
                # outside the destination.
                if root != target and root not in target.parents:
                    raise ApiDiffError(f"{source} contains an unsafe path: {member.filename}")
                if not member.is_dir():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
    except zipfile.BadZipFile as exc:
        raise ApiDiffError(f"{source} is not a valid archive: {exc}") from exc
    return dest
