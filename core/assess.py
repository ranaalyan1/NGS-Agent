"""The one door-facing entry point into core.

``assess_path(path)`` sniffs, routes to the right analyser, and returns a
Verdict. Doors call this and nothing else. There is no logic in the doors
because there is nothing left for them to do.

Each ``assess_*`` function is deliberately small: parse -> rules -> decide.
Every claim in the returned Verdict carries its receipts.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from .diagnose import diagnose
from .models import (
    KIND_CROMWELL_LOG,
    KIND_FASTQC_ZIP,
    KIND_FOLDER,
    KIND_MULTIQC,
    KIND_NEXTFLOW_LOG,
    KIND_SNAKEMAKE_LOG,
    KIND_UNKNOWN,
    KIND_VCF,
    Receipt,
    Verdict,
    unknown_verdict,
)
from .parse.fastqc import FastQCParseError, parse_fastqc
from .parse.folder import folder_digest, parse_folder
from .parse.multiqc import MultiQCParseError, parse_multiqc
from .parse.runner_log import diagnose_runner
from .parse.vcf import VCFParseError, parse_vcf
from .rules.audit_rules import decide as decide_folder
from .rules.audit_rules import evaluate as evaluate_folder
from .rules.multiqc_rules import decide as decide_multiqc
from .rules.multiqc_rules import evaluate as evaluate_multiqc
from .rules.qc_rules import decide, evaluate
from .rules.vcf_rules import (
    decide_vcf,
    evaluate_vcf,
    metric_receipts,
    summarize_vcf,
    unjudged_vcf_metrics,
)
from .sniff import (
    ACTION_AUDIT_FOLDER,
    ACTION_DIAGNOSE_LOG,
    ACTION_PARSE_FASTQC,
    ACTION_PARSE_MULTIQC,
    sniff,
)
from .util import sha256_file
from .version import RULESET_VERSION, TOOL_VERSION


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _input_receipt(path: Path) -> Receipt:
    """Every verdict is tied to the exact bytes it was made from.

    For a folder there are no bytes to hash, so the receipt says so instead of
    pretending: the audit hashes each file it inspects individually.
    """
    if path.is_dir():
        return Receipt(
            source=f"dir:{path.name or path}",
            version=f"listed by ngs-agent {TOOL_VERSION}",
            timestamp=_now(),
            locator=str(path),
            detail="Run folder; individual files carry their own hashes",
        )
    digest = sha256_file(path)
    return Receipt(
        source=f"file:{digest[:12]}",
        version=f"sha256:{digest}",
        timestamp=_now(),
        locator=str(path),
        detail="Input file, identified by content",
    )


def _tool_receipt(locator: str) -> Receipt:
    return Receipt(
        source="tool:ngs-agent",
        version=TOOL_VERSION,
        timestamp=_now(),
        locator=locator,
        detail=f"ruleset {RULESET_VERSION}",
    )


def assess_fastqc(path: str | Path) -> Verdict:
    """A FastQC report in, a Verdict out."""
    p = Path(path)
    facts = parse_fastqc(p)
    findings = evaluate(facts)
    decision, reason = decide(facts, findings)

    unknown: list[str] = []
    if facts.duplication_percent is None:
        unknown.append("FastQC did not report duplication levels, so they were not judged.")
    if not facts.per_base_quality:
        unknown.append("No per-base quality table was found, so quality was not judged.")

    return Verdict(
        subject=p.name,
        kind=KIND_FASTQC_ZIP,
        decision=decision,
        headline=reason,
        findings=findings,
        receipts=[_input_receipt(p), _tool_receipt("core/assess.py:assess_fastqc")],
        unknown=unknown,
        details={
            "facts": facts.to_dict(),
            "input_sha256": facts.source_sha256,
            "fastqc_version": facts.fastqc_version,
            "modules": [m.to_dict() for m in facts.modules],
        },
    )


def assess_multiqc(path: str | Path) -> Verdict:
    """A combined quality summary in, a Verdict out."""
    p = Path(path)
    facts = parse_multiqc(p)
    findings = evaluate_multiqc(facts)
    decision, reason = decide_multiqc(facts, findings)

    unknown: list[str] = []
    if all(s.duplication_percent is None for s in facts.samples):
        unknown.append("No duplication numbers were reported, so duplication was not judged.")
    if all(not s.per_base_quality and s.mean_quality is None for s in facts.samples):
        unknown.append(
            "No quality numbers were reported, so quality was not judged. "
            "The data file saved next to the report usually carries them."
        )
    if all(not s.adapter_content and s.adapter_percent is None for s in facts.samples):
        unknown.append("No adapter numbers were reported, so adapter content was not judged.")
    if all(s.gc_percent is None and not s.gc_curve for s in facts.samples):
        unknown.append("No GC numbers were reported, so GC content was not judged.")
    if all(s.read_length is None for s in facts.samples):
        unknown.append("No read lengths were reported, so length consistency was not judged.")
    if any(s.fails_percent is not None for s in facts.samples):
        unknown.append(
            "Per-module failure rates are shown per sample but have no thresholds "
            "in this ruleset, so they were not judged."
        )

    return Verdict(
        subject=p.name,
        kind=KIND_MULTIQC,
        decision=decision,
        headline=reason,
        findings=findings,
        receipts=[_input_receipt(p), _tool_receipt("core/assess.py:assess_multiqc")],
        unknown=unknown,
        details={
            "facts": facts.to_dict(),
            "input_sha256": facts.source_sha256,
            "multiqc_version": facts.multiqc_version,
            "format": facts.format,
            "n_samples": len(facts.samples),
        },
    )


def assess_folder(path: str | Path) -> Verdict:
    """A run folder in, a Verdict out."""
    p = Path(path)
    run = parse_folder(p)
    if not run.files:
        return unknown_verdict(
            subject=p.name,
            kind=KIND_FOLDER,
            reason="This folder contains no files to audit.",
        )
    findings = evaluate_folder(run)
    decision, reason = decide_folder(run, findings)

    unknown: list[str] = []
    if not run.bams:
        unknown.append(
            "No alignment files were found, so alignment and file-completeness checks were skipped."
        )
    if "annotation_contigs" not in run.metrics:
        unknown.append(
            "No gene list (annotation file) was found, so the chromosome-name "
            "consistency check was skipped."
        )
    if "assignment_rate" not in run.metrics:
        unknown.append("No counts summary was found, so the assignment rate was not judged.")
    if not run.metrics.get("duplication_by_sample"):
        unknown.append("No duplicate-marking metrics were found, so duplication was not judged.")
    if "freemix" not in run.metrics:
        unknown.append("No contamination estimate was found in this folder.")

    return Verdict(
        subject=p.name,
        kind=KIND_FOLDER,
        decision=decision,
        headline=reason,
        findings=findings,
        receipts=[_input_receipt(p), _tool_receipt("core/assess.py:assess_folder")],
        unknown=unknown,
        details={
            "run": run.to_dict(),
            "input_sha256": folder_digest(p),
            "n_files": len(run.files),
            "n_bams": len(run.bams),
            "n_logs": len(run.logs),
        },
    )


def assess_path(path: str | Path) -> Verdict:
    """Sniff, route, and return a Verdict. Never raises for unknown input."""
    p = Path(path)
    result = sniff(p)

    if result.kind == KIND_FASTQC_ZIP or result.suggested_action == ACTION_PARSE_FASTQC:
        try:
            return assess_fastqc(p)
        except FastQCParseError as exc:
            return unknown_verdict(
                subject=p.name,
                kind=KIND_FASTQC_ZIP,
                reason=f"This looked like a FastQC report but could not be read: {exc}",
                details={"sniff": result.to_dict()},
            )

    if result.kind == KIND_MULTIQC or result.suggested_action == ACTION_PARSE_MULTIQC:
        try:
            return assess_multiqc(p)
        except MultiQCParseError as exc:
            return unknown_verdict(
                subject=p.name,
                kind=KIND_MULTIQC,
                reason=f"This looked like a combined quality summary but could not be read: {exc}",
                details={"sniff": result.to_dict()},
            )

    if result.kind == KIND_FOLDER or result.suggested_action == ACTION_AUDIT_FOLDER:
        return assess_folder(p)

    if result.kind == KIND_NEXTFLOW_LOG or result.suggested_action == ACTION_DIAGNOSE_LOG:
        return diagnose(p)

    if result.kind == KIND_SNAKEMAKE_LOG:
        return diagnose_runner(p, "snakemake")

    if result.kind == KIND_CROMWELL_LOG:
        if any("WDL source" in note for note in result.notes):
            return unknown_verdict(subject=p.name, kind=KIND_CROMWELL_LOG,
                reason="This is a WDL workflow definition. WDL static analysis is out of scope; see ROADMAP.md.",
                details={"sniff": result.to_dict()})
        return diagnose_runner(p, "cromwell")

    if result.kind == KIND_VCF:
        try:
            facts = parse_vcf(p)
        except VCFParseError:
            return unknown_verdict(
                subject=p.name,
                kind=KIND_VCF,
                reason=(
                    "This is a recognised VCF, but its records could not be interpreted safely. "
                    "See ROADMAP.md."
                ),
                details={"sniff": result.to_dict()},
            )
        if facts.get("unsupported"):
            verdict = unknown_verdict(
                subject=p.name,
                kind=KIND_VCF,
                reason=(
                    f"This VCF is recognised, not judged: {facts['unsupported']}. "
                    "See ROADMAP.md."
                ),
                details={"sniff": result.to_dict(), "input_sha256": sha256_file(p)},
            )
            verdict.details["last_lines"] = facts.get("lines", [])[-20:]
            return verdict

        findings = evaluate_vcf(facts)
        decision, headline = decide_vcf(findings)
        unjudged = unjudged_vcf_metrics(facts)
        if not findings and unjudged:
            decision = DECISION_UNKNOWN
            headline = (
                "This VCF is recognised, not judged: evidence is insufficient for one or more "
                "configured QC metrics. See ROADMAP.md."
            )
        return Verdict(
            subject=p.name,
            kind=KIND_VCF,
            decision=decision,
            headline=headline,
            findings=findings,
            receipts=[_input_receipt(p), _tool_receipt("core/rules/vcf_rules.py"), *metric_receipts(facts)],
            unknown=unjudged + [
                "QC metrics judge call quality only; they do not assess pathogenicity or variant truth."
            ],
            details={
                "input_sha256": facts["source_sha256"],
                "n_sites": len(facts["records"]),
                "n_samples": len(facts["samples"]),
                "qc_scope": "call quality only",
                "metrics": summarize_vcf(facts),
            },
        )

    return unknown_verdict(
        subject=p.name,
        kind=result.kind or KIND_UNKNOWN,
        reason=(
            "I could not recognise this file from its contents, so I have no "
            "evidence to judge it on."
        ),
        details={"sniff": result.to_dict()},
    )
