"""Configuration management and first-run wizard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".ngsagent"
CONFIG_PATH = CONFIG_DIR / "config.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": "none",
    "anthropic_model": "claude-sonnet-4-20250514",
    "ollama_model": "llama3.2",
    "ollama_host": "http://localhost:11434",
}


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def save_config(cfg: dict[str, Any]) -> None:
    ensure_config_dir()
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, default_flow_style=False, sort_keys=False)


def run_wizard() -> dict[str, Any]:
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    console = Console()
    console.print("\n[bold]NGS-Agent configuration wizard[/bold]")
    console.print("LLM is optional. `watch` and `analyze` work without one.\n")

    cfg = load_config()
    use_llm = Confirm.ask("Configure an LLM backend for `debate`?", default=False)
    if not use_llm:
        cfg["llm"] = "none"
        save_config(cfg)
        console.print("[green]Saved.[/green] LLM disabled (default).")
        return cfg

    choice = Prompt.ask(
        "Backend",
        choices=["anthropic", "ollama"],
        default="anthropic",
    )
    cfg["llm"] = choice
    if choice == "anthropic":
        cfg["anthropic_model"] = Prompt.ask(
            "Anthropic model",
            default=cfg.get("anthropic_model", DEFAULT_CONFIG["anthropic_model"]),
        )
        console.print("Set ANTHROPIC_API_KEY in your environment.")
    else:
        cfg["ollama_model"] = Prompt.ask(
            "Ollama model",
            default=cfg.get("ollama_model", DEFAULT_CONFIG["ollama_model"]),
        )
        cfg["ollama_host"] = Prompt.ask(
            "Ollama host",
            default=cfg.get("ollama_host", DEFAULT_CONFIG["ollama_host"]),
        )

    save_config(cfg)
    console.print(f"[green]Saved[/green] to {CONFIG_PATH}")
    return cfg
