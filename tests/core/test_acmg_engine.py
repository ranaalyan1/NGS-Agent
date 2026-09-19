"""The deterministic ACMG/AMP engine: rule table, counting, and abstention.

Three layers are tested separately, because conflating them is how the legacy
engine ended up classifying from prose:

1. the *combination rule table* (Richards 2015 Table 5) against synthetic
   criterion evaluations — no evidence involved;
2. *strength counting* — the regression guard against criterion inflation;
3. the *engine* against structured evidence — where abstention, conflicts and
   the PVS1 mechanism gate live.
"""

from __future__ import annotations

import pytest

from ngs_agent.core.acmg.criteria import CRITERIA, Direction, Strength
from ngs_agent.core.acmg.derivation import (
    CriterionEvaluation,
    DerivationConfig,
    EvaluationState,
    classify_from_rules,
    count_strengths,
)
from ngs_agent.core.acmg.engine import AbstentionPolicy, AcmgEngine
from ngs_agent.core.acmg.rule_sets import get_rule_set
from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    VerificationStatus,
)
from tests.core.helpers import (
    clinvar_evidence,
    consequence_evidence,
    frequency_evidence,
    make_evidence,
    mechanism_evidence,
    missing_evidence,
    nmd_evidence,
    usable,
)

RICHARDS = get_rule_set("acmg-amp-2015")
SVI = get_rule_set("acmg-amp-2015+clingen-svi-2020")


def applied(code: str, strength: Strength | None = None, rule_set=RICHARDS) -> CriterionEvaluation:
    """A synthetic *applied* criterion, as the derivation layer would emit."""
    spec = CRITERIA[code]
    return CriterionEvaluation(
        code=code,
        state=EvaluationState.APPLIED.value,
        direction=spec.direction,
        strength=strength or spec.default_strength,
        guideline_strength=spec.default_strength,
        evidence_ids=("ev.v1.synthetic",),
        reason="synthetic evaluation for rule-table testing",
        citation=spec.citation,
        rule_set=rule_set.name,
    )


def not_applied(code: str, state: EvaluationState, rule_set=RICHARDS) -> CriterionEvaluation:
    spec = CRITERIA[code]
    return CriterionEvaluation(
        code=code,
        state=state.value,
        direction=spec.direction,
        guideline_strength=spec.default_strength,
        reason="synthetic non-applied criterion",
        citation=spec.citation,
        rule_set=rule_set.name,
    )


def classify(*evaluations: CriterionEvaluation, rule_set=SVI):
    label, rule, counts = classify_from_rules(rule_set, list(evaluations))
    return label, rule, counts


