"""Invariants of the typed evidence ledger.

These tests exist to make the mission's non-negotiable rule mechanically
enforceable rather than aspirational:

    *No evidence record, no ACMG criterion.*

Every test here asserts a refusal. A model that lets missing data be encoded as
a value — or lets an unverified record support a criterion — is the single most
likely route to an unsafe overcall.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ngs_agent.core.errors import EvidenceValidationError
from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    EvidenceSource,
    EvidenceStatus,
    VerificationStatus,
)
from ngs_agent.core.hashing import canonical_json_sha256
from tests.core.helpers import FIXED_NOW, TEST_SOURCE, make_evidence


class TestMissingDataIsNeverAValue:
    @pytest.mark.parametrize(
        "status",
        [
            EvidenceStatus.UNAVAILABLE,
            EvidenceStatus.RETRIEVAL_FAILED,
            EvidenceStatus.INVALID,
            EvidenceStatus.NOT_CONFIGURED,
        ],
    )
    def test_non_present_status_cannot_carry_an_observed_value(self, brca1_nonsense, status):
        """The core safety invariant: 'no record in ClinVar' != 'not pathogenic'."""
        with pytest.raises(EvidenceValidationError, match="must not carry an observed_value"):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
                status=status,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                observed_value={"classification_label": "benign"},
            )

    @pytest.mark.parametrize(
        "status",
        [
            EvidenceStatus.UNAVAILABLE,
            EvidenceStatus.RETRIEVAL_FAILED,
            EvidenceStatus.INVALID,
            EvidenceStatus.NOT_CONFIGURED,
        ],
    )
    def test_non_present_status_cannot_carry_a_strength_hint(self, brca1_nonsense, status):
        with pytest.raises(EvidenceValidationError, match="must not carry a strength_hint"):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type=EvidenceDataType.ALLELE_FREQUENCY,
                status=status,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                strength_hint="supporting",
            )

    def test_non_present_status_cannot_claim_applicability(self, brca1_nonsense):
        with pytest.raises(EvidenceValidationError, match="cannot be applicable"):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
                status=EvidenceStatus.UNAVAILABLE,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                applicability=ApplicabilityStatus.APPLIES,
            )

    def test_non_present_status_cannot_claim_verification(self, brca1_nonsense):
        with pytest.raises(EvidenceValidationError, match="cannot be verified"):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
                status=EvidenceStatus.UNAVAILABLE,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                verification=VerificationStatus.VERIFIED,
            )

    def test_present_requires_a_value(self, brca1_nonsense):
        with pytest.raises(EvidenceValidationError, match="requires a non-empty observed_value"):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
                status=EvidenceStatus.PRESENT,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                observed_value={},
            )

    def test_missing_record_describes_its_own_gap(self, brca1_nonsense):
        record = make_evidence(
            variant=brca1_nonsense,
            data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
            status=EvidenceStatus.UNAVAILABLE,
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.NOT_VERIFIED,
        )
        assert record.informative is False
        assert record.usable_as_evidence is False
        assert "no record for this variant" in record.describe_gap()


class TestUnverifiedEvidenceCannotSupportACriterion:
    def test_applies_requires_verified(self, brca1_nonsense):
        with pytest.raises(
            EvidenceValidationError, match="applicability=applies requires verification"):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
                status=EvidenceStatus.PRESENT,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                observed_value={"consequences": ["stop_gained"]},
                applicability=ApplicabilityStatus.APPLIES,
                verification=VerificationStatus.IDENTITY_UNVERIFIED,
            )

    def test_identity_unverified_record_is_informative_but_not_usable(self, brca1_nonsense):
        record = make_evidence(
            variant=brca1_nonsense,
            data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
            observed_value={"classification_label": "pathogenic", "review_status_stars": 3},
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.IDENTITY_UNVERIFIED,
        )
        assert record.informative is True
        assert record.usable_as_evidence is False
        assert "identity not verified" in record.describe_gap()

    def test_source_reported_conflict_is_not_usable(self, brca1_nonsense):
        record = make_evidence(
            variant=brca1_nonsense,
            data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
            observed_value={"classification_label": "conflicting"},
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.SOURCE_REPORTED_CONFLICT,
        )
        assert record.usable_as_evidence is False


class TestContentAddressedIdentity:
    def test_evidence_id_is_assigned_and_namespaced(self, brca1_nonsense):
        record = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
        )
        assert record.evidence_id.startswith("ev.v1.")

    def test_same_observation_fetched_twice_has_the_same_id(self, brca1_nonsense):
        first = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
        )
        second = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
            retrieved_at=FIXED_NOW.replace(hour=18),
        )
        assert first.evidence_id == second.evidence_id

    def test_retrieval_metadata_does_not_change_the_id(self, brca1_nonsense):
        """Cache hits and latency must not fork the identity of an observation."""
        from ngs_agent.core.evidence.models import RetrievalDetail

        plain = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.ALLELE_FREQUENCY,
            observed_value={"allele_frequency": "0.00002"},
        )
        cached = plain.model_copy(
            update={
                "retrieval": RetrievalDetail(
                    transport="http", cache_hit=True, latency_ms=17, http_status=200
                )
            }
        )
        assert cached.evidence_id == plain.evidence_id

    def test_a_different_observed_value_is_a_different_record(self, brca1_nonsense):
        first = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.ALLELE_FREQUENCY,
            observed_value={"allele_frequency": "0.00002"},
        )
        second = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.ALLELE_FREQUENCY,
            observed_value={"allele_frequency": "0.00003"},
        )
        assert first.evidence_id != second.evidence_id

    def test_evidence_about_a_different_variant_is_a_different_record(
        self, brca1_nonsense, brca1_missense_locus):
        first = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
        )
        second = make_evidence(
            variant=brca1_missense_locus, data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
        )
        assert first.evidence_id != second.evidence_id


class TestCanonicalValues:
    def test_floats_are_refused_in_observed_value(self, brca1_nonsense):
        """A float's serialization is not stable across builds, so it cannot be hashed."""
        with pytest.raises(EvidenceValidationError, match="canonically hashable"):
            make_evidence(
                variant=brca1_nonsense,
                data_type=EvidenceDataType.ALLELE_FREQUENCY,
                observed_value={"allele_frequency": 0.00002},
            )

    def test_floats_are_refused_in_provenance(self, brca1_nonsense):
        with pytest.raises(EvidenceValidationError, match="canonically hashable"):
            make_evidence(
                variant=brca1_nonsense,
                data_type=EvidenceDataType.ALLELE_FREQUENCY,
                observed_value={"allele_frequency": "0.00002"},
                provenance={"score": 0.5},
            )

    def test_exact_integer_ratio_is_accepted(self, brca1_nonsense):
        record = make_evidence(
            variant=brca1_nonsense,
            data_type=EvidenceDataType.ALLELE_FREQUENCY,
            observed_value={
                "allele_frequency": "0.00002",
                "allele_frequency_ratio": {"numerator": 2, "denominator": 100000},
            },
        )
        assert record.observed_value["allele_frequency_ratio"]["denominator"] == 100000

    def test_canonical_json_sorts_keys(self):
        assert canonical_json_sha256({"a": 1, "b": 2}) == canonical_json_sha256({"b": 2, "a": 1})


