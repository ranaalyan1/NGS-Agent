"""Integration and command tests for Click CLI."""

from pathlib import Path
from click.testing import CliRunner
import pytest

from ngs_agent.cli import main

DATA_DIR = Path(__file__).parent / "data"


class TestCLICommands:
    def test_cli_version(self):
        runner = CliRunner()
        res = runner.invoke(main, ["--version"])
        assert res.exit_code == 0
        assert "ngs-agent" in res.output or "version" in res.output.lower()

    def test_cli_doctor(self):
        runner = CliRunner()
        res = runner.invoke(main, ["doctor"])
        assert res.exit_code == 0
        assert "Doctor" in res.output or "Runtime" in res.output

    def test_cli_plan(self):
        runner = CliRunner()
        res = runner.invoke(main, ["plan", "Analyze", "RNA-Seq", "samples"])
        assert res.exit_code == 0
        assert "Pipeline Execution Plan" in res.output

    def test_cli_config_show(self):
        runner = CliRunner()
        res = runner.invoke(main, ["config", "show"])
        assert res.exit_code == 0

    def test_cli_analyze_simple_vcf(self):
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        runner = CliRunner()
        res = runner.invoke(main, ["analyze", str(vcf_path)])
        assert res.exit_code == 0
        assert "Variant Report" in res.output
        assert "BRCA1" in res.output

    def test_cli_analyze_with_html_export(self, tmp_path):
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        out_html = tmp_path / "report.html"
        runner = CliRunner()
        res = runner.invoke(main, ["analyze", str(vcf_path), "--html", str(out_html)])
        assert res.exit_code == 0
        assert out_html.exists()
