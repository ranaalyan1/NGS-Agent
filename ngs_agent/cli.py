"""Click CLI canonical entry point for NGS-Agent.

OpenCode-style UX: `ngsagent` with no args opens the interactive assistant,
`ngsagent run "..."` takes plain English, `ngsagent init` sets everything
up, and file arguments are optional — the CLI finds your data itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ngs_agent.analyzer import parse_vcf, render_report, scan_qc
from ngs_agent.backends.base import NoBackend
from ngs_agent.backends.factory import get_backend
from ngs_agent.config import CONFIG_PATH, load_config, run_wizard, save_config
from ngs_agent.debate import debate_variant
from ngs_agent.detect import (
    cached_providers,
    detect_providers,
    find_qc_files,
    resolve_input,
)
from ngs_agent.doctor import print_diagnostics, run_diagnostics
from ngs_agent.intent import INTENT_EXAMPLES, parse_intent
from ngs_agent.reports import generate_html_report
from ngs_agent.watcher import load_signatures, scan_file, tail_file

console = Console(force_terminal=True, legacy_windows=False)


class NGSGroup(click.Group):
    """Group that points typo'd commands at the plain-English runner."""

    def resolve_command(self, ctx: click.Context, args: list[str]):  # type: ignore[override]
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            # Click already appends its own "Did you mean ...?" suggestion;
            # we just add the OpenCode-style escape hatch (plain text only —
            # click errors don't render rich markup).
            if args:
                exc.message = (
                    f"{exc.message}\n\n"
                    "Tip: `ngsagent run \"...\"` understands plain English — "
                    "e.g. `ngsagent run \"check my log\"`."
                )
            raise


@click.group(cls=NGSGroup, invoke_without_command=True)
@click.version_option("0.2.0", "--version", "-V")
@click.pass_context
def main(ctx: click.Context) -> None:
    """NGS-Agent: Autonomous bioinformatics CLI, log watcher, and variant interpreter.

    \b
    Quickstart:
      ngsagent init                  guided one-command setup
      ngsagent run "check my log"    plain-English one-shot
      ngsagent                       interactive assistant (TUI)
    """
    if ctx.invoked_subcommand is None:
        from ngs_agent.tui import run_tui

        run_tui()


# ---------------------------------------------------------------------------
# Shared implementations (used by both click commands and `run`)
# ---------------------------------------------------------------------------


def _do_watch(logfile: Path, tail: bool, signatures: Path | None) -> None:
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


def _do_analyze(vcffile: Path, qc: Path | None, html: Path | None) -> None:
    variants = parse_vcf(vcffile)
    qc_metrics = scan_qc(qc) if qc else []
    render_report(variants, qc_metrics, console=console)

    if html:
        generate_html_report(variants, qc_metrics=qc_metrics, output_path=html)
        console.print(f"[green]HTML report exported to:[/green] [bold]{html}[/bold]")


