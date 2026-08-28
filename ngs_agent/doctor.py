"""Environment, bioinformatics binaries, and provider diagnostics."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import List, Optional

from rich.console import Console
from rich.table import Table

from ngs_agent.config import load_config


@dataclass
class DiagnosticCheck:
    category: str
    name: str
    status: str  # OK, WARN, MISSING
    details: str
    hint: str = ""


def run_diagnostics(console: Optional[Console] = None) -> List[DiagnosticCheck]:
    con = console or Console()
    checks: List[DiagnosticCheck] = []

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
            checks.append(DiagnosticCheck("Bioinformatics", label, "WARN", "Not in PATH", f"Required for execution: {desc}"))

    # 3. Container & Workflow Runtimes
    container_tools = [
        ("Docker", "docker"),
        ("Apptainer / Singularity", "apptainer"),
        ("Podman", "podman"),
    ]
    has_container = False
    for label, binary in container_tools:
        path = shutil.which(binary)
        if path:
            has_container = True
            checks.append(DiagnosticCheck("Containers", label, "OK", path))
        else:
            checks.append(DiagnosticCheck("Containers", label, "INFO", "Not installed"))

    # 4. LLM Providers
    cfg = load_config()
    active_llm = cfg.get("llm", "none")
    checks.append(DiagnosticCheck("LLM Config", "Configured Backend", "OK" if active_llm != "none" else "INFO", active_llm))

    gemini_key = os.environ.get("GEMINI_API_KEY") or cfg.get("gemini_api_key")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key")
    openai_key = os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key")

    checks.append(DiagnosticCheck("LLM Keys", "Gemini API Key", "OK" if gemini_key else "INFO", "Available" if gemini_key else "Not set"))
    checks.append(DiagnosticCheck("LLM Keys", "Anthropic API Key", "OK" if anthropic_key else "INFO", "Available" if anthropic_key else "Not set"))
    checks.append(DiagnosticCheck("LLM Keys", "OpenAI API Key", "OK" if openai_key else "INFO", "Available" if openai_key else "Not set"))

    return checks


def print_diagnostics(checks: List[DiagnosticCheck], console: Optional[Console] = None) -> None:
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
