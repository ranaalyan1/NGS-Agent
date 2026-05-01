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


class CommandResult(BaseModel):
    backend: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    metadata: dict[str, Any] = Field(default_factory=dict)
