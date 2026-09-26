"""QC rules: FastQC facts -> findings.

Six rules, each a pure function ``FastQCFacts -> Finding | None``. No file IO,
no network, no LLM, no guessing: if the number it needs is missing the rule
returns None, which is how "we cannot tell" is expressed.

Every finding carries two receipts:
  1. ``rule:<ID>``        — which rule said so, and under which ruleset version;
  2. ``file:<sha256-12>`` — the exact line of fastqc_data.txt it read.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC

from ..models import (
    DECISION_HEALTHY,
    DECISION_RESEQUENCE,
    DECISION_TRIM_AND_PROCEED,
    SEVERITY_FAIL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    FastQCFacts,
    Finding,
    Receipt,
)
from ..version import RULESET_VERSION

QC_QUAL = "QC-QUAL-01"
QC_ADAPT = "QC-ADAPT-01"
QC_DUP = "QC-DUP-01"
QC_GC = "QC-GC-01"
QC_N = "QC-N-01"
QC_LEN = "QC-LEN-01"

# Thresholds live here, in one place, so they can be argued about in review.
Q_DROP_FAIL = 20.0  # mean base quality below this is bad sequence
Q_DROP_WARN = 25.0  # below this, trimming is advisable
ADAPTER_WARN = 5.0  # % of reads with adapter at any cycle
ADAPTER_FAIL = 10.0
DUP_INFO = 20.0  # % duplicated
DUP_WARN = 50.0
DUP_FAIL = 70.0
GC_SPIKE_SHARE = 0.15  # one GC bin holding this share of the library
GC_SPIKE_RATIO = 5.0  # ... and this many times its neighbours
N_WARN = 5.0  # % N at any cycle
N_FAIL = 20.0
QUAL_COLLAPSE_SHARE = 0.5  # this much of the read below Q20 -> re-sequence


def _file_receipt(facts: FastQCFacts, locator: str, detail: str, snippet: str = "") -> Receipt:
    """Evidence receipt pointing at the row we actually read."""
    version = f"fastqc {facts.fastqc_version}" if facts.fastqc_version else "fastqc"
    return Receipt(
        source=f"file:{facts.source_sha256[:12]}",
        version=version,
        timestamp=_now(),
        locator=f"{facts.data_member or 'fastqc_data.txt'}:{locator}",
        detail=detail,
        snippet=snippet,
    )


def _rule_receipt(rule_id: str, locator: str) -> Receipt:
    """The rule that fired, and the ruleset version it fired under."""
    return Receipt(
        source=f"rule:{rule_id}",
        version=RULESET_VERSION,
        timestamp=_now(),
        locator=locator,
        detail=f"{rule_id} evaluated against {locator}",
    )


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finding(
    rule_id: str,
    module: str,
    severity: str,
    title: str,
    what: str,
    meaning: str,
    action: str,
    facts: FastQCFacts,
    locator: str,
    detail: str,
    details: dict | None = None,
    snippet: str = "",
) -> Finding:
    return Finding(
        id=rule_id,
        title=title,
        severity=severity,
        what=what,
        meaning=meaning,
        action=action,
        receipts=[_rule_receipt(rule_id, module), _file_receipt(facts, locator, detail, snippet)],
        details=details or {},
    )


# --------------------------------------------------------------------------
# QC-QUAL-01 — per-base quality collapse
# --------------------------------------------------------------------------
def rule_quality_drop(facts: FastQCFacts) -> Finding | None:
    """Quality falls below Q20 somewhere in the read -> trim there."""
    curve = facts.per_base_quality
    if not curve:
        return None
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
            "Most of the read is unreliable, not just the tail. Trimming would "
            "throw away the library, so there is nothing left to rescue."
        )
        action = "Re-sequence this library; do not try to trim your way out of it."
    else:
        meaning = (
            f"Base calls after position {int(marker.x)} are noisy. Aligners and "
            "quantifiers distrust them, which shows up as dropped reads and "
            "mismapped bases."
        )
        action = f"Trim every read at position {int(marker.x)} and re-check the QC report."

    return _finding(
        rule_id=QC_QUAL,
        module="Per base sequence quality",
        severity=severity,
        title="Read quality falls off towards the end of the read",
        what=(
            f"Base-calling confidence drops below Q{threshold:.0f} from about "
            f"position {int(marker.x)} of the read (last value: Q{marker.y:.1f})."
        ),
        meaning=meaning,
        action=action,
        facts=facts,
        locator=f"line={marker.line}",
        detail=(
            f"Per base sequence quality mean = {marker.y:.1f} at position "
            f"{marker.label} (threshold Q{threshold:.0f})"
        ),
        snippet=f"{marker.label}\t{marker.y}",
        details={
            "trim_position": int(marker.x),
            "min_quality": marker.y,
            "fraction_below_q20": round(share, 4),
            "needs_resequencing": share >= QUAL_COLLAPSE_SHARE,
            "fixable_by_trimming": share < QUAL_COLLAPSE_SHARE,
        },
    )


# --------------------------------------------------------------------------
# QC-ADAPT-01 — adapter contamination
# --------------------------------------------------------------------------
def rule_adapter_content(facts: FastQCFacts) -> Finding | None:
    """Adapter read-through above 5% -> adapter trimming is mandatory."""
    curve = facts.adapter_content
    if not curve:
        return None
    peak = max(curve, key=lambda p: p.y)
    if peak.y <= ADAPTER_WARN:
        return None
    severity = SEVERITY_FAIL if peak.y > ADAPTER_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=QC_ADAPT,
        module="Adapter Content",
        severity=severity,
        title="Adapter sequence is still in the reads",
        what=(f"Up to {peak.y:.1f}% of reads at position {peak.label} contain sequencing adapter."),
        meaning=(
            "The machine read past the end of the DNA fragment into the adapter. "
            "Those bases are not biology: they stop reads from aligning and they "
            "distort any count that includes them."
        ),
        action="Run adapter trimming (for example Cutadapt or fastp) before anything else.",
        facts=facts,
        locator=f"line={peak.line}",
        detail=f"Adapter content peaks at {peak.y:.2f}% at position {peak.label}",
        snippet=f"{peak.label}\t{peak.y}",
        details={
            "adapter_percent": peak.y,
            "peak_position": peak.label,
            "fixable_by_trimming": True,
            "needs_resequencing": False,
        },
    )


# --------------------------------------------------------------------------
# QC-DUP-01 — duplication (context matters: RNA-seq vs WGS)
# --------------------------------------------------------------------------
def rule_duplication(facts: FastQCFacts) -> Finding | None:
    """High duplication. Severity depends on scale; interpretation on assay."""
    dup = facts.duplication_percent
    if dup is None or dup <= DUP_INFO:
        return None
    severity = SEVERITY_INFO
    if dup > DUP_FAIL:
        severity = SEVERITY_FAIL
    elif dup > DUP_WARN:
        severity = SEVERITY_WARN

    meaning = (
        f"{dup:.0f}% of the library is copies of reads already seen. Whether that "
        "is a problem depends on the assay: in RNA-seq, highly expressed genes "
        "legitimately produce identical reads, so 40-60% can be normal; in WGS or "
        "WES the same number means the library was over-amplified from too little "
        "input DNA, and the duplicates bias variant calls."
    )
    action = (
        "Confirm the assay type. If this is DNA sequencing, re-prep with more "
        "input DNA (or de-duplicate and accept the reduced coverage). If it is "
        "RNA-seq, note it and continue."
    )
    if dup > DUP_FAIL:
        action = (
            "Do not use this library for quantitative work: re-prep with more "
            "input material, or sequence deeper to compensate."
        )

    return _finding(
        rule_id=QC_DUP,
        module="Sequence Duplication Levels",
        severity=severity,
        title=f"{dup:.0f}% of the reads are duplicates",
        what=(
            f"Only {100 - dup:.0f}% of the library is distinct sequence; the rest "
            "is duplicated reads."
        ),
        meaning=meaning,
        action=action,
        facts=facts,
        locator=(
            f"line={facts.duplication_line}"
            if facts.duplication_line
            else "module=Sequence Duplication Levels"
        ),
        detail=f"Deduplicated percentage = {100 - dup:.2f}%, i.e. duplication {dup:.2f}%",
        details={
            "duplication_percent": dup,
            "assay_unknown": True,
            "fixable_by_trimming": False,
            "needs_resequencing": dup > DUP_FAIL,
        },
    )


# --------------------------------------------------------------------------
# QC-GC-01 — GC spike means something else is in the tube
# --------------------------------------------------------------------------
def rule_gc_spike(facts: FastQCFacts) -> Finding | None:
    """A single sharp GC peak = contamination, not a genome."""
    curve = facts.gc_curve
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
        module="Per sequence GC content",
        severity=SEVERITY_FAIL,
        title="A spike in GC content suggests contamination",
        what=(
            f"{share * 100:.0f}% of reads share almost exactly the same GC content "
            f"({peak.label}% GC), while their neighbours are "
            f"{ratio:.0f}x lower."
        ),
        meaning=(
            "A real library gives a smooth GC distribution. A narrow spike means a "
            "large fraction of the reads come from one sequence or one organism — "
            "usually bacterial contamination, an adapter/primer dimer, or rRNA."
        ),
        action=(
            "Screen the reads for contamination (for example FastQ Screen or "
            "Kraken2) before using this library for anything quantitative."
        ),
        facts=facts,
        locator=f"line={peak.line}",
        detail=f"GC bin {peak.label}% holds {share * 100:.1f}% of reads",
        snippet=f"{peak.label}\t{peak.y}",
        details={
            "gc_peak_bin": peak.label,
            "gc_peak_share": round(share, 4),
            "gc_peak_ratio": round(ratio, 2) if ratio != float("inf") else None,
            "fixable_by_trimming": False,
            "needs_resequencing": True,
        },
    )


# --------------------------------------------------------------------------
# QC-N-01 — N bases in specific cycles
# --------------------------------------------------------------------------
def rule_n_content(facts: FastQCFacts) -> Finding | None:
    """Too many uncalled bases (N) at particular cycles."""
    curve = facts.n_content
    if not curve:
        return None
    peak = max(curve, key=lambda p: p.y)
    if peak.y <= N_WARN:
        return None
    severity = SEVERITY_FAIL if peak.y > N_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=QC_N,
        module="Per base N content",
        severity=severity,
        title="The machine could not call some bases",
        what=(f"Up to {peak.y:.1f}% of reads have an 'N' (no base called) at cycle {peak.label}."),
        meaning=(
            "A spike of Ns at particular cycles is an instrument or chemistry "
            "problem (a bad tile, a bubble, a failing cycle), not biology. Reads "
            "carrying Ns align worse or not at all."
        ),
        action=(
            f"Check cycle {peak.label} on the instrument (image/SAV report) and "
            "filter or trim reads with Ns before alignment."
        ),
        facts=facts,
        locator=f"line={peak.line}",
        detail=f"N content peaks at {peak.y:.2f}% at cycle {peak.label}",
        snippet=f"{peak.label}\t{peak.y}",
        details={
            "n_percent": peak.y,
            "cycle": peak.label,
            "fixable_by_trimming": True,
            "needs_resequencing": peak.y > N_FAIL,
        },
    )


# --------------------------------------------------------------------------
# QC-LEN-01 — inconsistent read length
# --------------------------------------------------------------------------
def rule_read_length(facts: FastQCFacts) -> Finding | None:
    """Reads are not all the same length -> something already trimmed them."""
    rng = facts.read_length_range
    if rng is None or rng[0] == rng[1]:
        return None
    lo, hi = rng
    return _finding(
        rule_id=QC_LEN,
        module="Sequence Length Distribution",
        severity=SEVERITY_WARN,
        title="Reads are not all the same length",
        what=f"Read lengths in this file range from {lo} to {hi} bases.",
        meaning=(
            "Raw Illumina output is fixed-length. Uneven lengths mean something "
            "has already trimmed this data (or the run was stopped early), so "
            "downstream tools that assume a fixed length may mis-handle it."
        ),
        action=(
            "Find out which step trimmed the reads; if it was unintentional, "
            "re-run from the untrimmed FASTQ and trim once, consistently."
        ),
        facts=facts,
        locator="module=Sequence Length Distribution",
        detail=f"Sequence length range {lo}-{hi}",
        details={
            "read_length_range": [lo, hi],
            "fixable_by_trimming": True,
            "needs_resequencing": False,
        },
    )


#: Every QC rule, in the order they are evaluated and reported.
RULES: list[Callable[[FastQCFacts], Finding | None]] = [
    rule_quality_drop,
    rule_adapter_content,
    rule_duplication,
    rule_gc_spike,
    rule_n_content,
    rule_read_length,
]

RULE_IDS = [QC_QUAL, QC_ADAPT, QC_DUP, QC_GC, QC_N, QC_LEN]


def evaluate(facts: FastQCFacts) -> list[Finding]:
    """Run all six rules; return the findings they produced (may be empty)."""
    findings: list[Finding] = []
    for rule in RULES:
        finding = rule(facts)
        if finding is not None:
            findings.append(finding)
    return findings


def decide(facts: FastQCFacts, findings: list[Finding]) -> tuple[str, str]:
    """The bottom line: RESEQUENCE / TRIM_AND_PROCEED / HEALTHY + one sentence.

    Rules say whether a problem is fixable by trimming (``fixable_by_trimming``)
    or whether the library itself has to go (``needs_resequencing``). This
    function only reads those flags — it has no thresholds of its own.
    """
    if not findings:
        return (
            DECISION_HEALTHY,
            "Every quality check passed on the data we can see, so this library "
            "is ready for the next step.",
        )

    must_resequence = [f for f in findings if f.details.get("needs_resequencing")]
    fixable = [f for f in findings if f.details.get("fixable_by_trimming")]

    if must_resequence:
        names = ", ".join(f.title.lower() for f in must_resequence)
        return (
            DECISION_RESEQUENCE,
            f"Trimming will not fix this: {names}. Sequence this library again "
            "before spending analysis time on the data.",
        )
    if fixable:
        names = ", ".join(f.title.lower() for f in fixable)
        return (
            DECISION_TRIM_AND_PROCEED,
            f"These are fixable problems ({names}). Clean the reads up as "
            "described above and the data is usable.",
        )
    return (
        DECISION_TRIM_AND_PROCEED,
        "Nothing here is fatal, but note the warnings above before you use this "
        "data for quantitative work.",
    )
