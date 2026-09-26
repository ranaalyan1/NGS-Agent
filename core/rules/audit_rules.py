"""Folder audit rules: RunModel -> findings.

These ten rules exist to catch the failures that do not announce themselves:
every step exits 0, every file is present, and the numbers are quietly wrong.
Each rule is a pure function ``RunModel -> Finding | None``. When the evidence
it needs is missing it returns None, because "we cannot tell" is an answer and
"I think probably" is not.

Every finding carries the rule receipt plus the file:line receipts of the
numbers it used.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from typing import Any

from ..models import (
    DECISION_FIX_AND_RERUN,
    DECISION_HEALTHY,
    DECISION_REVIEW,
    SEVERITY_FAIL,
    SEVERITY_WARN,
    BamInfo,
    Finding,
    Receipt,
    RunModel,
)
from ..util import sha256_file
from ..version import RULESET_VERSION

AUD_STRAND = "AUD-STRAND-01"
AUD_STRAND2 = "AUD-STRAND-02"
AUD_CONTAM = "AUD-CONTAM-02"
AUD_DUP = "AUD-DUP-04"
AUD_TRUNC = "AUD-TRUNC-01"
AUD_BUILD = "AUD-BUILD-01"
AUD_COUNT = "AUD-COUNT-01"
AUD_PAIRED = "AUD-PAIRED-01"
AUD_ADAPT = "AUD-ADAPT-03"
AUD_ALIGN = "AUD-ALIGN-01"

# Thresholds, all in one place so they can be reviewed together.
ASSIGNMENT_FAIL = 0.30  # below this, reads are not landing on genes
FREEMIX_WARN = 0.03  # 3% contamination
FREEMIX_FAIL = 0.05
DUP_OUTLIER_DELTA = 0.20  # this far above the cohort median
DUP_OUTLIER_FLOOR = 0.30  # ...and at least this duplicated
DUP_FAIL = 0.50
ALIGN_WARN = 75.0  # % of reads uniquely aligned
ALIGN_FAIL = 50.0
STRAND_RATIO_MIN = 0.10  # forward vs reverse must differ by at least this
ADAPTER_RISE_WARN = 10.0  # % adapter at its peak
ADAPTER_RISE_FAIL = 20.0

RULE_IDS = [
    AUD_STRAND,
    AUD_STRAND2,
    AUD_CONTAM,
    AUD_DUP,
    AUD_TRUNC,
    AUD_BUILD,
    AUD_COUNT,
    AUD_PAIRED,
    AUD_ADAPT,
    AUD_ALIGN,
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rule_receipt(rule_id: str, locator: str) -> Receipt:
    return Receipt(
        source=f"rule:{rule_id}",
        version=RULESET_VERSION,
        timestamp=_now(),
        locator=locator,
        detail=f"{rule_id} evaluated against {locator}",
    )


def _evidence(run: RunModel, *metrics: str, limit: int = 3) -> list[Receipt]:
    """The file:line receipts behind the metrics a rule used."""
    out: list[Receipt] = []
    for name in metrics:
        for receipt in run.evidence.get(name, [])[:limit]:
            if receipt not in out:
                out.append(receipt)
    return out


def _bam_receipt(bam: BamInfo, detail: str) -> Receipt:
    """Receipt for a BAM. If it cannot be hashed, cite the path instead: an
    empty hash would be a receipt that proves nothing."""
    digest = ""
    try:
        digest = sha256_file(bam.path)[:12]
    except OSError:
        digest = ""
    source = f"file:{digest}" if digest else f"path:{bam.relpath or bam.path}"
    return Receipt(
        source=source,
        version="BAM",
        timestamp=_now(),
        locator=f"{bam.relpath or bam.path}",
        detail=detail,
    )


def _finding(
    rule_id: str,
    severity: str,
    title: str,
    what: str,
    meaning: str,
    action: str,
    locator: str,
    run: RunModel,
    metrics: tuple[str, ...],
    details: dict[str, Any] | None = None,
    extra_receipts: list[Receipt] | None = None,
) -> Finding:
    return Finding(
        id=rule_id,
        title=title,
        severity=severity,
        what=what,
        meaning=meaning,
        action=action,
        receipts=[
            _rule_receipt(rule_id, locator),
            *_evidence(run, *metrics),
            *(extra_receipts or []),
        ],
        details=details or {},
    )


def _metric(run: RunModel, name: str) -> Any:
    return run.metrics.get(name)


# --------------------------------------------------------------------------
# AUD-STRAND-01 — the silent DE-killer
# --------------------------------------------------------------------------
def _normalise_contig(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("chr"):
        lowered = lowered[3:]
    return "m" if lowered in ("m", "mt", "chrm") else lowered


def _contig_overlap(bam: list[str], annotation: list[str]) -> float:
    if not bam or not annotation:
        return 0.0
    a = {_normalise_contig(c) for c in bam}
    b = {_normalise_contig(c) for c in annotation}
    return len(a & b) / max(len(b), 1)


def rule_strand_contig_mismatch(run: RunModel) -> Finding | None:
    """BAM contigs vs annotation contigs mismatch + assignment rate < 30%.

    The classic: reads aligned to an Ensembl-named reference (1, 2, ... MT) and
    counted with a UCSC-named annotation (chr1, chr2, ... chrM). Nothing errors;
    reads simply fail to land on a feature, so gene counts collapse.
    """
    assignment = _metric(run, "assignment_rate")
    bam_contigs = _metric(run, "bam_contigs") or []
    ann_contigs = _metric(run, "annotation_contigs") or []
    if assignment is None or not bam_contigs or not ann_contigs:
        return None
    if assignment >= ASSIGNMENT_FAIL:
        return None

    styles = _metric(run, "contig_styles") or {}
    bam_style = styles.get("bam")
    ann_style = styles.get("annotation")
    style_mismatch = bool(
        bam_style
        and ann_style
        and bam_style != ann_style
        and "unknown" not in (bam_style, ann_style)
    )
    overlap = _contig_overlap(bam_contigs, ann_contigs)
    disjoint = overlap < 0.5
    if not (style_mismatch or disjoint):
        return None

    if style_mismatch:
        what = (
            f"Only {assignment * 100:.0f}% of reads were assigned to genes. The "
            f"aligned reads name their chromosomes like '{bam_contigs[0]}', but the "
            f"gene list used for counting names them like '{ann_contigs[0]}'."
        )
    else:
        what = (
            f"Only {assignment * 100:.0f}% of reads were assigned to genes, and only "
            f"{overlap * 100:.0f}% of the chromosomes in the gene list "
            f"(for example '{ann_contigs[0]}') appear in the aligned reads."
        )
    return _finding(
        rule_id=AUD_STRAND,
        severity=SEVERITY_FAIL,
        title="Reads and gene annotation are naming chromosomes differently",
        what=what,
        meaning=(
            "The aligner and the gene annotation are working from two different "
            "dictionaries of chromosome names, so most reads have no gene to be "
            "counted against. Every step reported success, which is exactly why "
            "this one gets published by mistake: the counts are real numbers "
            "describing almost nothing. Differential expression from this run "
            "would be meaningless."
        ),
        action=(
            "Re-run quantification using the annotation that matches the genome the "
            "reads were aligned to (same source, same chromosome naming). You do "
            "not need to re-align."
        ),
        locator="bam contigs vs annotation contigs, assignment rate",
        run=run,
        metrics=("assignment_rate", "bam_contigs", "annotation_contigs"),
        details={
            "assignment_rate": round(assignment, 4),
            "contig_style_mismatch": style_mismatch,
            "bam_style": bam_style,
            "annotation_style": ann_style,
            "contig_overlap": round(overlap, 4),
            "needs_rerun": True,
        },
    )


# --------------------------------------------------------------------------
# AUD-STRAND-02 — strandness implausible from the assignment pattern
# --------------------------------------------------------------------------
def rule_strand_direction(run: RunModel) -> Finding | None:
    """Declared strandedness contradicted by the STAR ReadsPerGene columns."""
    totals = _metric(run, "reads_per_gene")
    declared = _metric(run, "strandedness_declared")
    if not totals or not declared or len(totals) < 3:
        return None
    _, forward, reverse = totals
    largest = max(forward, reverse)
    if largest <= 0:
        return None
    asymmetry = abs(forward - reverse) / largest
    observed = "reverse" if reverse > forward else "forward"

    if declared in ("forward", "reverse"):
        if declared != observed and asymmetry > STRAND_RATIO_MIN:
            return _finding(
                rule_id=AUD_STRAND2,
                severity=SEVERITY_FAIL,
                title="The library's strand setting looks wrong",
                what=(
                    f"The run was configured as '{declared}-stranded', but "
                    f"{observed}-strand counts are higher "
                    f"({forward:,} forward vs {reverse:,} reverse)."
                ),
                meaning=(
                    "Stranded RNA-seq relies on knowing which read of a pair came "
                    "from which strand. With the setting backwards, reads that "
                    "overlap a gene on the opposite strand are counted the wrong "
                    "way, so genes on opposite strands swap signal."
                ),
                action=(
                    f"Set strandedness to '{observed}' and re-run the quantification "
                    "step. Alignment does not need to be repeated."
                ),
                locator="ReadsPerGene.out.tab vs declared strandedness",
                run=run,
                metrics=("reads_per_gene", "strandedness_declared"),
                details={
                    "declared": declared,
                    "observed": observed,
                    "forward": forward,
                    "reverse": reverse,
                    "asymmetry": round(asymmetry, 4),
                    "needs_rerun": True,
                },
            )
        if asymmetry <= STRAND_RATIO_MIN:
            return _finding(
                rule_id=AUD_STRAND2,
                severity=SEVERITY_FAIL,
                title="The reads carry no strand information",
                what=(
                    f"Forward and reverse counts are within {asymmetry * 100:.0f}% of "
                    f"each other ({forward:,} vs {reverse:,}), but the run was "
                    f"configured as '{declared}-stranded'."
                ),
                meaning=(
                    "A stranded library should produce a strong imbalance between "
                    "the two strand counts. An even split means the reads do not "
                    "carry strand information: either the kit is unstranded, or the "
                    "strand setting is being ignored."
                ),
                action=(
                    "Confirm the library prep kit with the lab. If it is unstranded, "
                    "re-run quantification with strandedness 'unstranded'."
                ),
                locator="ReadsPerGene.out.tab vs declared strandedness",
                run=run,
                metrics=("reads_per_gene", "strandedness_declared"),
                details={
                    "declared": declared,
                    "forward": forward,
                    "reverse": reverse,
                    "asymmetry": round(asymmetry, 4),
                    "needs_rerun": True,
                },
            )
    elif declared == "unstranded" and asymmetry > 0.5:
        return _finding(
            rule_id=AUD_STRAND2,
            severity=SEVERITY_WARN,
            title="The reads look stranded but the run says unstranded",
            what=(
                f"Forward and reverse counts differ strongly ({forward:,} vs "
                f"{reverse:,}), yet the run is configured as unstranded."
            ),
            meaning=(
                "This library behaves like a stranded one. Counting it as unstranded "
                "merges signal from overlapping genes on opposite strands."
            ),
            action=(
                f"Re-run quantification with strandedness '{observed}' if the kit was "
                "strand-specific."
            ),
            locator="ReadsPerGene.out.tab vs declared strandedness",
            run=run,
            metrics=("reads_per_gene", "strandedness_declared"),
            details={
                "declared": declared,
                "observed": observed,
                "asymmetry": round(asymmetry, 4),
                "needs_rerun": True,
            },
        )
    return None


# --------------------------------------------------------------------------
# AUD-CONTAM-02 — freemix
# --------------------------------------------------------------------------
def rule_contamination(run: RunModel) -> Finding | None:
    freemix = _metric(run, "freemix")
    if freemix is None or freemix <= FREEMIX_WARN:
        return None
    severity = SEVERITY_FAIL if freemix > FREEMIX_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=AUD_CONTAM,
        severity=severity,
        title="This sample looks contaminated with DNA from somewhere else",
        what=(
            f"An estimated {freemix * 100:.1f}% of the reads in this sample come "
            "from somewhere other than the sample itself: another individual, "
            "another sample in the batch, or another organism."
        ),
        meaning=(
            "Contamination shifts everything downstream: variant calls pick up "
            "foreign alleles, and expression levels are diluted by material that "
            "is not yours. Above 3% it is worth chasing; above 5% the sample "
            "should not be used as-is."
        ),
        action=(
            "Screen the reads against likely sources (other samples in the batch, "
            "common contaminants, other species) before using this sample in any "
            "comparison."
        ),
        locator="contamination estimate",
        run=run,
        metrics=("freemix",),
        details={
            "freemix": round(freemix, 4),
            "threshold": FREEMIX_WARN,
            "needs_rerun": freemix > FREEMIX_FAIL,
        },
    )


# --------------------------------------------------------------------------
# AUD-DUP-04 — duplication outlier vs cohort
# --------------------------------------------------------------------------
def rule_duplication_outlier(run: RunModel) -> Finding | None:
    by_sample: dict[str, float] = _metric(run, "duplication_by_sample") or {}
    if len(by_sample) < 2:
        # Without a cohort there is no median, and no honest outlier call.
        return None
    values = sorted(by_sample.values())
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    outliers = {
        sample: value
        for sample, value in by_sample.items()
        if value > median + DUP_OUTLIER_DELTA and value > DUP_OUTLIER_FLOOR
    }
    if not outliers:
        return None
    worst_sample, worst = max(outliers.items(), key=lambda kv: kv[1])
    severity = SEVERITY_FAIL if worst > DUP_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=AUD_DUP,
        severity=severity,
        title=f"{worst_sample} has far more duplicate reads than the rest of the run",
        what=(
            f"{worst_sample} is {worst * 100:.0f}% duplicated, against a median of "
            f"{median * 100:.0f}% across {len(by_sample)} samples in this run."
        ),
        meaning=(
            "One sample standing far above its batch usually means it was "
            "over-amplified from too little input material, not that it is "
            "biologically unusual. Its effective coverage is lower than the raw "
            "numbers suggest, and it will look different from the others for a "
            "boring reason."
        ),
        action=(
            f"Check {worst_sample}'s input quantity and library prep. If it cannot "
            "be re-prepped, sequence it deeper and note the duplication in the "
            "methods."
        ),
        locator="MarkDuplicates metrics across samples",
        run=run,
        metrics=("duplication_by_sample",),
        details={
            "median_duplication": round(median, 4),
            "outliers": {k: round(v, 4) for k, v in outliers.items()},
            "n_samples": len(by_sample),
            "needs_rerun": worst > DUP_FAIL,
        },
    )


# --------------------------------------------------------------------------
# AUD-TRUNC-01 — BAM without its end-of-file marker
# --------------------------------------------------------------------------
def rule_truncated_bam(run: RunModel) -> Finding | None:
    truncated = [b for b in run.bams if not b.eof_ok]
    if not truncated:
        return None
    bam = truncated[0]
    return _finding(
        rule_id=AUD_TRUNC,
        severity=SEVERITY_FAIL,
        title="An alignment file is incomplete",
        what=(
            f"{bam.relpath or bam.path} is missing the marker that says 'this file "
            f"is complete'"
            + (
                f" ({len(truncated)} of {len(run.bams)} alignment files are affected)"
                if len(truncated) > 1
                else ""
            )
            + "."
        ),
        meaning=(
            "The file was still being written when something stopped: a job was "
            "killed, a disk filled, or a copy was interrupted. Everything after the "
            "cut is missing, and tools that read it will either fail or silently "
            "report smaller numbers than the truth."
        ),
        action=(
            "Delete the partial file and re-run the step that produced it. Check "
            "the disk and the job's memory limit first, or it will happen again."
        ),
        locator=bam.relpath or bam.path,
        run=run,
        metrics=(),
        details={
            "truncated_bams": [b.relpath or b.path for b in truncated],
            "n_bams": len(run.bams),
            "needs_rerun": True,
        },
        extra_receipts=[_bam_receipt(b, "BGZF end-of-file marker missing") for b in truncated[:3]],
    )


# --------------------------------------------------------------------------
# AUD-BUILD-01 — mixed genome builds
# --------------------------------------------------------------------------
def rule_mixed_builds(run: RunModel) -> Finding | None:
    evidence = _metric(run, "build_evidence") or []
    builds: dict[str, list[str]] = {}
    for build, locator in evidence:
        builds.setdefault(build, []).append(locator)
    if len(builds) < 2:
        return None
    names = sorted(builds)
    return _finding(
        rule_id=AUD_BUILD,
        severity=SEVERITY_FAIL,
        title="This run mixes two different genome builds",
        what=(
            "Different files in this run point at different reference genomes: "
            + ", ".join(
                f"{name} ({len(locs)} reference(s))" for name, locs in sorted(builds.items())
            )
            + "."
        ),
        meaning=(
            "Coordinates from two genome builds are not comparable: a position on "
            f"{names[0]} means something else on {names[-1]}. Anything that combines "
            "these files is comparing apples to oranges, and the error will not "
            "show up as a crash."
        ),
        action=(
            "Pick one build and re-run every step against it, or lift over the files "
            "that are on the wrong one. Do not mix the outputs."
        ),
        locator="genome build evidence across the run",
        run=run,
        metrics=("build_evidence",),
        details={"builds": {k: v[:5] for k, v in builds.items()}, "needs_rerun": True},
    )


# --------------------------------------------------------------------------
# AUD-COUNT-01 — normalised values in a counts slot
# --------------------------------------------------------------------------
def rule_normalised_counts(run: RunModel) -> Finding | None:
    units = _metric(run, "counts_units") or []
    wrong = [(path, unit) for path, unit in units if unit in ("tpm", "fpkm")]
    if not wrong:
        return None
    path, unit = wrong[0]
    return _finding(
        rule_id=AUD_COUNT,
        severity=SEVERITY_FAIL,
        title=f"A counts file actually holds {unit.upper()} values",
        what=(
            f"{path} sits where raw read counts are expected, but its values add up "
            f"like {unit.upper()} (a normalised measure), not like integer counts."
        ),
        meaning=(
            "Count-based statistics (DESeq2, edgeR) expect raw integers: how many "
            "reads landed on each gene. Feed them normalised values and the maths "
            "behind the statistics no longer holds, so the significance values are "
            "not trustworthy even though the table looks fine."
        ),
        action=(
            "Re-run the analysis from the raw count table (integer counts), or "
            "choose a method that accepts normalised input and say so explicitly."
        ),
        locator=path,
        run=run,
        metrics=("counts_units",),
        details={"files": wrong, "needs_rerun": True},
    )


# --------------------------------------------------------------------------
# AUD-PAIRED-01 — paired-end data counted as single-end
# --------------------------------------------------------------------------
def rule_paired_counted_as_single(run: RunModel) -> Finding | None:
    layout = _metric(run, "layout_declared")
    counting_pairs = _metric(run, "counting_pairs")
    if layout == "paired" and counting_pairs is False:
        return _finding(
            rule_id=AUD_PAIRED,
            severity=SEVERITY_FAIL,
            title="Paired-end reads were counted as single reads",
            what=(
                "The samplesheet declares paired-end input (two FASTQ files per "
                "sample) but the counting step recorded single-end reads."
            ),
            meaning=(
                "A read pair is one fragment, not two observations. Counting mates "
                "independently doubles the apparent library size and breaks the "
                "fragment-level assumptions of every downstream statistic, while "
                "looking like a perfectly normal run."
            ),
            action=(
                "Re-run the counting step with the paired-end setting (and the same "
                "for any QC that assumes a layout)."
            ),
            locator="samplesheet layout vs counting summary",
            run=run,
            metrics=("layout_declared", "counting_pairs"),
            details={
                "layout_declared": layout,
                "counting_pairs": counting_pairs,
                "needs_rerun": True,
            },
        )
    if layout == "single" and counting_pairs is True:
        return _finding(
            rule_id=AUD_PAIRED,
            severity=SEVERITY_WARN,
            title="Single-end input but the counter saw read pairs",
            what=(
                "The run is configured as single-end, yet the counting step recorded read pairs."
            ),
            meaning=(
                "One of the two is wrong. Reads that were counted as pairs while the "
                "rest of the pipeline treats them as single reads will not match the "
                "numbers elsewhere in the run."
            ),
            action="Check the samplesheet and the counting command agree on the layout.",
            locator="samplesheet layout vs counting summary",
            run=run,
            metrics=("layout_declared", "counting_pairs"),
            details={
                "layout_declared": layout,
                "counting_pairs": counting_pairs,
                "needs_rerun": True,
            },
        )
    return None


# --------------------------------------------------------------------------
# AUD-ADAPT-03 — adapter attrition pattern
# --------------------------------------------------------------------------
def rule_adapter_attrition(run: RunModel) -> Finding | None:
    """Adapter content still climbing at the end of the read.

    Reads longer than the insert sequence into the adapter. The give-away is a
    curve that keeps rising instead of plateauing; if the insert size is also
    shorter than the read length, read-through is certain.
    """
    curve = _metric(run, "adapter_curve") or []
    if len(curve) < 4:
        return None
    peak = max(y for _, y in curve)
    last = curve[-1][1]
    if peak < ADAPTER_RISE_WARN or last < peak * 0.8:
        return None
    insert = _metric(run, "insert_size_median")
    read_length = _metric(run, "read_length")
    read_through = bool(insert and read_length and insert < read_length)
    severity = SEVERITY_FAIL if (read_through or peak > ADAPTER_RISE_FAIL) else SEVERITY_WARN

    what = f"Adapter content climbs to {peak:.0f}% by the end of the read instead of levelling off."
    if read_through:
        what += (
            f" The fragments ({insert} bases) are shorter than the reads "
            f"({read_length} bases), so the machine read into the adapter."
        )
    return _finding(
        rule_id=AUD_ADAPT,
        severity=severity,
        title="Adapter is being read through to the end of the reads",
        what=what,
        meaning=(
            "When the DNA fragment is shorter than the read length, the sequencer "
            "keeps going into the adapter. Those bases are not yours: they drag down "
            "alignment and inflate apparent gene counts near the 3' end. Trimming "
            "once, properly, fixes it — trimming half of it does not."
        ),
        action=(
            "Trim adapters before quantification (Cutadapt/fastp), or shorten the "
            "read length / lengthen the insert if this run can be repeated."
        ),
        locator=str(_metric(run, "adapter_curve_source") or "adapter content curve"),
        run=run,
        metrics=("adapter_curve",),
        details={
            "adapter_peak_percent": round(peak, 2),
            "adapter_last_percent": round(last, 2),
            "insert_size_median": insert,
            "read_length": read_length,
            "read_through": read_through,
            "trimming_reported": bool(_metric(run, "trimming_reported")),
            "needs_rerun": read_through,
        },
    )


# --------------------------------------------------------------------------
# AUD-ALIGN-01 — alignment rate
# --------------------------------------------------------------------------
def rule_low_alignment(run: RunModel) -> Finding | None:
    rate = _metric(run, "alignment_rate")
    if rate is None or rate >= ALIGN_WARN:
        return None
    severity = SEVERITY_FAIL if rate < ALIGN_FAIL else SEVERITY_WARN
    return _finding(
        rule_id=AUD_ALIGN,
        severity=severity,
        title=f"Only {rate:.0f}% of reads aligned to the reference",
        what=f"{rate:.1f}% of reads were mapped to the reference genome by the aligner.",
        meaning=(
            "Most reads should find their home in the reference. A low rate means "
            "something is off with the reference itself, the organism, or the "
            "library: contamination, the wrong genome, a lot of rRNA or adapter, or "
            "a reference that does not include what you sequenced."
        ),
        action=(
            "Check what the unaligned reads are (screen a sample against likely "
            "sources and against rRNA) before trusting any counts from this run."
        ),
        locator="alignment summary",
        run=run,
        metrics=("alignment_rate",),
        details={
            "alignment_rate": round(rate, 2),
            "threshold": ALIGN_WARN,
            "needs_rerun": rate < ALIGN_FAIL,
        },
    )


#: Every audit rule, in evaluation order.
RULES: list[Callable[[RunModel], Finding | None]] = [
    rule_strand_contig_mismatch,
    rule_strand_direction,
    rule_contamination,
    rule_duplication_outlier,
    rule_truncated_bam,
    rule_mixed_builds,
    rule_normalised_counts,
    rule_paired_counted_as_single,
    rule_adapter_attrition,
    rule_low_alignment,
]


def evaluate(run: RunModel) -> list[Finding]:
    """Run all ten rules; return the findings (may legitimately be empty)."""
    findings: list[Finding] = []
    for rule in RULES:
        finding = rule(run)
        if finding is not None:
            findings.append(finding)
    return findings


def decide(run: RunModel, findings: list[Finding]) -> tuple[str, str]:
    """Bottom line for a folder: HEALTHY / REVIEW / FIX_AND_RERUN."""
    if not findings:
        return (
            DECISION_HEALTHY,
            "Every check passed on the files in this folder, and the numbers agree "
            "with each other.",
        )
    fails = [f for f in findings if f.severity == SEVERITY_FAIL]
    if fails:
        return (
            DECISION_FIX_AND_RERUN,
            f"{len(fails)} of the {len(findings)} findings need the run to be fixed "
            "and repeated; nothing here is a judgement call.",
        )
    return (
        DECISION_REVIEW,
        f"Nothing failed outright, but {len(findings)} finding(s) need a human eye "
        "before these results are used.",
    )
