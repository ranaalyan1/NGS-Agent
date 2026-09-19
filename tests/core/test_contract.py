"""The versioned JSON result contract.

The contract is what every interface serializes, so its invariants are the last
line of defence: a result that cannot be explained from evidence, or that
claims a sign-off nobody made, must be impossible to *construct* — not merely
undesirable.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ngs_agent.core.acmg.criteria import Strength
from ngs_agent.core.acmg.engine import ClassificationOutcome
from ngs_agent.core.contract import (
    CONTRACT_SCHEMA_VERSION,
    RESEARCH_USE_ONLY_BANNER,
    ExplanationBlock,
    GeneBlock,
    ProvenanceBlock,
    ReviewBlock,
    VariantReviewResult,
)
from ngs_agent.core.errors import ContractError
from tests.core.helpers import FIXED_NOW, clinvar_evidence, consequence_evidence, mechanism_evidence


def provenance(**overrides) -> ProvenanceBlock:
    payload = dict(
        engine_version="test",
        normalization_version="test",
        contract_schema_version=CONTRACT_SCHEMA_VERSION,
        rule_set="acmg-amp-2015",
        rule_set_version="1.0.0",
        generated_at=FIXED_NOW,
        input_hashes={"vcf": "0" * 64},
        database_versions={"test-fixture": "1.0.0"},
    )
    payload.update(overrides)
    return ProvenanceBlock(**payload)


def outcome(**overrides) -> ClassificationOutcome:
    payload = dict(
        label="uncertain_significance",
        display_label="VUS",
        abstained=True,
        decision_state="insufficient_evidence",
        decision_basis="no_criteria_met",
        requires_human_review=True,
        rule_set="acmg-amp-2015",
        rule_set_version="1.0.0",
        rule_set_citation="Richards S, et al. 2015",
    )
    payload.update(overrides)
    return ClassificationOutcome(**payload)


def build_result(variant, **kwargs) -> VariantReviewResult:
    defaults = dict(
        result_id="res.test.0001",
        variant=variant,
        gene="BRCA1",
        outcome=outcome(),
        evidence=(),
        provenance=provenance(),
    )
    defaults.update(kwargs)
    return VariantReviewResult.build(**defaults)


class TestSchemaVersioning:
    def test_schema_version_is_pinned(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        assert result.schema_version == CONTRACT_SCHEMA_VERSION
        assert result.provenance.contract_schema_version == CONTRACT_SCHEMA_VERSION

    def test_a_stale_schema_version_is_refused(self, brca1_nonsense):
        """Handing a consumer a contract it cannot interpret is worse than failing."""
        with pytest.raises(ContractError, match="schema_version"):
            build_result(brca1_nonsense).model_copy(update={"schema_version": "0.9.0"}).model_validate(
                {**json.loads(build_result(brca1_nonsense).model_dump_json()), "schema_version": "0.9.0"}
            )

    def test_contract_round_trips_through_json(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        restored = VariantReviewResult.model_validate_json(result.model_dump_json())
        assert restored == result

    def test_unknown_fields_are_refused(self, brca1_nonsense):
        payload = json.loads(build_result(brca1_nonsense).model_dump_json())
        payload["confidence"] = 0.95
        with pytest.raises(ValidationError):
            VariantReviewResult.model_validate(payload)

    def test_result_is_frozen(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        with pytest.raises(ValidationError):
            result.classification.label = "pathogenic"  # type: ignore[misc]


class TestResearchUseOnly:
    def test_ruo_is_a_literal_true(self, brca1_nonsense):
        """The flag cannot be set to False; it is part of the type."""
        result = build_result(brca1_nonsense)
        assert result.research_use_only is True
        payload = json.loads(result.model_dump_json())
        payload["research_use_only"] = False
        with pytest.raises(ValidationError):
            VariantReviewResult.model_validate(payload)

    def test_disclaimer_cannot_be_stripped(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        assert result.disclaimer == RESEARCH_USE_ONLY_BANNER
        assert "RESEARCH USE ONLY" in result.disclaimer

    def test_banner_is_present_in_the_limitations(self, brca1_nonsense):
        outcome_ = outcome(
            label="pathogenic",
            display_label="Pathogenic",
            abstained=False,
            decision_state="classified",
            decision_basis="authoritative_external_classification",
            limitations=(RESEARCH_USE_ONLY_BANNER,),
        )
        result = build_result(brca1_nonsense, outcome=outcome_)
        assert any("RESEARCH USE ONLY" in item for item in result.limitations)

    def test_no_confidence_or_probability_field_exists(self, brca1_nonsense):
        """An uncalibrated score presented as certainty is a safety defect."""
        fields = set(VariantReviewResult.model_fields)
        assert not any("confidence" in name for name in fields)
        assert not any("probability" in name for name in fields)
        payload = json.loads(build_result(brca1_nonsense).model_dump_json())
        assert "confidence" not in json.dumps(payload).lower()


class TestNoEvidenceRecordNoCriterion:
    def test_applied_criterion_without_evidence_is_unconstructable(self, brca1_nonsense):
        """The rule is enforced at the contract level, not merely by convention."""
        from ngs_agent.core.acmg.criteria import CRITERIA, Direction
        from ngs_agent.core.acmg.derivation import CriterionEvaluation, EvaluationState

        spec = CRITERIA["PVS1"]
        unsupported = CriterionEvaluation(
            code="PVS1",
            state=EvaluationState.APPLIED.value,
            direction=Direction.PATHOGENIC,
            strength=Strength.VERY_STRONG,
            guideline_strength=spec.default_strength,
            evidence_ids=(),  # <- the violation
            reason="an LLM asserted this criterion",
            citation=spec.citation,
            rule_set="acmg-amp-2015",
        )
        with pytest.raises(ContractError, match="no evidence record, no ACMG criterion"):
            build_result(
                brca1_nonsense,
                outcome=outcome(
                    label="likely_pathogenic",
                    display_label="Likely Pathogenic",
                    abstained=False,
                    decision_state="classified",
                    decision_basis="acmg_criteria",
                    applied_criteria=(unsupported,),
                ),
            )

    def test_a_criterion_in_any_other_state_may_lack_evidence(self, brca1_nonsense):
        """Rejected / indeterminate / not-evaluated criteria explain an *absence*."""
        from ngs_agent.core.acmg.criteria import CRITERIA, Direction
        from ngs_agent.core.acmg.derivation import CriterionEvaluation, EvaluationState

        spec = CRITERIA["PS2"]
        rejected = CriterionEvaluation(
            code="PS2",
            state=EvaluationState.NOT_EVALUATED.value,
            direction=Direction.PATHOGENIC,
            guideline_strength=spec.default_strength,
            evidence_ids=(),
            reason="no de-novo source configured",
            citation=spec.citation,
            rule_set="acmg-amp-2015",
        )
        result = build_result(brca1_nonsense, outcome=outcome(not_evaluated_criteria=(rejected,)))
        assert result.not_evaluated_criteria[0].evidence_ids == ()

    def test_applied_criteria_keep_their_evidence_ids(self, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
        ]
        from ngs_agent.core.acmg.engine import AcmgEngine

        engine_outcome = AcmgEngine().evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=tuple(records), all_evidence=records
        )
        result = build_result(brca1_nonsense, outcome=engine_outcome, evidence=tuple(records))
        known = {record.evidence_id for record in result.evidence}
        for criterion in result.applied_criteria:
            assert criterion.evidence_ids
            assert set(criterion.evidence_ids) <= known

    def test_every_serialized_criterion_names_its_rule_set_and_citation(self, brca1_nonsense):
        from ngs_agent.core.acmg.engine import AcmgEngine

        record = clinvar_evidence(brca1_nonsense, label="pathogenic")
        engine_outcome = AcmgEngine().evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=(record,), all_evidence=(record,)
        )
        result = build_result(brca1_nonsense, outcome=engine_outcome, evidence=(record,))
        buckets = (
            result.applied_criteria,
            result.rejected_criteria,
            result.indeterminate_criteria,
            result.not_evaluated_criteria,
        )
        assert sum(len(bucket) for bucket in buckets) == 28
        for bucket in buckets:
            for criterion in bucket:
                assert criterion.rule_set
                assert criterion.citation
                assert criterion.reason


class TestAbstentionConsistency:
    def test_abstained_with_classified_state_is_refused(self, brca1_nonsense):
        with pytest.raises(ContractError, match="inconsistent with decision_state"):
            build_result(
                brca1_nonsense,
                outcome=outcome(abstained=True, decision_state="classified"),
            )

    @pytest.mark.parametrize("state", ["insufficient_evidence", "conflict", "abstained"])
    def test_abstention_states_require_the_abstained_flag(self, brca1_nonsense, state):
        with pytest.raises(ContractError, match="requires abstained=True"):
            build_result(brca1_nonsense, outcome=outcome(abstained=False, decision_state=state))

    def test_manual_review_required_does_not_imply_abstention(self, brca1_nonsense):
        result = build_result(
            brca1_nonsense,
            outcome=outcome(abstained=False, decision_state="manual_review_required",
                            decision_basis="acmg_criteria"),
        )
        assert result.classification.abstained is False
        assert result.classification.requires_human_review is True


class TestReviewBlockInvariants:
    def test_default_review_is_pending_and_unsigned(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        assert result.review.status == "pending"
        assert result.review.reviewer is None
        assert result.review.signed_at is None
        assert result.review.is_signed is False

    def test_approval_without_a_reviewer_is_refused(self, brca1_nonsense):
        with pytest.raises(ContractError, match="requires both a reviewer"):
            build_result(
                brca1_nonsense,
                review=ReviewBlock(status="approved", signed_at=FIXED_NOW),
            )

    def test_approval_without_a_timestamp_is_refused(self, brca1_nonsense):
        with pytest.raises(ContractError, match="requires both a reviewer"):
            build_result(brca1_nonsense, review=ReviewBlock(status="approved", reviewer="dr.who"))

    def test_rejection_is_held_to_the_same_standard(self, brca1_nonsense):
        with pytest.raises(ContractError, match="requires both a reviewer"):
            build_result(brca1_nonsense, review=ReviewBlock(status="rejected", reviewer="dr.who"))

    def test_a_timestamp_without_a_status_is_refused(self, brca1_nonsense):
        with pytest.raises(ContractError, match="without a terminal review status"):
            build_result(brca1_nonsense, review=ReviewBlock(status="pending", signed_at=FIXED_NOW))

    def test_overriding_the_engine_requires_a_reason(self, brca1_nonsense):
        with pytest.raises(ContractError, match="requires an explicit override_reason"):
            build_result(
                brca1_nonsense,
                review=ReviewBlock(
                    status="approved",
                    reviewer="dr.who",
                    signed_at=FIXED_NOW,
                    decision="pathogenic",
                ),
            )

    def test_overriding_the_engine_with_a_reason_is_allowed(self, brca1_nonsense):
        result = build_result(
            brca1_nonsense,
            review=ReviewBlock(
                status="approved",
                reviewer="dr.who",
                signed_at=FIXED_NOW,
                decision="pathogenic",
                override_reason="Segregation data held by the laboratory, not in the ledger.",
            ),
        )
        assert result.review.is_signed is True
        assert result.review.decision == "pathogenic"
        # The engine's label is never rewritten by a human override.
        assert result.classification.label == "uncertain_significance"

    def test_agreeing_with_the_engine_needs_no_reason(self, brca1_nonsense):
        result = build_result(
            brca1_nonsense,
            review=ReviewBlock(
                status="approved",
                reviewer="dr.who",
                signed_at=FIXED_NOW,
                decision="uncertain_significance",
            ),
        )
        assert result.review.override_reason is None


class TestGeneBlock:
    def test_unresolved_gene_is_recorded_as_unresolved(self, brca1_nonsense):
        result = build_result(brca1_nonsense, gene=None)
        assert result.gene.resolved_from == "not_resolved"
        assert result.gene.symbol is None
        assert result.variant.gene is None

    def test_gene_resolution_provenance_is_kept(self, brca1_nonsense):
        result = build_result(
            brca1_nonsense,
            gene="BRCA1",
            gene_block=GeneBlock(
                symbol="BRCA1",
                resolved_from="cli",
                validated_against="curated_gene_table.v1",
                validation_status="validated",
            ),
        )
        assert result.gene.resolved_from == "cli"
        assert result.gene.validation_status == "validated"

    def test_a_gene_we_could_not_validate_says_so(self, brca1_nonsense):
        block = GeneBlock(symbol="BRCA1X", resolved_from="vcf_info", validation_status="not_found")
        result = build_result(brca1_nonsense, gene="BRCA1X", gene_block=block)
        assert result.gene.validation_status == "not_found"


class TestExplanationIsOutsideTheSignedPath:
    def test_explanation_defaults_to_absent(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        assert result.explanation.present is False
        assert result.explanation.text is None

    def test_explanation_cannot_change_the_classification(self, brca1_nonsense):
        result = build_result(
            brca1_nonsense,
            explanation=ExplanationBlock(
                present=True,
                provider="test",
                model="test-model",
                text="The model believes this variant is pathogenic.",
                prompt_hash="0" * 64,
                generated_at=FIXED_NOW,
            ),
        )
        assert result.classification.label == "uncertain_significance"
        assert result.classification.decision_basis == "no_criteria_met"

    def test_boundary_violations_are_recorded_on_the_block(self, brca1_nonsense):
        result = build_result(
            brca1_nonsense,
            explanation=ExplanationBlock(
                present=True,
                provider="test",
                model="test-model",
                text="...",
                boundary_violations=("attempted_to_add_criterion:PVS1",),
                unsupported_citations=("ev.v1.does-not-exist",),
            ),
        )
        assert result.explanation.boundary_violations
        assert result.explanation.unsupported_citations
        assert "not evidence" in result.explanation.disclaimer.lower() or result.explanation.disclaimer


class TestProvenance:
    def test_input_hashes_and_database_versions_are_required_context(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        assert result.provenance.input_hashes
        assert result.provenance.database_versions
        assert result.provenance.engine_version
        assert result.provenance.normalization_version

    def test_normalization_block_mirrors_the_variant(self, brca1_nonsense):
        result = build_result(brca1_nonsense)
        assert result.normalization.identity_string == brca1_nonsense.identity
        assert result.normalization.algorithm_version
        assert result.variant.spdi == brca1_nonsense.spdi
        assert result.variant.id == brca1_nonsense.variant_id

    def test_audit_linkage_is_present_when_a_log_was_used(self, brca1_nonsense):
        result = build_result(
            brca1_nonsense,
            provenance=provenance(audit_id="aud.test.0001", audit_path="/tmp/audit/audit.jsonl",
                                  run_id="run.test.0001"),
        )
        assert result.provenance.audit_id == "aud.test.0001"
        assert result.provenance.run_id == "run.test.0001"

    def test_serialization_is_stable(self, brca1_nonsense):
        """Same inputs => byte-identical JSON, so two runs can be diffed."""
        first = build_result(brca1_nonsense).model_dump_json()
        second = build_result(brca1_nonsense).model_dump_json()
        assert first == second

    def test_strength_counts_are_serialized_as_integers(self, brca1_nonsense):
        from ngs_agent.core.acmg.engine import AcmgEngine

        record = consequence_evidence(brca1_nonsense, "stop_gained")
        engine_outcome = AcmgEngine().evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=(record,), all_evidence=(record,)
        )
        payload = json.loads(build_result(brca1_nonsense, outcome=engine_outcome).model_dump_json())
        counts = payload["classification"]["strength_counts"]
        assert set(counts) == {"pathogenic", "benign"}
        assert all(isinstance(value, int) for value in counts["pathogenic"].values())
        assert Strength.VERY_STRONG.value in counts["pathogenic"]
