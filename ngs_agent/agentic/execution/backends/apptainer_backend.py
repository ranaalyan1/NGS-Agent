from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from rich.console import Console

from ngs_agent.agentic.execution.backends.base import ExecutionBackend
from ngs_agent.agentic.execution.backends.images import resolve_image
from ngs_agent.agentic.execution.models import CommandResult, CommandSpec


class ApptainerBackend(ExecutionBackend):
    """Runs commands inside Apptainer/Singularity containers with the working directory bound."""

    name = "apptainer"

    def _binary(self) -> str | None:
        return shutil.which("apptainer") or shutil.which("singularity")

    def is_available(self) -> bool:
        return self._binary() is not None

    def _build_argv(self, spec: CommandSpec, binary: str) -> list[str]:
        workdir = Path(spec.cwd or os.getcwd()).resolve()
        image = resolve_image(spec.argv)
        # Apptainer pulls docker images transparently via the docker:// prefix.
        image_ref = image if "://" in image else f"docker://{image}"
        argv = [
            binary,
            "exec",
            "--bind",
            f"{workdir}:{workdir}",
            "--pwd",
            str(workdir),
            image_ref,
        ]
        argv.extend(spec.argv)
        return argv

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        binary = self._binary()
        if binary is None:
            raise RuntimeError(
                "Apptainer backend selected but apptainer/singularity binary is missing. "
                "Install Apptainer or switch to the native backend."
            )
        apptainer_argv = self._build_argv(spec, binary)
        if spec.stream_output:
            console.print(f"[dim]apptainer: {' '.join(apptainer_argv)}[/dim]")
        env = {**os.environ, **spec.env} if spec.env else None
        started = time.perf_counter()
        completed = subprocess.run(
            apptainer_argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=spec.timeout_seconds,
            env=env,
        )
        ended = time.perf_counter()
        if spec.stream_output:
            if completed.stdout:
                console.print(completed.stdout.rstrip("\n"), style="white")
            if completed.stderr:
                console.print(completed.stderr.rstrip("\n"), style="yellow")
        return CommandResult(
            backend=self.name,
            command=apptainer_argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=ended - started,
            metadata={"image": resolve_image(spec.argv), "wrapped_command": spec.argv},
        )
