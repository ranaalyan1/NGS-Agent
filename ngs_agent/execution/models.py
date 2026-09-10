from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CommandSpec(BaseModel):
    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int | None = None
    stream_output: bool = True
    description: str = ""
    # Backend-specific hints. Examples:
    #   {"image": "quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0"}
    #   {"slurm": {"partition": "general", "mem": "16G", "cpus_per_task": 4}}
    #   {"mounts": ["/data"]}
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandResult(BaseModel):
    backend: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    metadata: dict[str, Any] = Field(default_factory=dict)
