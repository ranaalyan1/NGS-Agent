"""The typed evidence model.

This module defines what a piece of evidence *is* in NGS-Agent. It is the
enforcement point for the non-negotiable safety rule:

    No evidence record, no ACMG criterion.

The invariants are structural, not conventional — they are validated by
pydantic on construction, so an invalid record cannot exist in memory:

1. ``status != present`` implies an empty ``observed_value``, no
   ``strength_hint``, and ``applicability == indeterminate``. Missing data can
   therefore never be read as negative evidence.
2. ``applicability == applies`` requires ``verification == verified``. An
   unverified record cannot support a criterion.
3. ``evidence_id`` is a content digest of the record itself, so two identical
   retrievals from the same source version collide and two different ones
   cannot.
4. ``source`` is mandatory and carries a version. An evidence record without a
   source version is not reproducible and is rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ngs_agent.core.errors import EvidenceValidationError, NonCanonicalValueError
from ngs_agent.core.genome import GenomeBuild
from ngs_agent.core.hashing import assert_canonical_value, canonical_json, ga4gh_digest
from ngs_agent.core.version import EVIDENCE_SCHEMA_VERSION


class EvidenceStatus(str, Enum):
    """Why an evidence record does or does not carry a value.

    The distinction between ``unavailable`` and ``retrieval_failed`` is a
    safety property, not a cosmetic one:

    * ``unavailable`` — the source answered authoritatively that it has no
      record for this variant. That is a fact about the source's coverage.
    * ``retrieval_failed`` — we could not get an answer. That is a fact about
      *us*. It must never be reported as "the variant is absent from ClinVar".
    """

    PRESENT = "present"
    UNAVAILABLE = "unavailable"
    RETRIEVAL_FAILED = "retrieval_failed"
    INVALID = "invalid"
    NOT_CONFIGURED = "not_configured"


#: Statuses that carry no observed value, by definition.
NON_INFORMATIVE_STATUSES = frozenset(
    {
        EvidenceStatus.UNAVAILABLE,
        EvidenceStatus.RETRIEVAL_FAILED,
        EvidenceStatus.INVALID,
        EvidenceStatus.NOT_CONFIGURED,
    }
)


class ApplicabilityStatus(str, Enum):
    """Whether the evidence bears on *this* variant in *this* context."""

    APPLIES = "applies"
    DOES_NOT_APPLY = "does_not_apply"
    INDETERMINATE = "indeterminate"


class VerificationStatus(str, Enum):
    """How much we trust that the evidence is about the variant we asked for."""

    #: The source's own coordinates/identifiers match the queried variant.
    VERIFIED = "verified"
    #: The source returned a record we could not tie back to the query.
    IDENTITY_UNVERIFIED = "identity_unverified"
    #: The source itself reports internal disagreement (e.g. ClinVar conflicts).
    SOURCE_REPORTED_CONFLICT = "source_reported_conflict"
    #: No verification was possible (no record, or retrieval failed).
    NOT_VERIFIED = "not_verified"


class EvidenceDataType(str, Enum):
    """The kind of observation an evidence record carries.

    The engine's criterion derivations are typed against these; adding a new
    data type is the only way to add a new evidence class, which is what keeps
    "the LLM said so" out of the criterion space.
    """

    CLINICAL_SIGNIFICANCE = "clinical_significance"
    ALLELE_FREQUENCY = "allele_frequency"
    MOLECULAR_CONSEQUENCE = "molecular_consequence"
    GENE_DISEASE_MECHANISM = "gene_disease_mechanism"
    NMD_ESCAPE_PREDICTION = "nmd_escape_prediction"
    SPLICING_PREDICTION = "splicing_prediction"
    MISSENSE_PREDICTION = "missense_prediction"
    FUNCTIONAL_ASSAY = "functional_assay"
    DE_NOVO = "de_novo"
    SEGREGATION = "segregation"
    CASE_CONTROL = "case_control"
    PROTEIN_STRUCTURE = "protein_structure"
    MUTATIONAL_HOTSPOT = "mutational_hotspot"
    SEQUENCE_CONSTRAINT = "sequence_constraint"


class EvidenceSource(BaseModel):
    """Identity and version of an evidence provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    #: Version/release of the *data*, e.g. a ClinVar release date or gnomAD v4.1.
    version: str = Field(min_length=1)
    #: Version of the adapter code that retrieved it.
    adapter_version: str = Field(min_length=1)
    endpoint: str | None = None
    license: str | None = None
    #: Whether the data was produced inside the customer's boundary.
    hosted_by: Literal["vendor", "customer", "local", "recorded_fixture"] = "vendor"

    @property
    def citation(self) -> str:
        return f"{self.name} {self.version} (adapter {self.adapter_version})"


