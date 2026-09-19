"""Builders for hand-written structured evidence.

These produce exactly what an adapter would produce, so a test can exercise the
derivation and engine layers without a network, a cache, or a recording. Every
builder respects the :class:`~ngs_agent.core.evidence.models.EvidenceRecord`
invariants by construction — a test that wants to *break* an invariant has to
construct the record itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    EvidenceSource,
    EvidenceStatus,
    RetrievalDetail,
    VerificationStatus,
)

#: Frozen clock. Every golden test uses this so recorded timestamps, audit ids
#: and report hashes are byte-stable.
FIXED_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    """Callable clock for :class:`~ngs_agent.core.pipeline.ReviewPipeline`."""
    return FIXED_NOW


TEST_SOURCE = EvidenceSource(
    name="test-fixture",
    version="1.0.0",
    adapter_version="1.0.0",
    hosted_by="local",
    license="test-only",
)


TEST_SOURCE = EvidenceSource(
    name="test-fixture",
    version="1.0.0",
    adapter_version="1.0.0",
    hosted_by="local",
    license="test-only",
)


def make_evidence(
    *,
    variant,
    data_type: EvidenceDataType,
    observed_value: dict[str, Any] | None = None,
    status: EvidenceStatus = EvidenceStatus.PRESENT,
    applicability: ApplicabilityStatus = ApplicabilityStatus.APPLIES,
    verification: VerificationStatus = VerificationStatus.VERIFIED,
    gene: str | None = "BRCA1",
    accession: str | None = None,
    source: EvidenceSource = TEST_SOURCE,
    limitations: tuple[str, ...] = (),
    transcript: str | None = "NM_007294.4",
    retrieved_at: datetime = FIXED_NOW,
    provenance: dict[str, Any] | None = None,
) -> EvidenceRecord:
    """Build an evidence record with the invariants already satisfied.

    ``observed_value`` defaults to a minimal non-empty payload because
    ``status=present`` requires one. Callers that want a *missing* record pass
    ``status=EvidenceStatus.UNAVAILABLE`` and leave ``observed_value`` alone.
    """
    value = observed_value
    if status is EvidenceStatus.PRESENT and not value:
        value = {"observation": "fixture"}
    return EvidenceRecord(
        source=source,
        data_type=data_type,
        status=status,
        retrieved_at=retrieved_at,
        genome_build=variant.genome_build,
        queried_variant_identity=variant.identity,
        observed_variant_identity=variant.identity if verification is VerificationStatus.VERIFIED else None,
        transcript=transcript,
        accession=accession,
        gene=gene,
        observed_value=value or {},
        applicability=applicability,
        verification=verification,
        limitations=limitations,
        retrieval=RetrievalDetail(transport="none"),
        provenance=provenance or {},
    )


def consequence_evidence(variant, *terms: str, gene: str = "BRCA1") -> EvidenceRecord:
    return make_evidence(
        variant=variant,
        data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
        observed_value={"consequences": list(terms)},
        gene=gene,
    )


def mechanism_evidence(
    variant, *, gene: str = "BRCA1", mechanism: str = "loss_of_function", applicable: bool = True
) -> EvidenceRecord:
    return make_evidence(
        variant=variant,
        data_type=EvidenceDataType.GENE_DISEASE_MECHANISM,
        observed_value={
            "gene_symbol": gene,
            "mechanism": mechanism,
            "pvs1_mechanism_applicable": applicable,
            "assertion_basis": "test fixture",
        },
        gene=gene,
    )


def nmd_evidence(variant, *, gene: str = "BRCA1", escapes: bool = False) -> EvidenceRecord:
    return make_evidence(
        variant=variant,
        data_type=EvidenceDataType.NMD_ESCAPE_PREDICTION,
        observed_value={
            "gene_symbol": gene,
            "predicted_nmd_escape": escapes,
            "basis": "test fixture exon context",
        },
        gene=gene,
    )


def frequency_evidence(
    variant,
    *,
    numerator: int,
    denominator: int,
    source_name: str = "gnomAD",
    gene: str = "BRCA1",
) -> EvidenceRecord:
    """Allele frequency as an exact integer ratio — never a float."""
    from ngs_agent.core.quantities import ExactRatio

    ratio = ExactRatio(numerator=numerator, denominator=denominator)
    return make_evidence(
        variant=variant,
        data_type=EvidenceDataType.ALLELE_FREQUENCY,
        observed_value={
            "frequency_source": source_name,
            "allele_frequency": ratio.as_decimal_string(),
            "allele_frequency_ratio": {
                "numerator": ratio.numerator,
                "denominator": ratio.denominator,
            },
            "allele_count": numerator,
            "allele_number": denominator,
            "ancestry_specific": False,
        },
        gene=gene,
    )


def clinvar_evidence(
    variant,
    *,
    label: str,
    review_status: str = "reviewed by expert panel",
    stars: int = 3,
    authoritative: bool = True,
    accession: str = "VCV000000000",
    description: str | None = None,
    gene: str = "BRCA1",
    source: EvidenceSource = TEST_SOURCE,
) -> EvidenceRecord:
    return make_evidence(
        variant=variant,
        data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
        observed_value={
            "description_raw": description or label.replace("_", " "),
            "classification_label": label,
            "review_status": review_status,
            "review_status_stars": stars,
            "authoritative_review": authoritative,
            "last_evaluated": "2016-04-22",
            "accession": accession,
            "accession_version": "1",
            "variation_id": "0",
            "title": f"{gene} fixture",
            "traits": ["Hereditary breast ovarian cancer syndrome"],
            "matching_record_count": 1,
        },
        accession=accession,
        gene=gene,
        source=source,
    )


def missing_evidence(variant, data_type: EvidenceDataType, *, source_name: str = "test-fixture"):
    return make_evidence(
        variant=variant,
        data_type=data_type,
        status=EvidenceStatus.UNAVAILABLE,
        applicability=ApplicabilityStatus.INDETERMINATE,
        verification=VerificationStatus.NOT_VERIFIED,
        source=EvidenceSource(
            name=source_name,
            version="1.0.0",
            adapter_version="1.0.0",
            hosted_by="local",
        ),
    )



def usable(records) -> tuple[EvidenceRecord, ...]:
    """Filter to records the derivation layer is allowed to consume."""
    return tuple(record for record in records if record.usable_as_evidence)


def codes(evaluations) -> list[str]:
    """Criterion codes from a list of evaluations, in evaluation order."""
    return [item.code for item in evaluations]


def evaluation_for(outcome, code: str):
    """Fetch one :class:`CriterionEvaluation` out of an outcome by code."""
    for bucket in (
        outcome.applied,
        outcome.rejected,
        outcome.indeterminate,
        outcome.not_evaluated,
    ):
        for item in bucket:
            if item.code == code:
                return item
    raise AssertionError(f"criterion {code} was not evaluated at all")
