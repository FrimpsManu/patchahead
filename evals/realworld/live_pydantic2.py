"""Replay a live, unmerged pydantic 1 -> 2 upgrade, with the project's own tests.

technocore-rosetta has an open Dependabot pull request bumping pydantic from
1.10.26 to 2.13.5. This takes the repository at the commit that was tested,
reads the change from the two pydantic releases (as the GitHub Action does),
lets PatchAhead migrate it, and measures the result with the project's 401
tests.

On pydantic 2 the project does not import at all: six validators need changes
no rename can make (``@root_validator`` now needs ``skip_on_failure=True``;
validators taking ``field`` now take ``info``). Those six are made by hand, the
same way, in two copies -- one with PatchAhead's edits, one without -- and the
deprecation warning of each method PatchAhead renamed is turned into an error,
so a call left on the old name fails. The difference between the two runs is
what PatchAhead's edits did.

It needs network access, ``git`` and ``uv``. It is not part of the CI benchmark.

    python evals/realworld/live_pydantic2.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from patchahead import apidiff, engine  # noqa: E402
from patchahead.ingest.structured import change_to_mapping  # noqa: E402

REPO = "https://github.com/RosettaTcore/technocore-rosetta.git"
COMMIT = "c1ddd379943dc486199dbcabadf3ca2172fb4411"
OLD, NEW = "1.10.26", "2.13.5"
UV = os.environ.get("UV") or shutil.which("uv") or "uv"


def sh(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    return result.stdout + result.stderr


def venv(where: Path, project: Path, pydantic: str) -> Path:
    """The project's own requirements, then the pydantic version under test.

    Installed in two steps because the project pins ``pydantic<2``; the
    upgrade is what Dependabot's pull request does to that pin.
    """
    sh(UV, "venv", "-q", str(where), "--python", "3.12")
    python = where / "bin" / "python"
    for step in (["-r", str(project / "requirements-dev.in")], [f"pydantic=={pydantic}"]):
        sh(UV, "pip", "install", "-q", "--python", str(python), *step)
    check = sh(str(python), "-c", "import pydantic, pytest; print(pydantic.VERSION)").strip()
    if check != pydantic:
        raise SystemExit(f"could not install pydantic {pydantic} with the project's requirements")
    return python


def failing(python: Path, project: Path, *warnings: str) -> tuple[set[str], str]:
    """The failing test ids, and pytest's summary line."""
    args = [str(python)]
    for message in warnings:
        args += ["-W", f"error:{message}"]
    args += ["-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rfE", "-o", "addopts="]
    env = dict(os.environ, PYTHONPATH="src:.")
    output = sh(*args, cwd=project, env=env)
    ids = {
        re.sub(r"\[x{10,}[^\]]*\]", "[long]", line.split(" - ")[0].split(" ", 1)[1])
        for line in output.splitlines()
        if line.startswith(("FAILED ", "ERROR "))
    }
    return ids, output.strip().splitlines()[-1]


def hand_fixes(project: Path) -> None:
    """The six validator changes pydantic 2 needs before the project imports."""
    for path in ("src/rosetta/config.py", "src/rosetta/pilot_config.py"):
        file = project / path
        file.write_text(
            file.read_text().replace(
                "    @root_validator\n", "    @root_validator(skip_on_failure=True)\n"
            )
        )
    file = project / "src/rosetta/evolution.py"
    text = file.read_text().replace(
        "from pydantic import Field, StrictInt, StrictStr, validator",
        "from pydantic import Field, StrictInt, StrictStr, ValidationInfo, field_validator, "
        "validator",
    )
    for name in (
        "parent_digest_is_valid",
        "policy_paths_are_canonical",
        "policy_limits_are_positive",
        "evaluation_digest_is_valid",
    ):
        pattern = rf"    @validator\(([^)]*)\)\n    def {name}\(cls, value: ([^,]+), field: Any\)"
        match = re.search(pattern, text)
        assert match, name
        text = text.replace(
            match.group(0),
            f"    @field_validator({match.group(1)})\n"
            f"    def {name}(cls, value: {match.group(2)}, info: ValidationInfo)",
        )
    file.write_text(text.replace("field.name", "info.field_name"))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="patchahead-live-") as scratch:
        work = Path(scratch)
        project = work / "repo"
        sh("git", "clone", "--quiet", REPO, str(project))
        sh("git", "checkout", "--quiet", COMMIT, cwd=project)

        old_python = venv(work / "pydantic1", project, OLD)
        before, summary = failing(old_python, project)
        print(f"pydantic {OLD}, unchanged:  {summary}")
        new_python = venv(work / "pydantic2", project, NEW)
        _, summary = failing(new_python, project)
        print(f"pydantic {NEW}, unchanged:  {summary}")

        # The change, read from the two releases as the Action reads a bump.
        old = apidiff.read(apidiff.fetch("pydantic", OLD, work / "old"))
        new = apidiff.read(apidiff.fetch("pydantic", NEW, work / "new"))
        diff = apidiff.compare(old, new, "pydantic", (OLD, NEW))
        document = work / "changes.json"
        document.write_text(
            json.dumps({"changes": [change_to_mapping(c) for c in diff.changes if c.is_actionable]})
        )
        run = engine.migrate(
            project, document, engine.EngineOptions(run_tests=False, write_artifacts=False)
        )
        for result in run.results:
            edits = len(result.plan.transformations) if result.plan else 0
            refused = sum(1 for f in result.impact.findings if not f.patchable)
            print(f"  {result.impact.change.target.symbol:>10}: {edits} edit(s), {refused} refused")
        renamed = sorted(
            {
                r.impact.change.target.symbol
                for r in run.results
                if r.plan and r.plan.transformations
            }
        )

        copies = {}
        for name in ("without", "with"):
            copy = work / name
            shutil.copytree(project, copy)
            if name == "with":
                patch = work / "patchahead.diff"
                patch.write_text(run.diff)
                sh("git", "apply", str(patch), cwd=copy)
            hand_fixes(copy)
            # Pydantic 2 still accepts the old names, with a warning; make
            # exactly those an error, so a call left on an old name fails.
            warnings = [f"The `{symbol}` method" for symbol in renamed]
            copies[name], summary = failing(new_python, copy, *warnings)
            print(f"pydantic {NEW}, six validators fixed by hand, {name} PatchAhead:  {summary}")

        print(f"fixed by PatchAhead's edits: {len(copies['without'] - copies['with'])}")
        print(f"broken by PatchAhead's edits: {len(copies['with'] - copies['without'])}")
        print(f"failing on pydantic {OLD} before anything changed: {len(before)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
