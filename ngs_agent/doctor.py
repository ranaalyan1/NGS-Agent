"""Environment, bioinformatics binaries, and provider diagnostics."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from ngs_agent.config import load_config


@dataclass
class DiagnosticCheck:
    category: str
    name: str
    status: str  # OK, WARN, MISSING, INFO
    details: str
    hint: str = ""


# All LLM backends the config wizard offers, with where their credentials live.
LLM_BACKENDS = (
    ("anthropic", "Anthropic API Key", "ANTHROPIC_API_KEY", "anthropic_api_key"),
    ("openrouter", "OpenRouter API Key", "OPENROUTER_API_KEY", "openrouter_api_key"),
    ("groq", "Groq API Key", "GROQ_API_KEY", "groq_api_key"),
    ("deepseek", "DeepSeek API Key", "DEEPSEEK_API_KEY", "deepseek_api_key"),
    ("gemini", "Gemini API Key", "GEMINI_API_KEY", "gemini_api_key"),
    ("openai", "OpenAI API Key", "OPENAI_API_KEY", "openai_api_key"),
    ("openai_compat", "OpenAI-Compat API Key", "OPENAI_COMPAT_API_KEY", "openai_compat_api_key"),
)


def _has_key(cfg: dict, env_var: str, cfg_key: str) -> bool:
    return bool(os.environ.get(env_var) or cfg.get(cfg_key))


def run_diagnostics(console: Console | None = None) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []

    # 1. Python runtime
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        checks.append(DiagnosticCheck("Runtime", "Python Version", "OK", f"Python {py_ver}"))
    else:
        checks.append(DiagnosticCheck("Runtime", "Python Version", "WARN", f"Python {py_ver}", "Recommended Python >= 3.11"))

    # 2. Bioinformatics Binaries
    bio_tools = [
        ("FastQC", "fastqc", "Quality control for raw sequence data"),
        ("HISAT2", "hisat2", "Graph-based spliced aligner for RNA-Seq"),
        ("BWA", "bwa", "Burrows-Wheeler aligner for DNA-Seq"),
        ("Samtools", "samtools", "SAM/BAM alignment utilities"),
        ("GATK", "gatk", "Genome Analysis Toolkit for variant calling"),
        ("featureCounts", "featureCounts", "Read summarization for RNA-Seq"),
        ("Rscript", "Rscript", "R runtime for DESeq2 and clusterProfiler"),
    ]
    for label, binary, desc in bio_tools:
        path = shutil.which(binary)
        if path:
            checks.append(DiagnosticCheck("Bioinformatics", label, "OK", path))
        else:
            checks.append(DiagnosticCheck("Bioinformatics", label, "WARN", "Not in PATH", f"Only needed for pipeline execution: {desc}"))

    # 3. Container & Workflow Runtimes
    container_tools = [
        ("Docker", "docker"),
        ("Apptainer / Singularity", "apptainer"),
        ("Podman", "podman"),
    ]
    for label, binary in container_tools:
        path = shutil.which(binary)
        if path:
            checks.append(DiagnosticCheck("Containers", label, "OK", path))
        else:
            checks.append(DiagnosticCheck("Containers", label, "INFO", "Not installed"))

    # 4. LLM Providers — cover every backend the wizard offers.
    cfg = load_config()
    active_llm = str(cfg.get("llm", "none") or "none").lower()
    if active_llm in ("none", ""):
        checks.append(DiagnosticCheck(
            "LLM Config", "Configured Backend", "INFO", "none",
            "Only `debate` needs an LLM. Run `ngsagent config wizard` to set one up.",
        ))
    else:
        checks.append(DiagnosticCheck("LLM Config", "Configured Backend", "OK", active_llm))

    for backend, label, env_var, cfg_key in LLM_BACKENDS:
        has = _has_key(cfg, env_var, cfg_key)
        if backend == active_llm:
            # The active backend must actually have credentials.
            if has:
                checks.append(DiagnosticCheck("LLM Keys", label, "OK", "Available (active backend)"))
            else:
                checks.append(DiagnosticCheck(
                    "LLM Keys", label, "MISSING",
                    "Not set — `debate` will fail",
                    f"Set {env_var} or re-run `ngsagent config wizard`.",
                ))
        else:
            checks.append(DiagnosticCheck(
                "LLM Keys", label, "OK" if has else "INFO",
                "Available" if has else "Not set",
            ))

    # Ollama (local, no API key) — check reachability when selected.
    if active_llm == "ollama":
        host = str(cfg.get("ollama_host", "http://localhost:11434"))
        import urllib.request
        try:
            with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=3) as resp:
                reachable = resp.status == 200
        except Exception:
            reachable = False
        if reachable:
            checks.append(DiagnosticCheck("LLM Keys", "Ollama Server", "OK", f"Reachable at {host}"))
        else:
            checks.append(DiagnosticCheck(
                "LLM Keys", "Ollama Server", "MISSING",
                f"Unreachable at {host} — `debate` will fail",
                "Start Ollama (`ollama serve`) and pull a model (`ollama pull llama3.2`).",
            ))

    return checks


def overall_status(checks: list[DiagnosticCheck]) -> str:
    """Summarise diagnostics: 'ready', 'ready-no-llm', or 'action-needed'."""
    if any(c.status == "MISSING" for c in checks):
        return "action-needed"
    active = next((c for c in checks if c.name == "Configured Backend"), None)
    if active is not None and active.details.strip().lower() in ("none", ""):
        return "ready-no-llm"
    return "ready"


def print_diagnostics(checks: list[DiagnosticCheck], console: Console | None = None) -> None:
    con = console or Console()
    table = Table(title="NGS-Agent System Doctor & Readiness Check", show_header=True)
    table.add_column("Category", style="cyan")
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")
    table.add_column("Hint / Resolution")

    for c in checks:
        style = {"OK": "green", "WARN": "yellow", "MISSING": "red", "INFO": "dim white"}.get(c.status, "white")
        table.add_row(c.category, c.name, f"[{style}]{c.status}[/{style}]", c.details, c.hint)

    con.print(table)

    status = overall_status(checks)
    if status == "ready":
        con.print("\n[green]✓ Ready.[/green] `watch`, `analyze`, and `debate` should all work.")
    elif status == "ready-no-llm":
        con.print(
            "\n[green]✓ Ready for[/green] `watch` and `analyze` (no LLM needed)."
            "\n[dim]To enable `debate`, run:[/dim] [bold]ngsagent config wizard[/bold]"
        )
    else:
        con.print(
            "\n[red]✗ Action needed:[/red] your active LLM backend is missing credentials, "
            "so [bold]debate[/bold] will fail. (`watch` and `analyze` still work.)"
            "\n[dim]Fix with:[/dim] [bold]ngsagent config wizard[/bold]  [dim]or set the API key shown above.[/dim]"
        )
