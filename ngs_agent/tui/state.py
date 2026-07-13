"""TUI state — single source of truth for the app."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runtime.context import ContextBudget


PERMISSION_MODES = ("auto", "plan", "ask", "yolo")


@dataclass
class PendingPermission:
    """An outstanding permission request from the agent loop."""

    session_id: str
    tool: str
    args: dict[str, Any]
    response: bool | None = None


@dataclass
class AppState:
    model: str = "claude-sonnet-4-20250514"
    agent: str = "interpreter"
    session_id: str | None = None
    permission_mode_idx: int = 0
    is_running: bool = False
    context_budget: ContextBudget | None = None
    pending_permission: PendingPermission | None = None
    turns_completed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    messages_since_compaction: int = 0

    @property
    def permission_mode(self) -> str:
        return PERMISSION_MODES[self.permission_mode_idx]

    def cycle_permission_mode(self) -> str:
        self.permission_mode_idx = (self.permission_mode_idx + 1) % len(PERMISSION_MODES)
        return self.permission_mode

    def context_pct(self) -> float:
        if not self.context_budget:
            return 0.0
        return self.context_budget.used_ratio * 100

    def status_line(self) -> str:
        pct = self.context_pct()
        return (
            f"model={self.model} | session={(self.session_id or 'none')[:18]} | "
            f"mode={self.permission_mode} | ctx={pct:.0f}% | "
            f"turns={self.turns_completed}"
        )