class TestRichardsTable5Pathogenic:
    """Richards 2015 Table 5, Pathogenic column, verbatim."""

    @pytest.mark.parametrize(
        "rule_id,criteria",
        [
            ("R2015-P-i-a", ["PVS1", "PS1"]),
            ("R2015-P-i-a", ["PVS1", "PS2", "PS3"]),
            ("R2015-P-i-b", ["PVS1", "PM1", "PM3"]),
            ("R2015-P-i-c", ["PVS1", "PM1", "PP1"]),
            ("R2015-P-i-d", ["PVS1", "PP1", "PP3"]),
            ("R2015-P-ii", ["PS1", "PS4"]),
            ("R2015-P-iii", ["PS1", "PM1", "PM3", "PM4"]),
            ("R2015-P-iv", ["PS1", "PM1", "PM3", "PP1", "PP3"]),
            ("R2015-P-v", ["PS1", "PM1", "PP1", "PP2", "PP3", "PP4"]),
        ],
    )
    def test_pathogenic_combinations(self, rule_id, criteria):
        label, rule, _ = classify(*[applied(code) for code in criteria])
        assert label == "pathogenic"
        assert rule is not None and rule.rule_id == rule_id

    @pytest.mark.parametrize(
        "rule_id,criteria",
        [
            ("R2015-LP-i", ["PVS1", "PM1"]),
            ("R2015-LP-ii", ["PS1", "PM1"]),
            ("R2015-LP-ii", ["PS1", "PM1", "PM3"]),
            ("R2015-LP-iii", ["PS1", "PP1", "PP3"]),
            ("R2015-LP-iv", ["PM1", "PM3", "PM4"]),
            ("R2015-LP-v", ["PM1", "PM3", "PP1", "PP3"]),
            ("R2015-LP-vi", ["PM1", "PP1", "PP2", "PP3", "PP4"]),
        ],
    )
    def test_likely_pathogenic_combinations(self, rule_id, criteria):
        label, rule, _ = classify(*[applied(code) for code in criteria])
        assert label == "likely_pathogenic"
        assert rule is not None and rule.rule_id == rule_id

    def test_precedence_pathogenic_beats_likely_pathogenic(self):
        """PVS1 + 2 PM satisfies both LP-i and P-i-b; the guideline picks P."""
        label, rule, _ = classify(applied("PVS1"), applied("PM1"), applied("PM3"))
        assert label == "pathogenic"
        assert rule is not None and rule.rule_id == "R2015-P-i-b"

    @pytest.mark.parametrize(
        "criteria",
        [
            ["PVS1"],
            ["PS1"],
            ["PM1"],
            ["PP1"],
            ["PVS1"],
            ["PM1", "PM3"],
            ["PS1", "PP1"],
            ["PM1", "PP1", "PP3"],
        ],
    )
    def test_insufficient_combinations_produce_no_label(self, criteria):
        """No rule fires => the caller decides VUS/abstention, never a tier."""
        label, rule, _ = classify(*[applied(code) for code in criteria])
        assert label is None
        assert rule is None


class TestRichardsTable5Benign:
    @pytest.mark.parametrize(
        "rule_id,criteria",
        [
            ("R2015-B-i", ["BA1"]),
            ("R2015-B-ii", ["BS1", "BS2"]),
            ("R2015-B-ii", ["BS1", "BS2", "BS3"]),
            ("R2015-LB-i", ["BS1", "BP1", "BP4"]),
            ("R2015-LB-ii", ["BP1", "BP4"]),
        ],
    )
    def test_benign_combinations(self, rule_id, criteria):
        label, rule, _ = classify(*[applied(code) for code in criteria])
        assert label == "benign" if rule_id.startswith("R2015-B") else label == "likely_benign"
        assert rule is not None and rule.rule_id == rule_id

    def test_one_strong_plus_one_supporting_is_not_likely_benign(self):
        """Table 5 LB-i needs 1 Strong AND >=2 Supporting. Not 1 Supporting.

        This was a live bug in the legacy engine.
        """
        label, rule, _ = classify(applied("BS1"), applied("BP1"))
        assert label is None
        assert rule is None

    def test_ba1_is_stand_alone(self):
        assert CRITERIA["BA1"].default_strength is Strength.STAND_ALONE
        label, _, counts = classify(applied("BA1"))
        assert label == "benign"
        assert counts["benign"]["stand_alone"] == 1

    def test_benign_criteria_do_not_count_toward_pathogenic(self):
        label, _, counts = classify(applied("PVS1"), applied("BS1"), applied("BP1"))
        assert label is None
        assert counts["pathogenic"]["very_strong"] == 1
        assert counts["benign"]["strong"] == 1


class TestSviModifiers:
    def test_svi_adds_the_very_strong_plus_supporting_cap(self):
        """SVI: PVS1 + 1 Supporting tops out at Likely Pathogenic, never Pathogenic."""
        label, rule, _ = classify(applied("PVS1"), applied("PP3"), rule_set=SVI)
        assert label == "likely_pathogenic"
        assert rule is not None and rule.rule_id == "SVI-LP-vii"

    def test_richards_alone_does_not_fire_on_very_strong_plus_one_supporting(self):
        label, rule, _ = classify(applied("PVS1"), applied("PP3"), rule_set=RICHARDS)
        # R2015-P-i-d needs TWO supporting criteria.
        assert label is None
        assert rule is None

    def test_svi_downgrades_pm2_to_supporting(self):
        modifier = SVI.modifier_for("PM2")
        assert modifier is not None
        assert modifier.action == "modify_strength"
        assert modifier.strength is Strength.SUPPORTING

    @pytest.mark.parametrize("code", ["PP5", "BP6"])
    def test_svi_disables_reputable_source_criteria(self, code):
        modifier = SVI.modifier_for(code)
        assert modifier is not None
        assert modifier.action == "disable"
        assert SVI.allow_reputable_source_criteria is False

    def test_richards_rule_set_keeps_pp5_and_bp6(self):
        assert RICHARDS.allow_reputable_source_criteria is True
        assert RICHARDS.modifier_for("PP5") is None


