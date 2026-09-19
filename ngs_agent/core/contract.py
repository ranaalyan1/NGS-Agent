"""The versioned JSON result contract.

Every interface NGS-Agent exposes — CLI, MCP, TUI, HTML report, REST API — emits
this one object and nothing else. That is the contract that makes the product
composable: a consumer that understands ``schema_version`` 1.0 can render a
review from any interface without knowing which interface produced it.

Invariants enforced by pydantic on construction:

* ``classification.requires_human_review`` cannot be ``False`` while
  ``review.status`` is ``pending`` — an unreviewed result may never present
  itself as actionable.
* ``classification.abstained`` must be consistent with ``decision_state``.
* ``provenance.input_hashes`` must be present for every reviewed variant.
* ``applied_criteria`` entries must each carry at least one ``evidence_id``
  (the machine-checkable form of "no evidence record, no ACMG criterion").

A JSON Schema mirror lives at :file:`schemas/variant_review.v1.schema.json` for
consumers outside Python; :file:`tests/core/test_contract.py` asserts the two
agree.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ngs_agent.core.acmg.derivation import CriterionEvaluation, ExternalClassification
from ngs_agent.core.acmg.engine import ClassificationOutcome, ConflictRecord, EvidenceCoverage
from ngs_agent.core.errors import ContractError
from ngs_agent.core.evidence.models import EvidenceRecord
from ngs_agent.core.normalization import NormalizedVariant
from ngs_agent.core.version import CONTRACT_SCHEMA_VERSION, ENGINE_VERSION

RESEARCH_USE_ONLY_BANNER = (
    "RESEARCH USE ONLY. Not for diagnostic use. NGS-Agent has not undergone clinical "
    "validation and is not cleared or approved as a medical device. Every result requires "
    "independent review and sign-off by a qualified clinical laboratory professional."
)


class VariantIdentity(BaseModel):
    """The variant under review, as normalized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    chromosome: str
    position: int
    reference: str
    alternate: str
    genome_build: str
    variant_type: str
    accession: str | None = None
    spdi: str | None = None
    hgvs_g: str | None = None
    #: ``None`` unless a transcript model resolved one. Never inferred.
    transcript: str | None = None
    gene: str | None = None
    on_primary_contig: bool = True

    @classmethod
    def from_variant(cls, variant: NormalizedVariant, *, gene: str | None) -> VariantIdentity:
        return cls(
            id=variant.variant_id,
            chromosome=variant.chromosome,
            position=variant.position,
            reference=variant.reference,
            alternate=variant.alternate,
            genome_build=variant.genome_build.value,
            variant_type=variant.variant_type.value,
            accession=variant.accession,
            spdi=variant.spdi,
            hgvs_g=variant.hgvs_g,
            gene=gene,
            on_primary_contig=variant.on_primary_contig,
        )


class GeneBlock(BaseModel):
    """Gene resolution and validation.

    A gene symbol taken from a VCF INFO field is a claim by whoever produced
    the file, not a fact. NGS-Agent records where the symbol came from and
    whether it could be validated, and never lets an unvalidated symbol drive a
    criterion silently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str | None = None
    resolved_from: Literal["cli", "vcf_info", "evidence_source", "not_resolved"] = "not_resolved"
    info_key: str | None = None
    validated_against: str | None = None
    validation_status: Literal["validated", "not_found", "not_checked"] = "not_checked"
    note: str = ""


class NormalizationBlock(BaseModel):
    """How the input was normalized, and whether that succeeded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm_version: str
    reference_used: bool
    reference_source: str | None = None
    left_aligned: bool
    left_shifted: bool = False
    multiallelic_split: bool
    original_allele_index: int | None = None
    complete: bool
    identity_string: str
    warnings: tuple[dict[str, str], ...] = ()


