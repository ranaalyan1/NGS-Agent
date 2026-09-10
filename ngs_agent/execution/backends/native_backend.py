from __future__ import annotations

from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.backends.process import run_streaming_process
from ngs_agent.execution.models import CommandResult, CommandSpec


class NativeBackend(ExecutionBackend):
    """Runs commands directly on the host (e.g. tools from a conda env)."""

    name = "native"

    def is_available(self) -> bool:
        return True

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        console_callback = None
        if spec.stream_output:

            def console_callback(line: str, stream_name: str) -> None:  # noqa: F811 - conditional redefinition
                style = "white" if stream_name == "stdout" else "yellow"
                console.print(line, style=style)

        result = run_streaming_process(
            spec.argv,
            console=console_callback,
            cwd=spec.cwd,
            env=spec.env or None,
            timeout_seconds=spec.timeout_seconds,
        )
        if result.timed_out:
            raise RuntimeError(
                f"Command timed out after {spec.timeout_seconds}s: {' '.join(spec.argv)}"
            )
        return CommandResult(
            backend=self.name,
            command=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
        )
