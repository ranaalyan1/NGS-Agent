from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from platformdirs import user_config_dir


class NGSSettings(BaseModel):
    backend_preference: Literal["auto", "native", "docker", "apptainer"] = "auto"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-7"
    artifacts_dir: str = "./artifacts"
    logs_dir: str = "./logs"
    require_confirmation_for: set[str] = Field(default_factory=lambda: {"expensive", "destructive"})
    allow_destructive_without_confirmation: bool = False


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
    return value


def load_settings(config_path: Path | None = None) -> NGSSettings:
    merged: dict[str, Any] = {}

    for candidate in _config_candidates(config_path):
        cfg = _load_toml(candidate)
        if cfg:
            merged.update(cfg)
            break

    for field in NGSSettings.model_fields:
        env_name = f"NGS_{field.upper()}"
        if env_name in os.environ:
            merged[field] = _coerce_env_value(field, os.environ[env_name])

    return NGSSettings.model_validate(merged)
