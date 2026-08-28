"""Click CLI canonical entry point for NGS-Agent."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ngs_agent.analyzer import parse_vcf, render_report, scan_qc
from ngs_agent.backends.base import NoBackend
from ngs_agent.backends.factory import get_backend
from ngs_agent.config import CONFIG_PATH, load_config, run_wizard, save_config
from ngs_agent.debate import debate_variant
from ngs_agent.doctor import print_diagnostics, run_diagnostics
from ngs_agent.reports import generate_html_report
from ngs_agent.watcher import load_signatures, scan_file, tail_file

console = Console(force_terminal=True, legacy_windows=False)


@click.group(invoke_without_command=True)
@click.version_option("0.2.0", "--version", "-V")
@click.pass_context
def main(ctx: click.Context) -> None:
    """NGS-Agent: Autonomous bioinformatics CLI, log watcher, and variant interpreter."""
    if ctx.invoked_subcommand is None:
        from ngs_agent.tui import run_tui
        run_tui()


@main.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
@click.option("--tail", is_flag=True, help="Follow the log file for new lines.")
@click.option("--signatures", type=click.Path(exists=True, path_type=Path), default=None)
def watch(logfile: Path, tail: bool, signatures: Path | None) -> None:
    """Scan or tail a pipeline log for known failure signatures."""
    sigs = load_signatures(signatures)
    console.print(Panel(f"[bold]Watching[/bold] {logfile}", style="cyan"))
    console.print(f"Loaded {len(sigs)} failure signatures (no LLM required).\n")

    if tail:
        try:
            for match in tail_file(logfile, sigs):
                _print_match(match)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped.[/yellow]")
    else:
        matches = scan_file(logfile, sigs)
        if not matches:
            console.print("[green]No failure signatures detected.[/green]")
            return
        for match in matches:
            _print_match(match)
        console.print(f"\n[bold]{len(matches)}[/bold] issue(s) found.")


def _print_match(match) -> None:
    sig = match.signature
    sev_color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(sig.severity, "white")
    console.print(
        Panel(
            f"[bold]{sig.name}[/bold] (line {match.line_no})\n"
            f"[dim]{match.line.strip()}[/dim]\n\n"
            f"{sig.explanation}\n\n"
            f"[bold]Suggested fix:[/bold] {sig.suggested_fix}",
            title=f"[{sev_color}]{sig.severity.upper()}[/{sev_color}]",
            border_style=sev_color,
        )
    )


@main.command()
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option("--qc", type=click.Path(exists=True, path_type=Path), default=None, help="QC summary or FastQC file.")
@click.option("--html", type=click.Path(path_type=Path), default=None, help="Export interactive HTML report.")
def analyze(vcffile: Path, qc: Path | None, html: Path | None) -> None:
    """Parse a VCF and render a variant/QC report."""
    variants = parse_vcf(vcffile)
    qc_metrics = scan_qc(qc) if qc else []
    render_report(variants, qc_metrics, console=console)

    if html:
        generate_html_report(variants, qc_metrics=qc_metrics, output_path=html)
        console.print(f"[green]HTML report exported to:[/green] [bold]{html}[/bold]")


@main.command()
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option("--gene", default=None, help="Debate a specific gene (default: all VUS).")
@click.option("--html", type=click.Path(path_type=Path), default=None, help="Export HTML debate report.")
def debate(vcffile: Path, gene: str | None, html: Path | None) -> None:
    """Run a 3-persona LLM debate on VUS variants with ACMG criteria."""
    cfg = load_config()
    backend = get_backend(cfg)

    if isinstance(backend, NoBackend):
        console.print(
            Panel(
                "[bold red]No LLM backend configured.[/bold red]\n\n"
                "The `debate` command requires an LLM. `watch` and `analyze` work without one.\n\n"
                f"Run: [bold]ngsagent config wizard[/bold]\n"
                f"Or set: GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY",
                title="LLM Required",
                border_style="red",
            )
        )
        sys.exit(1)

    variants = [v for v in parse_vcf(vcffile) if v.is_vus]
    if gene:
        variants = [v for v in variants if v.gene.upper() == gene.upper()]

    if not variants:
        console.print("[yellow]No VUS variants to debate.[/yellow]")
        return

    results = []
    for variant in variants:
        console.print(Panel(f"[bold]{variant.gene}[/bold] {variant.chrom}:{variant.pos} {variant.ref}>{variant.alt}", style="magenta"))
        try:
            result = debate_variant(variant, backend)
            results.append(result)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(1)

        for op in result.opinions:
            acmg_str = f" [cyan]({' '.join(op.acmg_criteria)})[/cyan]" if op.acmg_criteria else ""
            console.print(f"\n[bold]{op.persona}[/bold] — [yellow]{op.stance}[/yellow]{acmg_str}")
            console.print(op.reasoning)
        console.print(f"\n[bold]Consensus:[/bold] {result.consensus}")
        console.print(f"[bold]ACMG Evaluation:[/bold] {result.acmg_evaluation.classification} ({result.acmg_evaluation.explanation})")
        console.print(f"[bold]Recommendation:[/bold] {result.recommendation}\n")

    if html:
        generate_html_report(variants, debates=results, output_path=html)
        console.print(f"[green]Debate report exported to:[/green] [bold]{html}[/bold]")


@main.command()
def doctor() -> None:
    """Run environment, bioinformatics tools, and LLM readiness checks."""
    checks = run_diagnostics(console=console)
    print_diagnostics(checks, console=console)


@main.command("plan")
@click.argument("intent", nargs=-1)
@click.option("--workflow", default="auto", help="Workflow (rnaseq, wgs, wes, auto)")
def plan(intent: tuple[str, ...], workflow: str) -> None:
    """Preview steps for an agentic bioinformatics workflow."""
    goal = " ".join(intent) if intent else "RNA-Seq differential expression analysis"
    console.print(Panel(f"[bold]Pipeline Execution Plan[/bold]\nObjective: {goal}", style="blue"))
    table = Table(show_header=True)
    table.add_column("Stage", style="cyan")
    table.add_column("Tool / Subagent")
    table.add_column("ETA")
    table.add_column("Expected Artifacts")

    table.add_row("1. Ingestion & QC", "FastQC + Trimmomatic", "2-5 min", "fastqc_report.html, clean_reads.fq.gz")
    table.add_row("2. Spliced Alignment", "HISAT2 / STAR", "15-45 min", "aligned_sorted.bam, align.log")
    table.add_row("3. Quantification", "featureCounts / StringTie", "5-15 min", "counts_matrix.tsv")
    table.add_row("4. Differential Expression", "DESeq2 / EdgeR", "3-8 min", "de_results.csv, volcano_plot.png")
    table.add_row("5. Interpretation & Report", "Multi-Agent Interpreter", "1-3 min", "clinical_report.html")

    con = console
    con.print(table)


@main.group()
def config() -> None:
    """Manage ~/.ngsagent/config.yaml."""


@config.command("show")
def config_show() -> None:
    """Print current configuration."""
    cfg = load_config()
    for key, value in cfg.items():
        console.print(f"{key}: {value}")


@config.command("wizard")
def config_wizard() -> None:
    """Interactive first-run setup wizard."""
    run_wizard()


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config value."""
    cfg = load_config()
    if key in ("anthropic_model", "ollama_model", "ollama_host", "llm", "gemini_model", "openai_model"):
        cfg[key] = value
    else:
        try:
            cfg[key] = float(value) if "." in value else int(value)
        except ValueError:
            cfg[key] = value
    save_config(cfg)
    console.print(f"[green]Set[/green] {key} = {value}")


if __name__ == "__main__":
    main()
