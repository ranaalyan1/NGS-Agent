"""MessageLog — scrollable log of conversation + tool calls.

Renders each turn as a card:
  - user message (cyan, prefixed with ❯)
  - assistant text (default color, streaming)
  - tool calls as collapsible cards with name + args + result
  - errors in red

Uses RichLog underneath for incremental writes.
"""
from __future__ import annotations

import json
from typing import Any

from rich.panel import Panel
from rich.text import Text
from textual.widgets import RichLog

from ...runtime.messages import Message


class MessageLog(RichLog):
    """Scrollable log of conversation turns + tool calls."""

    DEFAULT_CSS = """
    MessageLog {
        border: solid $accent;
        background: $surface;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    MessageLog:focus {
        border: solid $accent;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=True, wrap=True, auto_scroll=True, **kwargs)

    # ---------- public API ----------
    def add_user_message(self, text: str) -> None:
        self.write(Text("❯ ", style="bold cyan"), shrink=False)
        self.write(Text(text, style="cyan"))

    def add_assistant_chunk(self, text: str) -> None:
        """Stream a chunk of assistant text — no card, just append."""
        self.write(Text(text), shrink=False)

    def add_assistant_done(self) -> None:
        self.write("")

    def add_tool_call_start(self, tool_call_id: str, name: str, args: dict) -> None:
        args_str = json.dumps(args)
        if len(args_str) > 120:
            args_str = args_str[:120] + "…"
        self.write(
            Panel(
                f"[bold yellow]→ {name}[/bold yellow]\n"
                f"[dim]{args_str}[/dim]",
                border_style="yellow",
                title=f"tool_call {tool_call_id[:8]}",
                title_align="left",
                padding=(0, 1),
            )
        )

    def add_tool_result(
        self, tool_call_id: str, name: str, content: str, is_error: bool
    ) -> None:
        color = "red" if is_error else "green"
        symbol = "✗" if is_error else "✓"
        # Truncate very long results
        preview = content if len(content) <= 800 else content[:800] + "…"
        self.write(
            Panel(
                f"[{color}]{symbol} {name}[/{color}]\n{preview}",
                border_style=color,
                title=f"result {tool_call_id[:8]}",
                title_align="left",
                padding=(0, 1),
            )
        )

    def add_system(self, text: str) -> None:
        self.write(Text(text, style="dim italic"))

    def add_error(self, text: str) -> None:
        self.write(Panel(text, border_style="red", title="error", title_align="left"))

    def add_compaction(self, reason: str) -> None:
        self.write(
            Panel(
                f"[magenta]Compacting context[/magenta]\n[dim]{reason}[/dim]",
                border_style="magenta",
                title="compaction",
                title_align="left",
            )
        )

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.write(
            Text(
                f"  ↳ tokens: in={input_tokens} out={output_tokens}",
                style="dim",
            )
        )

    def add_session_info(self, session_id: str, model: str, agent: str) -> None:
        self.write(
            Panel(
                f"[bold]Session:[/bold] {session_id}\n"
                f"[bold]Agent:[/bold]   {agent}\n"
                f"[bold]Model:[/bold]   {model}",
                border_style="cyan",
                title="session start",
                title_align="left",
            )
        )

    def add_turn_separator(self, turn: int) -> None:
        self.write(Text(f"── turn {turn} ──", style="dim"))
