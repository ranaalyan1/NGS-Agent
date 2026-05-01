from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ngs_agent.agent.orchestrator import AgentOrchestrator
from ngs_agent.bioinformatics.rnaseq.pipeline import RNASeqPipelineAdapter
from ngs_agent.config.settings import load_settings
from ngs_agent.execution.models import CommandSpec
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.observability.logging import configure_logging
from ngs_agent.tools.registry import create_default_registry

app = typer.Typer(
    name="ngs",
    add_completion=True,
    no_args_is_help=False,
    help="Agentic NGS CLI with native-first execution and safety by default.",
)
run_app = typer.Typer(help="Structured pipeline execution commands.")
app.add_typer(run_app, name="run")
console = Console()


@dataclass
class PlanStep:
    title: str
    safety: str
    est_runtime: str
    command_preview: str


def _build_plan(intent: str, workflow: str, samples: str | None, reference: str | None) -> list[PlanStep]:
    workflow_name = workflow if workflow != "auto" else ("rnaseq" if "rna" in intent.lower() else "variant")
    sample_src = samples or "auto-discover"
    ref_src = reference or "auto-discover"

    return [
        PlanStep("Parse experiment context", "read", "<1 min", f"scan inputs from {sample_src}"),
        PlanStep("Validate references", "read", "1-3 min", f"validate reference {ref_src}"),
        PlanStep("QC and decision stage", "expensive", "5-20 min", "fastqc + AI trimming policy"),
        PlanStep("Core pipeline execution", "expensive", "30-240 min", f"execute {workflow_name} backend"),
        PlanStep("Generate report artifacts", "write", "2-10 min", "render HTML report + summaries"),
    ]


def _render_plan(plan: list[PlanStep], title: str) -> None:
    table = Table(title=title)
    table.add_column("Step", style="cyan")
    table.add_column("Safety")
    table.add_column("ETA")
    table.add_column("Preview")
    for step in plan:
        table.add_row(step.title, step.safety, step.est_runtime, step.command_preview)
    console.print(table)


def _confirm_execution() -> bool:
    return typer.confirm("Proceed with this plan?", default=True)


def _objective_from_tokens(tokens: list[str]) -> str:
    return " ".join(tokens).strip()


def _infer_workflow(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ["rnaseq", "rna-seq", "transcript", "expression"]):
        return "rnaseq"
    return "variant"


def _run_analysis(objective: str, workflow: str, working_directory: Path, dry_run: bool, resume: bool) -> dict[str, object]:
    settings = load_settings()
    configure_logging(settings.logs_dir)
    registry = create_default_registry()
    orchestrator = AgentOrchestrator(settings, BackendSelector(), registry, console)
    return orchestrator.run(
        objective=objective,
        workflow=workflow,
        working_directory=working_directory,
        dry_run=dry_run,
        resume=resume,
    )


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    intent: Annotated[list[str] | None, typer.Argument(help="Natural language intent", show_default=False)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview plan and commands only.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Enable verbose logging.")] = False,
) -> None:
    """Run natural-language mode when no explicit subcommand is provided."""
    if ctx.invoked_subcommand is not None:
        return

    settings = load_settings()
    configure_logging(settings.logs_dir, verbose=verbose)

    if not intent:
        console.print(
            Panel(
                "Use [bold]ngs run[/bold] for structured mode or pass a natural-language prompt.\n"
                "Example: [italic]ngs analyze this RNA-seq experiment in ./data[/italic]"
            )
        )
        raise typer.Exit(code=0)

    user_intent = _objective_from_tokens(intent)
    workflow = _infer_workflow(user_intent)
    _run_analysis(user_intent, workflow, Path.cwd(), dry_run=dry_run, resume=False)


@run_app.command("rnaseq")
def run_rnaseq(
    samplesheet: Annotated[Path | None, typer.Option("--samplesheet", help="Samplesheet CSV/TSV")] = None,
    reference: Annotated[str | None, typer.Option("--reference", help="Reference preset or index path")] = None,
    working_directory: Annotated[Path, typer.Option("--working-directory", help="Experiment root directory")] = Path.cwd(),
    output: Annotated[Path, typer.Option("--output", help="Output directory")] = Path("./runs/latest"),
    prompt: Annotated[str | None, typer.Option("--prompt", help="Optional natural-language run brief")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview actions without execution")] = False,
    resume: Annotated[bool, typer.Option("--resume", help="Resume from checkpoint if available")] = False,
) -> None:
    """Run the RNA-Seq agentic pipeline."""
    settings = load_settings()
    configure_logging(settings.logs_dir)
    adapter = RNASeqPipelineAdapter(settings=settings, console=console)
    objective = prompt or "Run an RNA-Seq analysis with QC, alignment, quantification, and reporting."
    result = adapter.run(
        objective=objective,
        working_directory=working_directory,
        output_dir=output,
        samplesheet=samplesheet,
        reference=reference,
        dry_run=dry_run,
        resume=resume,
    )
    console.print(Panel(str(result), title="RNA-Seq Run Result"))


@app.command("plan")
def plan(
    prompt: Annotated[str, typer.Argument(help="Natural language intent to plan")],
    workflow: Annotated[str, typer.Option("--workflow", help="Optional workflow override")] = "auto",
    samples: Annotated[str | None, typer.Option("--samples")] = None,
    reference: Annotated[str | None, typer.Option("--reference")] = None,
) -> None:
    """Create and print an execution plan without running it."""
    plan_steps = _build_plan(prompt, workflow, samples, reference)
    _render_plan(plan_steps, title="Plan Preview")


@app.command("analyze")
def analyze(
    prompt: Annotated[list[str], typer.Argument(help="Natural language analysis prompt", show_default=False)],
    working_directory: Annotated[Path, typer.Option("--working-directory", help="Experiment root directory")] = Path.cwd(),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview actions without execution")] = False,
    resume: Annotated[bool, typer.Option("--resume", help="Resume from checkpoint if available")] = False,
) -> None:
    """Natural-language analysis entrypoint."""
    objective = _objective_from_tokens(prompt)
    workflow = _infer_workflow(objective)
    result = _run_analysis(objective, workflow, working_directory, dry_run=dry_run, resume=resume)
    console.print(Panel(str(result), title="Analysis Result"))


@app.command("doctor")
def doctor() -> None:
    """Check local environment readiness with actionable diagnostics."""
    checks = [
        ("python", "python"),
        ("fastqc", "fastqc"),
        ("hisat2", "hisat2"),
        ("samtools", "samtools"),
        ("featureCounts", "featureCounts"),
        ("Rscript", "Rscript"),
    ]

    table = Table(title="Environment Doctor")
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("Hint")

    failures = 0
    for label, binary in checks:
        path = shutil.which(binary)
        if path:
            table.add_row(label, "OK", path)
        else:
            failures += 1
            table.add_row(
                label,
                "Missing",
                "Install via `mamba env update -f environment.yml` and re-run `ngs doctor`.",
            )

    console.print(table)

    if failures > 0:
        raise typer.Exit(code=1)


@app.command("debug")
def debug(command: Annotated[str, typer.Argument(help="Command to execute with selected backend")]) -> None:
    """Execute an arbitrary command through the selected backend for troubleshooting."""
    settings = load_settings()
    selector = BackendSelector()
    selected = selector.select(settings.backend_preference)

    result = selected.backend.run_command(
        CommandSpec(argv=shlex.split(command), stream_output=True),
        console,
    )
    raise typer.Exit(code=result.returncode)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
