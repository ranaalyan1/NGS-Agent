"""The Box — FastAPI app.

Three states, one route that matters, and no decisions of its own:

    POST /analyze  ->  core.assess.assess_path  ->  core.answer + core.report

Everything it returns was produced by core; this file only moves bytes around
and names the temp file. There are deliberately no settings, no menus and no
file-type pickers: the sniffer decides what a file is.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core.answer import answer_verdict
from core.assess import assess_path
from core.models import DECISION_LABELS, KIND_LABELS
from core.report import render_html, render_json
from core.version import RULESET_VERSION, TOOL_VERSION

PAGE_PATH = Path(__file__).with_name("index.html")

app = FastAPI(
    title="NGS-Agent",
    description="Drop an NGS file, get a plain-language answer with receipts.",
    version=TOOL_VERSION,
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The one page."""
    return FileResponse(PAGE_PATH, media_type="text/html")


def _safe_name(name: str) -> str:
    """Keep the uploaded name for display, drop anything filesystem-hostile.

    The sniffer judges content, so the name is only a label — but it is the
    label the reader sees, so it must survive the round trip.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")[:120]
    return safe or "upload"


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)) -> dict:
    """Sniff -> parse -> rules -> answer. Returns everything the page renders."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file was sent.")

    workdir = tempfile.mkdtemp(prefix="ngs-agent-")
    try:
        # Sanitise the name before it touches the filesystem; the sniffer
        # judges content, so this changes nothing about the verdict.
        saved = Path(workdir) / _safe_name(file.filename or "upload")
        with saved.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)

        verdict = assess_path(saved)
        answer = answer_verdict(verdict)
        html = render_html(verdict, answer)
        sidecar = render_json(verdict, answer)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {
        "subject": verdict.subject or file.filename,
        "kind": verdict.kind,
        "kind_label": KIND_LABELS.get(verdict.kind, verdict.kind),
        "decision": verdict.decision,
        "decision_label": DECISION_LABELS.get(verdict.decision, verdict.decision),
        "headline": verdict.headline,
        "sections": [
            {"heading": b.heading, "lines": b.lines, "details": b.details} for b in answer.blocks
        ],
        "findings": [f.to_dict() for f in verdict.findings],
        "unknown": verdict.unknown,
        "input_sha256": verdict.details.get("input_sha256", ""),
        "tool_version": TOOL_VERSION,
        "ruleset_version": RULESET_VERSION,
        "timestamp": verdict.timestamp,
        "report_html": html,
        "report_json": sidecar,
    }


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "tool_version": TOOL_VERSION, "ruleset_version": RULESET_VERSION}
