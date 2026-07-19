from __future__ import annotations

import os
import subprocess
import time
from queue import Queue
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

        stdout_queue: Queue[str] = Queue()
        stderr_queue: Queue[str] = Queue()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _drain_stream(stream, queue: Queue[str], style: str) -> None:  # type: ignore[no-untyped-def]
            if stream is None:
                return
            try:
                for line in stream:
                    queue.put(line)
                    if spec.stream_output:
                        console.print(line.rstrip("\n"), style=style)
            finally:
                queue.put(None)  # Signal end of stream

        stdout_thread = Thread(target=_drain_stream, args=(process.stdout, stdout_queue, "white"), daemon=True)
        stderr_thread = Thread(target=_drain_stream, args=(process.stderr, stderr_queue, "yellow"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            process.wait(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            # Ensure threads terminate
            stdout_thread.join(timeout=1.0)
            stderr_thread.join(timeout=1.0)
            raise RuntimeError(
                f"Command timed out after {spec.timeout_seconds}s: {' '.join(spec.argv)}"
            ) from exc
        finally:
            # Ensure process resources are cleaned up
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

        # Collect output from queues (thread-safe)
        while True:
            line = stdout_queue.get()
            if line is None:
                break
            stdout_lines.append(line)

        while True:
            line = stderr_queue.get()
            if line is None:
                break
            stderr_lines.append(line)

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
