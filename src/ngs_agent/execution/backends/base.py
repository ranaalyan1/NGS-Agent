from __future__ import annotations

from abc import ABC, abstractmethod

from rich.console import Console

from ngs_agent.execution.models import CommandResult, CommandSpec


class ExecutionBackend(ABC):
    name: str

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        raise NotImplementedError
