"""HintsBar \u2014 a thin bottom bar showing keyboard shortcuts.

Inspired by OpenCode's bottom hint bar:
    ctrl+l clear  tab agents  shift+tab mode  /help commands

Shown just above the StatusBar so the user always knows the hotkeys.
"""
from __future__ import annotations

from textual.widgets import Static


HINTS: list[tuple[str, str]] = [
    ("/help", "commands"),
    ("shift+tab", "mode"),
    ("ctrl+l", "clear"),
    ("ctrl+c", "quit"),
    ("\u2191/\u2193", "history"),
]


def _render_hints() -> str:
    """Render the hints as a single styled line for a Static widget."""
    parts: list[str] = []
    for key, label in HINTS:
        parts.append(f"[bold orange3]{key}[/bold orange3] [dim]{label}[/dim]")
    return "    ".join(parts)


class HintsBar(Static):
    """Bottom hint bar \u2014 keyboard shortcuts."""

    DEFAULT_CSS = """
    HintsBar {
        background: $boost;
        color: $text;
        padding: 0 1;
        dock: bottom;
        height: 1;
        layer: hints;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(_render_hints(), **kwargs)


__all__ = ["HintsBar", "HINTS"]