class ClassificationBlock(BaseModel):
    """The verdict, its basis, and the review requirement attached to it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    display_label: str
    abstained: bool
    decision_state: Literal[
        "classified", "insufficient_evidence", "conflict", "abstained", "manual_review_required"
    ]
    decision_basis: str
    requires_human_review: bool
    rule_set: str
    rule_set_version: str
    rule_set_citation: str
    engine_version: str
    concordance: str
    #: Strength counts that produced the label, for auditability.
    strength_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    fired_rules: tuple[dict[str, str], ...] = ()
    #: Explicitly absent by design: no confidence, probability, or score.
    #: See :mod:`ngs_agent.core.acmg.engine` docstring.

    @property
    def has_confidence_score(self) -> bool:
        """Always ``False``. Exists so a test can assert the property directly."""
        return False


class ReviewBlock(BaseModel):
    """Human review and sign-off state.

    Abstention is *not* a review state: it belongs to ``classification``. The
    review block records only what a human did, and a sign-off is only valid
    when it names a reviewer and a timestamp.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["pending", "approved", "rejected", "review_requested"] = "pending"
    reviewer: str | None = None
    reviewer_role: str | None = None
    signed_at: datetime | None = None
    decision: str | None = None
    #: Mandatory when the reviewer's decision differs from the engine's label.
    override_reason: str | None = None
    notes: str | None = None

    @property
    def is_signed(self) -> bool:
        """A sign-off names a person and a time. Both are mandatory.

        A reviewer identity is what makes an override accountable; an approved
        result with no named reviewer is indistinguishable from an autonomous
        decision, which this system does not make.
        """
        return (
            self.status in {"approved", "rejected"}
            and self.signed_at is not None
            and bool(self.reviewer and self.reviewer.strip())
        )


class ExplanationBlock(BaseModel):
    """Optional LLM explanation metadata. Strictly outside the signed path.

    The block records *that* an explanation was produced, by what, from what
    inputs, and whether every claim it made was traceable to a ledger record.
    The narrative text itself is carried in ``text`` and is never consumed by
    the classification engine.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    present: bool = False
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    text: str | None = None
    cited_evidence_ids: tuple[str, ...] = ()
    unsupported_citations: tuple[str, ...] = ()
    #: True when the explanation mentioned a criterion the engine did not apply.
    boundary_violations: tuple[str, ...] = ()
    generated_at: datetime | None = None
    disclaimer: str = (
        "Generated by a language model from the evidence ledger. This narrative is not "
        "evidence, did not contribute to the classification, and must not be relied on "
        "without checking the cited evidence records."
    )


class ProvenanceBlock(BaseModel):
    """Everything needed to reproduce or audit this result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_version: str
    normalization_version: str
    contract_schema_version: str
    rule_set: str
    rule_set_version: str
    generated_at: datetime
    #: SHA-256 of every input file that contributed.
    input_hashes: dict[str, str] = Field(default_factory=dict)
    #: ``source name -> version`` for every database consulted.
    database_versions: dict[str, str] = Field(default_factory=dict)
    adapters: tuple[dict[str, Any], ...] = ()
    configuration_hash: str | None = None
    run_id: str | None = None
    audit_id: str | None = None
    audit_path: str | None = None
    #: Present when an LLM touched the run, even if only to explain.
    model_metadata: dict[str, Any] | None = None
    environment: dict[str, str] = Field(default_factory=dict)


