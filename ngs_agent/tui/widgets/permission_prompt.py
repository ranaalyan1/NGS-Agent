"""PermissionPrompt — inline Y/N widget for tool approval.

When the agent loop requests permission for a tool call (bash, file_write,
vcf_annotate), this widget pops up at the bottom of the message log:

  ⚠ Allow bash("rm -rf /tmp/x")?
  [y] yes  [n] no  [a] always allow this tool this session
"""
from __future__ import annotations

import json
from typing import Any

from rich.panel import Panel
from textual.widgets import Static


class PermissionPrompt(Static):
    """Inline permission prompt."""

    DEFAULT_CSS = """
    PermissionPrompt {
        dock: bottom;
        height: auto;
        padding: 0 1;
        background: $warning 20%;
        border-top: solid $warning;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._visible = False

    def show_request(self, tool: str, args: dict) -> None:
        args_str = json.dumps(args)
        if len(args_str) > 200:
            args_str = args_str[:200] + "…"
        self.update(
            Panel(
                f"[bold yellow]⚠ Permission required[/bold yellow]\n\n"
                f"Tool: [bold]{tool}[/bold]\n"
                f"Args: [dim]{args_str}[/dim]\n\n"
                f"[green]y[/green]=yes  [red]n[/red]=no  "
                f"[blue]a[/blue]=always allow this session  [esc]=cancel",
                border_style="yellow",
                title="permission",
                title_align="left",
            )
        )
        self._visible = True
        self.display = True

    def hide(self) -> None:
        self._visible = False
        self.display = False
        self.update("")

    @property
    def is_visible(self) -> bool:
        return self._visible
