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
from ngs_agent.core.cli import register_review_commands
from ngs_agent.debate import debate_variant
from ngs_agent.doctor import print_diagnostics, run_diagnostics
from ngs_agent.reports import generate_html_report
from ngs_agent.watcher import load_signatures, scan_file, tail_file

console = Console(force_terminal=True, legacy_windows=False)


@click.group(invoke_without_command=True)
@click.version_option("0.2.0", "--version", "-V")
@click.pass_context
def main(ctx: click.Context) -> None:
    """NGS-Agent: auditable NGS quality control and evidence-backed variant review.

    The signed classification path is `review` / `replay` / `sign-off`: it is
    deterministic, runs offline against recorded or locally supplied evidence,
    and never consults a language model.

    `consult`, `analyze`, `watch`, `doctor` and `plan` are convenience commands.
    Anything a model writes there is narrative, not evidence.

    Research use only. Every classification requires human review and sign-off.
    """
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


@main.command("consult")
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option("--gene", default=None, help="Restrict to one gene (default: every VUS in the file).")
@click.option("--html", type=click.Path(path_type=Path), default=None, help="Export an HTML report.")
def consult(vcffile: Path, gene: str | None, html: Path | None) -> None:
    """Ask three LLM personas to discuss VUS variants. NARRATIVE ONLY.

    This command produces no classification, no ACMG criteria, and no confidence
    score. If a model emits tier language or a criterion code, it is stripped
    from the output and reported as a boundary violation.

    For an evidence-backed classification use `ngsagent review`, which derives
    criteria only from structured evidence records and abstains when it has none.
    """
    cfg = load_config()
    backend = get_backend(cfg)

    if isinstance(backend, NoBackend):
        console.print(
            Panel(
                "[bold red]No LLM backend configured.[/bold red]\n\n"
                "The `consult` command requires an LLM.\n\n"
                "[bold]The signed classification path does not:[/bold] `watch`, `analyze` and "
                "`review` all work with no model configured at all.\n\n"
                "Run: [bold]ngsagent config wizard[/bold]\n"
                "Or set: GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY",
                title="LLM required (for narrative only)",
                border_style="red",
            )
        )
        sys.exit(1)

    variants = [v for v in parse_vcf(vcffile) if v.is_vus]
    if gene:
        variants = [v for v in variants if v.gene.upper() == gene.upper()]

    if not variants:
        console.print("[yellow]No VUS variants to consult on.[/yellow]")
        return

    console.print(
        Panel(
            "Model-generated narrative. [bold]Not a classification, not evidence,[/bold] and not "
            "an input to any ACMG/AMP determination.\n"
            "For an evidence-backed result use [bold]ngsagent review[/bold].",
            title="Research use only",
            border_style="yellow",
        )
    )

    results = []
    for variant in variants:
        console.print(
            Panel(
                f"[bold]{variant.gene}[/bold] {variant.chrom}:{variant.pos} {variant.ref}>{variant.alt}",
                style="magenta",
            )
        )
        try:
            result = debate_variant(variant, backend)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(1)
        results.append(result)

        for opinion in result.opinions:
            console.print(f"\n[bold]{opinion.persona}[/bold]")
            console.print(opinion.reasoning or "[dim](no narrative produced)[/dim]")
            if opinion.redactions:
                console.print(
                    f"[yellow]removed by guardrail:[/yellow] {', '.join(opinion.redactions)}"
                )
        for error in result.errors:
            console.print(f"[red]persona error:[/red] {error}")

        console.print("\n[bold]What a reviewer should check:[/bold]")
        for item in result.summary.evidence_a_reviewer_should_check:
            console.print(f"  - {item}")
        if result.boundary_violations:
            console.print(
                f"\n[bold yellow]Boundary violations ({len(result.boundary_violations)}):[/bold yellow]"
            )
            for item in result.boundary_violations:
                console.print(f"  [yellow]-[/yellow] {item}")
        console.print(f"\n[dim]{result.disclaimer}[/dim]\n")

    if html:
        generate_html_report(variants, debates=results, output_path=html)
        console.print(f"[green]Consultation report exported to:[/green] [bold]{html}[/bold]")


#: Deprecated alias. Kept so existing scripts fail with an explanation rather
#: than a "no such command" error, and so the rename is discoverable.
@main.command("debate", hidden=True, deprecated=True)
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option("--gene", default=None)
@click.option("--html", type=click.Path(path_type=Path), default=None)
def debate(vcffile: Path, gene: str | None, html: Path | None) -> None:
    """Renamed to `consult`. The command no longer produces ACMG criteria."""
    console.print(
        Panel(
            "`debate` has been renamed to [bold]consult[/bold], and it no longer emits ACMG "
            "criteria or a classification. Model output could not be tied to an evidence record, "
            "so it is now narrative only.\n\n"
            "For an evidence-backed classification use [bold]ngsagent review[/bold].\n\n"
            "Re-running as `consult`...",
            title="Command renamed",
            border_style="yellow",
        )
    )
    ctx = click.get_current_context()
    ctx.invoke(consult, vcffile=vcffile, gene=gene, html=html)


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


# The signed classification path. Registered last so it owns the top-level
# names a reviewer is expected to reach for: review, replay, sign-off, audit,
# normalize.
register_review_commands(main)


if __name__ == "__main__":
    main()
