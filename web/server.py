#!/usr/bin/env python3
"""PatchAhead web dashboard.

A lightweight FastAPI app for the demo. The page is one self-contained HTML
file; this server just runs the real agent on demand and returns the
structured report.

    pip install fastapi uvicorn
    python web/server.py          # -> http://127.0.0.1:8000

Endpoints:
    GET  /            -> the dashboard
    POST /api/run     -> reset demo, run the migration agent, return report
    GET  /api/report  -> the most recent run (from outputs/run_log.json)
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from patchahead import agent, observability as obs, paths  # noqa: E402

app = FastAPI(title="PatchAhead", docs_url=None, redoc_url=None)

_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


def _report_payload(report) -> dict:
    pr_md = ""
    try:
        pr_md = paths.PR_SUMMARY_FILE.read_text(encoding="utf-8")
    except Exception:
        pr_md = ""
    return {
        "succeeded": report.succeeded,
        "breaking_change": report.breaking_change.to_dict(),
        "impact": report.impact.to_dict(),
        "patch": report.patch.to_dict(),  # includes the unified diff
        "tests_before": report.tests_before.to_dict(),
        "tests_after": report.tests_after.to_dict(),
        "similar_migrations": report.similar_migrations,
        "pr_summary_md": pr_md,
        "trace": {"backend": obs.backend(), "spans": obs.recorded_spans()},
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open(_INDEX, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/api/run")
def run():
    agent.reset_demo()
    report = agent.run()
    return JSONResponse(_report_payload(report))


@app.get("/api/report")
def last_report():
    if paths.RUN_LOG_FILE.exists():
        try:
            return JSONResponse(json.loads(paths.RUN_LOG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return JSONResponse({}, status_code=204)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print(f"PatchAhead dashboard -> http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
