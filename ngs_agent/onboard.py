"""One-command setup: `ngsagent init` (like OpenCode's `/init`).

Detects LLM providers, writes config, discovers project data files, and
prints concrete next steps — the whole onboarding in one go.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel


def run_init(
    console: Console | None = None,
    yes: bool = False,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run guided setup. Returns the saved config."""
    from ngs_agent.config import run_wizard
    from ngs_agent.detect import demo_hint, summarize_project

    con = console or Console()
    root = cwd or Path.cwd()

    cfg = run_wizard(yes=yes)

    # Project summary + concrete next steps.
    summary = summarize_project(root)
    steps: list[str] = []

    if summary["vcfs"]:
        first = summary["vcfs"][0].name
        steps.append(f"[cyan]ngsagent analyze {first}[/cyan]  — variant + QC report")
        if cfg.get("llm", "none") != "none":
            steps.append(f"[cyan]ngsagent debate {first}[/cyan]  — 3-persona VUS debate")
        else:
            steps.append("[dim]ngsagent debate …[/dim]  — needs an LLM (re-run init with a key)")
    if summary["logs"]:
        first = summary["logs"][0].name
        steps.append(f"[cyan]ngsagent watch {first}[/cyan]  — scan for failures")
    if summary["qcs"]:
        steps.append("[dim]QC files found — auto-attached to analyze[/dim]")
    if not summary["vcfs"] and not summary["logs"]:
        demo = demo_hint(root)
        if demo:
            steps.append(f"[cyan]ngsagent analyze {demo}[/cyan]  — try the bundled demo")
            steps.append("[cyan]ngsagent watch demo_data/sample.log[/cyan]  — try log scanning")
        else:
            steps.append("Copy a .vcf or .log into this folder, then re-run [cyan]ngsagent init[/cyan]")
    steps.append("[cyan]ngsagent[/cyan]  — open the interactive assistant")

    llm = cfg.get("llm", "none")
    llm_line = (
        f"LLM backend: [green]{llm}[/green]"
        if llm != "none"
        else "LLM backend: [yellow]none[/yellow] (watch + analyze work without one)"
    )
    files_line = (
        f"Found: {len(summary['vcfs'])} VCF · {len(summary['logs'])} log · "
        f"{len(summary['qcs'])} QC · {len(summary['fastqs'])} FASTQ"
    )
    con.print(
        Panel(
            f"{llm_line}\n{files_line}\n\n[bold]Next steps:[/bold]\n" + "\n".join(f"  {s}" for s in steps),
            title="Setup complete — you're ready",
            border_style="green",
        )
    )
    return cfg
