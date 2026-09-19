"""Tests for the model consultation layer.

This file used to test ``_extract_stance`` — a function that read "Pathogenic"
out of a language model's prose and fed it to a rule table. That behaviour is
the reason the module was rewritten, so these tests assert its absence as
strictly as they assert the replacement works.

Two kinds of guarantee are checked:

* **Structural.** There is no field in which a classification could be carried,
  so no downstream consumer can read one by accident.
* **Behavioural.** When a model emits tier vocabulary or an ACMG code anyway,
  the text is redacted and the overstep is reported.
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from click.testing import CliRunner

import ngs_agent.cli as cli_module
from ngs_agent.analyzer import Variant
from ngs_agent.backends.base import NoBackend
from ngs_agent.debate import (
    DISCLAIMER,
    PERSONAS,
    ConsultationSummary,
    DebateResult,
    PersonaOpinion,
    debate_variant,
    redact_classification_language,
)


class ScriptedBackend:
    """Returns one canned reply per call, in order."""

    def __init__(self, replies: list[str] | str, *, raises: Exception | None = None) -> None:
        self.replies = [replies] if isinstance(replies, str) else list(replies)
        self.raises = raises
        self.calls = 0
        self.system_prompts: list[str | None] = []

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.system_prompts.append(system)
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


@pytest.fixture
def variant() -> Variant:
    return Variant(
        gene="BRCA2",
        chrom="13",
        pos=32315086,
        ref="G",
        alt="A",
        consequence="missense_variant",
        clinvar="",
        af=None,
        depth=42,
        vaf=0.48,
        is_vus=True,
    )


class TestNoClassificationCanBeCarried:
    def test_persona_opinion_has_no_stance_field(self):
        """The field is gone, so nothing downstream can read a tier from it."""
        names = {field.name for field in fields(PersonaOpinion)}
        assert "stance" not in names
        assert "acmg_criteria" not in names
        assert "classification" not in names
        assert names == {"persona", "reasoning", "redactions"}

    def test_debate_result_has_no_classification_field(self):
        names = {field.name for field in fields(DebateResult)}
        assert "acmg_evaluation" not in names
        assert "classification" not in names
        assert "confidence" not in names
        assert "consensus" not in names
        assert "recommendation" not in names

    def test_no_persona_prompt_asks_for_a_classification(self):
        """The old prompts demanded 'STANCE: Pathogenic'. They no longer may."""
        for key, persona in PERSONAS.items():
            system = persona["system"].lower()
            assert "stance" not in system, key
            assert "do not state or imply a classification" in system, key
            assert "not name acmg/amp criteria" in system, key

    def test_the_user_prompt_forbids_classification(self, variant):
        from ngs_agent.debate import _variant_prompt

        prompt = _variant_prompt(variant).lower()
        assert "do not classify" in prompt
        assert "stance:" not in prompt
        assert "acmg_codes:" not in prompt

    def test_disclaimer_points_at_the_signed_path(self):
        assert "NOT a variant classification" in DISCLAIMER
        assert "ngsagent review" in DISCLAIMER


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "This variant is Pathogenic.",
            "I would call it likely pathogenic.",
            "Clearly benign.",
            "This is a VUS.",
            "uncertain significance",
            "LIKELY-BENIGN",
        ],
    )
    def test_tier_vocabulary_is_removed(self, text):
        redacted, redactions = redact_classification_language(text)
        assert "pathogenic" not in redacted.lower()
        assert "benign" not in redacted.lower()
        assert "vus" not in redacted.lower()
        assert redactions
        assert any(item.startswith("tier_language:") for item in redactions)

    def test_the_redaction_is_visible_not_silent(self):
        """A reviewer should see that the model tried to classify."""
        redacted, _ = redact_classification_language("This variant is Pathogenic.")
        assert "classification language removed" in redacted

    @pytest.mark.parametrize("code", ["PVS1", "PS1", "PM2", "PP3", "BA1", "BS2", "BP7"])
    def test_real_criterion_codes_are_removed(self, code):
        redacted, redactions = redact_classification_language(f"{code} applies here.")
        assert code not in redacted
        assert any("criterion" in item for item in redactions)

    @pytest.mark.parametrize("code", ["PS9", "BP12", "PM99"])
    def test_invented_criterion_codes_are_removed(self, code):
        """A code that does not exist is a fabrication, not a citation."""
        redacted, redactions = redact_classification_language(f"{code} applies here.")
        assert code not in redacted
        assert any(item.startswith("invented_criterion:") for item in redactions)

    def test_a_strength_modifier_is_removed_with_its_code(self):
        redacted, redactions = redact_classification_language("PM2_Supporting applies.")
        assert "PM2" not in redacted
        assert redactions

    def test_clean_prose_is_left_alone(self):
        text = (
            "The allele frequency was not provided, so rarity cannot be assessed. "
            "A reviewer should check gnomAD coverage at this position."
        )
        redacted, redactions = redact_classification_language(text)
        assert redacted == text
        assert redactions == ()

    def test_empty_input_is_safe(self):
        assert redact_classification_language("") == ("", ())

    def test_duplicate_redactions_are_collapsed(self):
        _, redactions = redact_classification_language("Pathogenic. Clearly pathogenic.")
        assert redactions.count("tier_language:Pathogenic") == 1


class TestDebateVariant:
    def test_three_personas_are_consulted(self, variant):
        backend = ScriptedBackend("The input did not include an allele frequency.")
        result = debate_variant(variant, backend)
        assert backend.calls == len(PERSONAS) == 3
        assert [opinion.persona for opinion in result.opinions] == [
            persona["name"] for persona in PERSONAS.values()
        ]

    def test_each_persona_gets_its_own_system_prompt(self, variant):
        backend = ScriptedBackend("narrative")
        debate_variant(variant, backend)
        assert backend.system_prompts == [persona["system"] for persona in PERSONAS.values()]

    def test_a_classifying_model_is_redacted_and_reported(self, variant):
        backend = ScriptedBackend("STANCE: Pathogenic. PVS1 and PM2 apply.")
        result = debate_variant(variant, backend)
        assert result.was_redacted is True
        assert result.boundary_violations
        for opinion in result.opinions:
            assert "pathogenic" not in opinion.reasoning.lower()
            assert "PVS1" not in opinion.reasoning
            assert opinion.redactions

    def test_a_well_behaved_model_produces_no_violations(self, variant):
        backend = ScriptedBackend(
            "No allele frequency was supplied, so rarity cannot be assessed. "
            "A reviewer should check gnomAD coverage at this position."
        )
        result = debate_variant(variant, backend)
        assert result.boundary_violations == ()
        assert result.was_redacted is False
        assert all(opinion.reasoning for opinion in result.opinions)

    def test_the_summary_carries_no_verdict(self, variant):
        result = debate_variant(variant, ScriptedBackend("narrative"))
        summary = result.summary
        assert isinstance(summary, ConsultationSummary)
        joined = " ".join(
            [*summary.points_of_agreement, *summary.points_of_disagreement]
        ).lower()
        assert "pathogenic" not in joined
        assert "benign" not in joined
        assert "does not evaluate agreement" in " ".join(summary.points_of_disagreement)

    def test_the_summary_tells_a_reviewer_what_to_check(self, variant):
        result = debate_variant(variant, ScriptedBackend("narrative"))
        checks = " ".join(result.summary.evidence_a_reviewer_should_check).lower()
        assert "ngsagent review" in checks
        # This variant has neither AF nor ClinVar, so both must be called out.
        assert "allele frequency" in checks
        assert "clinvar" in checks

    def test_a_failing_persona_does_not_lose_the_others(self, variant):
        class FlakyBackend:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, prompt: str, *, system: str | None = None) -> str:
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("persona unavailable")
                return "The transcript context should be confirmed."

        result = debate_variant(variant, FlakyBackend())
        assert len(result.opinions) == 2
        assert len(result.errors) == 1
        assert "persona unavailable" in result.errors[0]

    def test_a_missing_backend_is_an_error_not_an_empty_result(self, variant):
        """Silently returning nothing reads as 'the personas found no concern'."""
        with pytest.raises(RuntimeError, match="No LLM backend configured"):
            debate_variant(variant, NoBackend())

    def test_the_error_message_points_at_the_model_free_path(self, variant):
        with pytest.raises(RuntimeError) as excinfo:
            debate_variant(variant, NoBackend())
        assert "ngsagent review" in str(excinfo.value)

    def test_result_carries_the_disclaimer(self, variant):
        result = debate_variant(variant, ScriptedBackend("narrative"))
        assert result.disclaimer == DISCLAIMER

    def test_facts_absent_from_the_input_are_said_to_be_absent(self, variant):
        from ngs_agent.debate import _variant_prompt

        prompt = _variant_prompt(variant)
        assert "not provided" in prompt
        # The variant has no ClinVar annotation and no AF.
        assert prompt.count("not provided") >= 2


class TestReportIntegration:
    def test_the_report_renders_a_consultation(self, variant, tmp_path):
        from ngs_agent.reports import generate_html_report

        result = debate_variant(variant, ScriptedBackend("Confirm the transcript context."))
        path = tmp_path / "report.html"
        html = generate_html_report([variant], debates=[result], output_path=path)
        assert "Model Consultation" in html
        assert "Not a classification" in html
        assert "ngsagent review" in html

    def test_the_report_escapes_model_output(self, variant, tmp_path):
        from ngs_agent.reports import generate_html_report

        result = debate_variant(
            variant, ScriptedBackend("<script>alert('xss')</script> Confirm the build.")
        )
        path = tmp_path / "report.html"
        html = generate_html_report([variant], debates=[result], output_path=path)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_the_report_shows_redactions(self, variant, tmp_path):
        from ngs_agent.reports import generate_html_report

        result = debate_variant(variant, ScriptedBackend("This is Pathogenic. PVS1 applies."))
        path = tmp_path / "report.html"
        html = generate_html_report([variant], debates=[result], output_path=path)
        assert "Removed by guardrail" in html
        assert "Boundary violations detected" in html


class TestConsultCommandWhenEveryModelCallFails:
    """BUGS_FOUND.md B1: failed model calls must not be reported as success.

    The consultation produces no classification, so a failed run cannot inflate a
    tier any more. What it *could* still do is exit 0 and write an HTML report
    containing nothing but error messages, which reads as "the models reviewed
    this and had no concerns". That is the residual half of B1.
    """

    def test_exit_is_nonzero_and_no_report_is_written(self, tmp_path, monkeypatch):
        class DeadBackend:
            def complete(self, prompt: str, *, system: str | None = None) -> str:
                raise RuntimeError("upstream unavailable")

        monkeypatch.setattr(cli_module, "get_backend", lambda cfg: DeadBackend())
        report = tmp_path / "consultation.html"
        result = CliRunner().invoke(
            cli_module.main,
            ["consult", "demo_data/sample.vcf", "--html", str(report)],
        )
        assert result.exit_code == 1, result.output
        assert "Every model call failed" in result.output
        assert not report.exists(), "an empty consultation must not be exported as a report"

    def test_a_partial_failure_still_reports_and_says_it_is_partial(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        class HalfDeadBackend:
            def complete(self, prompt: str, *, system: str | None = None) -> str:
                calls["n"] += 1
                if calls["n"] % 3 == 1:
                    raise RuntimeError("one persona unavailable")
                return "The transcript context should be confirmed by the reviewer."

        monkeypatch.setattr(cli_module, "get_backend", lambda cfg: HalfDeadBackend())
        report = tmp_path / "consultation.html"
        result = CliRunner().invoke(
            cli_module.main,
            ["consult", "demo_data/sample.vcf", "--html", str(report)],
        )
        assert result.exit_code == 0, result.output
        assert report.exists()
        assert "this report is partial" in result.output
