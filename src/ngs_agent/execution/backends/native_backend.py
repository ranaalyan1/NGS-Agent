from __future__ import annotations

import os
import subprocess
import time
from threading import Thread

from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.models import CommandResult, CommandSpec


class NativeBackend(ExecutionBackend):
    name = "native"

    def is_available(self) -> bool:
        return True

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        started = time.perf_counter()
        process = subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            env={**os.environ, **spec.env} if spec.env else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _drain_stream(stream, sink: list[str], style: str) -> None:  # type: ignore[no-untyped-def]
            if stream is None:
                return
            for line in stream:
                sink.append(line)
                if spec.stream_output:
                    console.print(line.rstrip("\n"), style=style)

        stdout_thread = Thread(target=_drain_stream, args=(process.stdout, stdout_lines, "white"), daemon=True)
        stderr_thread = Thread(target=_drain_stream, args=(process.stderr, stderr_lines, "yellow"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            process.wait(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise RuntimeError(
                f"Command timed out after {spec.timeout_seconds}s: {' '.join(spec.argv)}"
            ) from exc

        stdout_thread.join()
        stderr_thread.join()

        ended = time.perf_counter()
        return CommandResult(
            backend=self.name,
            command=spec.argv,
            returncode=process.returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
            duration_seconds=ended - started,
        )
