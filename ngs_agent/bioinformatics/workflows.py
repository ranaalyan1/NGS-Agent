"""Workflow catalogue and natural-language intent classification.

Replaces the old binary "rna keyword → rnaseq else variant" guess with a
scored, auditable match over a catalogue of supported workflows, and falls
back to *file-based evidence* (which references/samplesheets were discovered)
when the user's words are ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    label: str
    description: str
    # Lower-case keywords/phrases used for intent scoring. Earlier entries in
    # a tuple are not weighted higher; scoring is by count of distinct hits.
    keywords: tuple[str, ...]
    # Required tool binaries, verified during planning.
    required_tools: tuple[str, ...] = field(default=())
    # File globs that constitute evidence for this workflow when found in the
    # experiment directory (e.g. a GTF is RNA-Seq evidence, a VCF is not).
    evidence_globs: tuple[str, ...] = field(default=())


WORKFLOWS: dict[str, WorkflowSpec] = {
    spec.key: spec
    for spec in (
        WorkflowSpec(
            key="rnaseq",
            label="RNA-Seq",
            description="Differential expression: QC, trimming, HISAT2 alignment, featureCounts,"
                "MultiQC.",
            keywords=(
                "rnaseq",
                "rna-seq",
                "rna seq",
                "transcriptom",
                "transcript",
                "expression",
                "differential expression",
                "deseq2",
                "counts",
                "mrna",
            ),
            required_tools=(
                "fastqc",
                "trimmomatic",
                "hisat2",
                "samtools",
                "featureCounts",
                "multiqc",
            ),
            evidence_globs=("**/*.gtf", "**/*.gff", "**/*.gff3"),
        ),
        WorkflowSpec(
            key="variant",
            label="Variant calling",
            description="Small variant calling: QC, BWA-MEM2 alignment, GATK variant calling,"
                "snpEff annotation.",
            keywords=(
                "variant",
                "variants",
                "snv",
                "snp",
                "indel",
                "wgs",
                "wes",
                "whole genome",
                "whole exome",
                "exome",
                "genotyp",
                "gatk",
                "vcf",
                "mutation",
            ),
            required_tools=("fastqc", "trimmomatic", "bwa-mem2", "samtools", "gatk4", "snpeff"),
            evidence_globs=("**/*.vcf", "**/*.vcf.gz"),
        ),
        WorkflowSpec(
            key="qc",
            label="Quality control only",
            description="Standalone QC: FastQC per sample plus a MultiQC aggregate report.",
            keywords=("qc", "quality control", "quality check", "fastqc", "multiqc"),
            required_tools=("fastqc", "multiqc"),
            evidence_globs=(),
        ),
    )
}

DEFAULT_WORKFLOW = "rnaseq"


class WorkflowInferenceError(ValueError):
    """Raised when intent matching is ambiguous and needs user input."""


@dataclass(frozen=True)
class WorkflowInference:
    workflow: WorkflowSpec
    matched_keywords: tuple[str, ...] = ()
    basis: str = "default"


def get_workflow(key: str) -> WorkflowSpec:
    key = key.strip().lower()
    if key in WORKFLOWS:
        return WORKFLOWS[key]
    # Common aliases.
    aliases = {
        "rna": "rnaseq",
        "variant-calling": "variant",
        "variant_calling": "variant",
        "wgs": "variant",
        "wes": "variant",
        "quality-control": "qc",
    }
    if key in aliases:
        return WORKFLOWS[aliases[key]]
    raise WorkflowInferenceError(
        f"Unknown workflow '{key}'. Supported workflows: {', '.join(sorted(WORKFLOWS))}"
    )


def infer_workflow(
    intent: str,
    evidence_files: list[str] | None = None,
    allow_default: bool = True,
) -> WorkflowInference:
    """Classify a natural-language intent into a workflow.

    Scoring: each distinct keyword hit counts 1; when an explicit workflow
    name (e.g. "rnaseq") appears, it dominates. If scores tie and file
    evidence exists, the evidence decides; if still tied and ``allow_default``
    is False, raises ``WorkflowInferenceError`` instead of guessing.
    """
    text = (intent or "").lower()
    scores: dict[str, tuple[int, tuple[str, ...]]] = {}
    for spec in WORKFLOWS.values():
        matched = tuple(keyword for keyword in spec.keywords if keyword in text)
        scores[spec.key] = (len(matched), matched)

    best_score = max((score for score, _ in scores.values()), default=0)
    candidates = [key for key, (score, _) in scores.items() if score == best_score and score > 0]

    if len(candidates) == 1:
        key = candidates[0]
        return WorkflowInference(WORKFLOWS[key], scores[key][1], basis="intent")

    # Tie or zero hits: use file evidence.
    if evidence_files:
        lowered = [str(name).lower() for name in evidence_files]
        for key, spec in WORKFLOWS.items():
            if key in candidates or not candidates:
                for glob in spec.evidence_globs:
                    suffix = glob.split("*")[-1]  # e.g. ".gtf"
                    if any(name.endswith(suffix) for name in lowered):
                        return WorkflowInference(spec, (), basis="file-evidence")

    if candidates and len(candidates) > 1 and not allow_default:
        raise WorkflowInferenceError(
            f"Intent matched multiple workflows ({', '.join(sorted(candidates))}). "
            "Pass --workflow to disambiguate."
        )
    if not allow_default and best_score == 0 and not evidence_files:
        raise WorkflowInferenceError(
            f"Could not infer a workflow from intent {intent!r}. "
            f"Pass --workflow (one of {', '.join(sorted(WORKFLOWS))})."
        )
    return WorkflowInference(WORKFLOWS[DEFAULT_WORKFLOW], (), basis="default")