class TestImmutabilityAndTyping:
    def test_record_is_frozen(self, brca1_nonsense):
        record = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
        )
        with pytest.raises(ValidationError):
            record.status = EvidenceStatus.UNAVAILABLE  # type: ignore[misc]

    def test_unknown_fields_are_refused(self, brca1_nonsense):
        with pytest.raises(ValidationError):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
                status=EvidenceStatus.PRESENT,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                observed_value={"consequences": ["stop_gained"]},
                verification=VerificationStatus.VERIFIED,
                applicability=ApplicabilityStatus.APPLIES,
                llm_says="pathogenic",  # type: ignore[call-arg]
            )

    def test_unknown_data_type_is_refused(self):
        """There is no evidence type for a model's opinion, by construction."""
        with pytest.raises(ValueError):
            EvidenceDataType("llm_opinion")

    def test_model_output_cannot_enter_the_evidence_record_as_a_type(self, brca1_nonsense):
        """No amount of prompting can create an evidence type for an opinion."""
        with pytest.raises(ValueError):
            EvidenceDataType("agent_consensus")
        with pytest.raises(ValidationError):
            EvidenceRecord(
                source=TEST_SOURCE,
                data_type="llm_opinion",  # type: ignore[arg-type]
                status=EvidenceStatus.PRESENT,
                retrieved_at=FIXED_NOW,
                genome_build=brca1_nonsense.genome_build,
                queried_variant_identity=brca1_nonsense.identity,
                observed_value={"consensus": "3 of 3 agents voted pathogenic"},
            )

    def test_source_requires_a_data_version(self):
        """A version-less source cannot be cited or replayed."""
        with pytest.raises(ValidationError):
            EvidenceSource(name="ClinVar", version="", adapter_version="1.0.0")

    def test_citation_names_source_and_both_versions(self):
        assert TEST_SOURCE.citation == "test-fixture 1.0.0 (adapter 1.0.0)"

    def test_hosted_by_records_where_data_was_produced(self):
        assert TEST_SOURCE.hosted_by == "local"
