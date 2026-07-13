"""WelcomePanel \u2014 the landing screen for the NGS-Agent TUI.

Inspired by Claude Code's welcome panel and OpenCode's minimalist landing:

  +---------------------------------------------------------------+
  |  NGS Agent v1.0.0                                             |
  |  Analyze \u2022 Automate \u2022 Accelerate                                |
  |                                                               |
  |     /\\  /\\              Tips for getting started               |
  |      |  |             - Run /help for slash commands            |
  |    .------.          - Try: interpret variants.vcf             |
  |   |  o  o  |         - Shift+Tab cycles permission mode        |
  |   |   __   |         - /mode auto | plan | ask | yolo          |
  |   |  (\u25c9)  |                                                   |
  |    \\______/          Recent sessions                            |
  |     |__|             - (no recent sessions)                     |
  |    ATCG~                                                       |
  |                                                               |
  |  Nibi is ready to analyze your genomic data!                 |
  |  model=claude-sonnet-4 | agent=interpreter | cwd=/path        |
  +---------------------------------------------------------------+

The panel is shown when no session is active, and hidden once the user
submits their first prompt.
"""
from __future__ import annotations

import os
from typing import Any

from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from ...nibi import DESIGN_DETAILS, TAGLINE, get_expression
from ...runtime.session import SessionStore


# Tips shown in the right column of the welcome panel.
TIPS: list[tuple[str, str]] = [
    ("/help", "List all slash commands"),
    ("interpret variants.vcf", "Run the interpreter agent on a VCF"),
    ("Shift+Tab", "Cycle permission mode (auto/plan/ask/yolo)"),
    ("/mode", "Show or switch permission mode"),
    ("/agent", "Switch agent (interpreter / qc_triage / title)"),
    ("/context", "Inspect context-window usage"),
    ("Ctrl+L", "Clear the conversation log"),
    ("/quit", "Exit Nibi"),
]


# Sample prompts shown below the tips.
SAMPLE_PROMPTS: list[str] = [
    "interpret demo_data/sample.vcf",
    "triage QC failures in demo_data/multiqc.txt",
    "diagnose the failure in demo_data/sample.log",
    "summarize ACMG classification for BRCA1 variants",
]


def _render_nibi_art() -> Text:
    """Render the Nibi 'happy' ASCII art as orange Rich Text."""
    art = get_expression("happy").rstrip("\n")
    return Text(art, style="bold orange3")


def _render_tips_table() -> Table:
    """Render the tips as a two-column Rich Table."""
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bold cyan", no_wrap=True)
    t.add_column(style="white", no_wrap=False)
    for cmd, desc in TIPS:
        t.add_row(cmd, desc)
    return t


def _render_recent_sessions() -> Text:
    """Render up to 3 recent sessions as a Rich Text block."""
    try:
        store = SessionStore()
        sessions = store.list(limit=3)
    except Exception:
        sessions = []

    if not sessions:
        return Text("  (no recent sessions \u2014 Nibi is fresh!)", style="dim italic")

    lines: list[str] = []
    for s in sessions:
        title = (s.title or "(untitled)")[:40]
        lines.append(f"  \u2022 {s.id[:12]}  {s.agent:<12}  {title}")
    return Text("\n".join(lines), style="white")


def _render_status_line(model: str, agent: str, cwd: str) -> Text:
    """Render the bottom status line: model | agent | cwd."""
    return Text(
        f"model={model}  |  agent={agent}  |  cwd={cwd}",
        style="dim",
    )


def build_welcome_panel(
    version: str,
    model: str,
    agent: str,
    cwd: str | None = None,
    expression: str = "happy",
) -> Panel:
    """Build the welcome panel as a Rich Panel.

    Layout (two columns inside the panel body):
      Left  : Nibi ASCII art (orange) + welcome line + tagline
      Right : "Tips for getting started" + "Recent sessions"
    Bottom : model / agent / cwd status line
    """
    cwd = cwd or os.getcwd()

    # Two-column table to host the left/right layout
    grid = Table.grid(expand=True, padding=(1, 2))
    grid.add_column(ratio=2, vertical="middle")  # left
    grid.add_column(ratio=3, vertical="top")     # right

    # Left cell: Nibi art + welcome text
    left_lines = Text.assemble(
        Text(f"NGS Agent v{version}\n", style="bold orange3"),
        Text(f"{TAGLINE}\n\n", style="dim"),
        _render_nibi_art(),
        Text("\n\n", style=""),
        Text("Nibi is ready to analyze your genomic data!\n", style="bold white"),
    )

    # Right cell: tips + recent sessions
    right_lines = Text.assemble(
        Text("Tips for getting started\n", style="bold orange3"),
        Text("(type any of these into the input bar below)\n\n", style="dim italic"),
    )
    # We'll use a sub-panel trick: render the tips table inside the right cell
    # by using Text.assemble with the table embedded via Text一段
    # Simpler: just join everything as text lines
    tips_text = Text("")
    for cmd, desc in TIPS:
        tips_text.append(Text(f"  {cmd:<22}", style="bold cyan"))
        tips_text.append(Text(f" {desc}\n", style="white"))

    recent_label = Text("\nRecent sessions\n", style="bold orange3")
    recent_body = _render_recent_sessions()
    recent_body.append(Text("\n"))

    # Sample prompts
    sample_label = Text("\nTry saying\n", style="bold orange3")
    sample_body = Text("")
    for p in SAMPLE_PROMPTS:
        sample_body.append(Text(f"  \u279c {p}\n", style="white"))

    right_combined = Text.assemble(
        Text("Tips for getting started\n", style="bold orange3"),
        Text("(type any of these into the input bar below)\n\n", style="dim italic"),
        tips_text,
        recent_label,
        recent_body,
        sample_label,
        sample_body,
    )

    grid.add_row(left_lines, right_combined)

    # Build the final panel with orange border and Nibi subtitle
    panel = Panel(
        grid,
        title=f"[bold orange3]NGS Agent v{version} \u2014 Nibi[/bold orange3]",
        subtitle=f"[dim]{_render_status_line(model, agent, cwd).plain}[/dim]",
        border_style="orange3",
        padding=(1, 2),
        expand=True,
    )
    return panel


class WelcomePanel(Static):
    """A Static widget that displays the welcome panel.

    The panel is shown on TUI launch and hidden once the user submits
    their first prompt (the app toggles its `display` CSS property).
    """

    DEFAULT_CSS = """
    WelcomePanel {
        layer: welcome;
        padding: 1 2;
        background: $surface;
        border: solid #ff8c00;
        color: $text;
        width: 1fr;
        height: 1fr;
        content-align: center middle;
    }
    WelcomePanel.hidden {
        display: none;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._rendered = False

    def refresh_panel(
        self,
        version: str,
        model: str,
        agent: str,
        cwd: str | None = None,
        expression: str = "happy",
    ) -> None:
        """Re-render the welcome panel with the given state."""
        panel = build_welcome_panel(
            version=version,
            model=model,
            agent=agent,
            cwd=cwd,
            expression=expression,
        )
        # Center the panel inside the widget
        self.update(Align.center(panel, vertical="middle"))
        self._rendered = True

    def hide(self) -> None:
        """Hide the welcome panel (call after the first user prompt)."""
        self.add_class("hidden")

    def show(self) -> None:
        """Show the welcome panel again (e.g., after /clear at top of session)."""
        self.remove_class("hidden")


__all__ = [
    "TIPS",
    "SAMPLE_PROMPTS",
    "WelcomePanel",
    "build_welcome_panel",
]
