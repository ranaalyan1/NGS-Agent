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


class DockerBackend(ExecutionBackend):
    """Runs commands inside Docker containers with the working directory mounted."""

    name = "docker"

    def __init__(self) -> None:
        self._available: bool | None = None

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        if shutil.which("docker") is None:
            self._available = False
            return False
        try:
            completed = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self._available = completed.returncode == 0
        except Exception:
            self._available = False
        return self._available

    def _build_docker_argv(self, spec: CommandSpec) -> list[str]:
        workdir = Path(spec.cwd or os.getcwd()).resolve()
        image = resolve_image(spec.argv)
        docker_argv = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{workdir}:{workdir}",
            "-w",
            str(workdir),
        ]
        # Run as the invoking user so output files are not owned by root.
        if hasattr(os, "getuid"):
            docker_argv.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        for key, value in spec.env.items():
            docker_argv.extend(["-e", f"{key}={value}"])
        docker_argv.append(image)
        docker_argv.extend(spec.argv)
        return docker_argv

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        docker_argv = self._build_docker_argv(spec)
        if spec.stream_output:
            console.print(f"[dim]docker: {' '.join(docker_argv)}[/dim]")
        started = time.perf_counter()
        completed = subprocess.run(
            docker_argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=spec.timeout_seconds,
        )
        ended = time.perf_counter()
        if spec.stream_output:
            if completed.stdout:
                console.print(completed.stdout.rstrip("\n"), style="white")
            if completed.stderr:
                console.print(completed.stderr.rstrip("\n"), style="yellow")
        return CommandResult(
            backend=self.name,
            command=docker_argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=ended - started,
            metadata={"image": resolve_image(spec.argv), "wrapped_command": spec.argv},
        )
