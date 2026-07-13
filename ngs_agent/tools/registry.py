"""Tool registry with per-turn partitioning — ported from Zero's registry.go.

Partitioning: at the start of each turn, the loop calls partition_for_run()
which returns the list of tool schemas to expose to the LLM this turn. Tools
marked `deferred=True` are hidden unless explicitly loaded via tool_search
(or always-on rules below).

This keeps the prompt token count low when the registry is large (e.g. when
many MCP servers are attached).
"""
from __future__ import annotations

from collections.abc import Iterable

from .base import BaseTool

# Tools that are always exposed regardless of partition
ALWAYS_ON: tuple[str, ...] = (
    "task",            # subagent dispatch
    "emit_verdict",    # final-emit for interpreter agent
    # add others as needed
)


class Registry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        info = tool.info()
        if info.name in self._tools:
            raise ValueError(f"Tool already registered: {info.name}")
        self._tools[info.name] = tool

    def register_many(self, tools: Iterable[BaseTool]) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def partition_for_run(
        self,
        permission_mode: str = "auto",
        loaded: set[str] | None = None,
    ) -> list[BaseTool]:
        """Return tools to expose to the LLM this turn.

        - ALWAYS_ON tools are always included
        - Non-deferred tools are always included
        - Deferred tools are included only if in `loaded` set
        """
        loaded = loaded or set()
        out: list[BaseTool] = []
        for name, tool in self._tools.items():
            info = tool.info()
            if name in ALWAYS_ON:
                out.append(tool)
                continue
            if not info.deferred:
                out.append(tool)
                continue
            if name in loaded:
                out.append(tool)
        return out

    def tool_definitions_for_provider(
        self,
        tools: list[BaseTool],
    ) -> list[dict]:
        """Render the tool list in provider-agnostic JSON-schema form.
        Backends translate this to their own tool-use schema."""
        defs: list[dict] = []
        for t in tools:
            info = t.info()
            defs.append({
                "name": info.name,
                "description": info.description,
                "input_schema": {
                    "type": "object",
                    "properties": info.parameters,
                    "required": info.required,
                },
            })
        return defs
