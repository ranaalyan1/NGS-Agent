"""InputBar — multiline input with slash command support.

Behavior:
  - Enter sends the message
  - Shift+Enter inserts a newline (textual Input doesn't natively support
    multiline; we use a TextArea for that)
  - / at start opens slash command completion popup
  - Up arrow recalls last prompt (history)
"""
from __future__ import annotations

from textual.widgets import Input

from ..state import PERMISSION_MODES


SLASH_COMMANDS = [
    ("/help", "Show this help"),
    ("/clear", "Clear the message log"),
    ("/quit", "Exit ngsagent"),
    ("/model", "Show / switch model"),
    ("/mode", "Cycle permission mode (auto/plan/ask/yolo)"),
    ("/tools", "List available tools"),
    ("/session", "Show current session info"),
    ("/context", "Show context window usage"),
    ("/compact", "Force context compaction now"),
    ("/resume", "Resume a session by ID"),
    ("/fork", "Fork the current session"),
    ("/agent", "Switch active agent"),
    ("/doctor", "Run doctor diagnostic"),
]


class InputBar(Input):
    """Single-line input with slash command awareness."""

    DEFAULT_CSS = """
    InputBar {
        border: solid #ff8c00;
        background: $surface;
        padding: 0 1;
    }
    InputBar:focus {
        border: solid #ff8c00;
        background: $boost;
    }
    InputBar .input--cursor {
        background: #ff8c00;
        color: $text;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            placeholder="Ask Nibi anything...  e.g. 'interpret variants.vcf'  (or /help)",
            **kwargs,
        )
        self._history: list[str] = []
        self._history_idx: int = -1

    def push_history(self, text: str) -> None:
        if text and (not self._history or self._history[-1] != text):
            self._history.append(text)
        self._history_idx = -1

    def history_prev(self) -> str | None:
        if not self._history:
            return None
        if self._history_idx == -1:
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        if 0 <= self._history_idx < len(self._history):
            return self._history[self._history_idx]
        return None

    def history_next(self) -> str | None:
        if not self._history:
            return None
        if self._history_idx == -1:
            return None
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            return self._history[self._history_idx]
        self._history_idx = -1
        return ""
