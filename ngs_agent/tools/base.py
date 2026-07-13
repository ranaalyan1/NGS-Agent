"""BaseTool interface — ported from OpenCode's tools.BaseTool.

Every tool — built-in or MCP-bridged — implements this. The agent loop is
agnostic to the tool's origin.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..runtime.events import EventBus
from ..runtime.permission import PermissionPolicy


@dataclass
class ToolInfo:
    name: str
    description: str
    parameters: dict[str, Any]      # JSON Schema
    required: list[str] = field(default_factory=list)
    deferred: bool = False          # Zero pattern: hidden until tool_search loads it


@dataclass
class ToolResponse:
    content: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None
    attachments: list[dict] | None = None


@dataclass
class ToolContext:
    session_id: str
    cwd: str
    permission: PermissionPolicy
    file_tracker: Any               # FileTracker or None
    bus: EventBus
    timeout_s: float = 120.0
    # v0.5: per-session evidence graph (None if not enabled)
    evidence_graph: Any = None      # EvidenceGraph or None


class BaseTool(ABC):
    """Contract every tool must implement."""

    @abstractmethod
    def info(self) -> ToolInfo:
        """Return static schema for this tool. Called once at registration."""

    @abstractmethod
    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        """Execute the tool with validated params. Must not raise — return
        ToolResponse(is_error=True) for failures so the loop can feed the
        error back to the LLM as a tool_result."""