class TestStrengthCounting:
    def test_duplicate_codes_count_once(self):
        """The legacy inflation bug: three personas naming PM2 gave pm=3.

        One criterion contributes at most one count, no matter how many times
        it is mentioned.
        """
        evaluations = [applied("PM2"), applied("PM2"), applied("PM2")]
        counts = count_strengths(evaluations, Direction.PATHOGENIC)
        assert sum(counts.values()) == 1

    def test_non_applied_criteria_do_not_count(self):
        evaluations = [
            applied("PM1"),
            not_applied("PM3", EvaluationState.INDETERMINATE),
            not_applied("PM4", EvaluationState.REJECTED),
            not_applied("PM5", EvaluationState.NOT_EVALUATED),
        ]
        counts = count_strengths(evaluations, Direction.PATHOGENIC)
        assert counts[Strength.MODERATE] == 1

    def test_direction_is_respected(self):
        counts = count_strengths([applied("BA1")], Direction.PATHOGENIC)
        assert sum(counts.values()) == 0

    def test_inflated_mentions_cannot_reach_a_tier(self):
        """PM2 x4 mentions must not manufacture Likely Pathogenic (needs 3 Moderate)."""
        label, _, _ = classify(*[applied("PM2") for _ in range(4)])
        assert label is None


class TestEngineAbstention:
    def test_no_evidence_at_all_abstains(self, engine, brca1_unrecorded):
        gaps = [
            missing_evidence(brca1_unrecorded, EvidenceDataType.CLINICAL_SIGNIFICANCE),
            missing_evidence(brca1_unrecorded, EvidenceDataType.ALLELE_FREQUENCY),
            missing_evidence(brca1_unrecorded, EvidenceDataType.MOLECULAR_CONSEQUENCE),
        ]
        outcome = engine.evaluate(
            variant=brca1_unrecorded, gene="BRCA1", usable_evidence=(), all_evidence=gaps
        )
        assert outcome.label == "uncertain_significance"
        assert outcome.abstained is True
        assert outcome.decision_state == "insufficient_evidence"
        assert outcome.applied_criteria == ()
        assert outcome.requires_human_review is True
        assert outcome.missing_evidence

    def test_absence_of_a_record_is_not_benign(self, engine, brca1_unrecorded):
        """The single most dangerous failure mode in variant interpretation."""
        outcome = engine.evaluate(
            variant=brca1_unrecorded,
            gene="BRCA1",
            usable_evidence=(),
            all_evidence=[missing_evidence(brca1_unrecorded,
                EvidenceDataType.CLINICAL_SIGNIFICANCE)],
        )
        assert outcome.label not in {"benign", "likely_benign"}
        assert outcome.abstained is True

    def test_unusable_evidence_only_abstains(self, engine, brca1_unrecorded):
        """A record about the wrong allele is not evidence for this one."""
        wrong_allele = make_evidence(
            variant=brca1_unrecorded,
            data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
            observed_value={"classification_label": "pathogenic", "review_status_stars": 3},
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.IDENTITY_UNVERIFIED,
        )
        assert wrong_allele.usable_as_evidence is False
        outcome = engine.evaluate(
            variant=brca1_unrecorded, gene="BRCA1", usable_evidence=(), all_evidence=[wrong_allele]
        )
        assert outcome.abstained is True
        assert outcome.decision_state == "insufficient_evidence"

    def test_incomplete_normalization_abstains(self, engine, brca1_nonsense):
        outcome = engine.evaluate(
            variant=brca1_nonsense,
            gene="BRCA1",
            usable_evidence=usable([consequence_evidence(brca1_nonsense, "stop_gained")]),
            normalization_complete=False,
        )
        assert outcome.abstained is True
        assert outcome.decision_state == "conflict"
        assert any(conflict.kind == "normalization" for conflict in outcome.conflicts)
        assert any("normalization did not complete" in item for item in outcome.limitations)

    def test_relaxing_the_normalization_policy_is_explicit(self, brca1_nonsense):
        """The safe default can only be turned off by a deliberate config change."""
        permissive = AcmgEngine(
            policy=AbstentionPolicy(abstain_on_incomplete_normalization=False)
        )
        outcome = permissive.evaluate(
            variant=brca1_nonsense,
            gene="BRCA1",
            usable_evidence=usable(
                [
                    consequence_evidence(brca1_nonsense, "stop_gained"),
                    mechanism_evidence(brca1_nonsense),
                    nmd_evidence(brca1_nonsense),
                ]
            ),
            normalization_complete=False,
        )
        assert outcome.abstained is False
        assert outcome.decision_state == "classified"
        assert any(item.code == "PVS1" for item in outcome.applied_criteria)
        # Relaxing the policy does not hide the problem.
        assert any("normalization did not complete" in item for item in outcome.limitations)

    def test_every_outcome_requires_human_review(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            nmd_evidence(brca1_nonsense),
            clinvar_evidence(brca1_nonsense, label="pathogenic"),
        ]
        for complete in (True, False):
            outcome = engine.evaluate(
                variant=brca1_nonsense,
                gene="BRCA1",
                usable_evidence=usable(records),
                all_evidence=records,
                normalization_complete=complete,
            )
            assert outcome.requires_human_review is True

    def test_outcome_carries_no_confidence_score(self, engine, brca1_nonsense):
        """An uncalibrated 0.95 is a model's guess presented as a measurement."""
        outcome = engine.evaluate(
            variant=brca1_nonsense,
            gene="BRCA1",
            usable_evidence=usable([consequence_evidence(brca1_nonsense, "stop_gained")]),
        )
        payload = outcome.model_dump()
        assert not any("confidence" in key.lower() for key in payload)
        assert not any("probability" in key.lower() for key in payload)
        assert not any("score" in key.lower() for key in payload)


