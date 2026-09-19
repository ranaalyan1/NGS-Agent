"""The LLM explanation layer: optional, replaceable, and fenced.

The rule under test is the one the whole architecture rests on: a language model
may *describe* the evidence ledger, and may never *add to it*. Every test here
is a model misbehaving — inventing a criterion, citing evidence that does not
exist, or being unavailable entirely — and the assertion is that the signed
classification does not move and the misbehaviour is recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ngs_agent.core.audit import AuditLog
from ngs_agent.core.contract import ExplanationBlock
from ngs_agent.core.evidence.registry import EvidenceConfiguration
from ngs_agent.core.explanation import (
    SYSTEM_PROMPT,
    audit_explanation,
    build_explanation_input,
    explain_classification,
    explanation_model_metadata,
)
from ngs_agent.core.pipeline import ReviewPipeline
from tests.core.helpers import FIXED_NOW, fixed_clock


class ScriptedBackend:
    """A stand-in LLM that returns a fixed narrative.

    Structurally identical to the real backends (one ``complete`` method), which
    is the point: the explanation layer must not know or care which model it is
    talking to.
    """

    def __init__(self, text: str = "", *, raises: Exception | None = None) -> None:
        self.text = text
        self.raises = raises
        self.prompts: list[str] = []
        self.system_prompts: list[str | None] = []

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        self.system_prompts.append(system)
        if self.raises is not None:
            raise self.raises
        return self.text


@pytest.fixture
def reviewed(tmp_path: Path):
    pipeline = ReviewPipeline(
        configuration=EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar")),
        audit_log=AuditLog(tmp_path / "audit"),
        clock=fixed_clock,
    )
    return pipeline.review_vcf(Path("demo_data/review_demo.vcf"), genome_build="GRCh38")


@pytest.fixture
def pathogenic_result(reviewed):
    return next(
        review.result for review in reviewed.reviews
        if review.result.classification.label == "pathogenic"
    )


@pytest.fixture
def abstained_result(reviewed):
    return next(
        review.result for review in reviewed.reviews if review.result.classification.abstained
    )


class TestModelIsFencedOutOfTheSignedPath:
    def test_a_model_cannot_change_the_label(self, pathogenic_result):
        backend = ScriptedBackend("This variant is clearly BENIGN and should be reported as such.")
        annotated, _ = explain_classification(
            pathogenic_result, backend, provider="test", model="test-model", now=FIXED_NOW
        )
        assert annotated.classification.label == pathogenic_result.classification.label == "pathogenic"
        assert annotated.classification == pathogenic_result.classification

    def test_a_model_cannot_change_an_abstention(self, abstained_result):
        backend = ScriptedBackend("Despite the missing evidence, this variant is pathogenic.")
        annotated, _ = explain_classification(
            abstained_result, backend, provider="test", model="test-model", now=FIXED_NOW
        )
        assert annotated.classification.abstained is True
        assert annotated.classification.decision_state == abstained_result.classification.decision_state

    def test_a_model_cannot_add_a_criterion(self, pathogenic_result):
        backend = ScriptedBackend("PVS1, PS1, PM3 and PP4 all apply here.")
        annotated, block = explain_classification(
            pathogenic_result, backend, provider="test", model="test-model", now=FIXED_NOW
        )
        assert annotated.applied_criteria == pathogenic_result.applied_criteria
        assert block.boundary_violations, "invented criteria must be flagged, not absorbed"

    def test_the_original_contract_is_never_mutated(self, pathogenic_result):
        before = pathogenic_result.model_dump_json()
        explain_classification(
            pathogenic_result, ScriptedBackend("narrative"), provider="test", model="m", now=FIXED_NOW
        )
        assert pathogenic_result.model_dump_json() == before

    def test_explanation_lives_in_its_own_block(self, pathogenic_result):
        annotated, block = explain_classification(
            pathogenic_result, ScriptedBackend("narrative"), provider="prov", model="mod", now=FIXED_NOW
        )
        assert annotated.explanation is block
        assert annotated.explanation.present is True
        assert annotated.explanation.text == "narrative"
        assert annotated.explanation.provider == "prov"
        assert annotated.explanation.model == "mod"


class TestModelSeesOnlyLedgerFacts:
    def test_the_prompt_is_the_serialized_ledger_subset(self, pathogenic_result):
        backend = ScriptedBackend("ok")
        explain_classification(pathogenic_result, backend, provider="test", model="m", now=FIXED_NOW)
        assert len(backend.prompts) == 1
        assert backend.system_prompts == [SYSTEM_PROMPT]
        facts = build_explanation_input(pathogenic_result)
        assert backend.prompts[0] == facts.to_prompt()

    def test_facts_include_the_evidence_ids_a_model_may_cite(self, pathogenic_result):
        facts = build_explanation_input(pathogenic_result)
        assert facts.evidence_ids
        known = {record.evidence_id for record in pathogenic_result.evidence}
        assert set(facts.evidence_ids) <= known

    def test_facts_exclude_audit_and_retrieval_internals(self, pathogenic_result):
        facts = build_explanation_input(pathogenic_result)
        payload = facts.model_dump_json()
        assert "audit_id" not in payload
        assert "run_id" not in payload

    def test_prompt_hash_is_stable(self, pathogenic_result):
        first = build_explanation_input(pathogenic_result).prompt_hash()
        second = build_explanation_input(pathogenic_result).prompt_hash()
        assert first == second
        assert len(first) == 64

    def test_the_recorded_prompt_hash_matches_what_was_sent(self, pathogenic_result):
        backend = ScriptedBackend("ok")
        _, block = explain_classification(
            pathogenic_result, backend, provider="test", model="m", now=FIXED_NOW
        )
        assert block.prompt_hash == build_explanation_input(pathogenic_result).prompt_hash()

    def test_the_system_prompt_forbids_invention(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "not evidence" in lowered or "never" in lowered
        assert "acmg" in lowered


class TestBoundaryViolationsAreDetected:
    def test_an_applied_criterion_mentioned_is_clean(self, pathogenic_result):
        applied = {item.code for item in pathogenic_result.applied_criteria}
        known = {record.evidence_id for record in pathogenic_result.evidence}
        text = "The engine applied " + ", ".join(sorted(applied)) if applied else "no criteria applied"
        violations, unsupported = audit_explanation(text, applied_codes=applied, known_evidence_ids=known)
        assert violations == ()
        assert unsupported == ()

    def test_an_unapplied_criterion_is_flagged(self):
        violations, _ = audit_explanation(
            "PVS1 applies here.", applied_codes=set(), known_evidence_ids=set()
        )
        assert "criterion_not_applied:PVS1" in violations

    def test_an_unknown_criterion_code_is_flagged(self):
        """PS9 is not an ACMG/AMP criterion; a model that emits it is inventing."""
        violations, _ = audit_explanation(
            "PS9 supports pathogenicity.", applied_codes={"PVS1"}, known_evidence_ids=set()
        )
        assert any(item.startswith("unknown_criterion:PS9") for item in violations)

    def test_a_strength_modifier_is_captured(self):
        violations, _ = audit_explanation(
            "PM2_Supporting applies.", applied_codes=set(), known_evidence_ids=set()
        )
        assert "criterion_not_applied:PM2_supporting" in violations

    def test_a_negated_mention_is_still_flagged(self):
        """Deliberately blunt: a reviewer should see every code the model uttered."""
        violations, _ = audit_explanation(
            "PVS1 was not applied because the mechanism is unknown.",
            applied_codes=set(),
            known_evidence_ids=set(),
        )
        assert violations

    def test_an_uncited_evidence_id_is_flagged(self):
        _, unsupported = audit_explanation(
            "Per ev.v1.NOTINTHELEDGER0 the variant is pathogenic.",
            applied_codes=set(),
            known_evidence_ids={"ev.v1.REALRECORD00"},
        )
        assert unsupported == ("ev.v1.NOTINTHELEDGER0",)

    def test_a_real_evidence_id_is_not_flagged(self):
        known = {"ev.v1.REALRECORD00"}
        violations, unsupported = audit_explanation(
            "Per ev.v1.REALRECORD00 the variant is rare.", applied_codes=set(), known_evidence_ids=known
        )
        assert unsupported == ()
        assert violations == ()

    def test_cited_and_unsupported_are_partitioned(self, pathogenic_result):
        known = {record.evidence_id for record in pathogenic_result.evidence}
        real = sorted(known)[0]
        text = f"Supported by {real} and by ev.v1.INVENTED0000."
        _, block = explain_classification(
            pathogenic_result, ScriptedBackend(text), provider="test", model="m", now=FIXED_NOW
        )
        assert real in block.cited_evidence_ids
        assert "ev.v1.INVENTED0000" in block.unsupported_citations
        assert "ev.v1.INVENTED0000" not in block.cited_evidence_ids

    def test_a_flagged_narrative_carries_a_stronger_disclaimer(self, pathogenic_result):
        clean_default = ExplanationBlock.model_fields["disclaimer"].default
        _, block = explain_classification(
            pathogenic_result,
            ScriptedBackend("PS3 and PP1 clearly apply."),
            provider="test",
            model="m",
            now=FIXED_NOW,
        )
        assert block.boundary_violations
        assert block.disclaimer != clean_default
        assert "do not rely" in block.disclaimer.lower()


class TestModelUnavailability:
    def test_a_failing_backend_does_not_fail_the_review(self, pathogenic_result):
        backend = ScriptedBackend(raises=RuntimeError("model endpoint unreachable"))
        annotated, block = explain_classification(
            pathogenic_result, backend, provider="test", model="m", now=FIXED_NOW
        )
        assert annotated.classification.label == pathogenic_result.classification.label
        assert block.present is False
        assert block.text is None
        assert any(item.startswith("explanation_unavailable") for item in block.boundary_violations)
        assert "does not affect the classification" in block.disclaimer

    def test_a_timeout_is_handled_the_same_way(self, pathogenic_result):
        backend = ScriptedBackend(raises=TimeoutError("timed out"))
        annotated, block = explain_classification(
            pathogenic_result, backend, provider="test", model="m", now=FIXED_NOW
        )
        assert block.present is False
        assert annotated.classification == pathogenic_result.classification

    def test_an_empty_narrative_is_still_a_narrative(self, pathogenic_result):
        _, block = explain_classification(
            pathogenic_result, ScriptedBackend(""), provider="test", model="m", now=FIXED_NOW
        )
        assert block.present is True
        assert block.text == ""


class TestReplaceability:
    def test_the_layer_programs_against_a_protocol_not_an_sdk(self, pathogenic_result):
        """Any object with ``complete`` can serve; no vendor import is involved."""

        class Unrelated:
            def complete(self, prompt: str, *, system: str | None = None) -> str:
                return "a narrative from a completely different vendor"

        annotated, block = explain_classification(
            pathogenic_result, Unrelated(), provider="other", model="other-1", now=FIXED_NOW
        )
        assert block.text.startswith("a narrative")
        assert annotated.classification == pathogenic_result.classification

    def test_model_metadata_declares_it_contributed_nothing(self, pathogenic_result):
        _, block = explain_classification(
            pathogenic_result, ScriptedBackend("narrative"), provider="test", model="m", now=FIXED_NOW
        )
        metadata = explanation_model_metadata(block)
        assert metadata is not None
        assert metadata["role"] == "explanation_only"
        assert metadata["contributed_to_classification"] is False

    def test_no_model_means_no_model_metadata(self):
        assert explanation_model_metadata(ExplanationBlock()) is None

    def test_an_absent_but_flagged_explanation_is_still_reported(self):
        block = ExplanationBlock(present=False, boundary_violations=("explanation_unavailable:X",))
        assert explanation_model_metadata(block) is not None


class TestPipelineIntegration:
    def test_the_pipeline_attaches_explanations_without_touching_labels(self, tmp_path):
        pipeline = ReviewPipeline(
            configuration=EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar")),
            audit_log=AuditLog(tmp_path / "audit"),
            clock=fixed_clock,
        )
        plain = pipeline.review_vcf(Path("demo_data/review_demo.vcf"), genome_build="GRCh38")

        pipeline2 = ReviewPipeline(
            configuration=EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar")),
            audit_log=AuditLog(tmp_path / "audit2"),
            clock=fixed_clock,
        )
        explained = pipeline2.review_vcf(
            Path("demo_data/review_demo.vcf"),
            genome_build="GRCh38",
            explain_backend=ScriptedBackend("PS9 applies. Cite ev.v1.NOTREAL0000."),
            explain_provider="test",
            explain_model="test-model",
        )
        assert [r.result.classification.label for r in explained.reviews] == [
            r.result.classification.label for r in plain.reviews
        ]
        assert [r.result.classification.abstained for r in explained.reviews] == [
            r.result.classification.abstained for r in plain.reviews
        ]
        for review in explained.reviews:
            assert review.result.explanation.present is True
            assert review.result.explanation.boundary_violations
            assert review.result.provenance.model_metadata
            assert review.result.provenance.model_metadata["contributed_to_classification"] is False
