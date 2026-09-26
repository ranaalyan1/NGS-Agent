"""Log matcher: load signatures, score the log, return the ONE root cause.

How it works, in the order it happens:

1. ``parse_nextflow_log`` gives us numbered lines.
2. The log is split into blocks (paragraphs). Each block is scored against every
   signature: +10 per matching line for each anchor (capped at 3 lines per
   anchor so one noisy block cannot run away with it), +5 per distinct anchor.
3. Each signature keeps its best-scoring block; its matched line numbers are the
   receipts.
4. Signatures are ranked by score, then fatal before warning, then earliest
   line. Warnings that did not kill the run are filtered out.
5. The winner is returned as a single Finding. If nothing scores, we say
   "unknown" and attach the last 20 lines for a human — we do not guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

import yaml

from .models import (
    DECISION_FIX_AND_RERUN,
    DECISION_HEALTHY,
    DECISION_UNKNOWN,
    KIND_NEXTFLOW_LOG,
    SEVERITY_FAIL,
    SEVERITY_WARN,
    Finding,
    LogFacts,
    LogMatch,
    Receipt,
    Verdict,
)
from .parse.nextflow_log import parse_nextflow_log, run_failed, run_succeeded
from .version import RULESET_VERSION, TOOL_VERSION

SIGNATURE_DIR = Path(__file__).with_name("signatures")

#: A cause needs either two different anchors or one anchor on two lines.
#: A single incidental hit (a tool name in a process path, say) is not evidence
#: of anything, and raising a false root cause is worse than saying "unknown".
MIN_SCORE = 20
MAX_LINES_PER_ANCHOR = 3
MAX_EVIDENCE_LINES = 6
#: How far apart two anchor hits can be and still count as the same incident.
CLUSTER_WINDOW = 15
TAIL_LINES = 20

REQUIRED_FIELDS = ("id", "anchors", "severity", "title", "explanation", "fix", "reference")


@dataclass
class Signature:
    """One failure signature, exactly as written in its YAML file."""

    id: str
    anchors: list[str] = field(default_factory=list)
    severity: str = "fatal"
    title: str = ""
    explanation: str = ""
    fix: str = ""
    reference: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "anchors": list(self.anchors),
            "severity": self.severity,
            "title": self.title,
            "explanation": self.explanation,
            "fix": self.fix,
            "reference": self.reference,
            "path": self.path,
        }


class SignatureError(Exception):
    """A signature file does not follow the schema."""


def load_signatures(directory: str | Path = SIGNATURE_DIR) -> list[Signature]:
    """Read every ``*.yaml`` signature file in the directory."""
    root = Path(directory)
    signatures: list[Signature] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise SignatureError(f"{path.name}: {exc}") from exc
        missing = [key for key in REQUIRED_FIELDS if key not in data]
        if missing:
            raise SignatureError(f"{path.name} is missing fields: {', '.join(missing)}")
        if not isinstance(data["anchors"], list) or not data["anchors"]:
            raise SignatureError(f"{path.name}: anchors must be a non-empty list")
        signatures.append(
            Signature(
                id=str(data["id"]),
                anchors=[str(a) for a in data["anchors"]],
                severity=str(data["severity"]),
                title=str(data["title"]),
                explanation=" ".join(str(data["explanation"]).split()),
                fix=" ".join(str(data["fix"]).split()),
                reference=str(data["reference"]),
                path=str(path),
            )
        )
    return signatures


def cluster_lines(line_numbers: list[int], window: int = CLUSTER_WINDOW) -> list[list[int]]:
    """Group line numbers that sit close together into one incident.

    Nextflow spreads a single failure across a few paragraphs ("Caused by",
    "Command error", "Work dir"), so hits are clustered by proximity rather than
    by blank lines: one incident, one cluster, one root cause.
    """
    clusters: list[list[int]] = []
    for number in sorted(line_numbers):
        if clusters and number - clusters[-1][-1] <= window:
            clusters[-1].append(number)
        else:
            clusters.append([number])
    return clusters


def score_signature(signature: Signature, log: LogFacts) -> LogMatch | None:
    """Best-scoring cluster of evidence for one signature, or None."""
    hits_by_anchor: dict[str, list[int]] = {}
    for anchor in signature.anchors:
        needle = anchor.lower()
        lines = [n for n, text in enumerate(log.lines, start=1) if needle in text.lower()]
        if lines:
            hits_by_anchor[anchor] = lines
    if not hits_by_anchor:
        return None

    all_hits = sorted({n for lines in hits_by_anchor.values() for n in lines})
    best: LogMatch | None = None
    for cluster in cluster_lines(all_hits):
        members = set(cluster)
        score = 0
        matched: set[int] = set()
        for lines in hits_by_anchor.values():
            hits = [n for n in lines if n in members]
            if not hits:
                continue
            score += 10 * min(len(hits), MAX_LINES_PER_ANCHOR) + 5
            matched.update(hits)
        if score <= 0:
            continue
        lines_sorted = sorted(matched)
        candidate = LogMatch(
            signature_id=signature.id,
            title=signature.title,
            severity=signature.severity,
            score=score,
            line_numbers=lines_sorted,
            evidence=[log.lines[n - 1] for n in lines_sorted[:MAX_EVIDENCE_LINES]],
            explanation=signature.explanation,
            fix=signature.fix,
            reference=signature.reference,
        )
        if best is None or candidate.score > best.score:
            best = candidate
    return best


def match_signatures(log: LogFacts, signatures: list[Signature]) -> list[LogMatch]:
    """Every signature that scored, best first."""
    matches = [
        m for m in (score_signature(s, log) for s in signatures) if m and m.score >= MIN_SCORE
    ]
    matches.sort(key=lambda m: (-m.score, 0 if m.severity == "fatal" else 1, m.line_numbers[0]))
    return matches


def best_match(
    log: LogFacts, signatures: list[Signature], filter_warnings: bool = True
) -> LogMatch | None:
    """The ONE root cause, or None.

    When the run failed, warning-severity signatures (things Nextflow retries,
    like a container pull) are dropped: they did not kill the run.
    """
    matches = match_signatures(log, signatures)
    if not matches:
        return None
    fatal = [m for m in matches if m.severity == "fatal"]
    if filter_warnings and fatal and run_failed(log):
        return fatal[0]
    if filter_warnings and not run_failed(log):
        # The log shows the run continuing, so a retryable warning is noise.
        fatal_only = fatal or [m for m in matches if m.severity != "warning"]
        return fatal_only[0] if fatal_only else None
    return matches[0]


def diagnose(path: str | Path, signatures: list[Signature] | None = None) -> Verdict:
    """A Nextflow log in, a Verdict out."""
    p = Path(path)
    signatures = signatures if signatures is not None else load_signatures()
    log = parse_nextflow_log(p)
    match = best_match(log, signatures)

    def now() -> str:
        from datetime import datetime

        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    input_receipt = Receipt(
        source=f"file:{log.source_sha256[:12]}",
        version=f"sha256:{log.source_sha256}",
        timestamp=now(),
        locator=str(p),
        detail=f"{log.n_lines} lines"
        + (f", Nextflow {log.nextflow_version}" if log.nextflow_version else ""),
    )
    tool_receipt = Receipt(
        source="tool:ngs-agent",
        version=TOOL_VERSION,
        timestamp=now(),
        locator="core/diagnose.py:diagnose",
        detail=f"ruleset {RULESET_VERSION}, {len(signatures)} signatures",
    )

    if match is None:
        tail = log.tail(TAIL_LINES)
        if run_succeeded(log):
            return Verdict(
                subject=p.name,
                kind=KIND_NEXTFLOW_LOG,
                decision=DECISION_HEALTHY,
                headline=(
                    "This run finished, and none of the known failure patterns appear in the log."
                ),
                findings=[],
                receipts=[input_receipt, tool_receipt],
                unknown=[
                    f"Only the {len(signatures)} patterns in this ruleset were "
                    "checked; a problem outside them would not be detected."
                ],
                details={
                    "last_lines": tail,
                    "n_lines": log.n_lines,
                    "nextflow_version": log.nextflow_version,
                    "input_sha256": log.source_sha256,
                    "signatures_loaded": len(signatures),
                },
            )
        verdict = Verdict(
            subject=p.name,
            kind=KIND_NEXTFLOW_LOG,
            decision=DECISION_UNKNOWN,
            headline="I could not match this log to any known failure pattern.",
            findings=[],
            receipts=[input_receipt, tool_receipt],
            unknown=[
                f"None of the {len(signatures)} known failure patterns matched, so "
                "I have nothing evidence-based to tell you.",
            ],
            details={
                "last_lines": tail,
                "n_lines": log.n_lines,
                "nextflow_version": log.nextflow_version,
                "input_sha256": log.source_sha256,
                "signatures_loaded": len(signatures),
            },
        )
        return verdict

    severity = SEVERITY_FAIL if match.severity == "fatal" else SEVERITY_WARN
    lines_text = ", ".join(str(n) for n in match.line_numbers[:MAX_EVIDENCE_LINES])
    evidence_receipts = [
        Receipt(
            source=f"file:{log.source_sha256[:12]}",
            version=f"nextflow {log.nextflow_version}" if log.nextflow_version else "nextflow",
            timestamp=now(),
            locator=f"{p.name}:line={line_no}",
            detail=(log.lines[line_no - 1].strip()[:200] if 0 < line_no <= log.n_lines else ""),
        )
        for line_no in match.line_numbers[:MAX_EVIDENCE_LINES]
    ]
    signature_receipt = Receipt(
        source=f"signature:{match.signature_id}",
        version=RULESET_VERSION,
        timestamp=now(),
        locator=f"{p.name}:lines={lines_text or 'n/a'}",
        detail=f"{match.title} matched with score {match.score}",
    )

    finding = Finding(
        id=match.signature_id,
        title=match.title,
        severity=severity,
        what=(
            f"{match.title}. This shows up {len(match.line_numbers)} time(s) in the log, "
            f"first at line {match.line_numbers[0]}."
        ),
        meaning=match.explanation,
        action=match.fix,
        receipts=[signature_receipt, *evidence_receipts],
        details={
            "signature_id": match.signature_id,
            "score": match.score,
            "line_numbers": match.line_numbers,
            "evidence": match.evidence,
            "reference": match.reference,
            "nextflow_version": log.nextflow_version,
        },
    )

    notes: list[str] = []
    if not run_failed(log) and run_succeeded(log):
        notes.append(
            "The log also shows the run finishing, so this error may have been "
            "retried and recovered."
        )

    return Verdict(
        subject=p.name,
        kind=KIND_NEXTFLOW_LOG,
        decision=DECISION_FIX_AND_RERUN,
        headline=f"This run stopped because {_lower_first(match.title)}.",
        findings=[finding],
        receipts=[input_receipt, tool_receipt],
        unknown=notes,
        details={
            "input_sha256": log.source_sha256,
            "n_lines": log.n_lines,
            "nextflow_version": log.nextflow_version,
            "match": match.to_dict(),
            "last_lines": log.tail(TAIL_LINES),
        },
    )


def _lower_first(text: str) -> str:
    """Lower-case the first letter unless it starts with an acronym (STAR, OOM)."""
    if not text:
        return text
    if len(text) > 1 and text[:2].isupper():
        return text
    return text[:1].lower() + text[1:]
