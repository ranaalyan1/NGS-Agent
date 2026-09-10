"""SLURM batch-scheduler backend.

99% of real NGS data lives on HPC clusters where heavy compute must go
through a scheduler. This backend wraps each command as an ``sbatch`` job,
streams the job's output log while it runs, and reports the job's exit code
from ``sacct`` once it leaves the queue.

Configuration (all optional) via ``spec.metadata["slurm"]`` or the matching
environment variable:

===========  =========================  ===============================
Directive    metadata key               Environment variable
===========  =========================  ===============================
--partition  partition                  NGS_SLURM_PARTITION
--account    account                    NGS_SLURM_ACCOUNT
--time       time                       NGS_SLURM_TIME
--mem        mem                        NGS_SLURM_MEM
--cpus-per-task  cpus_per_task          NGS_SLURM_CPUS
--gres       gres                       NGS_SLURM_GRES
===========  =========================  ===============================
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

SBATCH_DIRECTIVE_KEYS = (
    "partition",
    "account",
    "time",
    "mem",
    "cpus_per_task",
    "gres",
    "nodes",
    "qos",
)


class SlurmBackend(ExecutionBackend):
    """Executes commands as SLURM batch jobs and tails their output."""

    name = "slurm"

    def __init__(self, poll_interval_seconds: float = 2.0) -> None:
        self.poll_interval_seconds = poll_interval_seconds

    def is_available(self) -> bool:
        return shutil.which("sbatch") is not None

    # ------------------------------------------------------------------
    # Script / argument construction (kept pure for testability)
    # ------------------------------------------------------------------
    @staticmethod
    def slurm_options(spec: CommandSpec) -> dict[str, str]:
        """Merge metadata['slurm'] overrides with NGS_SLURM_* environment variables."""
        options: dict[str, str] = {}
        for key in SBATCH_DIRECTIVE_KEYS:
            env_value = os.environ.get(f"NGS_SLURM_{key.upper()}")
            if env_value:
                options[key] = env_value
        metadata_options = spec.metadata.get("slurm")
        if isinstance(metadata_options, dict):
            for key, value in metadata_options.items():
                if key in SBATCH_DIRECTIVE_KEYS and value is not None:
                    options[key] = str(value)
        return options

    @classmethod
    def build_batch_script(cls, spec: CommandSpec) -> str:
        """Render the sbatch shell script wrapping ``spec.argv``."""
        options = cls.slurm_options(spec)
        lines = [
            "#!/usr/bin/env bash",
            "#SBATCH --job-name=ngs-agent",
            "#SBATCH --output=%x-%j.out",
        ]
        flag_by_key = {"cpus_per_task": "cpus-per-task"}
        for key, value in options.items():
            flag = flag_by_key.get(key, key)
            lines.append(f"#SBATCH --{flag}={value}")
        for key, value in spec.env.items():
            lines.append(f"export {key}={shlex.quote(str(value))}")
        if spec.cwd:
            lines.append(f"cd {shlex.quote(spec.cwd)}")
        lines.append("")
        lines.append(" ".join(shlex.quote(str(part)) for part in spec.argv))
        return "\n".join(lines) + "\n"

    @staticmethod
    def parse_job_id(sbatch_output: str) -> str:
        """Parse ``--parsable`` sbatch output (``jobid`` or ``jobid;cluster``)."""
        token = sbatch_output.strip().splitlines()[0].strip() if sbatch_output.strip() else ""
        return token.split(";", 1)[0]

    @staticmethod
    def parse_exit_code(sacct_output: str, job_id: str) -> int | None:
        """Derive the job exit code from ``sacct -P -n`` output."""
        for line in sacct_output.strip().splitlines():
            fields = line.split("|")
            if len(fields) >= 2 and fields[0].split(".")[0] == job_id:
                exit_code_field = fields[1]
                try:
                    return int(exit_code_field.split(":")[0])
                except ValueError:
                    continue
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        if not self.is_available():
            raise RuntimeError("SLURM backend requested but 'sbatch' was not found on PATH.")

        workdir = Path(spec.cwd) if spec.cwd else Path(tempfile.mkdtemp(prefix="ngs-slurm-"))
        workdir.mkdir(parents=True, exist_ok=True)
        script_path = workdir / f"ngs-job-{int(time.time())}.sbatch"
        script_path.write_text(self.build_batch_script(spec), encoding="utf-8")

        import subprocess as _subprocess

        submit = _subprocess.run(
            ["sbatch", "--parsable", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(workdir),
        )
        if submit.returncode != 0:
            return CommandResult(
                backend=self.name,
                command=["sbatch", str(script_path)],
                returncode=submit.returncode,
                stdout=submit.stdout,
                stderr=submit.stderr,
                duration_seconds=0.0,
                metadata={"job_script": str(script_path)},
            )

        job_id = self.parse_job_id(submit.stdout)
        console.print(
            f"[cyan]Submitted SLURM job {job_id} for: {spec.description or spec.argv[0]}[/cyan]"
        )
        log_path = workdir / f"ngs-agent-{job_id}.out"

        started = time.perf_counter()
        log_position = 0

        def _tail_log() -> str:
            """Return log content appended since the previous poll."""
            nonlocal log_position
            if not log_path.exists():
                return ""
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(log_position)
                fresh = handle.read()
                log_position = handle.tell()
            return fresh

        while True:
            time.sleep(self.poll_interval_seconds)
            fresh = _tail_log()
            if fresh and spec.stream_output:
                for line in fresh.splitlines():
                    console.print(line, style="white")

            status = _subprocess.run(
                ["squeue", "-h", "-j", job_id, "-o", "%T"],
                capture_output=True,
                text=True,
                check=False,
            )
            still_queued = bool(status.stdout.strip())

            if spec.timeout_seconds and (time.perf_counter() - started) > spec.timeout_seconds:
                _subprocess.run(["scancel", job_id], capture_output=True, check=False)
                raise RuntimeError(
                    f"SLURM job {job_id} timed out after {spec.timeout_seconds}s: "
                    f"{' '.join(spec.argv)}"
                )

            if not still_queued:
                break

        # Flush any log tail written after the job left the queue.
        final_fresh = _tail_log()
        if final_fresh and spec.stream_output:
            for line in final_fresh.splitlines():
                console.print(line, style="white")

        returncode = self._accounting_exit_code(job_id)
        stdout_text = (
            log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        )
        return CommandResult(
            backend=self.name,
            command=[str(script_path), *spec.argv],
            returncode=returncode if returncode is not None else 1,
            stdout=stdout_text,
            # SLURM merges job stdout and stderr into the output log.
            stderr="",
            duration_seconds=time.perf_counter() - started,
            metadata={"job_id": job_id, "job_script": str(script_path), "log_file": str(log_path)},
        )

    def _accounting_exit_code(self, job_id: str) -> int | None:
        """Query sacct for the job's exit code, tolerating accounting lag."""
        import subprocess as _subprocess

        sacct_output = ""
        for _attempt in range(2):
            sacct = _subprocess.run(
                ["sacct", "-P", "-n", "-j", job_id, "--format=JobID,ExitCode,State"],
                capture_output=True,
                text=True,
                check=False,
            )
            sacct_output = sacct.stdout
            if sacct_output.strip():
                break
            time.sleep(self.poll_interval_seconds)

        exit_code = self.parse_exit_code(sacct_output, job_id)
        if exit_code is not None:
            return exit_code
        # Fall back to the job state when the exit code is unavailable.
        if "FAILED" in sacct_output:
            return 1
        return None
