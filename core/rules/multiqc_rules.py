"""MultiQC rules: combined quality summaries -> findings.

These are the SAME judgments as the QC rules (same IDs, same thresholds),
applied to aggregated evidence: per-sample numbers from a summary instead of
per-cycle curves from one FastQC zip. Thresholds are imported from
``qc_rules`` so the two can never drift apart; only the cohort-level cut-offs
(reached only when several samples can be compared against each other) live
here.

Every finding carries two receipts:
  1. ``rule:<ID>``        — which rule said so, and under which ruleset version;
  2. ``file:<sha256-12>`` — the summary row (``:line=N``) or sample
     (``:sample=<name>``) the number came from.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from ..models import (
    DECISION_HEALTHY,
    DECISION_RESEQUENCE,
    DECISION_REVIEW,
    DECISION_TRIM_AND_PROCEED,
    SEVERITY_FAIL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    Finding,
    MultiQCFacts,
    MultiQCSample,
    Receipt,
)
from ..version import RULESET_VERSION
from .qc_rules import (
    ADAPTER_FAIL,
    ADAPTER_WARN,
    DUP_FAIL,
    DUP_INFO,
    DUP_WARN,
    GC_SPIKE_RATIO,
    GC_SPIKE_SHARE,
    N_FAIL,
    N_WARN,
    Q_DROP_FAIL,
    Q_DROP_WARN,
    QC_ADAPT,
    QC_DUP,
    QC_GC,
    QC_LEN,
    QC_N,
    QC_QUAL,
    QUAL_COLLAPSE_SHARE,
)

#: A sample this far (GC points) from the cohort median is called out.
GC_COHORT_OUTLIER_GAP = 10.0

MOD_GENERAL_STATS = "General Statistics"
MOD_QUALITY = "Mean Quality Scores"
MOD_ADAPTER = "Adapter Content"
MOD_GC = "Per Sequence GC Content"
MOD_N = "Per Base N Content"


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_name(facts: MultiQCFacts) -> str:
    return Path(facts.source_path).name or "multiqc_summary"


def _file_receipt(facts: MultiQCFacts, sample: MultiQCSample, detail: str) -> Receipt:
    """Evidence receipt pointing at the sample (and row, when rows exist)."""
    version = f"multiqc {facts.multiqc_version}" if facts.multiqc_version else "multiqc"
    if facts.format == "general_stats" and sample.row:
        locator = f"{_source_name(facts)}:line={sample.row}"
    else:
        locator = f"{_source_name(facts)}:sample={sample.name}"
    return Receipt(
        source=f"file:{facts.source_sha256[:12]}",
        version=version,
        timestamp=_now(),
        locator=locator,
        detail=detail,
    )


def _rule_receipt(rule_id: str, locator: str) -> Receipt:
    return Receipt(
        source=f"rule:{rule_id}",
        version=RULESET_VERSION,
        timestamp=_now(),
        locator=locator,
        detail=f"{rule_id} evaluated against {locator}",
    )


def _finding(
    rule_id: str,
    module: str,
    severity: str,
    title: str,
    what: str,
    meaning: str,
    action: str,
    facts: MultiQCFacts,
    samples: list[MultiQCSample],
    detail: str,
    details: dict | None = None,
) -> Finding:
    receipts = [_rule_receipt(rule_id, module)]
    receipts.extend(_file_receipt(facts, sample, detail) for sample in samples)
    return Finding(
        id=rule_id,
        title=title,
        severity=severity,
        what=what,
        meaning=meaning,
        action=action,
        receipts=receipts,
        details=details or {},
    )


# --------------------------------------------------------------------------
# QC-DUP-01 — duplication, per sample
# --------------------------------------------------------------------------
def rule_sample_duplication(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    dup = sample.duplication_percent
    if dup is None or dup <= DUP_INFO:
        return None
    severity = SEVERITY_INFO
    if dup > DUP_FAIL:
        severity = SEVERITY_FAIL
    elif dup > DUP_WARN:
        severity = SEVERITY_WARN

    meaning = (
        f"{dup:.0f}% of sample {sample.name}'s library is copies of reads already "
        "seen. Whether that is a problem depends on the assay: in RNA-seq, highly "
        "expressed genes legitimately produce identical reads, so 40-60% can be "
        "normal; in WGS or WES the same number means the library was over-amplified "
        "from too little input DNA, and the duplicates bias variant calls."
    )
    action = (
        f"Confirm the assay type for {sample.name}. If this is DNA sequencing, "
        "re-prep with more input DNA (or de-duplicate and accept the reduced "
        "coverage). If it is RNA-seq, note it and continue."
    )
    if dup > DUP_FAIL:
        action = (
            f"Do not use {sample.name} for quantitative work: re-prep with more "
            "input material, or sequence deeper to compensate."
        )
    return _finding(
        rule_id=QC_DUP,
        module=MOD_GENERAL_STATS,
        severity=severity,
        title=f"{sample.name}: {dup:.0f}% of the reads are duplicates",
        what=(
            f"Only {100 - dup:.0f}% of sample {sample.name}'s library is distinct "
            "sequence; the rest is duplicated reads."
        ),
        meaning=meaning,
        action=action,
        facts=facts,
        samples=[sample],
        detail=f"duplication {dup:.2f}% for sample {sample.name}",
        details={
            "sample": sample.name,
            "duplication_percent": dup,
            "assay_unknown": True,
            "fixable_by_trimming": False,
            "needs_resequencing": dup > DUP_FAIL,
        },
    )


# --------------------------------------------------------------------------
# QC-QUAL-01 — quality, per sample (curves first, summary mean as fallback)
# --------------------------------------------------------------------------
def rule_sample_quality(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    if sample.per_base_quality:
        return _quality_from_curve(sample, facts)
    return _quality_from_mean(sample, facts)


def _quality_from_curve(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    curve = sample.per_base_quality
    below_fail = [p for p in curve if p.y < Q_DROP_FAIL]
    below_warn = [p for p in curve if p.y < Q_DROP_WARN]
    if not below_fail and not below_warn:
        return None
    fail = bool(below_fail)
    marker = below_fail[0] if below_fail else below_warn[0]
    threshold = Q_DROP_FAIL if fail else Q_DROP_WARN
    share = len(below_fail) / len(curve)
    severity = SEVERITY_FAIL if fail else SEVERITY_WARN
    if share >= QUAL_COLLAPSE_SHARE:
        meaning = (
            f"Most of the read in {sample.name} is unreliable, not just the tail. "
            "Trimming would throw away the library, so there is nothing left to rescue."
        )
        action = f"Re-sequence {sample.name}; do not try to trim your way out of it."
    else:
        meaning = (
            f"Base calls in {sample.name} after position {int(marker.x)} are noisy. "
            "Aligners and quantifiers distrust them, which shows up as dropped reads "
            "and mismapped bases."
        )
        action = (
            f"Trim every read in {sample.name} at position {int(marker.x)} and "
            "re-check the quality summary."
        )
    return _finding(
        rule_id=QC_QUAL,
        module=MOD_QUALITY,
        severity=severity,
        title=f"{sample.name}: read quality falls off towards the end of the read",
        what=(
            f"Base-calling confidence in {sample.name} drops below Q{threshold:.0f} "
            f"from about position {int(marker.x)} of the read."
        ),
        meaning=meaning,
        action=action,
        facts=facts,
        samples=[sample],
        detail=(
            f"mean quality {marker.y:.1f} at position {marker.label} "
            f"for sample {sample.name} (threshold Q{threshold:.0f})"
        ),
        details={
            "sample": sample.name,
            "trim_position": int(marker.x),
            "min_quality": marker.y,
            "fraction_below_q20": round(share, 4),
            "needs_resequencing": share >= QUAL_COLLAPSE_SHARE,
            "fixable_by_trimming": share < QUAL_COLLAPSE_SHARE,
        },
    )


def _quality_from_mean(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    mean = sample.mean_quality
    if mean is None:
        return None
    if mean >= Q_DROP_WARN:
        return None
    fail = mean < Q_DROP_FAIL
    severity = SEVERITY_FAIL if fail else SEVERITY_WARN
    return _finding(
        rule_id=QC_QUAL,
        module=MOD_GENERAL_STATS,
        severity=severity,
        title=f"{sample.name}: average read quality is low (Q{mean:.1f})",
        what=f"Reads in {sample.name} average Q{mean:.1f} across the whole read.",
        meaning=(
            "That average covers every position, so a low value means large parts of "
            "the read are unreliable — not one bad tail that trimming would remove."
            if fail
            else "That average covers every position. The ends of the reads are "
            "probably worse than this number, and they drag alignments down with them."
        ),
        action=(
            f"Re-sequence {sample.name}; the quality is too low for trimming to rescue."
            if fail
            else f"Open the per-position quality plot for {sample.name}, trim the "
            "noisy tail, and re-check the summary."
        ),
        facts=facts,
        samples=[sample],
        detail=f"mean quality Q{mean:.2f} for sample {sample.name}",
        details={
            "sample": sample.name,
            "mean_quality": mean,
            "needs_resequencing": fail,
            "fixable_by_trimming": not fail,
        },
    )


# --------------------------------------------------------------------------
# QC-ADAPT-01 — adapter, per sample (curves first, summary share as fallback)
# --------------------------------------------------------------------------
def rule_sample_adapter(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    if sample.adapter_content:
        return _adapter_from_curve(sample, facts)
    return _adapter_from_summary(sample, facts)


def _adapter_from_curve(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    peak = max(sample.adapter_content, key=lambda p: p.y)
    if peak.y <= ADAPTER_WARN:
        return None
    severity = SEVERITY_FAIL if peak.y > ADAPTER_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=QC_ADAPT,
        module=MOD_ADAPTER,
        severity=severity,
        title=f"{sample.name}: adapter sequence is still in the reads",
        what=(
            f"Up to {peak.y:.1f}% of reads in {sample.name} at position {peak.label} "
            "contain sequencing adapter."
        ),
        meaning=(
            "The machine read past the end of the DNA fragment into the adapter. "
            "Those bases are not biology: they stop reads from aligning and they "
            "distort any count that includes them."
        ),
        action=f"Run adapter trimming on {sample.name} before anything else.",
        facts=facts,
        samples=[sample],
        detail=f"adapter content peaks at {peak.y:.2f}% for sample {sample.name}",
        details={
            "sample": sample.name,
            "adapter_percent": peak.y,
            "peak_position": peak.label,
            "fixable_by_trimming": True,
            "needs_resequencing": False,
        },
    )


def _adapter_from_summary(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    pct = sample.adapter_percent
    if pct is None or pct <= ADAPTER_WARN:
        return None
    severity = SEVERITY_FAIL if pct > ADAPTER_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=QC_ADAPT,
        module=MOD_GENERAL_STATS,
        severity=severity,
        title=f"{sample.name}: adapter sequence is still in the reads",
        what=f"{pct:.1f}% of reads in {sample.name} contain sequencing adapter.",
        meaning=(
            "The machine read past the end of the DNA fragment into the adapter. "
            "Those bases are not biology: they stop reads from aligning and they "
            "distort any count that includes them."
        ),
        action=f"Run adapter trimming on {sample.name} before anything else.",
        facts=facts,
        samples=[sample],
        detail=f"adapter share {pct:.2f}% for sample {sample.name}",
        details={
            "sample": sample.name,
            "adapter_percent": pct,
            "fixable_by_trimming": True,
            "needs_resequencing": False,
        },
    )


# --------------------------------------------------------------------------
# QC-N-01 — uncalled bases, per sample (curves only; summaries do not report N)
# --------------------------------------------------------------------------
def rule_sample_n_content(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    if not sample.n_content:
        return None
    peak = max(sample.n_content, key=lambda p: p.y)
    if peak.y <= N_WARN:
        return None
    severity = SEVERITY_FAIL if peak.y > N_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=QC_N,
        module=MOD_N,
        severity=severity,
        title=f"{sample.name}: the machine could not call some bases",
        what=(
            f"Up to {peak.y:.1f}% of reads in {sample.name} have an 'N' (no base "
            f"called) at cycle {peak.label}."
        ),
        meaning=(
            "A spike of Ns at particular cycles is an instrument or chemistry "
            "problem (a bad tile, a bubble, a failing cycle), not biology. Reads "
            "carrying Ns align worse or not at all."
        ),
        action=(
            f"Check cycle {peak.label} for {sample.name} on the instrument report and "
            "filter or trim reads with Ns before alignment."
        ),
        facts=facts,
        samples=[sample],
        detail=f"N content peaks at {peak.y:.2f}% for sample {sample.name}",
        details={
            "sample": sample.name,
            "n_percent": peak.y,
            "cycle": peak.label,
            "fixable_by_trimming": True,
            "needs_resequencing": peak.y > N_FAIL,
        },
    )


# --------------------------------------------------------------------------
# QC-GC-01 — GC spike, per sample (curves only)
# --------------------------------------------------------------------------
def rule_sample_gc_spike(sample: MultiQCSample, facts: MultiQCFacts) -> Finding | None:
    curve = sample.gc_curve
    if len(curve) < 5:
        return None
    total = sum(max(p.y, 0.0) for p in curve)
    if total <= 0:
        return None
    peak = max(curve, key=lambda p: p.y)
    share = peak.y / total
    neighbours = [p.y for p in curve if p.label != peak.label and abs(p.x - peak.x) <= 3]
    neighbour_mean = sum(neighbours) / len(neighbours) if neighbours else 0.0
    ratio = peak.y / neighbour_mean if neighbour_mean > 0 else float("inf")
    if share < GC_SPIKE_SHARE or ratio < GC_SPIKE_RATIO:
        return None
    return _finding(
        rule_id=QC_GC,
        module=MOD_GC,
        severity=SEVERITY_FAIL,
        title=f"{sample.name}: a spike in GC content suggests contamination",
        what=(
            f"{share * 100:.0f}% of reads in {sample.name} share almost exactly the "
            f"same GC content ({peak.label}% GC), while their neighbours are "
            f"{ratio:.0f}x lower."
        ),
        meaning=(
            "A real library gives a smooth GC distribution. A narrow spike means a "
            "large fraction of the reads come from one sequence or one organism — "
            "usually bacterial contamination, an adapter/primer dimer, or rRNA."
        ),
        action=(
            f"Screen {sample.name} for contamination before using it for anything quantitative."
        ),
        facts=facts,
        samples=[sample],
        detail=f"GC bin {peak.label}% holds {share * 100:.1f}% of reads in {sample.name}",
        details={
            "sample": sample.name,
            "gc_peak_bin": peak.label,
            "gc_peak_share": round(share, 4),
            "gc_peak_ratio": round(ratio, 2) if ratio != float("inf") else None,
            "fixable_by_trimming": False,
            "needs_resequencing": True,
        },
    )


# --------------------------------------------------------------------------
# QC-LEN-01 — cohort: samples are not all the same length
# --------------------------------------------------------------------------
def rule_length_consistency(facts: MultiQCFacts) -> Finding | None:
    with_lengths = [s for s in facts.samples if s.read_length is not None]
    lengths = {s.read_length for s in with_lengths}
    if len(lengths) < 2:
        return None
    lo = min(lengths)
    hi = max(lengths)
    shortest = next(s for s in with_lengths if s.read_length == lo)
    longest = next(s for s in with_lengths if s.read_length == hi)
    return _finding(
        rule_id=QC_LEN,
        module=MOD_GENERAL_STATS,
        severity=SEVERITY_WARN,
        title="Samples are not all the same length",
        what=(
            f"Average read lengths in this summary range from {lo} to {hi} bases "
            f"across {len(with_lengths)} samples."
        ),
        meaning=(
            "Raw output from one run is fixed-length. Uneven lengths mean samples "
            "were trimmed differently (or the run was stopped early), so tools that "
            "assume one length may mis-handle the short ones."
        ),
        action=(
            "Check that every sample went through the same trimming; if not, "
            "re-trim the outliers the same way and rebuild the summary."
        ),
        facts=facts,
        samples=[shortest, longest],
        detail=f"read lengths span {lo}-{hi} bases across samples",
        details={
            "read_length_range": [lo, hi],
            "samples_involved": sorted({shortest.name, longest.name}),
            "fixable_by_trimming": True,
            "needs_resequencing": False,
        },
    )


# --------------------------------------------------------------------------
# QC-GC-01 — cohort: one sample's GC stands apart (contamination or swap)
# --------------------------------------------------------------------------
def rule_gc_cohort_outlier(facts: MultiQCFacts) -> Finding | None:
    with_gc = [s for s in facts.samples if s.gc_percent is not None]
    if len(with_gc) < 2:
        return None
    ordered = sorted(s.gc_percent for s in with_gc)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    outliers = [s for s in with_gc if abs(s.gc_percent - median) > GC_COHORT_OUTLIER_GAP]
    if not outliers:
        return None
    names = sorted(s.name for s in outliers)
    shown = ", ".join(names[:3]) + (" and others" if len(names) > 3 else "")
    return _finding(
        rule_id=QC_GC,
        module=MOD_GENERAL_STATS,
        severity=SEVERITY_WARN,
        title=f"GC content stands apart in {shown}",
        what=(
            f"{shown} averages "
            + ", ".join(
                f"{s.gc_percent:.0f}% GC ({s.name})"
                for s in sorted(outliers, key=lambda s: s.name)[:3]
            )
            + f", while the cohort median is {median:.0f}%."
        ),
        meaning=(
            "Samples from one run usually share the same GC. A sample this far from "
            "the cohort is often contamination, a sample swap, or a different "
            "organism sequenced alongside."
        ),
        action=(
            f"Confirm the identity of {shown} and screen it for contamination "
            "before pooling it with the rest."
        ),
        facts=facts,
        samples=outliers[:3],
        detail=f"cohort GC median {median:.1f}%; outliers: {', '.join(names)}",
        details={
            "outlier_samples": names,
            "cohort_median_gc": round(median, 2),
            "fixable_by_trimming": False,
            "needs_resequencing": False,
        },
    )


#: Per-sample rules, in the order they are evaluated and reported.
SAMPLE_RULES = [
    rule_sample_duplication,
    rule_sample_quality,
    rule_sample_adapter,
    rule_sample_n_content,
    rule_sample_gc_spike,
]

#: Cohort rules: need at least two samples to compare.
COHORT_RULES = [
    rule_length_consistency,
    rule_gc_cohort_outlier,
]


def evaluate(facts: MultiQCFacts) -> list[Finding]:
    """Run every rule; return the findings produced (may be empty)."""
    findings: list[Finding] = []
    for sample in sorted(facts.samples, key=lambda s: s.name):
        for rule in SAMPLE_RULES:
            finding = rule(sample, facts)
            if finding is not None:
                findings.append(finding)
    if len(facts.samples) >= 2:
        for rule in COHORT_RULES:
            finding = rule(facts)
            if finding is not None:
                findings.append(finding)
    return findings


def _affected_samples(findings: list[Finding]) -> list[str]:
    names: set[str] = set()
    for finding in findings:
        if finding.details.get("sample"):
            names.add(str(finding.details["sample"]))
        for key in ("outlier_samples", "samples_involved"):
            values = finding.details.get(key)
            if isinstance(values, list):
                names.update(str(v) for v in values)
    return sorted(names)


def _name_list(names: list[str]) -> str:
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:3])} and {len(names) - 3} more"


def decide(facts: MultiQCFacts, findings: list[Finding]) -> tuple[str, str]:
    """The bottom line for the cohort, plus one sentence.

    A summary covers many samples, so a problem in one of them is REVIEW
    ("look at these samples") rather than a cohort-wide sentence: only a
    problem affecting at least half the cohort speaks for the whole run.
    """
    n = len(facts.samples)
    plural = "sample" if n == 1 else "samples"
    if not findings:
        return (
            DECISION_HEALTHY,
            f"Every quality check passed for all {n} {plural} in this summary, "
            "so the cohort is ready for the next step.",
        )

    must_resequence = [f for f in findings if f.details.get("needs_resequencing")]
    if must_resequence:
        affected = _affected_samples(must_resequence)
        if len(affected) >= max(1, n / 2):
            names = ", ".join(f.title.lower() for f in must_resequence[:2])
            return (
                DECISION_RESEQUENCE,
                f"Trimming will not fix this ({names}). Sequence these libraries "
                "again before spending analysis time on the data.",
            )
        return (
            DECISION_REVIEW,
            f"Most of the cohort is usable, but {_name_list(affected)} "
            f"{'needs' if len(affected) == 1 else 'need'} re-sequencing: "
            "trimming will not fix it.",
        )

    fixable = [f for f in findings if f.details.get("fixable_by_trimming")]
    if fixable:
        affected = _affected_samples(fixable)
        return (
            DECISION_TRIM_AND_PROCEED,
            f"These are fixable problems in {_name_list(affected)}. Clean those "
            "samples up as described above and the data is usable.",
        )
    affected = _affected_samples(findings)
    return (
        DECISION_REVIEW,
        f"Nothing here is fatal, but note the warnings on {_name_list(affected)} "
        "before using this data for quantitative work.",
    )
