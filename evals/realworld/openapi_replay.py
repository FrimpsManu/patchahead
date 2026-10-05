"""Apply renames read from real OpenAPI specs to real public code.

Each case in ``cases_openapi.json`` is a public repository, pinned to a commit,
whose Python code contains the old name of a rename that
``spec_history.py`` read from GitHub's or Stripe's spec. Most use the same word
for something else entirely, and those are the point: every edit PatchAhead
makes there is wrong. This fetches the matched files at the pinned commit,
migrates them with the rename as ``openapi-diff`` writes it, and prints every
edit and every site reported instead, for reading.

It measures editing, not verification: the repositories' tests are not run.
Only the matched files are fetched, which is enough for these two families:
a field rename and an endpoint move are decided within the file a name is used
in. Needs network access.

    python evals/realworld/openapi_replay.py [--json]
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from patchahead import engine  # noqa: E402

CASES = Path(__file__).with_name("cases_openapi.json")


def main(argv: list[str]) -> int:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    results = []
    totals: dict[str, Counter[str]] = {}
    with tempfile.TemporaryDirectory(prefix="patchahead-openapi-replay-") as scratch:
        documents = {}
        for key, rename in data["renames"].items():
            document = Path(scratch) / f"{key}.json"
            target = {k: rename[k] for k in ("symbol", "replacement", "owner")}
            document.write_text(
                json.dumps(
                    {
                        "changes": [
                            {
                                "title": f"{rename['symbol']} -> {rename['replacement']}",
                                "kind": rename["kind"],
                                "target": {**target, "owner_explicit": True},
                            }
                        ]
                    }
                )
            )
            documents[key] = document
        for case in data["cases"]:
            root = Path(scratch) / case["rename"] / case["repo"].replace("/", "__")
            for relative in case["paths"]:
                url = (
                    f"https://raw.githubusercontent.com/{case['repo']}/{case['commit']}/"
                    f"{urllib.parse.quote(relative)}"
                )
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    destination.write_bytes(urllib.request.urlopen(url, timeout=60).read())
                except OSError:
                    continue
            run = engine.migrate(
                root,
                documents[case["rename"]],
                engine.EngineOptions(run_tests=False, write_artifacts=False),
            )
            result = run.results[0]
            edits = [str(t.reference) for t in (result.plan.transformations if result.plan else [])]
            reported = [str(f.reference) for f in result.impact.findings if not f.patchable]
            tally = totals.setdefault(case["rename"], Counter())
            tally.update(repos=1, edits=len(edits), reported=len(reported))
            results.append({**case, "edits": edits, "reported": reported})
    if "--json" in argv:
        print(json.dumps(results, indent=2))
        return 0
    for key, tally in totals.items():
        rename = data["renames"][key]
        print(
            f"{rename['api']} {rename['owner']}: {rename['symbol']} -> {rename['replacement']}: "
            f"{tally['repos']} repositories, {tally['edits']} edit(s), "
            f"{tally['reported']} reported"
        )
    for result in results:
        for edit in result["edits"]:
            print(f"  edit  {result['repo']} {edit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
