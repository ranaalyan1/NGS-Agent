from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from ngs_agent.tools.permissions import PermissionPolicy, SafetyLevel

if False:  # pragma: no cover
    from rich.console import Console

    from ngs_agent.execution.selector import BackendSelector

InputModelT = TypeVar("InputModelT", bound=BaseModel)
OutputModelT = TypeVar("OutputModelT", bound=BaseModel)


@dataclass
class ToolContext:
    dry_run: bool
    permission_policy: PermissionPolicy
    backend_selector: Any | None = None
    backend_preference: str = "auto"
    console: Any | None = None


class Tool(ABC, Generic[InputModelT, OutputModelT]):
    name: str
    description: str
    safety_level: SafetyLevel
    estimated_cost: str
    dry_run_support: bool
    input_model: type[InputModelT]
    output_model: type[OutputModelT]

    @abstractmethod
    def execute(self, payload: InputModelT, context: ToolContext) -> OutputModelT:
        raise NotImplementedError

    def run(
        self,
        payload: dict[str, Any],
        context: ToolContext,
        confirm_callback: Callable[[str], bool] | None = None,
    ) -> OutputModelT:
        parsed_payload = self.input_model.model_validate(payload)

        if context.permission_policy.requires_confirmation(self.safety_level):
            if confirm_callback is None:
                raise PermissionError(
                    f"Tool '{self.name}' requires confirmation due to safety level '{self.safety_level.value}'."
                )
            message = (
                f"Tool '{self.name}' requires confirmation. "
                f"Safety={self.safety_level.value}, estimated_cost={self.estimated_cost}. Continue?"
            )
            if not confirm_callback(message):
                raise PermissionError(f"Execution cancelled by user for tool '{self.name}'.")

        if context.dry_run and not self.dry_run_support:
            raise RuntimeError(
                f"Tool '{self.name}' does not support dry-run mode. "
                "Disable --dry-run or use a tool with dry-run support."
            )

        return self.execute(parsed_payload, context)
