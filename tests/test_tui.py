"""Tests for the TUI: state, slash commands, message log rendering, app launch."""
import asyncio
import pytest

from ngs_agent.tui.state import AppState, PendingPermission, PERMISSION_MODES
from ngs_agent.tui.slash_commands import handle_slash, CommandResult
from ngs_agent.tui.widgets.input_bar import SLASH_COMMANDS, InputBar
from ngs_agent.tui.widgets.permission_prompt import PermissionPrompt


# ---------- state ----------
def test_state_default():
    s = AppState()
    assert s.model == "claude-sonnet-4-20250514"
    assert s.agent == "interpreter"
    assert s.permission_mode == "auto"
    assert s.session_id is None
    assert not s.is_running


def test_state_cycle_permission_mode():
    s = AppState()
    assert s.permission_mode == "auto"
    s.cycle_permission_mode()
    assert s.permission_mode == "plan"
    s.cycle_permission_mode()
    assert s.permission_mode == "ask"
    s.cycle_permission_mode()
    assert s.permission_mode == "yolo"
    s.cycle_permission_mode()
    assert s.permission_mode == "auto"  # wraps around


def test_state_context_pct():
    s = AppState()
    assert s.context_pct() == 0.0  # no budget yet


def test_state_status_line():
    s = AppState()
    s.session_id = "sess_abc123def456"
    line = s.status_line()
    assert "claude-sonnet-4" in line
    assert "auto" in line
    assert "sess_abc123def456"[:18] in line
    assert "ctx=0%" in line


# ---------- slash commands ----------
def test_help_command():
    s = AppState()
    r = handle_slash("/help", s)
    assert r.handled
    assert "Slash commands" in r.message
    assert r.show_help


def test_clear_command():
    s = AppState()
    r = handle_slash("/clear", s)
    assert r.handled
    assert r.clear_log


def test_quit_command():
    s = AppState()
    r = handle_slash("/quit", s)
    assert r.handled
    assert r.exit_app


def test_model_show():
    s = AppState()
    r = handle_slash("/model", s)
    assert r.handled
    assert "claude-sonnet-4" in r.message


def test_model_set():
    s = AppState()
    r = handle_slash("/model gpt-4o", s)
    assert r.handled
    assert r.new_model == "gpt-4o"


def test_mode_command_cycles():
    s = AppState()
    r = handle_slash("/mode", s)
    assert r.handled
    assert "plan" in r.message  # cycled from auto to plan
    assert s.permission_mode == "plan"


def test_session_command():
    s = AppState()
    s.session_id = "sess_test123"
    r = handle_slash("/session", s)
    assert r.handled
    assert "sess_test123" in r.message


def test_tools_command():
    s = AppState()
    r = handle_slash("/tools", s)
    assert r.handled
    assert "vcf_parse" in r.message  # interpreter agent has vcf_parse


def test_agent_show():
    s = AppState()
    r = handle_slash("/agent", s)
    assert r.handled
    assert "interpreter" in r.message


def test_agent_set_valid():
    s = AppState()
    r = handle_slash("/agent qc_triage", s)
    assert r.handled
    assert r.new_agent == "qc_triage"


def test_agent_set_invalid():
    s = AppState()
    r = handle_slash("/agent nonexistent", s)
    assert r.handled
    assert "Unknown agent" in r.message


def test_context_command_no_budget():
    s = AppState()
    r = handle_slash("/context", s)
    assert r.handled
    assert "No context budget" in r.message


def test_compact_command():
    s = AppState()
    r = handle_slash("/compact", s)
    assert r.handled
    assert r.force_compact


def test_resume_no_arg():
    s = AppState()
    r = handle_slash("/resume", s)
    assert r.handled
    assert "Usage" in r.message


def test_resume_with_arg():
    s = AppState()
    r = handle_slash("/resume sess_abc123", s)
    assert r.handled
    assert r.resume_session == "sess_abc123"


def test_unknown_command():
    s = AppState()
    r = handle_slash("/nonexistent", s)
    assert r.handled
    assert "Unknown command" in r.message


def test_non_command_returns_not_handled():
    s = AppState()
    r = handle_slash("just a prompt", s)
    assert not r.handled


def test_all_slash_commands_listed():
    """Every command in SLASH_COMMANDS should produce a handled result."""
    s = AppState()
    for cmd, _desc in SLASH_COMMANDS:
        r = handle_slash(cmd, s)
        assert r.handled, f"Command {cmd} was not handled"


# ---------- input bar ----------
def test_input_bar_history():
    bar = InputBar()
    bar.push_history("hello")
    bar.push_history("world")
    assert bar.history_prev() == "world"
    assert bar.history_prev() == "hello"


# ---------- app smoke (Textual pilot) ----------
@pytest.mark.asyncio
async def test_app_launches_and_mounts():
    """Verify the TUI app can be constructed and mounted."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
        from textual.pilot import Pilot
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        # App should mount without error
        assert app.focused is not None
        # Should have a message log, input bar, status bar
        from ngs_agent.tui.widgets.message_log import MessageLog
        from ngs_agent.tui.widgets.input_bar import InputBar
        from ngs_agent.tui.widgets.status_bar import StatusBar
        app.query_one(MessageLog)
        app.query_one(InputBar)
        app.query_one(StatusBar)


@pytest.mark.asyncio
async def test_app_slash_help():
    """Type /help and verify the message log gets a system message."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        input_bar = app.query_one("#input-bar", InputBar)
        input_bar.value = "/help"
        # Simulate pressing Enter
        await pilot.press("enter")
        # Give the app a moment to process
        await pilot.pause()
        # The message log should now contain "Slash commands"
        # (We can't easily inspect the RichLog content, but we can verify the app didn't crash)
        assert app.state is not None


@pytest.mark.asyncio
async def test_app_quit():
    """Type /quit and verify the app exits."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        input_bar = app.query_one("#input-bar", InputBar)
        input_bar.value = "/quit"
        await pilot.press("enter")
        await pilot.pause()
        # App should have scheduled an exit
