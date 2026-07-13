"""Config management — profile-based, OpenClaude pattern.

Config lives at ~/.ngsagent/config.yaml. Supports:
  - llm: anthropic | openai | ollama | none
  - anthropic_model, openai_model, ollama_model, ollama_host
  - providers: profile dict (multiple saved profiles)
  - mcp_servers: dict of MCP server configs
  - permission_mode: auto | plan | ask | yolo
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".ngsagent"
CONFIG_PATH = CONFIG_DIR / "config.yaml"


@dataclass
class MCPServerConfig:
    type: str = "stdio"        # stdio | sse | http
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    llm: str = "none"
    anthropic_model: str = "claude-sonnet-4-20250514"
    openai_model: str = "gpt-4o"
    openai_base_url: str | None = None
    ollama_model: str = "llama3.2"
    ollama_host: str = "http://localhost:11434/v1"
    permission_mode: str = "auto"
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_provider: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Config:
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return Config.from_dict(data)
    except Exception:
        return Config()


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(yaml.safe_dump(cfg.to_dict(), default_flow_style=False))


def run_wizard() -> Config:
    """Interactive wizard — sets LLM provider + key."""
    cfg = load_config()
    print("\nNGS-Agent config wizard\n")
    print("Choose LLM provider:")
    print("  1. Anthropic (Claude)")
    print("  2. OpenAI / Azure OpenAI / OpenAI-compatible")
    print("  3. Ollama (local)")
    print("  4. None (use watch + analyze only)")
    choice = input("\n> ").strip()

    if choice == "1":
        cfg.llm = "anthropic"
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            key = input("Anthropic API key: ").strip()
            os.environ["ANTHROPIC_API_KEY"] = key
        model = input(f"Model [{cfg.anthropic_model}]: ").strip()
        if model:
            cfg.anthropic_model = model
    elif choice == "2":
        cfg.llm = "openai"
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            key = input("OpenAI API key: ").strip()
            os.environ["OPENAI_API_KEY"] = key
        model = input(f"Model [{cfg.openai_model}]: ").strip()
        if model:
            cfg.openai_model = model
        base = input(f"Base URL (empty for OpenAI) [{cfg.openai_base_url or ''}]: ").strip()
        if base:
            cfg.openai_base_url = base
    elif choice == "3":
        cfg.llm = "ollama"
        host = input(f"Ollama host [{cfg.ollama_host}]: ").strip()
        if host:
            cfg.ollama_host = host
        model = input(f"Model [{cfg.ollama_model}]: ").strip()
        if model:
            cfg.ollama_model = model
    elif choice == "4":
        cfg.llm = "none"
    else:
        print("Invalid choice.")
        return cfg

    save_config(cfg)
    print(f"\nSaved to {CONFIG_PATH}")
    return cfg
