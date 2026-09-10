"""PBS/Torque batch-scheduler backend.

Wraps each command as a ``qsub`` job, streams the job's output log while it
runs, and reports the job's ``Exit_status`` once it completes. Configuration
via ``spec.metadata["pbs"]`` or ``NGS_PBS_*`` environment variables
(``queue``, ``walltime``, ``mem``, ``cpus``, ``account``).
"""

from __future__ import annotations

import os
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.models import CommandResult, CommandSpec

PBS_DIRECTIVE_KEYS = ("queue", "walltime", "mem", "cpus", "account", "nodes")


class PBSBackend(ExecutionBackend):
    """Executes commands as PBS/Torque batch jobs and tails their output."""

    name = "pbs"

    def __init__(self, poll_interval_seconds: float = 2.0) -> None:
        self.poll_interval_seconds = poll_interval_seconds

    def is_available(self) -> bool:
        return shutil.which("qsub") is not None

    # ------------------------------------------------------------------
    # Script / argument construction (kept pure for testability)
    # ------------------------------------------------------------------
    @staticmethod
    def pbs_options(spec: CommandSpec) -> dict[str, str]:
        options: dict[str, str] = {}
        for key in PBS_DIRECTIVE_KEYS:
            env_value = os.environ.get(f"NGS_PBS_{key.upper()}")
            if env_value:
                options[key] = env_value
        metadata_options = spec.metadata.get("pbs")
        if isinstance(metadata_options, dict):
            for key, value in metadata_options.items():
                if key in PBS_DIRECTIVE_KEYS and value is not None:
                    options[key] = str(value)
        return options

    @classmethod
    def build_batch_script(cls, spec: CommandSpec) -> str:
        """Render the qsub shell script wrapping ``spec.argv``."""
        options = cls.pbs_options(spec)
        lines = [
            "#!/usr/bin/env bash",
            "#PBS -N ngs-agent",
            "#PBS -j oe",  # merge stdout and stderr into one output file
        ]
        if "queue" in options:
            lines.append(f"#PBS -q {options['queue']}")
        if "account" in options:
            lines.append(f"#PBS -A {options['account']}")
        if "walltime" in options:
            lines.append(f"#PBS -l walltime={options['walltime']}")
        if "mem" in options and "cpus" in options:
            lines.append(f"#PBS -l select=1:ncpus={options['cpus']}:mem={options['mem']}")
        elif "mem" in options:
            lines.append(f"#PBS -l mem={options['mem']}")
        elif "cpus" in options:
            lines.append(f"#PBS -l ncpus={options['cpus']}")
        if "nodes" in options:
            lines.append(f"#PBS -l nodes={options['nodes']}")
        for key, value in spec.env.items():
            lines.append(f"export {key}={shlex.quote(str(value))}")
        if spec.cwd:
            lines.append(f"cd {shlex.quote(spec.cwd)}")
        lines.append("")
        lines.append(" ".join(shlex.quote(str(part)) for part in spec.argv))
        return "\n".join(lines) + "\n"

    @staticmethod
    def parse_job_id(qsub_output: str) -> str:
        token = qsub_output.strip().splitlines()[0].strip() if qsub_output.strip() else ""
        return token.split(".", 1)[0]

    @staticmethod
    def parse_exit_status(qstat_output: str) -> int | None:
        """Extract ``Exit_status`` from ``qstat -f -x <id>`` output."""
        for line in qstat_output.splitlines():
            if "Exit_status" in line and "=" in line:
                try:
                    return int(line.split("=", 1)[1].strip())
                except ValueError:
                    continue
        return None

    @staticmethod
    def parse_job_state(qstat_output: str) -> str | None:
        """Extract ``job_state`` from ``qstat -f`` output."""
        for line in qstat_output.splitlines():
            if "job_state" in line and "=" in line:
                return line.split("=", 1)[1].strip()
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        if not self.is_available():
            raise RuntimeError("PBS backend requested but 'qsub' was not found on PATH.")

        import subprocess as _subprocess

        workdir = Path(spec.cwd) if spec.cwd else Path(tempfile.mkdtemp(prefix="ngs-pbs-"))
        workdir.mkdir(parents=True, exist_ok=True)
        script_path = workdir / f"ngs-job-{int(time.time())}.pbs"
        script_path.write_text(self.build_batch_script(spec), encoding="utf-8")

        submit = _subprocess.run(
            ["qsub", "-o", str(workdir / "ngs-job.out"), str(script_path)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(workdir),
        )
        if submit.returncode != 0:
            return CommandResult(
                backend=self.name,
                command=["qsub", str(script_path)],
                returncode=submit.returncode,
                stdout=submit.stdout,
                stderr=submit.stderr,
                duration_seconds=0.0,
                metadata={"job_script": str(script_path)},
            )

        job_id = self.parse_job_id(submit.stdout)
        console.print(
            f"[cyan]Submitted PBS job {job_id} for: {spec.description or spec.argv[0]}[/cyan]"
        )

        started = time.perf_counter()
        log_path = workdir / f"ngs-job.out.{job_id}"
        if not log_path.exists():
            # Some PBS variants name the output file differently.
            candidates = sorted(workdir.glob(f"*{job_id}*"))
            if candidates:
                log_path = candidates[0]
        log_position = 0

        def _tail_log() -> str:
            nonlocal log_position
            if not log_path.exists():
                return ""
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(log_position)
                fresh = handle.read()
                log_position = handle.tell()
            return fresh

        final_exit_status: int | None = None
        while True:
            time.sleep(self.poll_interval_seconds)
            fresh = _tail_log()
            if fresh and spec.stream_output:
                for line in fresh.splitlines():
                    console.print(line, style="white")

            qstat = _subprocess.run(
                ["qstat", "-f", job_id],
                capture_output=True,
                text=True,
                check=False,
            )
            state = self.parse_job_state(qstat.stdout)
            exit_status = self.parse_exit_status(qstat.stdout)

            if spec.timeout_seconds and (time.perf_counter() - started) > spec.timeout_seconds:
                _subprocess.run(["qdel", job_id], capture_output=True, check=False)
                raise RuntimeError(
                    f"PBS job {job_id} timed out after {spec.timeout_seconds}s: "
                    f"{' '.join(spec.argv)}"
                )

            if state is None and exit_status is None:
                # qstat no longer knows the job; use the last observed status.
                break
            if state == "C" or exit_status is not None:
                final_exit_status = exit_status
                break

        final_fresh = _tail_log()
        if final_fresh and spec.stream_output:
            for line in final_fresh.splitlines():
                console.print(line, style="white")

        stdout_text = (
            log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        )
        return CommandResult(
            backend=self.name,
            command=[str(script_path), *spec.argv],
            returncode=final_exit_status if final_exit_status is not None else 0,
            stdout=stdout_text,
            stderr="",
            duration_seconds=time.perf_counter() - started,
            metadata={"job_id": job_id, "job_script": str(script_path), "log_file": str(log_path)},
        )