def _do_debate(vcffile: Path, gene: str | None, html: Path | None) -> None:
    cfg = load_config()
    backend = get_backend(cfg)

    if isinstance(backend, NoBackend):
        console.print(
            Panel(
                "[bold red]No LLM backend configured.[/bold red]\n\n"
                "The `debate` command requires an LLM. `watch` and `analyze` work without one.\n\n"
                "Run: [bold]ngsagent init[/bold]  (auto-detects API keys)\n"
                "Or set: ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY",
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


def _do_doctor(fix: bool = False) -> None:
    if fix:
        from ngs_agent.config import ensure_config_dir

        ensure_config_dir()
        cfg = load_config()
        if not CONFIG_PATH.exists():
            save_config(cfg)
            console.print(f"[green]Created[/green] {CONFIG_PATH}")
        if (cfg.get("llm", "none") or "none") in ("none", ""):
            providers = detect_providers(cfg)
            if providers:
                best = providers[0]
                cfg["llm"] = best.id
                cfg.setdefault(f"{best.id}_model", best.model)
                save_config(cfg)
                console.print(f"[green]Fixed:[/green] LLM backend set to [bold]{best.id}[/bold] ({best.source})")
    checks = run_diagnostics(console=console)
    print_diagnostics(checks, console=console)


def _do_models() -> None:
    cfg = load_config()
    active = (cfg.get("llm", "none") or "none").lower()
    providers = {p.id: p for p in detect_providers(cfg)}

    table = Table(title="LLM Providers", show_header=True)
    table.add_column("Provider", style="cyan")
    table.add_column("Status")
    table.add_column("Model")
    table.add_column("Source")

    order = ["anthropic", "openai", "gemini", "openrouter", "groq", "deepseek", "ollama"]
    for pid in order:
        marker = " [green]● active[/green]" if pid == active else ""
        if pid in providers:
            p = providers[pid]
            table.add_row(f"{pid}{marker}", "[green]ready[/green]", p.model, p.source)
        else:
            table.add_row(f"{pid}{marker}", "[dim]no key[/dim]", "[dim]—[/dim]", "[dim]—[/dim]")
    console.print(table)
    if active in ("none", ""):
        auto = cached_providers(cfg)
        if auto:
            console.print(
                f"\n[dim]Keys detected but no backend selected — "
                f"run [cyan]ngsagent init --yes[/cyan] to use {auto[0].id}.[/dim]"
            )
        else:
            console.print(
                "\n[dim]No providers detected. Set an API key (e.g. OPENAI_API_KEY) "
                "or start Ollama, then run [cyan]ngsagent init[/cyan].[/dim]"
            )
    else:
        console.print(f"\n[dim]Active backend: [cyan]{active}[/cyan] — change with `ngsagent config set llm <name>`.[/dim]")


def _do_plan(goal: str) -> None:
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

    console.print(table)


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


def _auto_qc() -> Path | None:
    """Auto-attach a QC file when exactly one is present (quiet convenience)."""
    qcs = find_qc_files()
    if len(qcs) == 1:
        console.print(f"[dim]Auto-attached QC file: {qcs[0].name}[/dim]")
        return qcs[0]
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@main.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--tail", is_flag=True, help="Follow the log file for new lines.")
@click.option("--signatures", type=click.Path(exists=True, path_type=Path), default=None)
def watch(logfile: Path | None, tail: bool, signatures: Path | None) -> None:
    """Scan or tail a pipeline log for known failure signatures.

    LOGFILE is optional — with no argument the newest .log in this
    folder is used automatically.
    """
    _do_watch(resolve_input(logfile, "log", console=console), tail, signatures)


@main.command()
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--qc", type=click.Path(exists=True, path_type=Path), default=None, help="QC summary or FastQC file.")
@click.option("--html", type=click.Path(path_type=Path), default=None, help="Export interactive HTML report.")
def analyze(vcffile: Path | None, qc: Path | None, html: Path | None) -> None:
    """Parse a VCF and render a variant/QC report.

    VCFFILE is optional — with no argument the .vcf in this folder is
    used automatically, and a lone QC file is attached for free.
    """
    if qc is None:
        qc = _auto_qc()
    _do_analyze(resolve_input(vcffile, "vcf", console=console), qc, html)


@main.command()
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path), required=False)
@click.option("--gene", default=None, help="Debate a specific gene (default: all VUS).")
@click.option("--html", type=click.Path(path_type=Path), default=None, help="Export HTML debate report.")
def debate(vcffile: Path | None, gene: str | None, html: Path | None) -> None:
    """Run a 3-persona LLM debate on VUS variants with ACMG criteria.

    VCFFILE is optional — with no argument the .vcf in this folder is
    used automatically.
    """
    _do_debate(resolve_input(vcffile, "vcf", console=console), gene, html)


