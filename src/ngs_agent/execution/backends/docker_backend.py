from __future__ import annotations

import shutil

from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.models import CommandResult, CommandSpec


class DockerBackend(ExecutionBackend):
    name = "docker"

    def is_available(self) -> bool:
        return shutil.which("docker") is not None

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        raise NotImplementedError(
            "Docker backend scaffold is in place but tool wrapping is not implemented yet. "
            "Use native backend (default) or implement image mapping in execution/backends/docker_backend.py."
        )
