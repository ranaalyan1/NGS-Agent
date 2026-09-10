from __future__ import annotations

import shutil

from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.backends.containers import collect_bind_roots, resolve_image
from ngs_agent.execution.backends.process import run_streaming_process
from ngs_agent.execution.models import CommandResult, CommandSpec


class DockerBackend(ExecutionBackend):
    """Runs commands inside ephemeral Docker containers.

    Each command is wrapped as ``docker run --rm`` with the working directory
    and every absolute input/output path bind-mounted at the same path, so the
    wrapped tool sees an identical filesystem layout. Images are resolved from
    the pinned BioContainers map unless overridden via ``NGS_CONTAINER_IMAGE``
    or ``spec.metadata["image"]``.
    """

    name = "docker"

    def __init__(self, docker_binary: str = "docker") -> None:
        self._docker_binary = docker_binary

    def is_available(self) -> bool:
        return shutil.which(self._docker_binary) is not None

    def build_command(self, spec: CommandSpec) -> list[str]:
        """Return the full ``docker run ...`` argv (also used in tests)."""
        image = resolve_image(spec)
        if not image:
            raise RuntimeError(
                f"No container image configured for command "
                f"'{spec.argv[0] if spec.argv else '<empty>'}'. "
                f"Set the {self.name.upper()}_IMAGE/NGS_CONTAINER_IMAGE environment variable, add "
                f"'image' to the command metadata, or extend BIOCONTAINER_IMAGES."
            )
        wrapped: list[str] = [self._docker_binary, "run", "--rm"]
        if spec.cwd:
            wrapped.extend(["-v", f"{spec.cwd}:{spec.cwd}", "-w", spec.cwd])
        for root in collect_bind_roots(spec):
            wrapped.extend(["-v", f"{root}:{root}"])
        for key, value in spec.env.items():
            wrapped.extend(["-e", f"{key}={value}"])
        wrapped.append(image)
        wrapped.extend(spec.argv)
        return wrapped

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        wrapped = self.build_command(spec)
        console_callback = None
        if spec.stream_output:

            def _stream_to_console(line: str, stream_name: str) -> None:
                style = "white" if stream_name == "stdout" else "yellow"
                console.print(line, style=style)

            console_callback = _stream_to_console

        result = run_streaming_process(
            wrapped,
            console=console_callback,
            cwd=spec.cwd,
            timeout_seconds=spec.timeout_seconds,
        )
        if result.timed_out:
            raise RuntimeError(
                f"Docker command timed out after {spec.timeout_seconds}s: {' '.join(wrapped)}"
            )
        return CommandResult(
            backend=self.name,
            command=result.command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            metadata={"image": resolve_image(spec), "wrapped_command": wrapped},
        )
