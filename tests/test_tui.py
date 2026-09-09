"""Tests for the Claude Code-style interactive terminal (ngs_agent.tui)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

import ngs_agent.cli as cli_module
from ngs_agent import tui
from ngs_agent.tui import (
    COMMANDS,
    SLASH_COMMANDS,
    THEMES,
    _pt_color,
    complete_line,
    complete_path,
    dispatch,
    handle_slash,
    render_banner,
    render_nibi_mini,
    render_status_bar,
    run_subcommand,
    show_help,
)

DATA_DIR = Path(__file__).parent / "data"


def make_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    con = Console(file=buf, width=120, force_terminal=False, legacy_windows=False)
    return con, buf


def texts(completions: list) -> list[str]:
    return [c.text for c in completions]


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Keep the REPL away from the developer's real ~/.ngsagent config."""
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr(tui, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(tui, "load_config", lambda: {"llm": "none", "theme": "dark"})
    monkeypatch.setattr(tui, "save_config", lambda cfg: cfg_path.write_text("ok"))
    monkeypatch.setattr(tui, "HISTORY_PATH", tmp_path / "history")
    return cfg_path


@pytest.fixture
def captured_cli_console(monkeypatch):
    """Route output from CLI commands into a capture buffer."""
    con, buf = make_console()
    monkeypatch.setattr(cli_module, "console", con)
    return con, buf


# ---------------------------------------------------------------------------
# Command catalogue
# ---------------------------------------------------------------------------


class TestCatalogue:
    def test_all_core_subcommands_registered(self):
        for name in ("watch", "analyze", "debate", "doctor", "plan", "config", "tui"):
            assert name in COMMANDS

    def test_slash_commands_covered(self):
        names = [n for n, _ in SLASH_COMMANDS]
        for needed in ("/help", "/files", "/status", "/model", "/theme", "/clear", "/exit"):
            assert needed in names

    def test_themes_exist(self):
        assert "dark" in THEMES
        assert "light" in THEMES
        for theme in THEMES.values():
            for key in ("accent", "border", "muted", "prompt"):
                assert key in theme


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


class TestCompleteLine:
    def test_empty_line_lists_subcommands(self, tmp_path):
        got = texts(complete_line("", cwd=tmp_path))
        assert "watch" in got and "analyze" in got and "debate" in got

    def test_partial_subcommand(self, tmp_path):
        got = texts(complete_line("ana", cwd=tmp_path))
        assert got == ["analyze"]

    def test_slash_commands(self, tmp_path):
        got = texts(complete_line("/", cwd=tmp_path))
        assert "/help" in got and "/exit" in got

    def test_partial_slash_command(self, tmp_path):
        got = texts(complete_line("/th", cwd=tmp_path))
        assert got == ["/theme"]

    def test_watch_prefers_logs(self, tmp_path):
        (tmp_path / "run.log").write_text("x")
        (tmp_path / "vars.vcf").write_text("x")
        got = texts(complete_line("watch ", cwd=tmp_path))
        assert "run.log" in got
        assert "vars.vcf" not in got

    def test_analyze_prefers_vcfs(self, tmp_path):
        (tmp_path / "run.log").write_text("x")
        (tmp_path / "vars.vcf").write_text("x")
        got = texts(complete_line("analyze ", cwd=tmp_path))
        assert "vars.vcf" in got
        assert "run.log" not in got

    def test_analyze_fragment_filters(self, tmp_path):
        (tmp_path / "vars.vcf").write_text("x")
        (tmp_path / "run.log").write_text("x")
        got = texts(complete_line("analyze va", cwd=tmp_path))
        assert got == ["vars.vcf"]

    def test_nested_path_completion(self, tmp_path):
        sub = tmp_path / "demo_data"
        sub.mkdir()
        (sub / "sample.vcf").write_text("x")
        (sub / "sample.log").write_text("x")
        got = texts(complete_line("analyze demo_data/sam", cwd=tmp_path))
        assert got == ["sample.vcf"]

    def test_directory_completion_offers_slash(self, tmp_path):
        (tmp_path / "demo_data").mkdir()
        got = texts(complete_line("analyze dem", cwd=tmp_path))
        assert "demo_data/" in got

    def test_flag_completion(self, tmp_path):
        got = texts(complete_line("analyze --", cwd=tmp_path))
        assert sorted(got) == ["--html", "--qc"]

    def test_flag_value_completion_trailing_space(self, tmp_path):
        got = texts(complete_line("plan --workflow ", cwd=tmp_path))
        assert sorted(got) == ["auto", "rnaseq", "wes", "wgs"]

    def test_flag_value_completion_prefix(self, tmp_path):
        got = texts(complete_line("plan --workflow w", cwd=tmp_path))
        assert sorted(got) == ["wes", "wgs"]

    def test_config_subcommands(self, tmp_path):
        got = texts(complete_line("config s", cwd=tmp_path))
        assert sorted(got) == ["set", "show"]

    def test_config_set_keys(self, tmp_path):
        got = texts(complete_line("config set ol", cwd=tmp_path))
        assert "ollama_model" in got and "ollama_host" in got

    def test_hidden_files_skipped(self, tmp_path):
        (tmp_path / ".secret.vcf").write_text("x")
        (tmp_path / "real.vcf").write_text("x")
        got = texts(complete_line("analyze ", cwd=tmp_path))
        assert ".secret.vcf" not in got
        assert "real.vcf" in got

    def test_generic_commands_complete_any_path(self, tmp_path):
        (tmp_path / "notes.md").write_text("x")
        got = texts(complete_line("config ", cwd=tmp_path))
        assert "notes.md" in got


class TestCompletePath:
    def test_home_shorthand(self, tmp_path):
        got = texts(complete_path("~", cwd=tmp_path, exts=None))
        assert got == ["~/"]

    def test_no_exts_filter_lists_all_files(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.bin").write_text("x")
        got = texts(complete_path("", cwd=tmp_path, exts=None))
        assert "a.txt" in got and "b.bin" in got

    def test_dirs_listed_even_with_ext_filter(self, tmp_path):
        (tmp_path / "sub").mkdir()
        got = texts(complete_path("", cwd=tmp_path, exts=(".vcf",)))
        assert "sub/" in got


# ---------------------------------------------------------------------------
# In-process dispatch
# ---------------------------------------------------------------------------


class TestRunSubcommand:
    def test_analyze_runs_in_process(self, captured_cli_console):
        con, buf = captured_cli_console
        rc = run_subcommand(
            ["analyze", str(DATA_DIR / "simple_biallelic.vcf")], con, THEMES["dark"]
        )
        assert rc == 0
        assert "Variant Report" in buf.getvalue()

    def test_unknown_command_returns_usage_error(self):
        con, buf = make_console()
        rc = run_subcommand(["frobnicate"], con, THEMES["dark"])
        assert rc == 2
        assert "frobnicate" in buf.getvalue()
        assert "/help" in buf.getvalue()

    def test_missing_file_argument(self):
        con, _ = make_console()
        rc = run_subcommand(["analyze", "/nonexistent/x.vcf"], con, THEMES["dark"])
        assert rc == 2

    def test_version_flag(self, captured_cli_console, capsys):
        con, _ = captured_cli_console
        rc = run_subcommand(["--version"], con, THEMES["dark"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "0.3" in captured.out


class TestDispatch:
    def test_empty_line_is_noop(self):
        con, _ = make_console()
        assert dispatch("   ", con, THEMES["dark"]) == 0

    def test_shell_escape_runs_command(self):
        con, _ = make_console()
        rc = dispatch("!echo hello-from-shell", con, THEMES["dark"])
        assert rc == 0

    def test_shell_escape_unknown_command(self):
        con, _ = make_console()
        rc = dispatch("!definitely-not-a-command-xyz", con, THEMES["dark"])
        assert rc == 127

    def test_tui_command_does_not_recurse(self):
        con, buf = make_console()
        rc = dispatch("tui", con, THEMES["dark"])
        assert rc == 0
        assert "interactive" in buf.getvalue()

    def test_success_indicator(self, captured_cli_console):
        con, buf = captured_cli_console
        dispatch("plan find biomarkers", con, THEMES["dark"])
        assert "✓" in buf.getvalue()

    def test_failure_indicator(self, captured_cli_console):
        con, buf = captured_cli_console
        dispatch("analyze /nonexistent/x.vcf", con, THEMES["dark"])
        assert "✗" in buf.getvalue()

    def test_parse_error_handled(self):
        con, buf = make_console()
        rc = dispatch('analyze "unterminated', con, THEMES["dark"])
        assert rc == 1
        assert "parse error" in buf.getvalue()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


class TestHandleSlash:
    def test_exit_stops_loop(self):
        con, _ = make_console()
        cont, _, _, _ = handle_slash("/exit", con, THEMES["dark"], {})
        assert cont is False

    def test_quit_stops_loop(self):
        con, _ = make_console()
        cont, _, _, _ = handle_slash("/quit", con, THEMES["dark"], {})
        assert cont is False

    def test_help_renders(self):
        con, buf = make_console()
        cont, _, _, _ = handle_slash("/help", con, THEMES["dark"], {})
        assert cont is True
        assert "watch" in buf.getvalue()
        assert "/files" in buf.getvalue()

    def test_unknown_slash_command(self):
        con, buf = make_console()
        cont, _, _, _ = handle_slash("/wat", con, THEMES["dark"], {})
        assert cont is True
        assert "Unknown slash command" in buf.getvalue()

    def test_theme_direct_arg(self, isolated_config):
        con, _ = make_console()
        cont, _, theme, cfg = handle_slash("/theme midnight", con, THEMES["dark"], {})
        assert cont is True
        assert theme is THEMES["midnight"]
        assert cfg["theme"] == "midnight"

    def test_theme_invalid_arg_falls_back_to_picker(self, monkeypatch):
        con, _ = make_console()
        monkeypatch.setattr(tui, "pick_theme", lambda console: "dark")
        cont, _, theme, _ = handle_slash("/theme nope", con, THEMES["dark"], {})
        assert cont is True
        assert theme is THEMES["dark"]

    def test_status_renders(self):
        con, buf = make_console()
        cfg = {"llm": "none", "anthropic_model": "claude-sonnet-4-20250514"}
        cont, _, _, _ = handle_slash("/status", con, THEMES["dark"], cfg)
        assert cont is True
        assert "claude-sonnet" in buf.getvalue()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_banner_renders(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        con, buf = make_console()
        render_banner(con, THEMES["dark"], {"llm": "none"})
        out = buf.getvalue()
        assert "NGS" in out
        assert "Quick start" in out
        assert "no LLM" in out

    def test_banner_uses_local_demo_data(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "demo_data").mkdir()
        (tmp_path / "demo_data" / "sample.vcf").write_text("x")
        con, buf = make_console()
        render_banner(con, THEMES["dark"], {"llm": "none"})
        assert "analyze demo_data/sample.vcf" in buf.getvalue()

    def test_banner_with_llm_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        con, buf = make_console()
        render_banner(con, THEMES["dark"], {"llm": "anthropic", "anthropic_model": "claude-x"})
        assert "anthropic" in buf.getvalue()

    def test_status_bar_shows_model(self):
        con, buf = make_console()
        render_status_bar(con, THEMES["dark"], {"llm": "ollama", "ollama_model": "llama3.2"})
        assert "llama3.2" in buf.getvalue()

    def test_help_renders_directly(self):
        con, buf = make_console()
        show_help(con, THEMES["light"])
        assert "Slash commands" in buf.getvalue()
        assert "Input shortcuts" in buf.getvalue()

    def test_mini_nibi_renders(self):
        art = render_nibi_mini(THEMES["dark"], "happy")
        plain = art.plain
        assert plain.count("\n") >= 6
        assert "◉" in plain
        assert "⊛" in plain

    def test_mini_nibi_expressions(self):
        for expr in ("thinking", "success", "error", "curious"):
            art = render_nibi_mini(THEMES["dark"], expr)  # type: ignore[arg-type]
            assert len(art.plain) > 0


# ---------------------------------------------------------------------------
# prompt_toolkit helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_pt_color_hex_passthrough(self):
        assert _pt_color("#00FF9C") == "#00FF9C"

    def test_pt_color_named(self):
        assert _pt_color("green") == "ansigreen"
        assert _pt_color("bright_green") == "ansibrightgreen"

    def test_pt_color_invalid_falls_back(self):
        assert _pt_color("sparkly_rainbow") == "ansiwhite"


# ---------------------------------------------------------------------------
# End-to-end: scriptable REPL over piped stdin
# ---------------------------------------------------------------------------


class TestScriptableRepl:
    def test_piped_commands_run_and_exit(
        self, tmp_path, monkeypatch, isolated_config, captured_cli_console
    ):
        monkeypatch.chdir(tmp_path)
        con, buf = captured_cli_console

        feed = iter(["plan test intent", "/exit"])
        monkeypatch.setattr(tui, "_read_line_fallback", lambda theme: next(feed, None))
        monkeypatch.setattr(tui.sys.stdin, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(tui.sys.stdout, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(tui, "render_banner", lambda *a, **k: None)
        monkeypatch.setattr(tui, "Console", lambda **kw: con)

        tui.run_tui()
        assert "Pipeline Execution Plan" in buf.getvalue()
        assert "Goodbye" in buf.getvalue()

    def test_piped_eof_exits(self, monkeypatch, isolated_config, captured_cli_console):
        con, buf = captured_cli_console
        feed = iter([None])  # immediate EOF
        monkeypatch.setattr(tui, "_read_line_fallback", lambda theme: next(feed, None))
        monkeypatch.setattr(tui.sys.stdin, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(tui.sys.stdout, "isatty", lambda: False, raising=False)
        monkeypatch.setattr(tui, "render_banner", lambda *a, **k: None)
        monkeypatch.setattr(tui, "Console", lambda **kw: con)

        tui.run_tui()  # must not raise
        assert "Goodbye" in buf.getvalue()
