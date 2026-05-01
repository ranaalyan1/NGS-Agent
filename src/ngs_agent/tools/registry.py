from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from ngs_agent.tools.base import Tool, ToolContext
from ngs_agent.tools.builtins.bioinformatics_tools import register_bioinformatics_tools


@dataclass
class ToolMetadata:
    name: str
    description: str
    safety_level: str
    estimated_cost: str
    dry_run_support: bool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool[BaseModel, BaseModel]] = {}

    def register(self, tool: Tool[BaseModel, BaseModel]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[BaseModel, BaseModel]:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Tool not found: {name}")
        return tool

    def list_metadata(self) -> list[ToolMetadata]:
        return [
            ToolMetadata(
                name=tool.name,
                description=tool.description,
                safety_level=tool.safety_level.value,
                estimated_cost=tool.estimated_cost,
                dry_run_support=tool.dry_run_support,
            )
            for tool in self._tools.values()
        ]

    def execute(
        self,
        name: str,
        payload: dict,
        context: ToolContext,
        confirm_callback: Callable[[str], bool] | None = None,
    ) -> BaseModel:
        tool = self.get(name)
        return tool.run(payload, context, confirm_callback)


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    return register_bioinformatics_tools(registry)
