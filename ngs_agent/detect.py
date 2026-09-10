"""Zero-config auto-detection for NGS-Agent (OpenCode-style).

Finds project files (VCF / logs / QC), discovers LLM providers from the
environment + config, and summarizes what's in the current directory so
commands can run with no arguments and `init` can set everything up.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ngs_agent.backends.openai_compat import PROVIDER_PRESETS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VCF_GLOBS = ("*.vcf", "*.vcf.gz")
LOG_GLOBS = ("*.log", "*.out", "*.err")
QC_GLOBS = ("*qc*.txt", "*summary*.txt", "*metrics*.txt", "fastqc_data.txt", "*_fastqc.txt")

#: Project-local config overlay (like `opencode.json`) — values here win over
#: the global `~/.ngsagent/config.yaml` for this directory only.
PROJECT_CONFIG_NAME = ".ngsagent.yaml"

#: Provider -> env var, in priority order for auto-detection.
PROVIDER_ENV_KEYS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic Claude",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "ollama": "Ollama (local)",
    "openai_compat": "OpenAI-compatible",
}

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash",
    "ollama": "llama3.2",
}
for _pid, (_url, _model) in PROVIDER_PRESETS.items():
    DEFAULT_MODELS.setdefault(_pid, _model)


@dataclass
class DetectedProvider:
    """An LLM provider we can use right now without extra setup."""

    id: str
    label: str
    source: str  # e.g. "env:OPENAI_API_KEY", "config file", "localhost:11434"
    model: str


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def find_files(patterns: tuple[str, ...], cwd: Path | None = None) -> list[Path]:
    """Return matching files in `cwd` (non-recursive), newest first."""
    root = cwd or Path.cwd()
    seen: dict[str, Path] = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file() and not path.name.startswith("."):
                seen.setdefault(path.name, path)
    return list(seen.values())


def find_vcfs(cwd: Path | None = None) -> list[Path]:
    return find_files(VCF_GLOBS, cwd)


def find_logs(cwd: Path | None = None) -> list[Path]:
    return find_files(LOG_GLOBS, cwd)


def find_qc_files(cwd: Path | None = None) -> list[Path]:
    return find_files(QC_GLOBS, cwd)


def summarize_project(cwd: Path | None = None) -> dict[str, Any]:
    """Snapshot of NGS-relevant files in a directory for `init` / `status`."""
    root = cwd or Path.cwd()
    vcfs = find_vcfs(root)
    logs = find_logs(root)
    qcs = find_qc_files(root)
    fastqs = find_files(("*.fastq", "*.fastq.gz", "*.fq", "*.fq.gz"), root)
    return {
        "cwd": root,
        "vcfs": vcfs,
        "logs": logs,
        "qcs": qcs,
        "fastqs": fastqs,
        "has_project_config": (root / PROJECT_CONFIG_NAME).exists(),
        "has_demo_data": (root / "demo_data" / "sample.vcf").exists(),
    }


def demo_hint(cwd: Path | None = None) -> str | None:
    """Return a demo-data suggestion if the bundled demo files are nearby."""
    root = cwd or Path.cwd()
    if (root / "demo_data" / "sample.vcf").exists():
        return "demo_data/sample.vcf"
    return None


# ---------------------------------------------------------------------------
# Provider discovery
# ---------------------------------------------------------------------------


def ollama_reachable(host: str = "http://localhost:11434", timeout: float = 0.4) -> bool:
    """Fast TCP check — never raises, never hangs longer than `timeout`."""
    try:
        parsed = urlparse(host if "://" in host else f"http://{host}")
        port = parsed.port or 11434
        sock = socket.create_connection((parsed.hostname or "localhost", port), timeout=timeout)
        sock.close()
        return True
    except (OSError, ValueError):
        return False


def detect_providers(cfg: dict[str, Any] | None = None, check_ollama: bool = True) -> list[DetectedProvider]:
    """List LLM providers usable right now (env vars, config keys, local Ollama).

    Order is priority order: first entry is the recommended pick for `init`.
    """
    cfg = cfg or {}
    found: list[DetectedProvider] = []

    for pid, env_key in PROVIDER_ENV_KEYS.items():
        model_key = f"{pid}_model"
        if os.environ.get(env_key):
            found.append(
                DetectedProvider(
                    id=pid,
                    label=PROVIDER_LABELS[pid],
                    source=f"env:{env_key}",
                    model=cfg.get(model_key, DEFAULT_MODELS.get(pid, "")),
                )
            )
        elif cfg.get(f"{pid}_api_key"):
            found.append(
                DetectedProvider(
                    id=pid,
                    label=PROVIDER_LABELS[pid],
                    source="config file",
                    model=cfg.get(model_key, DEFAULT_MODELS.get(pid, "")),
                )
            )

    # Local Ollama needs no key — just a reachable server.
    if check_ollama:
        host = cfg.get("ollama_host", "http://localhost:11434")
        if ollama_reachable(host):
            found.append(
                DetectedProvider(
                    id="ollama",
                    label=PROVIDER_LABELS["ollama"],
                    source=host,
                    model=cfg.get("ollama_model", DEFAULT_MODELS["ollama"]),
                )
            )

    return found


_providers_cache: dict[str, Any] = {"at": 0.0, "value": []}
PROVIDERS_CACHE_TTL = 30.0


def cached_providers(cfg: dict[str, Any] | None = None) -> list[DetectedProvider]:
    """Cached `detect_providers` for hot paths like the TUI status bar."""
    now = time.monotonic()
    if now - _providers_cache["at"] < PROVIDERS_CACHE_TTL:
        return list(_providers_cache["value"])
    value = detect_providers(cfg)
    _providers_cache["at"] = now
    _providers_cache["value"] = value
    return list(value)


# ---------------------------------------------------------------------------
# Argument resolution — "just work" when the user passes no file
# ---------------------------------------------------------------------------

_KIND_LABEL = {"vcf": "VCF file", "log": "pipeline log", "qc": "QC summary"}
_KIND_FINDERS = {"vcf": find_vcfs, "log": find_logs, "qc": find_qc_files}
_KIND_EXAMPLE = {
    "vcf": 'ngsagent analyze variants.vcf',
    "log": 'ngsagent watch pipeline.log [--tail]',
    "qc": 'ngsagent analyze variants.vcf --qc multiqc_summary.txt',
}


def resolve_input(
    value: Path | str | None,
    kind: str,
    cwd: Path | None = None,
    console: Any = None,
) -> Path:
    """Resolve a file argument, auto-detecting when `value` is None.

    - Exactly one candidate  -> use it (and say so).
    - None                   -> friendly error with next steps, exit 2.
    - Several + interactive  -> numbered picker.
    - Several + non-interactive -> friendly error listing candidates, exit 2.
    """
    from rich.console import Console
    from rich.panel import Panel

    con: Any = console or Console()
    root = cwd or Path.cwd()

    if value:
        return Path(value)

    label = _KIND_LABEL.get(kind, "input file")
    candidates = _KIND_FINDERS[kind](root)

    if len(candidates) == 1:
        con.print(f"[dim]Auto-detected {label}: {candidates[0].name}[/dim]")
        return candidates[0]

    if not candidates:
        lines = [
            f"[bold]No {label} found[/bold] in {root}",
            "",
            f"Pass one explicitly:  [cyan]{_KIND_EXAMPLE[kind]}[/cyan]",
        ]
        demo = demo_hint(root)
        if demo:
            lines.append(f"Or try the bundled demo:  [cyan]ngsagent analyze {demo}[/cyan]")
        lines.append("Or run [cyan]ngsagent init[/cyan] for guided setup.")
        con.print(Panel("\n".join(lines), title="File needed", border_style="yellow"))
        raise SystemExit(2)

    # Several candidates.
    if sys.stdin.isatty():
        from rich.prompt import Prompt

        con.print(f"[bold]Multiple {label}s found:[/bold]")
        for i, cand in enumerate(candidates, 1):
            size_kb = cand.stat().st_size / 1024
            con.print(f"  [cyan]{i}[/cyan]. {cand.name}  [dim]({size_kb:.1f} KB)[/dim]")
        try:
            raw = Prompt.ask("Pick a number", default="1", console=con).strip()
            idx = int(raw) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
        except (ValueError, IndexError, EOFError, KeyboardInterrupt):
            pass
        con.print("[yellow]No valid selection.[/yellow]")
        raise SystemExit(2)

    listing = "\n".join(f"  • {c.name}" for c in candidates[:8])
    con.print(
        Panel(
            f"[bold]Multiple {label}s found[/bold] in {root}\n\n{listing}\n\n"
            f"Pass one explicitly:  [cyan]{_KIND_EXAMPLE[kind]}[/cyan]",
            title="Be specific",
            border_style="yellow",
        )
    )
    raise SystemExit(2)
