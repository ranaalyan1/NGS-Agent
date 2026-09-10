"""Shared streaming subprocess runner used by all local-execution backends.

Every backend that ultimately spawns a local process (native, docker,
apptainer, slurm/pbs job submission and log tailing) streams stdout/stderr
line-by-line through the same code path so users see identical live output
regardless of backend.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Queue
from threading import Thread


@dataclass
class StreamedResult:
    """Result of a streamed process execution."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    command: list[str] = field(default_factory=list)


def run_streaming_process(
    argv: list[str],
    console: Callable[[str, str], None] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> StreamedResult:
    """Run ``argv`` as a subprocess, streaming output to ``console``.

    Args:
        argv: Command argument vector to execute.
        console: Optional callback receiving ``(line, stream_name)`` where
            ``stream_name`` is ``"stdout"`` or ``"stderr"``. Used to forward
            live output to a rich Console.
        cwd: Working directory for the child process.
        env: Extra environment variables merged over ``os.environ``.
        timeout_seconds: Kill the process after this many seconds.
        on_start: Called with the ``Popen`` object right after spawn; used by
            scheduler backends that need the PID while the process runs.
    """
    started = time.perf_counter()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env={**os.environ, **env} if env else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if on_start is not None:  # pragma: no cover - timing dependent
        on_start(process)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    stdout_queue: Queue[str | None] = Queue()
    stderr_queue: Queue[str | None] = Queue()

    def _drain(stream, queue: Queue[str | None], lines: list[str], stream_name: str) -> None:
        if stream is None:
            queue.put(None)
            return
        try:
            for line in stream:
                queue.put(line)
                lines.append(line)
                if console is not None:
                    console(line.rstrip("\n"), stream_name)
        finally:
            queue.put(None)

    stdout_thread = Thread(
        target=_drain, args=(process.stdout, stdout_queue, stdout_lines, "stdout"), daemon=True
    )
    stderr_thread = Thread(
        target=_drain, args=(process.stderr, stderr_queue, stderr_lines, "stderr"), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
    finally:
        for thread in (stdout_thread, stderr_thread):
            thread.join(timeout=5.0)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    # Drain anything still queued after thread termination.
    while True:
        line = stdout_queue.get_nowait() if not stdout_queue.empty() else None
        if line is None:
            break
        stdout_lines.append(line)
    while True:
        line = stderr_queue.get_nowait() if not stderr_queue.empty() else None
        if line is None:
            break
        stderr_lines.append(line)

    return StreamedResult(
        returncode=process.returncode if not timed_out else -9,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        duration_seconds=time.perf_counter() - started,
        timed_out=timed_out,
        command=list(argv),
    )