class TestPvs1MechanismGate:
    def _null_variant(self, brca1_nonsense):
        return brca1_nonsense

    def test_consequence_alone_is_never_pvs1(self, engine, brca1_nonsense):
        """PVS1 is not inferred from a consequence. That is the classic overcall."""
        outcome = engine.evaluate(
            variant=brca1_nonsense,
            gene="BRCA1",
            usable_evidence=usable([consequence_evidence(brca1_nonsense, "stop_gained")]),
        )
        pvs1 = next(item for item in outcome.indeterminate_criteria if item.code == "PVS1")
        assert "mechanism" in pvs1.reason.lower()
        assert outcome.applied_criteria == ()

    def test_null_in_a_non_lof_gene_rejects_pvs1(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense, mechanism="gain_of_function", applicable=False),
        ]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        rejected = {item.code: item for item in outcome.rejected_criteria}
        assert "PVS1" in rejected
        assert "mechanism" in rejected["PVS1"].reason.lower()

    def test_null_in_lof_gene_without_transcript_context_is_indeterminate(
        self, engine, brca1_nonsense):
        """No downgrade in the absence of evidence: that needs positive evidence."""
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
        ]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        states = {item.code for item in outcome.indeterminate_criteria}
        assert "PVS1" in states
        assert not any(item.code == "PVS1" for item in outcome.applied_criteria)
        assert not any(item.code == "PVS1" for item in outcome.rejected_criteria)

    def test_full_pvs1_context_applies_at_very_strong(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            nmd_evidence(brca1_nonsense),
        ]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        pvs1 = next(item for item in outcome.applied_criteria if item.code == "PVS1")
        assert pvs1.strength is Strength.VERY_STRONG
        assert pvs1.evidence_ids, "no evidence record, no ACMG criterion"
        assert len(pvs1.evidence_ids) == 3

    def test_non_null_consequence_does_not_trigger_pvs1(self, engine, brca1_missense_locus):
        records = [
            consequence_evidence(brca1_missense_locus, "missense_variant"),
            mechanism_evidence(brca1_missense_locus),
            nmd_evidence(brca1_missense_locus),
        ]
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable(records),
            all_evidence=records,
        )
        assert not any(item.code == "PVS1" for item in outcome.applied_criteria)