@main.command()
@click.argument("prompt", nargs=-1, required=True)
def run(prompt: tuple[str, ...]) -> None:
    """Do something in plain English. Like `opencode run`.

    \b
    Examples:
      ngsagent run "check my pipeline log"
      ngsagent run "analyze variants.vcf"
      ngsagent run "debate the VUS in BRCA2"
      ngsagent run "is my system ready?"
    """
    from ngs_agent.onboard import run_init

    text = " ".join(prompt).strip()
    intent = parse_intent(text)

    if intent.action == "unknown":
        console.print(
            Panel(
                "I couldn't tell what you want.\n\n[bold]Try things like:[/bold]\n"
                + "\n".join(f"  [cyan]{ex}[/cyan]" for ex in INTENT_EXAMPLES),
                title="Hmm",
                border_style="yellow",
            )
        )
        sys.exit(2)

    console.print(f"[dim]→ {intent.describe()}[/dim]")
    if intent.action == "watch":
        target = Path(intent.target) if intent.target else None
        if target is not None and not target.exists():
            console.print(f"[red]File not found: {target}[/red]")
            sys.exit(2)
        _do_watch(resolve_input(target, "log", console=console), intent.tail, None)
    elif intent.action == "analyze":
        target = Path(intent.target) if intent.target else None
        if target is not None and not target.exists():
            console.print(f"[red]File not found: {target}[/red]")
            sys.exit(2)
        _do_analyze(resolve_input(target, "vcf", console=console), _auto_qc(), None)
    elif intent.action == "debate":
        target = Path(intent.target) if intent.target else None
        if target is not None and not target.exists():
            console.print(f"[red]File not found: {target}[/red]")
            sys.exit(2)
        _do_debate(resolve_input(target, "vcf", console=console), intent.gene, None)
    elif intent.action == "doctor":
        _do_doctor()
    elif intent.action == "init":
        run_init(console=console)
    elif intent.action == "models":
        _do_models()
    elif intent.action == "plan":
        _do_plan(text)
    elif intent.action == "status":
        _show_config()
    elif intent.action == "help":
        console.print(
            Panel(
                "\n".join(f"  [cyan]{ex}[/cyan]" for ex in INTENT_EXAMPLES)
                + "\n\nOr run [cyan]ngsagent[/cyan] for the interactive assistant.",
                title="Things you can say",
                border_style="cyan",
            )
        )


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Non-interactive: auto-detect everything.")
def init(yes: bool) -> None:
    """Guided one-command setup. Like OpenCode's /init.

    Detects API keys, picks an LLM backend, finds your data files,
    and prints concrete next steps.
    """
    from ngs_agent.onboard import run_init

    run_init(console=console, yes=yes)


@main.command()
def models() -> None:
    """List LLM providers and which ones are ready to use."""
    _do_models()


@main.command()
@click.option("--fix", is_flag=True, help="Auto-fix what can be fixed (config, backend).")
def doctor(fix: bool) -> None:
    """Run environment, bioinformatics tools, and LLM readiness checks."""
    _do_doctor(fix=fix)


@main.command()
def pipeline() -> None:
    """Full RNA-Seq / WGS / WES swarm pipeline (Temporal + Docker).

    The swarm runs end-to-end pipelines across containers. This command
    checks prerequisites and shows the setup steps.
    """
    import shutil

    has_docker = shutil.which("docker") is not None
    try:
        import temporalio  # noqa: F401

        has_temporal = True
    except ImportError:
        has_temporal = False

    status = (
        f"Docker: {'[green]installed[/green]' if has_docker else '[red]missing[/red]'}   "
        f"Temporal SDK: {'[green]installed[/green]' if has_temporal else '[red]missing[/red]'}"
    )
    steps = ["[cyan]cp .env.example .env[/cyan]  — fill in keys/buckets"]
    if not has_temporal:
        steps.append('[cyan]pip install "ngs-agent[swarm]"[/cyan]  — Temporal + MinIO + Redis clients')
    if not has_docker:
        steps.append("[cyan]install Docker Engine[/cyan]  — required for agent containers")
    steps += [
        "[cyan]docker compose up -d[/cyan]  — Temporal, MinIO, Redis",
        "[cyan]bash scripts/build-agents.sh[/cyan]  — build agent images",
        "[cyan]python worker.py[/cyan]  — start the Temporal worker (new terminal)",
        "[cyan]python cli.py submit --experiment RNA-Seq --organism human --ref-genome ... --gtf ... --fastq-r1 ... --fastq-r2 ... --paired[/cyan]",
    ]
    console.print(
        Panel(
            f"{status}\n\n[bold]Setup:[/bold]\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(steps, 1)),
            title="Swarm pipeline",
            border_style="blue",
        )
    )


@main.command("plan")
@click.argument("intent", nargs=-1)
@click.option("--workflow", default="auto", help="Workflow (rnaseq, wgs, wes, auto)")
def plan(intent: tuple[str, ...], workflow: str) -> None:
    """Preview steps for an agentic bioinformatics workflow."""
    goal = " ".join(intent) if intent else "RNA-Seq differential expression analysis"
    _do_plan(goal)


def _show_config() -> None:
    cfg = load_config()
    for key, value in cfg.items():
        if "key" in key.lower() and value:
            value = "****" + str(value)[-4:]
        console.print(f"{key}: {value}")


@main.group()
def config() -> None:
    """Manage ~/.ngsagent/config.yaml (plus per-project .ngsagent.yaml)."""


@config.command("show")
def config_show() -> None:
    """Print current configuration (secrets redacted)."""
    _show_config()


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
    if key in ("anthropic_model", "ollama_model", "ollama_host", "llm", "gemini_model", "openai_model",
               "openrouter_model", "groq_model", "deepseek_model"):
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
