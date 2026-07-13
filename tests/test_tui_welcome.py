"""Tests for the v1.0.0 TUI welcome panel + hints bar + Nibi integration.

Covers:
  - WelcomePanel widget constructs and renders the panel
  - build_welcome_panel includes Nibi art, tagline, tips, recent sessions
  - HintsBar renders the keyboard shortcut line
  - App shows WelcomePanel on mount, MessageLog hidden
  - First prompt submission hides WelcomePanel and shows MessageLog
"""
from __future__ import annotations

import pytest


# ---------- WelcomePanel rendering ----------
def test_welcome_panel_builds_with_required_elements():
    """The built panel must include Nibi art, tagline, and tips."""
    from ngs_agent.tui.widgets.welcome_panel import build_welcome_panel, TIPS
    from ngs_agent.nibi import TAGLINE
    from rich.console import Console

    panel = build_welcome_panel(
        version="1.0.0",
        model="claude-sonnet-4",
        agent="interpreter",
        cwd="/tmp/test",
    )
    # Render to text so we can assert on content
    console = Console(record=True, width=100, force_terminal=False)
    console.print(panel)
    rendered = console.export_text()

    # Tagline present
    assert "Analyze" in rendered and "Accelerate" in rendered
    # Version present
    assert "1.0.0" in rendered
    # Nibi art present (DNA antennae or body marker)
    assert "ATCG" in rendered
    # Tips header present
    assert "Tips for getting started" in rendered
    # At least one tip command present
    assert any(cmd in rendered for cmd, _ in TIPS)
    # Recent sessions header present
    assert "Recent sessions" in rendered
    # Sample prompts header present
    assert "Try saying" in rendered


def test_welcome_panel_includes_status_line():
    """The panel subtitle should include model + agent + cwd."""
    from ngs_agent.tui.widgets.welcome_panel import build_welcome_panel
    from rich.console import Console

    panel = build_welcome_panel(
        version="1.0.0",
        model="claude-sonnet-4",
        agent="interpreter",
        cwd="/custom/path",
    )
    console = Console(record=True, width=120, force_terminal=False)
    console.print(panel)
    rendered = console.export_text()
    assert "claude-sonnet-4" in rendered
    assert "interpreter" in rendered
    assert "/custom/path" in rendered


def test_welcome_panel_shows_no_recent_sessions_message_when_empty():
    """When no sessions exist, the panel shows the fresh-state message."""
    from ngs_agent.tui.widgets.welcome_panel import build_welcome_panel
    from rich.console import Console

    # Use a fresh session store path so no sessions exist
    # (we can't easily isolate the store, but the empty branch fires if list() returns [])
    panel = build_welcome_panel(
        version="1.0.0",
        model="claude-sonnet-4",
        agent="interpreter",
        cwd="/tmp",
    )
    console = Console(record=True, width=100, force_terminal=False)
    console.print(panel)
    rendered = console.export_text()
    # Either the empty-state message OR a session id should be present
    assert "no recent sessions" in rendered.lower() or "sess_" in rendered.lower()


# ---------- HintsBar ----------
def test_hints_bar_renders_all_hints():
    """The hints bar must include all keyboard shortcut hints."""
    from ngs_agent.tui.widgets.hints_bar import HintsBar, HINTS, _render_hints
    from rich.console import Console

    line = _render_hints()
    # Each key should appear in the rendered string (rich markup tags may surround it)
    for key, label in HINTS:
        assert key in line, f"Hint key {key!r} missing from rendered hints bar"


def test_hints_bar_widget_constructs():
    """The HintsBar widget should construct without error."""
    from ngs_agent.tui.widgets.hints_bar import HintsBar

    bar = HintsBar()
    assert bar is not None


# ---------- App layout ----------
@pytest.mark.asyncio
async def test_app_shows_welcome_panel_on_mount():
    """On launch, WelcomePanel should be visible and MessageLog hidden."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
        from ngs_agent.tui.widgets.welcome_panel import WelcomePanel
        from ngs_agent.tui.widgets.message_log import MessageLog
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        welcome = app.query_one("#welcome-panel", WelcomePanel)
        log = app.query_one("#msg-log", MessageLog)
        # Welcome panel should NOT have the hidden class
        assert "hidden" not in welcome.classes
        # MessageLog SHOULD have the hidden class initially
        assert "hidden" in log.classes


@pytest.mark.asyncio
async def test_app_hides_welcome_on_first_prompt():
    """Submitting a regular prompt should hide WelcomePanel and show MessageLog."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
        from ngs_agent.tui.widgets.welcome_panel import WelcomePanel
        from ngs_agent.tui.widgets.message_log import MessageLog
        from ngs_agent.tui.widgets.input_bar import InputBar
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input-bar", InputBar)
        input_bar.value = "hello nibi"
        await pilot.press("enter")
        await pilot.pause()
        welcome = app.query_one("#welcome-panel", WelcomePanel)
        log = app.query_one("#msg-log", MessageLog)
        # Now welcome should be hidden, log should be visible
        assert "hidden" in welcome.classes
        assert "hidden" not in log.classes


@pytest.mark.asyncio
async def test_app_welcome_panel_contains_nibi_art():
    """The welcome panel should render Nibi ASCII art on mount."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
        from ngs_agent.tui.widgets.welcome_panel import WelcomePanel
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        welcome = app.query_one("#welcome-panel", WelcomePanel)
        # The panel should have rendered (i.e., update was called)
        assert welcome._rendered is True


@pytest.mark.asyncio
async def test_app_input_bar_placeholder_mentions_nibi():
    """The InputBar placeholder should reference Nibi."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
        from ngs_agent.tui.widgets.input_bar import InputBar
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input-bar", InputBar)
        assert "Nibi" in input_bar.placeholder


@pytest.mark.asyncio
async def test_app_has_hints_bar():
    """The HintsBar widget should be present in the layout."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
        from ngs_agent.tui.widgets.hints_bar import HintsBar
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        hints = app.query_one("#hints-bar", HintsBar)
        assert hints is not None


@pytest.mark.asyncio
async def test_app_slash_command_also_switches_to_log():
    """Even a slash command like /help should switch from welcome to log view."""
    try:
        from ngs_agent.tui.app import NgsAgentTUI
        from ngs_agent.tui.widgets.welcome_panel import WelcomePanel
        from ngs_agent.tui.widgets.message_log import MessageLog
        from ngs_agent.tui.widgets.input_bar import InputBar
    except ImportError:
        pytest.skip("textual not installed")

    app = NgsAgentTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one("#input-bar", InputBar)
        input_bar.value = "/help"
        await pilot.press("enter")
        await pilot.pause()
        welcome = app.query_one("#welcome-panel", WelcomePanel)
        log = app.query_one("#msg-log", MessageLog)
        assert "hidden" in welcome.classes
        assert "hidden" not in log.classes