class TestFrequencyCriteria:
    def test_ba1_fires_above_five_percent(self, engine, brca1_missense_locus):
        record = frequency_evidence(brca1_missense_locus, numerator=6, denominator=100)
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        ba1 = next(item for item in outcome.applied_criteria if item.code == "BA1")
        assert ba1.strength is Strength.STAND_ALONE
        assert outcome.label == "benign"

    def test_rare_frequency_applies_pm2_at_moderate_under_published_richards(
        self, engine, brca1_missense_locus
    ):
        """The published 2015 guideline assigns PM2 moderate strength."""
        record = frequency_evidence(brca1_missense_locus, numerator=2, denominator=100000)
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        pm2 = next(item for item in outcome.applied_criteria if item.code == "PM2")
        assert pm2.strength is Strength.MODERATE
        assert pm2.modifier is None

    def test_rare_frequency_applies_pm2_at_supporting_under_svi(
        self, svi_engine, brca1_missense_locus
    ):
        """ClinGen SVI 2020 downgrades PM2 to supporting; the modifier is recorded."""
        record = frequency_evidence(brca1_missense_locus, numerator=2, denominator=100000)
        outcome = svi_engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        pm2 = next(item for item in outcome.applied_criteria if item.code == "PM2")
        assert pm2.strength is Strength.SUPPORTING
        assert pm2.guideline_strength is Strength.MODERATE
        assert pm2.modifier is not None

    def test_pm2_rarity_threshold_is_disclosed_as_an_implementation_default(
        self, engine, brca1_missense_locus):
        record = frequency_evidence(brca1_missense_locus, numerator=2, denominator=100000)
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        pm2 = next(item for item in outcome.applied_criteria if item.code == "PM2")
        assert any("implementation default" in item for item in pm2.limitations)

    def test_rare_but_no_allele_number_blocks_pm2(self, engine, brca1_missense_locus):
        """Weak observation quality blocks a pathogenic criterion (safety asymmetry)."""
        record = make_evidence(
            variant=brca1_missense_locus,
            data_type=EvidenceDataType.ALLELE_FREQUENCY,
            observed_value={
                "frequency_source": "gnomAD",
                "allele_frequency": "0.00002",
                "allele_frequency_ratio": {"numerator": 2, "denominator": 100000},
                "allele_number": None,
            },
        )
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        states = {item.code for item in outcome.indeterminate_criteria}
        assert "PM2" in states
        assert not any(item.code == "PM2" for item in outcome.applied_criteria)

    def test_observation_quality_only_limits_benign_criteria(self, engine, brca1_missense_locus):
        """...but does not block a benign one. Failing toward VUS is intended."""
        record = make_evidence(
            variant=brca1_missense_locus,
            data_type=EvidenceDataType.ALLELE_FREQUENCY,
            observed_value={
                "frequency_source": "gnomAD",
                "allele_frequency": "0.06",
                "allele_frequency_ratio": {"numerator": 6, "denominator": 100},
                "allele_number": None,
            },
        )
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        assert any(item.code == "BA1" for item in outcome.applied_criteria)
        assert outcome.label == "benign"

    def test_frequency_above_the_rarity_threshold_rejects_pm2(self, engine, brca1_missense_locus):
        record = frequency_evidence(brca1_missense_locus, numerator=1, denominator=1000)
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        assert any(item.code == "PM2" for item in outcome.rejected_criteria)


