from __future__ import annotations

import shutil

from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.models import CommandResult, CommandSpec


class ApptainerBackend(ExecutionBackend):
    name = "apptainer"

    def is_available(self) -> bool:
        return shutil.which("apptainer") is not None or shutil.which("singularity") is not None

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        raise NotImplementedError(
            "Apptainer backend scaffold is in place but container command mapping is not implemented yet. "
            "Use native backend (default) or implement image mapping in execution/backends/apptainer_backend.py."
        )
