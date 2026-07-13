"""StatusBar — bottom bar showing model / session / mode / context usage."""
from __future__ import annotations

from textual.widgets import Static

from ..state import AppState


# Mini Nibi faces keyed by runtime state. Tiny 1-line faces for the status bar.
_NIBI_FACES = {
    "idle": "(\u2009-\u2009-\u2009)",      # sleeping
    "ready": "( o.o )",   # happy
    "thinking": "(\u2009-\u2009-\u2009)",
    "analyzing": "( O.O )",
    "running": "( > < )",
    "tool_call": "( > < )",
    "tool_result": "( O.O )",
    "success": "( ^ ^ )",
    "error": "( x x )",
    "permission": "( ? ? )",
    "long_running": "( ~ ~ )",
    "compacting": "(\u2009-\u2009-\u2009)",
}


def _nibi_face(state: AppState) -> str:
    """Pick a Nibi face based on current app state."""
    if state.pending_permission:
        return _NIBI_FACES["permission"]
    if state.is_running:
        return _NIBI_FACES["running"]
    if state.turns_completed > 0:
        return _NIBI_FACES["ready"]
    return _NIBI_FACES["idle"]


class StatusBar(Static):
    """Bottom status bar."""

    DEFAULT_CSS = """
    StatusBar {
        background: $accent 50%;
        color: $text;
        padding: 0 1;
        dock: bottom;
        height: 1;
    }
    """

    def __init__(self, state: AppState, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._state = state

    def refresh_status(self) -> None:
        pct = self._state.context_pct()
        # Color the context meter
        if pct < 60:
            ctx_color = "green"
        elif pct < 85:
            ctx_color = "yellow"
        else:
            ctx_color = "red"

        sid = (self._state.session_id or "none")[:18]
        mode = self._state.permission_mode
        running = "\u25cf" if self._state.is_running else "\u25cb"
        face = _nibi_face(self._state)

        self.update(
            f"[bold orange3]{face}[/bold orange3] "
            f"[bold]{running}[/bold] "
            f"model=[cyan]{self._state.model}[/cyan] | "
            f"session=[dim]{sid}[/dim] | "
            f"mode=[yellow]{mode}[/yellow] | "
            f"ctx=[{ctx_color}]{pct:.0f}%[/{ctx_color}] | "
            f"turns=[bold]{self._state.turns_completed}[/bold]"
        )
