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
    # OpenAI-compatible (OpenRouter, Groq, DeepSeek, Gemini, etc.)
    "openai_compat_base_url": "https://openrouter.ai/api/v1",
    "openai_compat_model": "openrouter/auto",
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
    console.print("LLM is optional. [dim]watch[/dim] and [dim]analyze[/dim] work without one.\n")

    cfg = load_config()
    use_llm = Confirm.ask("Configure an LLM backend for [bold]debate[/bold]?", default=False)
    if not use_llm:
        cfg["llm"] = "none"
        save_config(cfg)
        console.print("[green]Saved.[/green] LLM disabled.")
        return cfg

    console.print()
    console.print("[bold]Choose a backend:[/bold]")
    backends = [
        ("anthropic",    "Anthropic Claude  (requires ANTHROPIC_API_KEY)"),
        ("openrouter",   "OpenRouter        (free & paid models, requires OPENROUTER_API_KEY)"),
        ("groq",         "Groq              (fast inference, requires GROQ_API_KEY)"),
        ("deepseek",     "DeepSeek          (requires DEEPSEEK_API_KEY)"),
        ("gemini",       "Google Gemini     (requires GEMINI_API_KEY)"),
        ("ollama",       "Ollama            (local, no API key)"),
        ("openai_compat","Other OpenAI-compatible provider"),
    ]
    for i, (key, label) in enumerate(backends, 1):
        console.print(f"  [cyan]{i}[/cyan]. {label}")
    console.print()

    choice_raw = Prompt.ask("Backend number", default="1").strip()
    try:
        choice_idx = int(choice_raw) - 1
        choice = backends[choice_idx][0] if 0 <= choice_idx < len(backends) else "anthropic"
    except (ValueError, IndexError):
        if choice_raw in [b[0] for b in backends]:
            choice = choice_raw
        else:
            choice = "anthropic"

    cfg["llm"] = choice

    if choice == "anthropic":
        cfg["anthropic_model"] = Prompt.ask(
            "Model",
            default=cfg.get("anthropic_model", DEFAULT_CONFIG["anthropic_model"]),
        )
        console.print("[dim]Set ANTHROPIC_API_KEY in your environment.[/dim]")

    elif choice == "openrouter":
        cfg["openrouter_model"] = Prompt.ask(
            "Model  [dim](e.g. openrouter/auto, meta-llama/llama-3.3-70b-instruct:free)[/dim]",
            default=cfg.get("openrouter_model", "openrouter/auto"),
        )
        console.print("[dim]Set OPENROUTER_API_KEY in your environment or paste it here.[/dim]")
        key = Prompt.ask("API key [dim](leave blank to use env var)[/dim]", default="", password=True)
        if key:
            cfg["openrouter_api_key"] = key

    elif choice == "groq":
        cfg["groq_model"] = Prompt.ask(
            "Model  [dim](e.g. llama-3.3-70b-versatile, mixtral-8x7b-32768)[/dim]",
            default=cfg.get("groq_model", "llama-3.3-70b-versatile"),
        )
        key = Prompt.ask("API key [dim](leave blank to use GROQ_API_KEY env var)[/dim]", default="", password=True)
        if key:
            cfg["groq_api_key"] = key

    elif choice == "deepseek":
        cfg["deepseek_model"] = Prompt.ask(
            "Model",
            default=cfg.get("deepseek_model", "deepseek-chat"),
        )
        key = Prompt.ask("API key [dim](leave blank to use DEEPSEEK_API_KEY env var)[/dim]", default="", password=True)
        if key:
            cfg["deepseek_api_key"] = key

    elif choice == "gemini":
        cfg["gemini_model"] = Prompt.ask(
            "Model  [dim](e.g. gemini-2.0-flash, gemini-2.5-pro)[/dim]",
            default=cfg.get("gemini_model", "gemini-2.0-flash"),
        )
        key = Prompt.ask("API key [dim](leave blank to use GEMINI_API_KEY env var)[/dim]", default="", password=True)
        if key:
            cfg["gemini_api_key"] = key

    elif choice == "ollama":
        cfg["ollama_model"] = Prompt.ask(
            "Model",
            default=cfg.get("ollama_model", DEFAULT_CONFIG["ollama_model"]),
        )
        cfg["ollama_host"] = Prompt.ask(
            "Host",
            default=cfg.get("ollama_host", DEFAULT_CONFIG["ollama_host"]),
        )

    elif choice == "openai_compat":
        cfg["openai_compat_base_url"] = Prompt.ask(
            "Base URL",
            default=cfg.get("openai_compat_base_url", "https://openrouter.ai/api/v1"),
        )
        cfg["openai_compat_model"] = Prompt.ask(
            "Model",
            default=cfg.get("openai_compat_model", "openrouter/auto"),
        )
        key = Prompt.ask("API key [dim](leave blank to use OPENAI_COMPAT_API_KEY env var)[/dim]", default="", password=True)
        if key:
            cfg["openai_compat_api_key"] = key

    save_config(cfg)
    console.print(f"\n[green]Saved[/green] to {CONFIG_PATH}")
    return cfg
