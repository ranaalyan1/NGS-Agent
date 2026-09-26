"""Regex-only diagnosis for Snakemake and Cromwell run logs."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import (
    DECISION_FIX_AND_RERUN,
    DECISION_UNKNOWN,
    KIND_CROMWELL_LOG,
    KIND_SNAKEMAKE_LOG,
    SEVERITY_FAIL,
    Finding,
    Receipt,
    Verdict,
)
from ..util import sha256_file
from ..version import RULESET_VERSION

SIGNATURE_DIR = Path(__file__).resolve().parents[1] / "signatures"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_signatures(runner: str) -> list[dict[str, Any]]:
    prefix = "sm_" if runner == "snakemake" else "cw_"
    signatures = []
    for path in sorted(SIGNATURE_DIR.glob(f"{prefix}*.yaml")):
        signatures.append(json.loads(path.read_text(encoding="utf-8")))
    return signatures


def _matching_lines(patterns: list[str], lines: list[str]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for pattern in patterns:
        try:
            expression = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        hits.extend(
            (line_number, line)
            for line_number, line in enumerate(lines, start=1)
            if expression.search(line)
        )
    return hits


def diagnose_runner(path: str | Path, runner: str) -> Verdict:
    """Return the highest-ranked evidence-supported runner finding or unknown."""
    if runner not in ("snakemake", "cromwell"):
        raise ValueError("runner must be 'snakemake' or 'cromwell'")

    source = Path(path)
    kind = KIND_SNAKEMAKE_LOG if runner == "snakemake" else KIND_CROMWELL_LOG
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    lines = text.splitlines()
    digest = sha256_file(source) if source.is_file() else "0" * 64
    signatures = _load_signatures(runner)

    matches: list[tuple[int, dict[str, Any], list[tuple[int, str]]]] = []
    for signature in signatures:
        hits = _matching_lines(signature["evidence"], lines)
        if hits:
            matches.append((int(signature.get("rank", 999)), signature, hits[:5]))
    matches.sort(key=lambda match: (match[0], match[2][0][0]))

    base_receipts = [
        Receipt(
            source=f"file:{digest[:12]}",
            version=f"sha256:{digest}",
            timestamp=_now(),
            locator=str(source),
            detail=f"{len(lines)} lines",
        )
    ]

    if matches:
        _, signature, hits = matches[0]
        line_numbers = [number for number, _ in hits]
        receipts = [
            Receipt(
                source=f"signature:{signature['id']}",
                version=RULESET_VERSION,
                timestamp=_now(),
                locator=f"{source.name}:lines=" + ",".join(map(str, line_numbers)),
                detail=signature["problem"],
            )
        ]
        receipts.extend(
            Receipt(
                source=f"file:{digest[:12]}",
                version=f"sha256:{digest}",
                timestamp=_now(),
                locator=f"{source.name}:line={line_number}",
                detail=line.strip(),
            )
            for line_number, line in hits
        )
        finding = Finding(
            id=signature["id"],
            title=signature["problem"],
            severity=SEVERITY_FAIL,
            what=signature["problem"],
            meaning=signature["meaning"],
            action=signature["action"],
            receipts=receipts,
            details={"line_numbers": line_numbers, "evidence": [line for _, line in hits]},
        )
        problem = signature["problem"]
        headline = f"This run stopped because {problem[0].lower() + problem[1:]}"
        return Verdict(
            subject=source.name,
            kind=kind,
            decision=DECISION_FIX_AND_RERUN,
            headline=headline,
            findings=[finding],
            receipts=base_receipts,
            details={
                "input_sha256": digest,
                "n_lines": len(lines),
                "last_lines": lines[-20:],
            },
        )

    return Verdict(
        subject=source.name,
        kind=kind,
        decision=DECISION_UNKNOWN,
        headline="I could not match this log to any known failure pattern.",
        findings=[],
        receipts=base_receipts,
        unknown=[
            f"None of the {len(signatures)} known {runner} failure patterns matched; "
            "the cause is unknown."
        ],
        details={
            "input_sha256": digest,
            "n_lines": len(lines),
            "last_lines": lines[-20:],
        },
    )
