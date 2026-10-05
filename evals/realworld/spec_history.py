"""Read every change PatchAhead finds in a spec's history, month by month.

For a spec file in a public GitHub repository, this takes the version current
at the start of each month, compares each with the next using
``patchahead.openapi``, and prints every reading -- the renames PatchAhead would
migrate and the changes it only reports -- as JSON lines, with a summary.
Checking the readings against what the API did is the study; this produces them.

    python evals/realworld/spec_history.py github/rest-api-description \\
        descriptions/api.github.com/api.github.com.json 2021-01 2025-10 > github.jsonl
    python evals/realworld/spec_history.py stripe/openapi openapi/spec3.json \\
        2021-01 2025-10 > stripe.jsonl

Snapshots are cached in ``~/.cache/patchahead-spec-history``, or in
``SPEC_HISTORY_CACHE`` when set. Set
``GITHUB_TOKEN`` to raise GitHub's API rate limit. Needs network access.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from patchahead import openapi  # noqa: E402

CACHE = Path(
    os.environ.get("SPEC_HISTORY_CACHE") or Path.home() / ".cache" / "patchahead-spec-history"
)


def _get(url: str, attempts: int = 4) -> bytes:
    request = urllib.request.Request(url)
    if os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except (OSError, http.client.HTTPException):
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def months(start: str, end: str):
    year, month = map(int, start.split("-"))
    last = tuple(map(int, end.split("-")))
    while (year, month) <= last:
        yield f"{year:04d}-{month:02d}"
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def snapshot(repo: str, path: str, month: str) -> tuple[str, Path] | None:
    """The commit current at the start of ``month``, and its spec file."""
    api = f"https://api.github.com/repos/{repo}/commits?path={path}&until={month}-01T00:00:00Z&per_page=1"
    commits = json.loads(_get(api))
    if not commits:
        return None
    sha = commits[0]["sha"]
    target = CACHE / repo.replace("/", "__") / f"{sha}.json"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written whole or not at all: a cut-off download must not be cached.
        partial = target.with_suffix(".partial")
        partial.write_bytes(_get(f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"))
        partial.replace(target)
    return sha, target


def main(argv: list[str]) -> int:
    repo, path, start, end = argv[1:5]
    kinds: Counter[str] = Counter()
    previous = None
    for month in months(start, end):
        found = snapshot(repo, path, month)
        if found is None or (previous and found[0] == previous[1]):
            continue
        spec = openapi.load(found[1])
        if previous is not None:
            for change in openapi.compare(previous[2], spec).changes:
                kinds[change.kind.value] += 1
                print(
                    json.dumps(
                        {
                            "from": previous[0],
                            "to": month,
                            "kind": change.kind.value,
                            "title": change.title,
                            "symbol": change.target.symbol,
                            "replacement": change.target.replacement,
                            "owner": change.target.owner,
                            "confidence": change.confidence.value,
                            "why": change.classification_reason,
                        }
                    )
                )
        previous = (month, found[0], spec)
    print(json.dumps(dict(kinds)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