class RetrievalDetail(BaseModel):
    """Exactly how the evidence was obtained, for replay and audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["http", "file", "recorded_fixture", "snapshot", "none"] = "none"
    urls: tuple[str, ...] = ()
    request_hash: str | None = None
    response_sha256: str | None = None
    latency_ms: int | None = None
    cache_hit: bool = False
    #: Raw response bytes are *not* stored inline by default; a path may be.
    response_artifact_path: str | None = None
    error: str | None = None
    http_status: int | None = None


class EvidenceRecord(BaseModel):
    """One structured observation about one normalized variant.

    Immutable. Once created, an evidence record is never mutated — corrections
    are new records with a new ``retrieved_at``. This is what makes the ledger
    append-only and the audit trail meaningful.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = EVIDENCE_SCHEMA_VERSION
    evidence_id: str = ""
    source: EvidenceSource
    data_type: EvidenceDataType
    status: EvidenceStatus
    retrieved_at: datetime

    genome_build: GenomeBuild
    #: Identity string of the variant we asked about.
    queried_variant_identity: str
    #: Identity string the source says this evidence is about, if it states one.
    observed_variant_identity: str | None = None

    transcript: str | None = None
    accession: str | None = None
    gene: str | None = None

    #: The observation itself. Empty unless ``status == present``.
    observed_value: dict[str, Any] = Field(default_factory=dict)

    applicability: ApplicabilityStatus = ApplicabilityStatus.INDETERMINATE
    verification: VerificationStatus = VerificationStatus.NOT_VERIFIED

    #: A *hint* only. The deterministic engine re-derives criteria from
    #: ``observed_value`` and never trusts this field. Adapters may set it to
    #: make a record self-describing; the engine ignores it.
    strength_hint: str | None = None

    limitations: tuple[str, ...] = ()
    retrieval: RetrievalDetail = Field(default_factory=RetrievalDetail)
    provenance: dict[str, Any] = Field(default_factory=dict)

    # -- invariants ---------------------------------------------------------

    @model_validator(mode="after")
    def _enforce_canonical_values(self) -> EvidenceRecord:
        """Observed values must be hashable deterministically.

        This is what makes ``evidence_id`` stable across Python builds and
        makes a recorded fixture byte-comparable with a live retrieval.
        Adapters encode real numbers as strings or as exact integer ratios
        (see :mod:`ngs_agent.core.quantities`), never as floats.
        """
        for field_name in ("observed_value", "provenance"):
            try:
                assert_canonical_value(getattr(self, field_name), path=f"$.{field_name}")
            except NonCanonicalValueError as exc:
                raise EvidenceValidationError(
                    f"{self.source.name}: {field_name} must be canonically hashable so that "
                    "evidence_id is reproducible. Encode numbers as strings or exact integer "
                    f"ratios. Detail: {exc}"
                ) from exc
        return self

    @model_validator(mode="after")
    def _enforce_boundary(self) -> EvidenceRecord:
        if self.status in NON_INFORMATIVE_STATUSES:
            if self.observed_value:
                raise EvidenceValidationError(
                    f"{self.source.name}: a record with status={self.status.value} must not carry "
                    f"an "
                    "observed_value; missing data may never be encoded as a value."
                )
            if self.strength_hint:
                raise EvidenceValidationError(
                    f"{self.source.name}: a record with status={self.status.value} must not carry "
                    f"a "
                    "strength_hint."
                )
            if self.applicability is ApplicabilityStatus.APPLIES:
                raise EvidenceValidationError(
                    f"{self.source.name}: status={self.status.value} cannot be applicable."
                )
            if self.verification is VerificationStatus.VERIFIED:
                raise EvidenceValidationError(
                    f"{self.source.name}: status={self.status.value} cannot be verified."
                )
        if self.status is EvidenceStatus.PRESENT:
            if not self.observed_value:
                raise EvidenceValidationError(
                    f"{self.source.name}: status=present requires a non-empty observed_value."
                )
            if self.applicability is ApplicabilityStatus.APPLIES and (
                self.verification is not VerificationStatus.VERIFIED
            ):
                raise EvidenceValidationError(
                    f"{self.source.name}: applicability=applies requires verification=verified; "
                    f"got verification={self.verification.value}. Unverified evidence cannot "
                    "support an ACMG criterion."
                )
        return self

    @model_validator(mode="after")
    def _stamp_identity(self) -> EvidenceRecord:
        if self.evidence_id:
            return self
        # ``retrieved_at`` is excluded from the digest: the same observation
        # fetched twice must have the same identity, otherwise deduplication
        # and cache validation are impossible.
        payload = self.model_dump(
            mode="json",
            exclude={"evidence_id", "retrieved_at", "retrieval"},
        )
        object.__setattr__(self, "evidence_id", f"ev.v1.{ga4gh_digest(canonical_json(payload))}")
        return self

    # -- helpers ------------------------------------------------------------

    @property
    def informative(self) -> bool:
        """True when this record can be used by the engine at all."""
        return self.status is EvidenceStatus.PRESENT

    @property
    def usable_as_evidence(self) -> bool:
        """True when the record may support or refute an ACMG criterion.

        This is the gate the derivation layer checks. It is deliberately
        narrower than :attr:`informative`: a present record about a different
        variant, or one the source contradicts internally, is informative but
        not usable.
        """
        return (
            self.status is EvidenceStatus.PRESENT
            and self.applicability is ApplicabilityStatus.APPLIES
            and self.verification is VerificationStatus.VERIFIED
        )

    def describe_gap(self) -> str:
        """Human-readable explanation of why this record is not usable."""
        if self.status is EvidenceStatus.PRESENT:
            # Verification first: "we are not sure this record is about the
            # variant we asked about" is a more actionable gap than "not
            # applicable", and the two collapse to the same indeterminate
            # applicability status.
            if self.verification is VerificationStatus.IDENTITY_UNVERIFIED:
                return (
                    "identity not verified: the source record was not confirmed to describe "
                    "this exact allele (locus match alone is not allele match)"
                )
            if self.verification is VerificationStatus.SOURCE_REPORTED_CONFLICT:
                return "the source reports conflicting classifications for this allele"
            if self.applicability is not ApplicabilityStatus.APPLIES:
                return f"not applicable ({self.applicability.value})"
            return f"identity not verified ({self.verification.value})"
        if self.status is EvidenceStatus.UNAVAILABLE:
            return f"{self.source.name} has no record for this variant"
        if self.status is EvidenceStatus.RETRIEVAL_FAILED:
            detail = self.retrieval.error or "unknown transport failure"
            return f"{self.source.name} could not be reached: {detail}"
        if self.status is EvidenceStatus.INVALID:
            return f"{self.source.name} returned data that failed validation"
        return f"{self.source.name} is not configured"


def utc_now() -> datetime:
    """The only sanctioned clock read in the evidence layer.

    Tests monkeypatch this single function to make retrieval timestamps
    deterministic.
    """
    return datetime.now(tz=UTC).replace(microsecond=0)
