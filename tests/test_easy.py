"""Tests for OpenCode-style usability: auto-detect, NL intents, init/run."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from ngs_agent import config as config_module
from ngs_agent.backends.base import NoBackend
from ngs_agent.backends.factory import get_backend
from ngs_agent.cli import main
from ngs_agent.detect import (
    detect_providers,
    find_logs,
    find_vcfs,
    resolve_input,
    summarize_project,
)
from ngs_agent.intent import parse_intent, to_command

DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Intent router
# ---------------------------------------------------------------------------


class TestParseIntent:
    def test_watch_keywords(self):
        assert parse_intent("check my pipeline log").action == "watch"
        assert parse_intent("what failed in the run?").action == "watch"
        assert parse_intent("scan the log for errors").action == "watch"

    def test_analyze_keywords(self):
        assert parse_intent("analyze my variants").action == "analyze"
        assert parse_intent("summarize the VCF report").action == "analyze"

    def test_debate_keywords(self):
        assert parse_intent("debate the VUS").action == "debate"
        assert parse_intent("is this pathogenic or benign?").action == "debate"

    def test_doctor_keywords(self):
        assert parse_intent("is my system ready?").action == "doctor"
        assert parse_intent("check my environment").action == "doctor"

    def test_init_keywords(self):
        assert parse_intent("set things up").action == "init"
        assert parse_intent("i want to get started").action == "init"
        # Leading explicit command still wins over setup words.
        assert parse_intent("help me get started").action == "help"

    def test_models_keywords(self):
        assert parse_intent("which model am I using?").action == "models"
        assert parse_intent("list providers").action == "models"

    def test_help_keywords(self):
        assert parse_intent("what can you do?").action == "help"

    def test_unknown_stays_unknown(self):
        assert parse_intent("blorple the wumpus framistat").action == "unknown"
        assert parse_intent("").action == "unknown"

    def test_explicit_subcommand_wins(self):
        intent = parse_intent("analyze pipeline.log")
        assert intent.action == "watch"  # file type disambiguates to log watcher
        assert intent.target == "pipeline.log"

    def test_file_extraction(self):
        assert parse_intent("analyze variants.vcf").target == "variants.vcf"
        assert parse_intent('check "my run.log" please').target == "my run.log"
        assert parse_intent("debate 'cohort.vcf.gz'").target == "cohort.vcf.gz"

    def test_gene_extraction(self):
        assert parse_intent("debate the VUS in BRCA2 --gene BRCA2").gene == "BRCA2"
        assert parse_intent("discuss gene tp53 please").gene == "TP53"

    def test_tail_detection(self):
        assert parse_intent("follow pipeline.log live").tail is True
        assert parse_intent("watch the log as it grows").tail is True
        assert parse_intent("scan the log").tail is False

    def test_vcf_hint_disambiguates(self):
        intent = parse_intent("look at cohort.vcf")
        assert intent.action == "analyze"
        assert intent.target == "cohort.vcf"

    def test_debate_beats_bare_vcf(self):
        intent = parse_intent("debate uncertain significance in cohort.vcf")
        assert intent.action == "debate"


class TestToCommand:
    def test_watch_with_tail(self):
        intent = parse_intent("follow run.log live")
        assert to_command(intent) == ["watch", "run.log", "--tail"]

    def test_debate_with_gene(self):
        intent = parse_intent("debate cohort.vcf gene BRCA1")
        assert to_command(intent) == ["debate", "cohort.vcf", "--gene", "BRCA1"]

    def test_help_returns_none(self):
        assert to_command(parse_intent("what can you do?")) is None


# ---------------------------------------------------------------------------
# File auto-detection
# ---------------------------------------------------------------------------


class TestFileDiscovery:
    def test_find_vcfs(self, tmp_path):
        (tmp_path / "a.vcf").write_text("x")
        (tmp_path / "b.txt").write_text("x")
        assert [p.name for p in find_vcfs(tmp_path)] == ["a.vcf"]

    def test_find_logs(self, tmp_path):
        (tmp_path / "run.log").write_text("x")
        (tmp_path / "a.vcf").write_text("x")
        assert [p.name for p in find_logs(tmp_path)] == ["run.log"]

    def test_summarize_project(self, tmp_path):
        (tmp_path / "a.vcf").write_text("x")
        (tmp_path / "run.log").write_text("x")
        summary = summarize_project(tmp_path)
        assert len(summary["vcfs"]) == 1
        assert len(summary["logs"]) == 1
        assert summary["has_project_config"] is False

    def test_resolve_single_auto(self, tmp_path):
        (tmp_path / "only.vcf").write_text("x")
        assert resolve_input(None, "vcf", cwd=tmp_path).name == "only.vcf"

    def test_resolve_explicit_passthrough(self, tmp_path):
        target = tmp_path / "given.vcf"
        target.write_text("x")
        assert resolve_input(target, "vcf", cwd=tmp_path) == target

    def test_resolve_none_exits(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            resolve_input(None, "vcf", cwd=tmp_path)
        assert exc.value.code == 2

    def test_resolve_multiple_noninteractive_exits(self, tmp_path):
        (tmp_path / "a.vcf").write_text("x")
        (tmp_path / "b.vcf").write_text("x")
        # Under pytest stdin is not a tty → friendly error, exit 2.
        with pytest.raises(SystemExit) as exc:
            resolve_input(None, "vcf", cwd=tmp_path)
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Provider detection + factory auto-pick
# ---------------------------------------------------------------------------


ALL_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                "OPENROUTER_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY")


def _only_env(monkeypatch, **keys):
    for var in ALL_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, val in keys.items():
        monkeypatch.setenv(var, val)


class TestProviders:
    def test_detect_env_key(self, monkeypatch):
        _only_env(monkeypatch, OPENAI_API_KEY="sk-test")
        providers = detect_providers({}, check_ollama=False)
        assert [p.id for p in providers] == ["openai"]
        assert providers[0].source == "env:OPENAI_API_KEY"

    def test_detect_priority_order(self, monkeypatch):
        _only_env(monkeypatch, GROQ_API_KEY="g", ANTHROPIC_API_KEY="a")
        providers = detect_providers({}, check_ollama=False)
        assert providers[0].id == "anthropic"

    def test_detect_config_key(self):
        providers = detect_providers({"gemini_api_key": "x"}, check_ollama=False)
        assert [p.id for p in providers] == ["gemini"]

    def test_factory_uses_env_key(self, monkeypatch):
        _only_env(monkeypatch, OPENAI_API_KEY="sk-test")
        backend = get_backend({"llm": "none"})
        assert not isinstance(backend, NoBackend)
        assert backend.model == "gpt-4o"

    def test_factory_none_when_nothing(self, monkeypatch):
        _only_env(monkeypatch)
        # factory imports ollama_reachable from ngs_agent.detect at call time,
        # so patching the source module takes effect.
        monkeypatch.setattr("ngs_agent.detect.ollama_reachable", lambda *a, **k: False)
        assert isinstance(get_backend({"llm": "none"}), NoBackend)


# ---------------------------------------------------------------------------
# Config: project overlay + non-interactive wizard
# ---------------------------------------------------------------------------


class TestConfigEasy:
    def test_project_overlay_wins(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "global.yaml")
        (tmp_path / ".ngsagent.yaml").write_text("llm: ollama\n")
        assert config_module.load_config()["llm"] == "ollama"

    def test_wizard_yes_picks_detected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / ".ngsagent")
        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / ".ngsagent" / "config.yaml")
        _only_env(monkeypatch, OPENAI_API_KEY="sk-test")
        cfg = config_module.run_wizard(yes=True)
        assert cfg["llm"] == "openai"


# ---------------------------------------------------------------------------
# CLI: run / init / optional args / suggestions
# ---------------------------------------------------------------------------


class TestEasyCLI:
    def test_run_analyze(self):
        runner = CliRunner()
        res = runner.invoke(main, ["run", "analyze", str(DATA_DIR / "simple_biallelic.vcf")])
        assert res.exit_code == 0
        assert "BRCA1" in res.output

    def test_run_doctor(self):
        runner = CliRunner()
        res = runner.invoke(main, ["run", "is my system ready?"])
        assert res.exit_code == 0
        assert "Runtime" in res.output or "Doctor" in res.output

    def test_run_unknown(self):
        runner = CliRunner()
        res = runner.invoke(main, ["run", "blorple the wumpus framistat"])
        assert res.exit_code == 2
        assert "couldn't tell" in res.output

    def test_run_missing_file(self):
        runner = CliRunner()
        res = runner.invoke(main, ["run", "analyze nope-missing.vcf"])
        assert res.exit_code == 2
        assert "not found" in res.output.lower()

    def test_analyze_no_args_autodetect(self, tmp_path, monkeypatch):
        shutil.copy(DATA_DIR / "simple_biallelic.vcf", tmp_path / "cohort.vcf")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        res = runner.invoke(main, ["analyze"])
        assert res.exit_code == 0
        assert "BRCA1" in res.output

    def test_watch_no_args_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        res = runner.invoke(main, ["watch"])
        assert res.exit_code == 2
        assert "No pipeline log found" in res.output

    def test_init_yes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / ".ngsagent")
        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / ".ngsagent" / "config.yaml")
        _only_env(monkeypatch, GEMINI_API_KEY="test-key")
        runner = CliRunner()
        res = runner.invoke(main, ["init", "--yes"])
        assert res.exit_code == 0
        assert "Setup complete" in res.output
        assert (tmp_path / ".ngsagent" / "config.yaml").exists()

    def test_models_lists_providers(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        runner = CliRunner()
        res = runner.invoke(main, ["models"])
        assert res.exit_code == 0
        assert "openai" in res.output
        assert "ready" in res.output

    def test_typo_suggests_command(self):
        runner = CliRunner()
        res = runner.invoke(main, ["analys"])
        assert res.exit_code == 2
        assert "analyze" in res.output

    def test_doctor_fix_creates_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / ".ngsagent")
        monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / ".ngsagent" / "config.yaml")
        runner = CliRunner()
        res = runner.invoke(main, ["doctor", "--fix"])
        assert res.exit_code == 0
        assert (tmp_path / ".ngsagent" / "config.yaml").exists()

    def test_pipeline_help(self):
        runner = CliRunner()
        res = runner.invoke(main, ["pipeline"])
        assert res.exit_code == 0
        assert "Swarm pipeline" in res.output
