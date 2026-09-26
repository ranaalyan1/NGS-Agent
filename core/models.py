"""Data models shared by every station.

Rule of the house: any object here must survive a JSON round-trip without
loss, because the JSON sidecar next to a report is the archival copy of a
verdict. If you add a field, add it to ``to_dict``/``from_dict`` too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from .version import RULESET_VERSION, TOOL_VERSION

# --------------------------------------------------------------------------
# Input kinds (produced by core/sniff.py)
# --------------------------------------------------------------------------
KIND_FASTQC_ZIP = "fastqc_zip"
KIND_VCF = "vcf"
KIND_BAM = "bam"
KIND_NEXTFLOW_LOG = "nextflow_log"
KIND_SNAKEMAKE_LOG = "snakemake_log"
KIND_CROMWELL_LOG = "cromwell_log"
KIND_MULTIQC = "multiqc_report"
KIND_FOLDER = "folder"
KIND_UNKNOWN = "unknown"

#: Human labels for kinds. Doors render these verbatim.
KIND_LABELS = {
    KIND_FASTQC_ZIP: "FastQC report",
    KIND_VCF: "VCF variant file",
    KIND_BAM: "BAM alignment file",
    KIND_NEXTFLOW_LOG: "Nextflow run log",
    KIND_SNAKEMAKE_LOG: "Snakemake run log",
    KIND_CROMWELL_LOG: "Cromwell / WDL run log",
    KIND_MULTIQC: "MultiQC summary report",
    KIND_FOLDER: "Run folder",
    KIND_UNKNOWN: "Unrecognised file",
}

# --------------------------------------------------------------------------
# Confidence / severity / decision vocabularies
# --------------------------------------------------------------------------
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"

SEVERITY_FAIL = "fail"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"

#: Bottom-line decisions.
DECISION_RESEQUENCE = "RESEQUENCE"
#: A pipeline/run problem with a specific fix: fix the cause, then re-run.
DECISION_FIX_AND_RERUN = "FIX_AND_RERUN"
#: Nothing is broken outright, but something needs a human's eye before the
#: results are used.
DECISION_REVIEW = "REVIEW"
DECISION_TRIM_AND_PROCEED = "TRIM_AND_PROCEED"
DECISION_HEALTHY = "HEALTHY"
DECISION_UNKNOWN = "UNKNOWN"

DECISION_LABELS = {
    DECISION_RESEQUENCE: "Re-sequence this library",
    DECISION_FIX_AND_RERUN: "Fix this and re-run",
    DECISION_REVIEW: "Review before using these results",
    DECISION_TRIM_AND_PROCEED: "Trim, then continue",
    DECISION_HEALTHY: "Looks healthy",
    DECISION_UNKNOWN: "Unknown",
}


def _utc_now() -> str:
    """UTC timestamp in ISO-8601 form, seconds precision, always suffixed Z."""
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Receipts — the Law of Receipts
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Receipt:
    """Evidence behind a claim: where it came from and how to find it again.

    ``source``   what produced the claim (``file:<sha8>``, ``rule:QC-QUAL-01``,
                 ``signature:NF-STAR-003``, ``tool:<version>``).
    ``version``  version of that source (tool, ruleset, or parser version).
    ``timestamp`` when the evidence was read/derived (UTC ISO-8601).
    ``locator``  where inside the source: module name, ``file:line``, byte range.
    """

    source: str
    version: str
    timestamp: str
    locator: str
    detail: str = ""
    snippet: str = ""

    def is_valid(self) -> bool:
        """A receipt with a missing field is not evidence."""
        return all(
            isinstance(v, str) and v.strip() != ""
            for v in (self.source, self.version, self.timestamp, self.locator)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "timestamp": self.timestamp,
            "locator": self.locator,
            "detail": self.detail,
            "snippet": self.snippet,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Receipt:
        return cls(
            source=data["source"],
            version=data["version"],
            timestamp=data["timestamp"],
            locator=data["locator"],
            detail=data.get("detail", ""),
            snippet=data.get("snippet", ""),
        )

    def short(self) -> str:
        """One-line, human-readable form used in report footers and CLI cards."""
        base = f"{self.source} @ {self.locator}"
        if self.detail:
            base += f" — {self.detail}"
        return base


def receipt(
    source: str,
    locator: str,
    detail: str = "",
    snippet: str = "",
    version: str = RULESET_VERSION,
    timestamp: str | None = None,
) -> Receipt:
    """Convenience constructor: stamps version + timestamp for you."""
    return Receipt(
        source=source,
        version=version,
        timestamp=timestamp or _utc_now(),
        locator=locator,
        detail=detail,
        snippet=snippet,
    )


# --------------------------------------------------------------------------
# Sniff result (Station 1)
# --------------------------------------------------------------------------
@dataclass
class SniffResult:
    """What a file is, decided by content, never by name."""

    kind: str
    confidence: str
    suggested_action: str
    path: str = ""
    notes: list[str] = field(default_factory=list)

    def label(self) -> str:
        return KIND_LABELS.get(self.kind, "Unrecognised file")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "confidence": self.confidence,
            "suggested_action": self.suggested_action,
            "path": self.path,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SniffResult:
        return cls(
            kind=data["kind"],
            confidence=data["confidence"],
            suggested_action=data["suggested_action"],
            path=data.get("path", ""),
            notes=list(data.get("notes", [])),
        )


# --------------------------------------------------------------------------
# Findings & verdicts
# --------------------------------------------------------------------------
@dataclass
class Finding:
    """One problem (or one reassurance), in plain language, with evidence.

    The three mandatory sentences are the Law of Language:
    ``what``    — what is this?
    ``meaning`` — what does it mean for my experiment?
    ``action``  — the ONE action to take.
    """

    id: str
    title: str
    severity: str
    what: str
    meaning: str
    action: str
    receipts: list[Receipt] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def has_valid_receipts(self) -> bool:
        return bool(self.receipts) and all(r.is_valid() for r in self.receipts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "what": self.what,
            "meaning": self.meaning,
            "action": self.action,
            "receipts": [r.to_dict() for r in self.receipts],
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            id=data["id"],
            title=data["title"],
            severity=data["severity"],
            what=data["what"],
            meaning=data["meaning"],
            action=data["action"],
            receipts=[Receipt.from_dict(r) for r in data.get("receipts", [])],
            details=dict(data.get("details", {})),
        )


@dataclass
class Verdict:
    """The complete answer for one input. Doors render this; they never edit it."""

    subject: str
    kind: str
    decision: str
    headline: str
    what_this_is: str = ""
    what_matters: list[str] = field(default_factory=list)
    what_to_do: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    tool_version: str = TOOL_VERSION
    ruleset_version: str = RULESET_VERSION
    timestamp: str = field(default_factory=_utc_now)

    # -- convenience ------------------------------------------------------
    @property
    def severity_rank(self) -> int:
        """Worst severity present. Used by the CLI exit code."""
        rank = 0
        for f in self.findings:
            weights = {SEVERITY_INFO: 1, SEVERITY_WARN: 2, SEVERITY_FAIL: 3}
            rank = max(rank, weights.get(f.severity, 0))
        return rank

    def all_receipts(self) -> list[Receipt]:
        out = list(self.receipts)
        for f in self.findings:
            out.extend(f.receipts)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "kind": self.kind,
            "decision": self.decision,
            "headline": self.headline,
            "what_this_is": self.what_this_is,
            "what_matters": list(self.what_matters),
            "what_to_do": list(self.what_to_do),
            "findings": [f.to_dict() for f in self.findings],
            "receipts": [r.to_dict() for r in self.receipts],
            "unknown": list(self.unknown),
            "details": dict(self.details),
            "tool_version": self.tool_version,
            "ruleset_version": self.ruleset_version,
            "timestamp": self.timestamp,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Verdict:
        return cls(
            subject=data["subject"],
            kind=data["kind"],
            decision=data["decision"],
            headline=data["headline"],
            what_this_is=data.get("what_this_is", ""),
            what_matters=list(data.get("what_matters", [])),
            what_to_do=list(data.get("what_to_do", [])),
            findings=[Finding.from_dict(f) for f in data.get("findings", [])],
            receipts=[Receipt.from_dict(r) for r in data.get("receipts", [])],
            unknown=list(data.get("unknown", [])),
            details=dict(data.get("details", {})),
            tool_version=data.get("tool_version", TOOL_VERSION),
            ruleset_version=data.get("ruleset_version", RULESET_VERSION),
            timestamp=data.get("timestamp", _utc_now()),
        )

    @classmethod
    def from_json(cls, text: str) -> Verdict:
        return cls.from_dict(json.loads(text))


def unknown_verdict(subject: str, kind: str, reason: str, details: dict | None = None) -> Verdict:
    """The honest answer when we cannot interpret something.

    Never guess a verdict. Say what we could not do and hand the human the
    evidence we do have.
    """
    return Verdict(
        subject=subject,
        kind=kind,
        decision=DECISION_UNKNOWN,
        headline="I can't interpret this yet.",
        what_this_is=KIND_LABELS.get(kind, "Unrecognised input"),
        what_matters=[reason],
        what_to_do=["Open it with the tool that made it, or send it to your bioinformatician."],
        findings=[],
        receipts=[],
        unknown=[reason],
        details=details or {},
    )


# --------------------------------------------------------------------------
# Parsed facts
# --------------------------------------------------------------------------
@dataclass
class Point:
    """A single point on a curve (base position, GC bin, cycle…).

    ``line`` is the 1-based line number inside the source file where this point
    was read, so a rule can point its receipt at the exact evidence.
    """

    x: float
    y: float
    label: str = ""
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "label": self.label, "line": self.line}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Point:
        return cls(x=d["x"], y=d["y"], label=d.get("label", ""), line=int(d.get("line", 0)))


@dataclass
class ModuleStatus:
    """One FastQC module and its own verdict (pass/warn/fail)."""

    name: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModuleStatus:
        return cls(name=d["name"], status=d["status"])


@dataclass
class FastQCFacts:
    """Everything we extracted from a FastQC zip. Pure facts, no judgement."""

    source_path: str = ""
    source_sha256: str = ""
    fastqc_version: str = ""
    #: Name of the member inside the zip the facts came from (a locator root).
    data_member: str = ""
    modules: list[ModuleStatus] = field(default_factory=list)
    per_base_quality: list[Point] = field(default_factory=list)
    adapter_content: list[Point] = field(default_factory=list)
    gc_curve: list[Point] = field(default_factory=list)
    n_content: list[Point] = field(default_factory=list)
    duplication_percent: float | None = None
    #: Line in fastqc_data.txt where the deduplication percentage was read.
    duplication_line: int = 0
    total_sequences: int | None = None
    read_length: int | None = None
    read_length_range: tuple[int, int] | None = None
    gc_percent: float | None = None

    def module_status(self, name: str) -> str:
        for m in self.modules:
            if m.name == name:
                return m.status
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "fastqc_version": self.fastqc_version,
            "data_member": self.data_member,
            "modules": [m.to_dict() for m in self.modules],
            "per_base_quality": [p.to_dict() for p in self.per_base_quality],
            "adapter_content": [p.to_dict() for p in self.adapter_content],
            "gc_curve": [p.to_dict() for p in self.gc_curve],
            "n_content": [p.to_dict() for p in self.n_content],
            "duplication_percent": self.duplication_percent,
            "duplication_line": self.duplication_line,
            "total_sequences": self.total_sequences,
            "read_length": self.read_length,
            "read_length_range": list(self.read_length_range) if self.read_length_range else None,
            "gc_percent": self.gc_percent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FastQCFacts:
        rng = d.get("read_length_range")
        return cls(
            source_path=d.get("source_path", ""),
            source_sha256=d.get("source_sha256", ""),
            fastqc_version=d.get("fastqc_version", ""),
            data_member=d.get("data_member", ""),
            modules=[ModuleStatus.from_dict(m) for m in d.get("modules", [])],
            per_base_quality=[Point.from_dict(p) for p in d.get("per_base_quality", [])],
            adapter_content=[Point.from_dict(p) for p in d.get("adapter_content", [])],
            gc_curve=[Point.from_dict(p) for p in d.get("gc_curve", [])],
            n_content=[Point.from_dict(p) for p in d.get("n_content", [])],
            duplication_percent=d.get("duplication_percent"),
            duplication_line=int(d.get("duplication_line", 0)),
            total_sequences=d.get("total_sequences"),
            read_length=d.get("read_length"),
            read_length_range=tuple(rng) if rng else None,
            gc_percent=d.get("gc_percent"),
        )


@dataclass
class MultiQCSample:
    """One sample row of a combined quality summary. Pure facts, no judgement.

    ``row`` is the 1-based row number in a general-stats table (so a rule can
    cite ``multiqc_general_stats.txt:line=5``); it is 0 when the source has no
    rows (JSON, HTML), in which case receipts cite ``sample=<name>`` instead.
    Curves come from the JSON plot data when the report carries it and are
    empty otherwise — rules that need them stay silent rather than guess.
    """

    name: str = ""
    total_sequences: int | None = None
    read_length: int | None = None
    gc_percent: float | None = None
    duplication_percent: float | None = None
    fails_percent: float | None = None
    mean_quality: float | None = None
    adapter_percent: float | None = None
    row: int = 0
    per_base_quality: list[Point] = field(default_factory=list)
    adapter_content: list[Point] = field(default_factory=list)
    gc_curve: list[Point] = field(default_factory=list)
    n_content: list[Point] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_sequences": self.total_sequences,
            "read_length": self.read_length,
            "gc_percent": self.gc_percent,
            "duplication_percent": self.duplication_percent,
            "fails_percent": self.fails_percent,
            "mean_quality": self.mean_quality,
            "adapter_percent": self.adapter_percent,
            "row": self.row,
            "per_base_quality": [p.to_dict() for p in self.per_base_quality],
            "adapter_content": [p.to_dict() for p in self.adapter_content],
            "gc_curve": [p.to_dict() for p in self.gc_curve],
            "n_content": [p.to_dict() for p in self.n_content],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MultiQCSample:
        return cls(
            name=d.get("name", ""),
            total_sequences=d.get("total_sequences"),
            read_length=d.get("read_length"),
            gc_percent=d.get("gc_percent"),
            duplication_percent=d.get("duplication_percent"),
            fails_percent=d.get("fails_percent"),
            mean_quality=d.get("mean_quality"),
            adapter_percent=d.get("adapter_percent"),
            row=int(d.get("row", 0)),
            per_base_quality=[Point.from_dict(p) for p in d.get("per_base_quality", [])],
            adapter_content=[Point.from_dict(p) for p in d.get("adapter_content", [])],
            gc_curve=[Point.from_dict(p) for p in d.get("gc_curve", [])],
            n_content=[Point.from_dict(p) for p in d.get("n_content", [])],
        )


@dataclass
class MultiQCFacts:
    """Everything extracted from a combined quality summary. Pure facts."""

    source_path: str = ""
    source_sha256: str = ""
    multiqc_version: str = ""
    #: Which shape the source had: "json" | "general_stats" | "html".
    format: str = ""
    samples: list[MultiQCSample] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "multiqc_version": self.multiqc_version,
            "format": self.format,
            "samples": [s.to_dict() for s in self.samples],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MultiQCFacts:
        return cls(
            source_path=d.get("source_path", ""),
            source_sha256=d.get("source_sha256", ""),
            multiqc_version=d.get("multiqc_version", ""),
            format=d.get("format", ""),
            samples=[MultiQCSample.from_dict(s) for s in d.get("samples", [])],
        )


@dataclass
class FileEntry:
    """One file found while walking a run folder."""

    path: str
    relpath: str
    kind: str
    confidence: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "relpath": self.relpath,
            "kind": self.kind,
            "confidence": self.confidence,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileEntry:
        return cls(
            path=d["path"],
            relpath=d["relpath"],
            kind=d["kind"],
            confidence=d["confidence"],
            size=d["size"],
        )


@dataclass
class BamInfo:
    """Facts read from a BAM/CRAM-ish file (headers at minimum)."""

    path: str = ""
    relpath: str = ""
    contigs: list[str] = field(default_factory=list)
    build: str = ""
    eof_ok: bool = False
    truncated: bool = True
    header_lines: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "relpath": self.relpath,
            "contigs": list(self.contigs),
            "build": self.build,
            "eof_ok": self.eof_ok,
            "truncated": self.truncated,
            "header_lines": self.header_lines,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BamInfo:
        return cls(
            path=d.get("path", ""),
            relpath=d.get("relpath", ""),
            contigs=list(d.get("contigs", [])),
            build=d.get("build", ""),
            eof_ok=bool(d.get("eof_ok", False)),
            truncated=bool(d.get("truncated", True)),
            header_lines=int(d.get("header_lines", 0)),
            notes=list(d.get("notes", [])),
        )


@dataclass
class CountsInfo:
    """Facts about a counts table (featureCounts / HTSeq / salmon / RSEM…)."""

    path: str = ""
    relpath: str = ""
    columns: list[str] = field(default_factory=list)
    n_rows: int = 0
    max_value: float | None = None
    has_decimals: bool = False
    inferred_units: str = ""  # "counts" | "tpm" | "fpkm" | "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "relpath": self.relpath,
            "columns": list(self.columns),
            "n_rows": self.n_rows,
            "max_value": self.max_value,
            "has_decimals": self.has_decimals,
            "inferred_units": self.inferred_units,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CountsInfo:
        return cls(
            path=d.get("path", ""),
            relpath=d.get("relpath", ""),
            columns=list(d.get("columns", [])),
            n_rows=int(d.get("n_rows", 0)),
            max_value=d.get("max_value"),
            has_decimals=bool(d.get("has_decimals", False)),
            inferred_units=d.get("inferred_units", ""),
        )


@dataclass
class RunModel:
    """A run folder, reduced to facts the audit rules can judge."""

    root: str = ""
    files: list[FileEntry] = field(default_factory=list)
    bams: list[BamInfo] = field(default_factory=list)
    counts: list[CountsInfo] = field(default_factory=list)
    qc_files: list[FileEntry] = field(default_factory=list)
    logs: list[FileEntry] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Where each metric came from: metric name -> receipts with file:line
    #: locators. Every audit rule cites these; none of them invent a number.
    evidence: dict[str, list[Receipt]] = field(default_factory=dict)

    def files_of_kind(self, kind: str) -> list[FileEntry]:
        return [f for f in self.files if f.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files": [f.to_dict() for f in self.files],
            "bams": [b.to_dict() for b in self.bams],
            "counts": [c.to_dict() for c in self.counts],
            "qc_files": [f.to_dict() for f in self.qc_files],
            "logs": [f.to_dict() for f in self.logs],
            "metrics": dict(self.metrics),
            "evidence": {k: [r.to_dict() for r in v] for k, v in self.evidence.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunModel:
        return cls(
            root=d.get("root", ""),
            files=[FileEntry.from_dict(f) for f in d.get("files", [])],
            bams=[BamInfo.from_dict(b) for b in d.get("bams", [])],
            counts=[CountsInfo.from_dict(c) for c in d.get("counts", [])],
            qc_files=[FileEntry.from_dict(f) for f in d.get("qc_files", [])],
            logs=[FileEntry.from_dict(f) for f in d.get("logs", [])],
            metrics=dict(d.get("metrics", {})),
            evidence={
                k: [Receipt.from_dict(r) for r in v] for k, v in d.get("evidence", {}).items()
            },
        )


@dataclass
class LogMatch:
    """A signature matched against a log, with the lines that prove it."""

    signature_id: str
    title: str
    severity: str
    score: int
    line_numbers: list[int] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    explanation: str = ""
    fix: str = ""
    reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature_id": self.signature_id,
            "title": self.title,
            "severity": self.severity,
            "score": self.score,
            "line_numbers": list(self.line_numbers),
            "evidence": list(self.evidence),
            "explanation": self.explanation,
            "fix": self.fix,
            "reference": self.reference,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LogMatch:
        return cls(
            signature_id=d["signature_id"],
            title=d.get("title", ""),
            severity=d.get("severity", SEVERITY_FAIL),
            score=int(d.get("score", 0)),
            line_numbers=list(d.get("line_numbers", [])),
            evidence=list(d.get("evidence", [])),
            explanation=d.get("explanation", ""),
            fix=d.get("fix", ""),
            reference=d.get("reference", ""),
        )


@dataclass
class LogFacts:
    """A log file split into lines, ready for signature matching."""

    source_path: str = ""
    source_sha256: str = ""
    lines: list[str] = field(default_factory=list)
    nextflow_version: str = ""
    error_blocks: list[list[int]] = field(default_factory=list)

    @property
    def n_lines(self) -> int:
        return len(self.lines)

    def tail(self, n: int = 20) -> list[str]:
        return self.lines[-n:]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "lines": list(self.lines),
            "nextflow_version": self.nextflow_version,
            "error_blocks": [list(b) for b in self.error_blocks],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LogFacts:
        return cls(
            source_path=d.get("source_path", ""),
            source_sha256=d.get("source_sha256", ""),
            lines=list(d.get("lines", [])),
            nextflow_version=d.get("nextflow_version", ""),
            error_blocks=[list(b) for b in d.get("error_blocks", [])],
        )
