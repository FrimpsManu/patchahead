#!/usr/bin/env python3
"""Optional web UI for PatchAhead.

    pip install 'patchahead[web]'
    patchahead demo                      # the bundled walkthrough
    patchahead web --repo ./my-service   # your own repository

The CLI is the product. This exists because a diff, an impact list, and five
gate results are easier to read side by side than stacked in a terminal.

It is a *view*, with no logic of its own: every endpoint calls
:mod:`patchahead.engine` exactly as ``patchahead analyze`` and
``patchahead migrate`` do, and renders the same result objects. There is no
demo-only code path here -- that was the prototype's arrangement, and it meant
the dashboard could only ever work on the bundled fixtures. ``patchahead demo``
is this same app, pointed at the bundled repository with a list of scenarios
attached; the scenarios choose *which* change document to run and say what to
look at, and nothing else.

Binds to 127.0.0.1. It runs a repository's test command, so it must not be
exposed to a network -- see ``docs/safety.md``.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from patchahead import __version__, engine, handlers, observability, reporting
from patchahead.config import ConfigError
from patchahead.config import load as load_config
from patchahead.demo import Scenario
from patchahead.ingest import IngestError
from patchahead.workspace import RepositoryError

log = logging.getLogger("patchahead.web")

#: Allowed change-document extensions, mirroring what the ingest layer reads.
DOCUMENT_SUFFIXES = {".md", ".txt", ".rst", ".json", ".yaml", ".yml"}


def static_root() -> Path:
    """Directory holding the UI's static assets.

    Package data. Resolved from the module rather than the working directory so
    that an installed wheel serves the same page a checkout does.
    """
    return Path(__file__).resolve().parent / "static"


def index_path() -> Path:
    path = static_root() / "index.html"
    if not path.is_file():  # pragma: no cover - a broken install
        raise RuntimeError(
            f"the web UI's page is missing from the installed package (expected {path})"
        )
    return path


def create_app(repo: Path, changes_dir: Path, scenarios: Sequence[Scenario] = ()):
    """Build the FastAPI app for one repository and one directory of changes.

    ``scenarios`` is presentation metadata for the bundled demo: which documents
    to offer first and a sentence about each. It never affects how a migration
    runs -- a scenario only chooses a change document and whether tests execute,
    both of which are ordinary engine inputs.
    """
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError:  # pragma: no cover - optional dependency
        raise SystemExit("the web UI needs FastAPI: pip install 'patchahead[web]'") from None

    app = FastAPI(title="PatchAhead", version=__version__, docs_url=None, redoc_url=None)
    by_id = {scenario.id: scenario for scenario in scenarios}

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
        return HTMLResponse(index_path().read_text(encoding="utf-8"))

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
                "repo_name": repo.name,
                "changes_dir": str(changes_dir),
                "demo": bool(scenarios),
                "scenarios": [scenario.to_dict() for scenario in scenarios],
                "documents": sorted(
                    path.name
                    for path in changes_dir.iterdir()
                    if path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES
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

    @app.get("/api/document")
    def document_source(document: str) -> JSONResponse:
        """The raw text of a change document.

        The UI shows the upstream change as the vendor wrote it, next to what
        PatchAhead made of it. Reading the release note is the first step of the
        story and the one a viewer can check for themselves.
        """
        path = _resolve_change(document)
        return JSONResponse({"name": path.name, "text": path.read_text(encoding="utf-8")})

    @app.post("/api/analyze")
    def analyze(document: str) -> JSONResponse:
        try:
            result = engine.analyze(repo, _resolve_change(document))
        except (RepositoryError, IngestError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result.to_dict())

    @app.post("/api/migrate")
    def migrate(
        document: str,
        dry_run: bool = False,
        use_llm: bool = False,
        run_tests: bool = True,
        scenario: str = "",
    ) -> JSONResponse:
        # A scenario supplies exactly two ordinary engine inputs: the document
        # and whether tests run. It cannot reach anything else.
        if scenario:
            if scenario not in by_id:
                raise HTTPException(status_code=404, detail=f"no such scenario: {scenario}")
            chosen = by_id[scenario]
            document, run_tests = chosen.document, chosen.run_tests

        options = engine.EngineOptions(
            dry_run=dry_run, use_llm=use_llm, run_tests=run_tests, write_artifacts=False
        )
        try:
            run = engine.migrate(repo, _resolve_change(document), options)
        except (RepositoryError, IngestError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        payload = run.to_dict()
        payload["scenario"] = scenario
        payload["run_tests"] = run_tests
        # The reviewable artifact, rendered by the same code the CLI's
        # `--pr-summary` uses.
        payload["pr_summaries"] = [reporting.render_pr_markdown(result) for result in run.results]
        return JSONResponse(payload)

    return app


def main(argv: list[str] | None = None) -> int:
    """Serve the UI against an arbitrary repository. ``patchahead web``."""
    from patchahead import demo as demo_module

    parser = argparse.ArgumentParser(
        prog="patchahead web",
        description="Optional web UI for PatchAhead. Binds to localhost only.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="repository to analyze (default: the bundled example)",
    )
    parser.add_argument(
        "--changes",
        default=None,
        help="directory of change documents to offer (default: the bundled ones)",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    observability.configure_logging()

    repo = Path(args.repo).expanduser().resolve() if args.repo else demo_module.repo_root()
    changes = (
        Path(args.changes).expanduser().resolve() if args.changes else demo_module.changes_root()
    )
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
    print(f"  repository       {repo}")
    print(f"  change documents {changes}")
    print("  note: migrating runs this repository's test command. localhost only.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