class TestConflicts:
    def test_ba1_with_pathogenic_evidence_is_a_blocking_conflict(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            nmd_evidence(brca1_nonsense),
            frequency_evidence(brca1_nonsense, numerator=6, denominator=100),
        ]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        assert outcome.abstained is True
        assert outcome.decision_state == "conflict"
        assert outcome.label == "uncertain_significance"
        assert outcome.conflicts
        assert outcome.requires_human_review is True

    def test_two_authoritative_sources_disagreeing_is_a_conflict(self, engine, brca1_nonsense):
        from ngs_agent.core.evidence.models import EvidenceSource

        first = clinvar_evidence(brca1_nonsense, label="pathogenic", accession="VCV000000001")
        second = clinvar_evidence(
            brca1_nonsense,
            label="benign",
            accession="VCV000000002",
            source=EvidenceSource(
                name="other-authority", version="2026-01", adapter_version="1.0.0",
                    hosted_by="vendor"
            ),
        )
        assert first.evidence_id != second.evidence_id
        outcome = engine.evaluate(
            variant=brca1_nonsense,
            gene="BRCA1",
            usable_evidence=usable([first, second]),
            all_evidence=[first, second],
        )
        assert outcome.abstained is True
        assert outcome.label == "uncertain_significance"


class TestAuthoritativeExternalClassification:
    def test_expert_panel_pathogenic_is_adopted_when_no_criteria_apply(
        self, engine, brca1_nonsense):
        record = clinvar_evidence(brca1_nonsense, label="pathogenic")
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable([record]),
                all_evidence=[record]
        )
        assert outcome.label == "pathogenic"
        assert outcome.decision_basis == "authoritative_external_classification"
        assert outcome.abstained is False
        assert outcome.external_classifications
        assert outcome.requires_human_review is True

    def test_expert_panel_benign_is_adopted(self, engine, brca1_missense_locus):
        record = clinvar_evidence(brca1_missense_locus, label="benign")
        outcome = engine.evaluate(
            variant=brca1_missense_locus,
            gene="BRCA1",
            usable_evidence=usable([record]),
            all_evidence=[record],
        )
        assert outcome.label == "benign"
        assert outcome.decision_basis == "authoritative_external_classification"

    def test_low_star_classification_is_not_authoritative(self, engine, brca1_nonsense):
        record = clinvar_evidence(
            brca1_nonsense,
            label="pathogenic",
            review_status="no assertion provided",
            stars=0,
            authoritative=False,
        )
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable([record]),
                all_evidence=[record]
        )
        assert outcome.decision_basis != "authoritative_external_classification"
        assert outcome.label == "uncertain_significance"
        assert outcome.abstained is True

    def test_contradicting_external_and_criteria_is_a_blocking_conflict(
        self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            nmd_evidence(brca1_nonsense),
            clinvar_evidence(brca1_nonsense, label="benign"),
        ]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        assert outcome.label == "uncertain_significance"
        assert outcome.abstained is True
        # "discordant": the criteria tier and the external tier are in different
        # groups. "contradictory" is not a value this engine emits.
        assert outcome.concordance == "discordant"
        assert outcome.decision_state == "conflict"
        assert any(conflict.severity == "blocking" for conflict in outcome.conflicts)


