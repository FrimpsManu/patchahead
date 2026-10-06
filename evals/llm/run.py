"""Measure the LLM fallback: how often it fixes what the deterministic handlers refuse.

Not part of the CI benchmark: it calls a model, which costs money and is not
deterministic. Each case in ``cases.json`` is written to a temporary repository
and migrated with ``--use-llm``; the result is classified as

``verified``    the model's patch passed all five gates
``unverified``  a patch was made and nothing proved it
``rejected``    the gates, or PatchAhead's own checks on the proposal, refused it
``declined``    the model said it could not migrate the code
``deterministic``  no model was needed: a handler planned the migration itself

and, for every case, whether the patch changed a line marked ``# PROTECTED`` --
the same word on another object -- which is a wrong edit whatever the gates say.

    python evals/llm/run.py              # needs ANTHROPIC_API_KEY
    python evals/llm/run.py --no-llm     # the baseline: what is refused without it
    python evals/llm/run.py --repeat 3   # run each case three times
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from patchahead import engine  # noqa: E402

CASES = Path(__file__).with_name("cases.json")


def classify(run) -> str:
    result = run.results[0]
    outcome = result.outcome.value if result.outcome else ""
    proposal = result.proposal
    used_llm = proposal is not None and proposal.engine == "llm"
    error = (proposal.error if proposal else "") or ""
    if not used_llm and outcome in ("migrated", "patched_unverified", "validation_failed"):
        return "deterministic"
    if "declined" in error:
        return "declined"
    if outcome == "migrated":
        return "verified"
    if outcome == "patched_unverified":
        return "unverified"
    if outcome in ("validation_failed", "patch_failed") or "rejected" in error:
        return "rejected"
    return outcome or "error"


def wrong_edit(diff: str, protected: list[str]) -> bool:
    removed = [
        line for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
    ]
    return any(text in line for line in removed for text in protected)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])

    data = json.loads(CASES.read_text(encoding="utf-8"))
    rows = []
    for case in data["cases"]:
        for attempt in range(args.repeat):
            with tempfile.TemporaryDirectory(prefix="patchahead-llm-eval-") as scratch:
                root = Path(scratch) / "repo"
                for relative, text in case["files"].items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                document = Path(scratch) / "change.json"
                document.write_text(json.dumps({"changes": [case["change"]]}), encoding="utf-8")
                run = engine.migrate(
                    root,
                    document,
                    engine.EngineOptions(
                        use_llm=not args.no_llm,
                        write_artifacts=False,
                        test_command=f"{sys.executable} -m pytest -q -p no:cacheprovider",
                    ),
                )
                verdict = classify(run)
                wrong = wrong_edit(run.diff or "", case["protected"])
                rows.append(
                    {
                        "id": case["id"],
                        "kind": case["kind"],
                        "attempt": attempt + 1,
                        "result": verdict,
                        "wrong_edit": wrong,
                        "wrong_edit_verified": wrong and verdict == "verified",
                        "message": run.results[0].message[:300],
                        "diff": run.diff or "",
                    }
                )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        flag = "  WRONG EDIT" if row["wrong_edit"] else ""
        print(f"{row['kind']:8} {row['id']:34} #{row['attempt']}  {row['result']}{flag}")
    for kind in ("fixable", "trap", "trap_untested"):
        tally = Counter(row["result"] for row in rows if row["kind"] == kind)
        print(f"{kind}: {dict(tally)}")
    print(
        f"wrong edits: {sum(r['wrong_edit'] for r in rows)}  "
        f"(verified despite being wrong: {sum(r['wrong_edit_verified'] for r in rows)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
