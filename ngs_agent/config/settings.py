from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_config_dir
from pydantic import BaseModel, Field, field_validator

# Single source of truth for the default model. Uses a non-dated alias so it
# keeps resolving as Anthropic retires dated snapshot IDs.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"


class NGSSettings(BaseModel):
    backend_preference: Literal["auto", "native", "docker", "apptainer", "slurm", "pbs"] = "auto"
    anthropic_api_key: str = ""
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    artifacts_dir: str = "./artifacts"
    logs_dir: str = "./logs"
    require_confirmation_for: set[str] = Field(default_factory=lambda: {"expensive", "destructive"})
    allow_destructive_without_confirmation: bool = False
    # Upper bound on concurrently executing plan steps (per-sample branches).
    max_parallel_steps: int = 4
    # Default thread count for multi-threaded tools (hisat2/samtools/...).
    default_threads: int = 4

    @field_validator("require_confirmation_for")
    @classmethod
    def validate_safety_levels(cls, v: set[str]) -> set[str]:
        from ngs_agent.tools.permissions import SafetyLevel
        valid_levels = {level.value for level in SafetyLevel}
        for level in v:
            if level not in valid_levels:
                raise ValueError(f"Invalid safety level '{level}'. Must be one of: {valid_levels}")
        return v

    @field_validator("max_parallel_steps")
    @classmethod
    def validate_parallelism(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_parallel_steps must be >= 1")
        return v


def _config_candidates(explicit_path: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit_path is not None:
        candidates.append(explicit_path)
    candidates.append(Path.cwd() / "ngs.toml")
    candidates.append(Path(user_config_dir("ngs-agent", "ngs-agent")) / "ngs.toml")
    return candidates


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data.get("ngs", data)


def _coerce_env_value(key: str, value: str) -> Any:
    if key in {"allow_destructive_without_confirmation"}:
        return value.lower() in {"1", "true", "yes", "on"}
    if key in {"require_confirmation_for"}:
        return {part.strip() for part in value.split(",") if part.strip()}
    if key in {"max_parallel_steps", "default_threads"}:
        return int(value)
    return value


def load_settings(config_path: Path | None = None) -> NGSSettings:
    merged: dict[str, Any] = {}

    for candidate in _config_candidates(config_path):
        cfg = _load_toml(candidate)
        if cfg:
            merged.update(cfg)
            break

    for field_name in NGSSettings.model_fields:
        env_name = f"NGS_{field_name.upper()}"
        if env_name in os.environ:
            merged[field_name] = _coerce_env_value(field_name, os.environ[env_name])

    return NGSSettings.model_validate(merged)