class TestNonDerivableCriteria:
    def test_criteria_without_a_configured_source_are_reported_not_invented(
        self, engine, brca1_nonsense):
        """PS2/PS3/PP1 need de-novo, assay and segregation data we do not have."""
        records = [consequence_evidence(brca1_nonsense, "stop_gained")]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        evaluated = {
            item.code
            for bucket in (
                outcome.applied_criteria,
                outcome.rejected_criteria,
                outcome.indeterminate_criteria,
                outcome.not_evaluated_criteria,
            )
            for item in bucket
        }
        assert evaluated == set(CRITERIA), "every criterion must be accounted for exactly once"
        not_evaluated = {item.code for item in outcome.not_evaluated_criteria}
        assert {"PS2", "PS3", "PS4", "PP1", "BS3", "BS4"} <= not_evaluated

    def test_each_criterion_appears_in_exactly_one_state(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            frequency_evidence(brca1_nonsense, numerator=2, denominator=100000),
        ]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        all_codes = [
            item.code
            for bucket in (
                outcome.applied_criteria,
                outcome.rejected_criteria,
                outcome.indeterminate_criteria,
                outcome.not_evaluated_criteria,
            )
            for item in bucket
        ]
        assert len(all_codes) == len(set(all_codes)) == len(CRITERIA)

    def test_pp5_is_not_evaluated_under_svi(self, svi_engine, brca1_nonsense):
        record = clinvar_evidence(
            brca1_nonsense, label="pathogenic", review_status="criteria provided, single submitter",
            stars=1, authoritative=False,
        )
        outcome = svi_engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable([record]),
                all_evidence=[record]
        )
        states = {item.code for item in outcome.not_evaluated_criteria}
        assert "PP5" in states
        assert "BP6" in states

    def test_pp5_never_laundered_through_an_authoritative_source(self, engine, brca1_nonsense):
        """An expert-panel classification is recorded as external, not as PP5."""
        record = clinvar_evidence(brca1_nonsense, label="pathogenic")
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable([record]),
                all_evidence=[record]
        )
        assert not any(item.code == "PP5" for item in outcome.applied_criteria)
        rejected = {item.code: item for item in outcome.rejected_criteria}
        assert "PP5" in rejected
        assert "authoritative_external_classification" in rejected["PP5"].reason


class TestProvenanceOnEveryCriterion:
    def test_no_applied_criterion_without_an_evidence_id(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            nmd_evidence(brca1_nonsense),
            frequency_evidence(brca1_nonsense, numerator=2, denominator=100000),
        ]
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        assert outcome.applied_criteria
        for item in outcome.applied_criteria:
            assert item.evidence_ids, f"{item.code} applied with no evidence record"
            assert item.citation
            assert item.rule_set == RICHARDS.name
            assert item.reason

    def test_evidence_ids_point_at_real_records(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            nmd_evidence(brca1_nonsense),
        ]
        known = {record.evidence_id for record in records}
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        for item in outcome.applied_criteria:
            assert set(item.evidence_ids) <= known

    def test_strength_hint_is_ignored_by_the_engine(self, engine, brca1_nonsense):
        """An adapter's guess at strength must not become the applied strength."""
        record = make_evidence(
            variant=brca1_nonsense,
            data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
        )
        hinted = record.model_copy(update={"strength_hint": "stand_alone"})
        outcome = engine.evaluate(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable([hinted]),
                all_evidence=[hinted]
        )
        assert not any(item.strength is Strength.STAND_ALONE for item in outcome.applied_criteria)


class TestEnginePurity:
    def test_repeated_evaluation_is_identical(self, engine, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
            nmd_evidence(brca1_nonsense),
            frequency_evidence(brca1_nonsense, numerator=2, denominator=100000),
        ]
        kwargs = dict(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        first = engine.evaluate(**kwargs)
        second = engine.evaluate(**kwargs)
        assert first.model_dump_json() == second.model_dump_json()

    def test_engine_instances_agree(self, brca1_nonsense):
        records = [consequence_evidence(
            brca1_nonsense, "stop_gained"), mechanism_evidence(brca1_nonsense)]
        kwargs = dict(
            variant=brca1_nonsense, gene="BRCA1", usable_evidence=usable(records),
                all_evidence=records
        )
        assert AcmgEngine().evaluate(**kwargs).model_dump_json() == AcmgEngine().evaluate(
            **kwargs
        ).model_dump_json()

    def test_rule_set_version_is_recorded(self, engine, brca1_nonsense):
        outcome = engine.evaluate(variant=brca1_nonsense, gene="BRCA1", usable_evidence=())
        assert outcome.rule_set == "acmg-amp-2015"
        assert outcome.rule_set_version
        assert "Richards" in outcome.rule_set_citation

    def test_derivation_config_is_frozen_and_explicit(self):
        config = DerivationConfig()
        assert config.require_transcript_context_for_pvs1 is True
        assert config.require_observation_quality_for_pm2 is True
