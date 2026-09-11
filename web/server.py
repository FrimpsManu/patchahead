#!/usr/bin/env python3
"""Optional web UI for PatchAhead.

    pip install 'patchahead[web]'
    python web/server.py --repo ./my-service

The CLI is the product. This exists because a diff, an impact list, and five
gate results are easier to read side by side than stacked in a terminal.

It is a *view*, with no logic of its own: every endpoint calls
:mod:`patchahead.engine` exactly as ``patchahead analyze`` and
``patchahead migrate`` do, and renders the same result objects. There is no
demo-only code path here -- that was the prototype's arrangement, and it meant
the dashboard could only ever work on the bundled fixtures.

Binds to 127.0.0.1. It runs a repository's test command, so it must not be
exposed to a network -- see ``docs/safety.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

if __package__ is None and str(Path(__file__).resolve().parents[1] / "src") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from patchahead import __version__, engine, handlers, observability, reporting  # noqa: E402
from patchahead.config import ConfigError  # noqa: E402
from patchahead.config import load as load_config  # noqa: E402
from patchahead.ingest import IngestError  # noqa: E402
from patchahead.workspace import RepositoryError  # noqa: E402

log = logging.getLogger("patchahead.web")

INDEX = Path(__file__).parent / "index.html"


def create_app(repo: Path, changes_dir: Path):
    """Build the FastAPI app for one repository and one directory of changes."""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:  # pragma: no cover - optional dependency
        raise SystemExit(
            "the web UI needs FastAPI: pip install 'patchahead[web]'"
        ) from None

    app = FastAPI(title="PatchAhead", version=__version__, docs_url=None, redoc_url=None)

    def _resolve_change(name: str) -> Path:
        """Resolve a change-document name inside the configured directory.

        Path traversal is refused: the UI exposes one directory, and a crafted
        name must not be able to read outside it.
        """
        candidate = (changes_dir / name).resolve()
        if not candidate.is_file() or changes_dir.resolve() not in candidate.parents:
            raise HTTPException(status_code=404, detail=f"no such change document: {name}")
        return candidate

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(INDEX.read_text(encoding="utf-8"))

    @app.get("/api/context")
    def context() -> JSONResponse:
        """What this instance is pointed at, and what it can do."""
        try:
            config = load_config(repo)
        except ConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(
            {
                "version": __version__,
                "repo": str(repo),
                "changes_dir": str(changes_dir),
                "documents": sorted(
                    path.name
                    for path in changes_dir.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in {".md", ".txt", ".rst", ".json", ".yaml", ".yml"}
                ),
                "config": config.to_dict(),
                "handlers": [
                    {
                        "name": handler.name,
                        "summary": handler.summary,
                        "kinds": [kind.value for kind in handler.kinds],
                        "limitations": list(handler.limitations),
                    }
                    for handler in handlers.registered()
                ],
            }
        )

    @app.post("/api/analyze")
    def analyze(document: str) -> JSONResponse:
        try:
            result = engine.analyze(repo, _resolve_change(document))
        except (RepositoryError, IngestError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result.to_dict())

    @app.post("/api/migrate")
    def migrate(document: str, dry_run: bool = False, use_llm: bool = False) -> JSONResponse:
        options = engine.EngineOptions(
            dry_run=dry_run, use_llm=use_llm, write_artifacts=False
        )
        try:
            run = engine.migrate(repo, _resolve_change(document), options)
        except (RepositoryError, IngestError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        payload = run.to_dict()
        # The reviewable artifact, rendered by the same code the CLI's
        # `--pr-summary` uses.
        payload["pr_summaries"] = [
            reporting.render_pr_markdown(result) for result in run.results
        ]
        return JSONResponse(payload)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patchahead-web",
        description="Optional web UI for PatchAhead. Binds to localhost only.",
    )
    parser.add_argument(
        "--repo", default="examples/orders-service",
        help="repository to analyze (default: the bundled example)",
    )
    parser.add_argument(
        "--changes", default="examples/changes",
        help="directory of change documents to offer (default: examples/changes)",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    observability.configure_logging()

    repo = Path(args.repo).expanduser().resolve()
    changes = Path(args.changes).expanduser().resolve()
    if not repo.is_dir():
        log.error("not a directory: %s", repo)
        return 2
    if not changes.is_dir():
        log.error("not a directory: %s", changes)
        return 2

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - optional dependency
        log.error("the web UI needs uvicorn: pip install 'patchahead[web]'")
        return 2

    app = create_app(repo, changes)
    print(f"PatchAhead {__version__}  ->  http://127.0.0.1:{args.port}")
    print(f"  repository      {repo}")
    print(f"  change documents {changes}")
    print("  note: migrating runs this repository's test command. localhost only.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
