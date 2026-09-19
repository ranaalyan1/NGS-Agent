"""Loading recorded HTTP responses as an evidence transport.

A recording directory looks like::

    <dir>/
        manifest.json     recording_version, clinvar_release, recorded_at, recordings[]
        <name>.json       verbatim HTTP response bodies

The manifest is what makes a recording *auditable* rather than merely
convenient: it states where the responses came from, when they were captured,
under what licence, and any alteration that was made to them. A fixture whose
provenance is undocumented is a fixture nobody can trust.

Two consumers use this:

* the test suite, so adapter parsing is exercised against real response shapes
  without depending on a live service;
* air-gapped and demo deployments, so ``ngsagent review`` can run end to end
  with no egress at all.

Nothing here fabricates a response. If a request has no recording, the
transport raises — an unmatched request in a test means the test proved nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.errors import CoreError
from ngs_agent.core.evidence.transport import (
    EUTILS_BASE_URL,
    HttpResponse,
    RecordedTransport,
    canonical_url,
)

DEFAULT_CLINVAR_RECORDINGS = (
    Path(__file__).resolve().parents[1] / "data" / "recordings" / "clinvar"
)


class RecordingsError(CoreError, ValueError):
    """A recording directory is missing, malformed, or incomplete."""


class RecordingEntry(BaseModel):
    """One recorded request/response pair."""

    model_config = ConfigDict(extra="forbid")

    file: str
    path: str
    status: int = 200
    params: dict[str, str] = Field(default_factory=dict)
    purpose: str = ""


class RecordingManifest(BaseModel):
    """Provenance for a recording directory."""

    model_config = ConfigDict(extra="forbid")

    recording_version: str
    description: str = ""
    recorded_at: str = ""
    recorded_by: str = ""
    clinvar_release: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    integrity_note: str = ""
    retrieval_note: str = ""
    recordings: list[RecordingEntry] = Field(default_factory=list)
    alleles_covered: list[dict[str, Any]] = Field(default_factory=list)


def load_manifest(directory: Path | str) -> RecordingManifest:
    resolved = Path(directory)
    manifest_path = resolved / "manifest.json"
    if not manifest_path.is_file():
        raise RecordingsError(f"no manifest.json in recording directory {resolved}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordingsError(f"{manifest_path}: {exc}") from exc
    return RecordingManifest.model_validate(payload)


def load_recordings(
    directory: Path | str, *, base_url: str = EUTILS_BASE_URL
) -> tuple[RecordedTransport, RecordingManifest]:
    """Build a :class:`RecordedTransport` from a recording directory."""
    resolved = Path(directory)
    manifest = load_manifest(resolved)
    responses: dict[str, HttpResponse] = {}
    for entry in manifest.recordings:
        body_path = resolved / entry.file
        if not body_path.is_file():
            raise RecordingsError(
                f"manifest lists {entry.file} but {body_path} does not exist"
            )
        body = body_path.read_bytes()
        url = f"{base_url}/{entry.path}"
        key = canonical_url(url, entry.params)
        responses[key] = HttpResponse(
            status_code=entry.status, body=body, url=key, elapsed_ms=0
        )
    if not responses:
        raise RecordingsError(f"recording directory {resolved} contains no recordings")
    return RecordedTransport(responses), manifest


def recording_facts(manifest: RecordingManifest) -> dict[str, Any]:
    """Manifest fields worth surfacing in a result's provenance block."""
    return {
        "recording_version": manifest.recording_version,
        "recorded_at": manifest.recorded_at,
        "clinvar_release": manifest.clinvar_release,
        "integrity_note": manifest.integrity_note,
        "retrieval_note": manifest.retrieval_note,
        "source": manifest.source,
    }
