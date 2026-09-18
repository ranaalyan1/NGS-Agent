"""Click CLI canonical entry point for NGS-Agent."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ngs_agent import __version__
from ngs_agent.analyzer import parse_vcf, render_report, scan_qc
from ngs_agent.backends.base import NoBackend
from ngs_agent.backends.factory import get_backend
from ngs_agent.config import (
    CONFIG_PATH,
    SECRET_KEYS,
    VALID_CONFIG_KEYS,
    VALID_LLM_BACKENDS,
    load_config,
    run_wizard,
    save_config,
    suggest_key,
)
from ngs_agent.debate import DebateBackendError, debate_variant
from ngs_agent.demo import demo_log, demo_vcf
from ngs_agent.doctor import overall_status, print_diagnostics, run_diagnostics
from ngs_agent.reports import generate_html_report
from ngs_agent.watcher import load_signatures, scan_file, tail_file

console = Console(force_terminal=True, legacy_windows=False)


def _fail(message: str, hint: str = "", code: int = 1) -> None:
    """Print a friendly error with an actionable hint, then exit."""
    console.print(f"[red]Error:[/red] {message}")
    if hint:
        console.print(f"[dim]Hint:[/dim] {hint}")
    sys.exit(code)


def _require_file(path: Path, label: str) -> Path:
    if not path.exists():
        _fail(
            f"{label} not found: {path}",
            "Check the path, or try the bundled demo: ngsagent demo",
        )
    if not path.is_file():
        _fail(f"{label} is not a file: {path}")
    return path


@click.group(invoke_without_command=True)
@click.version_option(__version__, "--version", "-V")
@click.pass_context
def main(ctx: click.Context) -> None:
    """NGS-Agent: log watcher, VCF/QC interpreter, and VUS debate for NGS teams.

    \b
    Quick start:
      ngsagent demo                  Run everything on bundled demo data
      ngsagent watch pipeline.log    Scan a log for known failures
      ngsagent analyze variants.vcf  Variant + QC report
      ngsagent tui                   Interactive terminal UI
    """
    if ctx.invoked_subcommand is None:
        # Bare `ngsagent` shows a quickstart (NOT the TUI): new users typing
        # the command for the first time should see what it does, not a
        # theme picker. The interactive UI is still one command away.
        console.print(
            Panel(
                "[bold]NGS-Agent[/bold] — log watcher, VCF/QC interpreter, VUS debate\n\n"
                "[cyan]ngsagent demo[/cyan]                  Try it now on bundled demo data\n"
                "[cyan]ngsagent watch[/cyan] <logfile>       Scan a pipeline log for failures\n"
                "[cyan]ngsagent analyze[/cyan] <vcf> [--qc]  Variant + QC report (+ --html)\n"
                "[cyan]ngsagent debate[/cyan] <vcf>          3-persona LLM debate on VUS (needs LLM)\n"
                "[cyan]ngsagent doctor[/cyan]                Check tools, containers, LLM setup\n"
                "[cyan]ngsagent tui[/cyan]                   Interactive terminal UI\n"
                "[cyan]ngsagent examples[/cyan]              Copy-paste recipes for each command\n\n"
                "[dim]Full pipelines? See `ngs-agent run rnaseq --help` (local) or\n"
                "`python cli.py --help` (Temporal swarm). `ngsagent --help` lists all.[/dim]",
                title=f"NGS-Agent v{__version__}",
                border_style="cyan",
            )
        )
        ctx.exit(0)


@main.command()
@click.argument("logfile", type=click.Path(path_type=Path))
@click.option("--tail", is_flag=True, help="Follow the log file for new lines.")
@click.option(
    "--signatures",
    type=click.Path(path_type=Path),
    default=None,
    help="Custom signatures: a directory of YAML files or a single YAML file.",
)
def watch(logfile: Path, tail: bool, signatures: Path | None) -> None:
    """Scan or tail a pipeline log for known failure signatures.

    Example: ngsagent watch pipeline.log
    """
    _require_file(logfile, "Log file")
    try:
        sigs = load_signatures(signatures)
    except (ValueError, FileNotFoundError) as exc:
        _fail(str(exc), "Omit --signatures to use the 5 built-in signatures.")
        return
    if not sigs:
        _fail("No failure signatures loaded.", "Omit --signatures to use the built-in set.")
        return

    console.print(Panel(f"[bold]Watching[/bold] {logfile}", style="cyan"))
    console.print(f"Loaded {len(sigs)} failure signatures (no LLM required).\n")

    if tail:
        console.print("[dim]Following new lines. Press Ctrl+C to stop.[/dim]\n")
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
@click.argument("vcffile", type=click.Path(path_type=Path))
@click.option("--qc", type=click.Path(path_type=Path), default=None, help="QC summary or FastQC file.")
@click.option("--html", type=click.Path(path_type=Path), default=None, help="Export interactive HTML report.")
def analyze(vcffile: Path, qc: Path | None, html: Path | None) -> None:
    """Parse a VCF and render a variant/QC report.

    Example: ngsagent analyze variants.vcf --qc multiqc_summary.txt
    """
    _require_file(vcffile, "VCF file")
    try:
        variants = parse_vcf(vcffile)
    except Exception as exc:
        _fail(f"Could not parse {vcffile}: {exc}", "Is this a valid VCF file?")
        return
    if not variants:
        _fail(
            f"No variants found in {vcffile}.",
            "The file may be header-only, empty, or not tab-separated VCF. "
            "Try: ngsagent demo",
        )
        return

    qc_metrics = []
    if qc:
        _require_file(qc, "QC file")
        qc_metrics = scan_qc(qc)
        if not qc_metrics:
            console.print(
                "[yellow]Warning:[/yellow] no QC metrics recognised in "
                f"{qc} (expected FastQC, MultiQC, samtools flagstat, or a "
                "summary with mapping rate / coverage / duplication / Q30)."
            )
    render_report(variants, qc_metrics, console=console)

    if html:
        generate_html_report(variants, qc_metrics=qc_metrics, output_path=html)
        console.print(f"[green]HTML report exported to:[/green] [bold]{html}[/bold]")


@main.command()
@click.argument("vcffile", type=click.Path(path_type=Path))
@click.option("--gene", default=None, help="Debate a specific gene (default: all VUS).")
@click.option("--html", type=click.Path(path_type=Path), default=None, help="Export HTML debate report.")
def debate(vcffile: Path, gene: str | None, html: Path | None) -> None:
    """Run a 3-persona LLM debate on VUS variants with ACMG criteria.

    Example: ngsagent debate variants.vcf --gene BRCA2
    """
    _require_file(vcffile, "VCF file")
    cfg = load_config()
    backend = get_backend(cfg)

    if isinstance(backend, NoBackend):
        console.print(
            Panel(
                "[bold red]No LLM backend configured.[/bold red]\n\n"
                "The `debate` command requires an LLM. `watch` and `analyze` work without one.\n\n"
                "Run: [bold]ngsagent config wizard[/bold]\n"
                "Or set one of: GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY /\n"
                "OPENROUTER_API_KEY / GROQ_API_KEY / DEEPSEEK_API_KEY",
                title="LLM Required",
                border_style="red",
            )
        )
        sys.exit(1)

    try:
        variants = [v for v in parse_vcf(vcffile) if v.is_vus]
    except Exception as exc:
        _fail(f"Could not parse {vcffile}: {exc}", "Is this a valid VCF file?")
        return
    if gene:
        variants = [v for v in variants if v.gene.upper() == gene.upper()]
        if not variants:
            _fail(
                f"No VUS variants found for gene {gene}.",
                "Run `ngsagent analyze` on this VCF to see which genes have VUS entries.",
            )
            return

    if not variants:
        console.print("[yellow]No VUS variants to debate.[/yellow]")
        console.print("[dim]Run `ngsagent analyze` to see the variant breakdown.[/dim]")
        return

    results = []
    for variant in variants:
        console.print(Panel(f"[bold]{variant.gene}[/bold] {variant.chrom}:{variant.pos} {variant.ref}>{variant.alt}", style="magenta"))
        try:
            result = debate_variant(variant, backend)
            results.append(result)
        except DebateBackendError as exc:
            # All LLM calls failed: abort loudly with no fake consensus and
            # no HTML report. Exit code 2 distinguishes backend failure.
            console.print(f"[red]Debate aborted:[/red] {exc}")
            sys.exit(2)
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
        if not results:
            _fail("No debate results to export.", code=2)
            return
        generate_html_report(variants, debates=results, output_path=html)
        console.print(f"[green]Debate report exported to:[/green] [bold]{html}[/bold]")


@main.command()
def doctor() -> None:
    """Check tools, containers, and LLM setup. Exit 1 if action is needed."""
    checks = run_diagnostics(console=console)
    print_diagnostics(checks, console=console)
    if overall_status(checks) == "action-needed":
        sys.exit(1)


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
    con.print(
        "\n[dim]To actually run a pipeline: `ngs-agent run rnaseq --help` (local) or "
        "`python cli.py submit --help` (Temporal swarm).[/dim]"
    )


@main.command()
def demo() -> None:
    """Run watch + analyze on bundled demo data (works anywhere)."""
    vcf = demo_vcf()
    log = demo_log()
    if not vcf.is_file() or not log.is_file():
        _fail(
            "Bundled demo files are missing from this install.",
            "Reinstall with: pip install --force-reinstall ngs-agent",
        )
        return
    console.print(Panel("[bold]Demo 1/2:[/bold] watch (log failure scan)", style="cyan"))
    for match in scan_file(log):
        _print_match(match)
    console.print(Panel("[bold]Demo 2/2:[/bold] analyze (VCF + QC report)", style="cyan"))
    render_report(parse_vcf(vcf), [], console=console)
    console.print(
        "\n[green]Done.[/green] Next steps:\n"
        f"  ngsagent watch {log} --tail\n"
        f"  ngsagent analyze {vcf} --html report.html\n"
        "  ngsagent debate <your.vcf>   [dim](needs LLM: ngsagent config wizard)[/dim]"
    )


@main.command()
def examples() -> None:
    """Show copy-paste recipes for every command."""
    console.print(
        Panel(
            "[bold]watch[/bold] — find failures in pipeline logs (no LLM needed)\n"
            "  ngsagent watch pipeline.log\n"
            "  ngsagent watch pipeline.log --tail\n"
            "  ngsagent watch run.log --signatures my_signatures/\n\n"
            "[bold]analyze[/bold] — VCF + QC report (no LLM needed)\n"
            "  ngsagent analyze variants.vcf\n"
            "  ngsagent analyze variants.vcf --qc multiqc_summary.txt\n"
            "  ngsagent analyze variants.vcf --html report.html\n\n"
            "[bold]debate[/bold] — 3-persona LLM debate on VUS (needs LLM)\n"
            "  ngsagent config wizard            [dim]# one-time LLM setup[/dim]\n"
            "  ngsagent debate variants.vcf\n"
            "  ngsagent debate variants.vcf --gene BRCA2 --html debate.html\n\n"
            "[bold]config[/bold] — manage ~/.ngsagent/config.yaml\n"
            "  ngsagent config wizard\n"
            "  ngsagent config show\n"
            "  ngsagent config set llm anthropic\n"
            "  ngsagent config set anthropic_model claude-sonnet-4-5\n\n"
            "[bold]Full pipelines[/bold]\n"
            "  ngs-agent run rnaseq --samplesheet samples.csv --dry-run\n"
            "  python cli.py quick --fastq reads.fastq --ref-genome <idx> --gtf <genes.gtf>",
            title="NGS-Agent recipes",
            border_style="cyan",
        )
    )


@main.command()
def tui() -> None:
    """Launch the interactive terminal UI (mascot, slash commands)."""
    from ngs_agent.tui import run_tui
    run_tui()


@main.group()
def config() -> None:
    """Manage ~/.ngsagent/config.yaml."""


@config.command("show")
def config_show() -> None:
    """Print current configuration (secrets masked)."""
    cfg = load_config()
    for key, value in cfg.items():
        if key in SECRET_KEYS and value:
            console.print(f"{key}: **** (set, hidden)")
        else:
            console.print(f"{key}: {value}")
    console.print(f"\n[dim]Config file: {CONFIG_PATH}[/dim]")


@config.command("wizard")
def config_wizard() -> None:
    """Interactive first-run setup wizard."""
    run_wizard()


@config.command("keys")
def config_keys() -> None:
    """List all valid `config set` keys."""
    table = Table(title="Valid config keys", show_header=True)
    table.add_column("Key", style="cyan")
    table.add_column("Description")
    for key, desc in VALID_CONFIG_KEYS.items():
        table.add_row(key, desc)
    console.print(table)


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config value. Example: ngsagent config set llm anthropic."""
    cfg = load_config()
    if key not in VALID_CONFIG_KEYS:
        if key == "model":
            _fail(
                "There is no generic 'model' key — set the model for your backend, "
                "e.g. `ngsagent config set anthropic_model claude-sonnet-4-5`.",
                "Run `ngsagent config keys` for the full list.",
                code=2,
            )
            return
        suggestion = suggest_key(key)
        msg = f"Unknown config key: {key}."
        if suggestion:
            msg += f" Did you mean '{suggestion}'?"
        msg += " Run `ngsagent config keys` for the full list."
        _fail(msg, code=2)
        return
    if key == "llm" and value.lower() not in VALID_LLM_BACKENDS:
        _fail(
            f"Unknown backend: {value}.",
            f"Valid backends: {', '.join(VALID_LLM_BACKENDS)}",
            code=2,
        )
        return
    # Model ids, URLs, and hostnames are always strings — never coerce to
    # numbers (a model tag like '3.3' must stay a string in YAML).
    cfg[key] = value
    save_config(cfg)
    console.print(f"[green]Set[/green] {key} = {value if key not in SECRET_KEYS else '**** (hidden)'}")


if __name__ == "__main__":
    main()