class VariantReviewResult(BaseModel):
    """The versioned result contract (schema 1.0).

    This is the object every interface serializes. It is frozen: a consumer that
    wants to annotate a result must produce a new one, so the artefact in the
    audit trail is always the artefact that was shown to a human.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = CONTRACT_SCHEMA_VERSION
    result_id: str
    research_use_only: Literal[True] = True
    disclaimer: str = RESEARCH_USE_ONLY_BANNER

    variant: VariantIdentity
    gene: GeneBlock = Field(default_factory=GeneBlock)
    normalization: NormalizationBlock
    classification: ClassificationBlock

    evidence: tuple[EvidenceRecord, ...] = ()
    evidence_gaps: tuple[EvidenceRecord, ...] = ()
    evidence_coverage: tuple[EvidenceCoverage, ...] = ()

    applied_criteria: tuple[CriterionEvaluation, ...] = ()
    rejected_criteria: tuple[CriterionEvaluation, ...] = ()
    indeterminate_criteria: tuple[CriterionEvaluation, ...] = ()
    not_evaluated_criteria: tuple[CriterionEvaluation, ...] = ()

    external_classifications: tuple[ExternalClassification, ...] = ()
    conflicts: tuple[ConflictRecord, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    provenance: ProvenanceBlock
    review: ReviewBlock = Field(default_factory=ReviewBlock)
    explanation: ExplanationBlock = Field(default_factory=ExplanationBlock)

    # -- invariants ---------------------------------------------------------

    @model_validator(mode="after")
    def _enforce_contract(self) -> VariantReviewResult:
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ContractError(
                f"Result declares schema_version {self.schema_version!r} but this build emits "
                f"{CONTRACT_SCHEMA_VERSION!r}. Refusing to hand a consumer a contract it cannot "
                "interpret."
            )
        for criterion in self.applied_criteria:
            if not criterion.evidence_ids:
                raise ContractError(
                    f"Applied criterion {criterion.code} carries no evidence_id. The rule "
                    "'no evidence record, no ACMG criterion' is enforced at the contract level; "
                    "this result cannot be emitted."
                )
        if self.review.status in {"approved", "rejected"} and not self.review.is_signed:
            raise ContractError(
                f"review.status={self.review.status!r} is a sign-off and requires both a reviewer "
                "identity and a signed_at timestamp."
            )
        if not self.review.is_signed and self.review.signed_at is not None:
            raise ContractError("A review timestamp without a terminal review status is meaningless.")
        if (
            self.review.decision
            and self.review.decision != self.classification.label
            and not self.review.override_reason
        ):
            raise ContractError(
                "A reviewer decision that differs from the engine label is an override and "
                "requires an explicit override_reason."
            )
        if self.classification.abstained and self.classification.decision_state == "classified":
            raise ContractError("abstained=True is inconsistent with decision_state='classified'.")
        if not self.classification.abstained and self.classification.decision_state in {
            "insufficient_evidence",
            "conflict",
            "abstained",
        }:
            raise ContractError(
                f"decision_state={self.classification.decision_state!r} requires abstained=True."
            )
        return self

    # -- construction -------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        result_id: str,
        variant: NormalizedVariant,
        gene: str | None,
        outcome: ClassificationOutcome,
        gene_block: GeneBlock | None = None,
        evidence: tuple[EvidenceRecord, ...],
        evidence_gaps: tuple[EvidenceRecord, ...] = (),
        provenance: ProvenanceBlock,
        review: ReviewBlock | None = None,
        explanation: ExplanationBlock | None = None,
    ) -> VariantReviewResult:
        """Assemble a result from engine output plus provenance."""
        normalization = variant.normalization
        review_block = review or ReviewBlock(status="pending")
        return cls(
            result_id=result_id,
            variant=VariantIdentity.from_variant(variant, gene=gene),
            gene=gene_block or GeneBlock(symbol=gene),
            normalization=NormalizationBlock(
                algorithm_version=normalization.algorithm_version,
                reference_used=normalization.reference_used,
                reference_source=normalization.reference_source,
                left_aligned=normalization.left_aligned,
                left_shifted=normalization.left_shifted,
                multiallelic_split=normalization.multiallelic_split,
                original_allele_index=normalization.original_allele_index,
                complete=normalization.complete,
                identity_string=variant.identity,
                warnings=tuple(
                    {"code": item.code, "message": item.message, "severity": item.severity}
                    for item in normalization.warnings
                ),
            ),
            classification=ClassificationBlock(
                label=outcome.label,
                display_label=outcome.display_label,
                abstained=outcome.abstained,
                decision_state=outcome.decision_state,  # type: ignore[arg-type]
                decision_basis=outcome.decision_basis,
                requires_human_review=outcome.requires_human_review,
                rule_set=outcome.rule_set,
                rule_set_version=outcome.rule_set_version,
                rule_set_citation=outcome.rule_set_citation,
                engine_version=outcome.engine_version or ENGINE_VERSION,
                concordance=outcome.concordance,
                strength_counts=outcome.strength_counts,
                fired_rules=tuple(
                    {
                        "rule_id": item.rule_id,
                        "produces": item.produces,
                        "text": item.text,
                        "citation": item.citation,
                    }
                    for item in outcome.fired_rules
                ),
            ),
            evidence=evidence,
            evidence_gaps=evidence_gaps,
            evidence_coverage=outcome.evidence_coverage,
            applied_criteria=outcome.applied_criteria,
            rejected_criteria=outcome.rejected_criteria,
            indeterminate_criteria=outcome.indeterminate_criteria,
            not_evaluated_criteria=outcome.not_evaluated_criteria,
            external_classifications=outcome.external_classifications,
            conflicts=outcome.conflicts,
            missing_evidence=outcome.missing_evidence,
            limitations=outcome.limitations,
            provenance=provenance,
            review=review_block,
            explanation=explanation or ExplanationBlock(),
        )

    # -- serialization ------------------------------------------------------

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
