"""Answer writer: Verdict -> plain language.

TEMPLATE-FIRST, and in v1 that means templates ONLY: no LLM is called anywhere
in this file or in anything it imports. The structure is fixed so a reader knows
where to look:

    WHAT THIS IS / WHAT MATTERS / WHAT TO DO / RECEIPTS

Language rules enforced here:
  * the primary output is written for a PI, not for a bioinformatician;
  * jargon (Phred, BGZF, contig names, tool names) lives in "details", which
    the reader has to open, never in the primary sentences;
  * every finding answers three questions: what is it, what does it mean, and
    the ONE action to take next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    DECISION_LABELS,
    KIND_CROMWELL_LOG,
    KIND_FASTQC_ZIP,
    KIND_FOLDER,
    KIND_LABELS,
    KIND_MULTIQC,
    KIND_NEXTFLOW_LOG,
    KIND_SNAKEMAKE_LOG,
    KIND_VCF,
    SEVERITY_FAIL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    Verdict,
)

SECTION_WHAT = "What this is"
SECTION_MATTERS = "What matters"
SECTION_TODO = "What to do"
SECTION_RECEIPTS = "Receipts"

#: Words that must not appear in the primary, non-details output.
JARGON = (
    "phred",
    "bgzf",
    "cram",
    "samtools",
    "featurecounts",
    "multiqc",
    "gencode",
    ".gtf",
    "contig",
    "ensembl",
    "ucsc",
    "freemix",
    "readspergene",
    "markduplicates",
)

#: Sections a non-specialist reads first; these must stay jargon-free. Receipts
#: are technical on purpose and are not checked.
PRIMARY_SECTIONS = (SECTION_WHAT, SECTION_MATTERS, SECTION_TODO)


def primary_text(answer: Answer) -> str:
    """The text a PI reads before opening any details."""
    return "\n".join(
        line for block in answer.blocks if block.heading in PRIMARY_SECTIONS for line in block.lines
    )


SEVERITY_LABELS = {
    SEVERITY_FAIL: "Problem",
    SEVERITY_WARN: "Warning",
    SEVERITY_INFO: "Note",
}

SEVERITY_ORDER = {SEVERITY_FAIL: 0, SEVERITY_WARN: 1, SEVERITY_INFO: 2}


@dataclass
class AnswerBlock:
    """One of the four fixed sections."""

    heading: str
    lines: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        out = [self.heading.upper()]
        out += [f"  {line}" for line in self.lines]
        if self.details:
            out.append("  details:")
            out += [f"    {line}" for line in self.details]
        return "\n".join(out)


@dataclass
class Answer:
    """A rendered verdict. Four blocks, always in the same order."""

    blocks: list[AnswerBlock] = field(default_factory=list)

    def section(self, heading: str) -> AnswerBlock:
        for block in self.blocks:
            if block.heading == heading:
                return block
        raise KeyError(heading)

    def to_text(self) -> str:
        return "\n\n".join(block.to_text() for block in self.blocks)


def _n(value: int | float | None) -> str:
    if value is None:
        return "an unknown number of"
    return f"{int(value):,}"


def _describe_fastqc(verdict: Verdict) -> str:
    facts = verdict.details.get("facts") or {}
    reads = facts.get("total_sequences")
    length = facts.get("read_length") or (facts.get("read_length_range") or [None])[0]
    version = facts.get("fastqc_version") or "unknown version"
    what = (
        f"This is a FastQC quality-control report (made by FastQC {version}). "
        f"It summarises {_n(reads)} reads"
    )
    if length:
        what += f" of {int(length)} bases each"
    what += ". FastQC checks raw sequencing reads before they are used for anything else."
    return what


def _describe_folder(verdict: Verdict) -> str:
    return (
        "This is a folder of results from one sequencing run. NGS-Agent checks "
        "the files inside it against each other."
    )


def _describe_log(verdict: Verdict) -> str:
    if verdict.kind == KIND_SNAKEMAKE_LOG:
        return "This is a log file from a Snakemake pipeline run."
    if verdict.kind == KIND_CROMWELL_LOG:
        sniff = verdict.details.get("sniff") or {}
        notes = " ".join(sniff.get("notes", []))
        if "WDL source" in notes:
            return "This is a WDL workflow definition file."
        return "This is a log file from a Cromwell (WDL) pipeline run."
    return "This is a log file from a Nextflow pipeline run."


def _describe_multiqc(verdict: Verdict) -> str:
    n = verdict.details.get("n_samples")
    if not n:
        samples = (verdict.details.get("facts") or {}).get("samples") or []
        n = len(samples)
    plural = "sample" if n == 1 else "samples"
    return (
        f"This is a combined quality-control summary covering {n} {plural}, one row "
        "per sample. Each sample is judged on its own numbers, and the cohort is "
        "checked for samples that stand apart from the rest."
    )


def _describe_unknown(verdict: Verdict) -> str:
    label = KIND_LABELS.get(verdict.kind, "")
    if label:
        article = "an" if label[:1].lower() in "aeiou" else "a"
        return f"This is {article} {label}."
    return "I could not work out what this file is from its contents."


def answer_verdict(verdict: Verdict) -> Answer:
    """Render a Verdict as the four fixed blocks. Pure function, no side effects."""
    blocks = [
        _what_this_is(verdict),
        _what_matters(verdict),
        _what_to_do(verdict),
        _receipts(verdict),
    ]
    return Answer(blocks=blocks)


def _what_this_is(verdict: Verdict) -> AnswerBlock:
    if verdict.kind == KIND_FASTQC_ZIP:
        text = _describe_fastqc(verdict)
    elif verdict.kind == KIND_MULTIQC:
        text = _describe_multiqc(verdict)
    elif verdict.kind == KIND_FOLDER:
        text = _describe_folder(verdict)
    elif verdict.kind in (KIND_NEXTFLOW_LOG, KIND_SNAKEMAKE_LOG, KIND_CROMWELL_LOG):
        text = _describe_log(verdict)
    else:
        text = _describe_unknown(verdict)

    lines = [text]
    decision_label = DECISION_LABELS.get(verdict.decision, verdict.decision.title())
    lines.append(f"Bottom line: {decision_label}. {verdict.headline}".strip())

    block = AnswerBlock(heading=SECTION_WHAT, lines=lines)
    if verdict.kind == KIND_FASTQC_ZIP:
        facts = verdict.details.get("facts") or {}
        if facts:
            block.details.append(
                f"Source: {facts.get('source_path', 'unknown')} "
                f"(FastQC {facts.get('fastqc_version', '?')})"
            )
    return block


def _what_matters(verdict: Verdict) -> AnswerBlock:
    findings = sorted(verdict.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.id))
    block = AnswerBlock(heading=SECTION_MATTERS)
    if not findings:
        if verdict.unknown:
            block.lines = list(verdict.unknown)
            if verdict.details.get("last_lines"):
                block.details.append("Last lines of the input:")
                block.details += [f"  {line}" for line in verdict.details["last_lines"]]
        else:
            block.lines = ["Nothing stood out in this file. All checks passed."]
        if verdict.kind == KIND_VCF:
            metrics = verdict.details.get("metrics") or {}
            titv = metrics.get("titv")
            observed = "not computed" if titv is None else f"{titv:.3g}"
            context = metrics.get(
                "titv_context",
                "Ti/Tv expectations vary between whole-genome and exome data; no assay type is inferred.",
            )
            block.lines.append(f"Observed Ti/Tv is {observed}. {context}")
        return block

    for finding in findings:
        label = SEVERITY_LABELS.get(finding.severity, finding.severity.title())
        block.lines.append(f"{label} — {finding.title}.")
        block.lines.append(f"    What it is: {finding.what}")
        block.lines.append(f"    What it means: {finding.meaning}")
        block.details.append(f"{finding.id} ({finding.severity}): {_details_line(finding.details)}")
    for note in verdict.unknown:
        block.lines.append(f"Not judged: {note}")
    if verdict.details.get("last_lines"):
        block.details.append("Last lines of the input:")
        block.details += [f"  {line}" for line in verdict.details["last_lines"]]
    return block


def _details_line(details: dict[str, Any]) -> str:
    parts = []
    for key, value in details.items():
        if isinstance(value, float):
            value = round(value, 4)
        parts.append(f"{key}={value}")
    return ", ".join(parts) or "no further detail"


def _what_to_do(verdict: Verdict) -> AnswerBlock:
    block = AnswerBlock(heading=SECTION_TODO)
    findings = sorted(verdict.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.id))
    actions: list[str] = []
    for finding in findings:
        if finding.action and finding.action not in actions:
            actions.append(finding.action)
    if not actions:
        if verdict.unknown:
            block.lines = [
                "Open this file with the tool that produced it, or send it to "
                "your bioinformatician: NGS-Agent has no evidence to judge it on."
            ]
        else:
            block.lines = ["Nothing to fix. Carry on with the next step."]
        return block
    block.lines = [f"{i}. {action}" for i, action in enumerate(actions, start=1)]
    return block


def _receipts(verdict: Verdict) -> AnswerBlock:
    block = AnswerBlock(heading=SECTION_RECEIPTS)
    for finding in verdict.findings:
        for receipt in finding.receipts:
            block.lines.append(f"{finding.id}: {receipt.short()}")
    for receipt in verdict.receipts:
        block.lines.append(receipt.short())
    block.lines.append(f"Tool: ngs-agent {verdict.tool_version}, ruleset {verdict.ruleset_version}")
    return block


def answer_text(verdict: Verdict) -> str:
    """Convenience: the four blocks as one plain-text document."""
    return answer_verdict(verdict).to_text()


def has_jargon(text: str) -> list[str]:
    """Which banned jargon words appear in this text? Used by the tests."""
    lowered = text.lower()
    return [word for word in JARGON if word in lowered]
