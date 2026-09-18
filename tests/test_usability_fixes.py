"""Regression tests for usability + safety fixes (pip CLI)."""

import pytest
from click.testing import CliRunner

from ngs_agent.acmg import compute_acmg_classification
from ngs_agent.analyzer import Variant
from ngs_agent.backends.base import LLMBackend
from ngs_agent.cli import main
from ngs_agent.debate import DebateBackendError, _extract_stance, debate_variant
from ngs_agent.doctor import DiagnosticCheck, overall_status
from ngs_agent.qc import QCParser
from ngs_agent.reports import generate_html_report
from ngs_agent.watcher import load_signatures


def _variant() -> Variant:
    return Variant(
        chrom="1", pos=100, ref="A", alt="G", gene="BRCA1",
        consequence="missense", clinvar="Uncertain_significance",
        af=0.001, depth=100, vaf=0.5, is_vus=True,
    )


class _FailingBackend(LLMBackend):
    def complete(self, prompt: str, system: str = "") -> str:
        raise RuntimeError("bad key")


class _FlakyBackend(LLMBackend):
    """Fails the first call, succeeds afterwards."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient 500")
        return "STANCE: VUS\nACMG_CODES: PM2\nREASONING: Uncertain significance."


class _GoodBackend(LLMBackend):
    def complete(self, prompt: str, system: str = "") -> str:
        return "STANCE: VUS\nACMG_CODES: PM2\nREASONING: Uncertain significance."


class TestAcmgDedup:
    def test_duplicate_pm2_does_not_inflate(self):
        res = compute_acmg_classification(["PM2", "PM2", "PM2"])
        assert res.classification == "VUS"

    def test_duplicate_ps4_does_not_inflate(self):
        res = compute_acmg_classification(["PS4", "PS4"])
        assert res.classification == "VUS"

    def test_distinct_codes_still_combine(self):
        res = compute_acmg_classification(["PS1", "PS2", "PM2"])
        assert res.classification == "Pathogenic"


class TestDebateFailureHandling:
    def test_total_failure_raises(self):
        with pytest.raises(DebateBackendError, match="All LLM calls failed"):
            debate_variant(_variant(), _FailingBackend())

    def test_partial_failure_warns_but_continues(self):
        result = debate_variant(_variant(), _FlakyBackend())
        assert "1 of 3" in result.consensus
        assert any("LLM call failed" in op.reasoning for op in result.opinions)

    def test_all_success_no_warning(self):
        result = debate_variant(_variant(), _GoodBackend())
        assert "persona calls failed" not in result.consensus


class TestVusSpelling:
    def test_stance_is_uppercase_vus(self):
        assert _extract_stance("Remains a Variant of Uncertain Significance (VUS).") == "VUS"
        assert _extract_stance("STANCE: vus") == "VUS"


class TestConfigValidation:
    def test_unknown_key_rejected_with_suggestion(self):
        runner = CliRunner()
        res = runner.invoke(main, ["config", "set", "anthropic_modle", "x"])
        assert res.exit_code != 0
        assert "Unknown config key" in res.output
        assert "Did you mean" in res.output

    def test_generic_model_key_explains_backend_models(self):
        runner = CliRunner()
        res = runner.invoke(main, ["config", "set", "model", "gpt-4o"])
        assert res.exit_code != 0
        assert "no generic" in res.output and "anthropic_model" in res.output

    def test_unknown_backend_rejected(self):
        runner = CliRunner()
        res = runner.invoke(main, ["config", "set", "llm", "anthropci"])
        assert res.exit_code != 0
        assert "Unknown backend" in res.output

    def test_keys_lists_valid_keys(self):
        runner = CliRunner()
        res = runner.invoke(main, ["config", "keys"])
        assert res.exit_code == 0
        assert "anthropic_model" in res.output
        assert "openrouter_model" in res.output


class TestSignaturesFile:
    def test_single_file_accepted(self, tmp_path):
        sig_file = tmp_path / "custom.yaml"
        sig_file.write_text(
            "id: custom\nname: Custom\nseverity: warning\n"
            "patterns: ['boom']\nexplanation: test\nsuggested_fix: fix it\n"
        )
        sigs = load_signatures(sig_file)
        assert len(sigs) == 1
        assert sigs[0].name == "Custom"

    def test_empty_dir_raises_helpful_error(self, tmp_path):
        with pytest.raises(ValueError, match="No signature files"):
            load_signatures(tmp_path)


class TestReportsEscaping:
    def test_html_escapes_variant_fields(self):
        v = Variant(
            chrom="1", pos=1, ref="<A>", alt="G", gene="<b>BRCA</b>",
            consequence="x", clinvar="<script>alert(1)</script>",
            af=None, depth=None, vaf=None,
        )
        html = generate_html_report([v])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&lt;b&gt;BRCA&lt;/b&gt;" in html


class TestQcUnits:
    def test_generic_metrics_have_units(self, tmp_path):
        qc = tmp_path / "q.txt"
        qc.write_text("mapping rate: 92.5\nmean coverage: 45\n")
        metrics = QCParser.parse(qc)
        by_name = {m.name: m.value for m in metrics}
        assert by_name["Mapping Rate"].endswith("%")
        assert by_name["Mean Coverage"].endswith("x")


class TestDoctorStatus:
    def _checks(self, statuses):
        return [DiagnosticCheck("c", f"n{i}", s, "d") for i, s in enumerate(statuses)]

    def test_missing_means_action_needed(self):
        checks = self._checks(["OK", "MISSING"])
        assert overall_status(checks) == "action-needed"

    def test_no_llm_is_ready_no_llm(self):
        checks = [
            DiagnosticCheck("LLM Config", "Configured Backend", "INFO", "none"),
        ]
        assert overall_status(checks) == "ready-no-llm"


class TestBareCommand:
    def test_no_args_shows_quickstart_not_tui(self):
        runner = CliRunner()
        res = runner.invoke(main, [])
        assert res.exit_code == 0
        assert "ngsagent demo" in res.output

    def test_demo_runs_offline(self):
        runner = CliRunner()
        res = runner.invoke(main, ["demo"])
        assert res.exit_code == 0
        assert "Variant Report" in res.output

    def test_examples_lists_recipes(self):
        runner = CliRunner()
        res = runner.invoke(main, ["examples"])
        assert res.exit_code == 0
        assert "ngsagent debate" in res.output

    def test_missing_vcf_hint(self):
        runner = CliRunner()
        res = runner.invoke(main, ["analyze", "does-not-exist.vcf"])
        assert res.exit_code != 0
        assert "ngsagent demo" in res.output
