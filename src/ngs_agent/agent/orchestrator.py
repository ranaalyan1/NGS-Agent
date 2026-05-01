from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ngs_agent.agent.executor import ExecutorAgent
from ngs_agent.agent.models import Plan, ReportBundle, VerificationReport
from ngs_agent.agent.planner import PlannerAgent
from ngs_agent.agent.reporter import ReporterAgent
from ngs_agent.agent.verifier import VerifierAgent
from ngs_agent.artifacts.store import LocalArtifactStore
from ngs_agent.config.settings import NGSSettings
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.reproducibility.checkpoint import CheckpointStore
from ngs_agent.tools.permissions import PermissionPolicy, SafetyLevel
from ngs_agent.tools.registry import ToolRegistry


class AgentOrchestrator:
    def __init__(
        self,
        settings: NGSSettings,
        backend_selector: BackendSelector,
        tool_registry: ToolRegistry,
        console: Console | None = None,
    ) -> None:
        self.settings = settings
        self.backend_selector = backend_selector
        self.tool_registry = tool_registry
        self.console = console or Console()
        self.artifact_store = LocalArtifactStore(Path(settings.artifacts_dir))
        self.checkpoints = CheckpointStore(Path(settings.artifacts_dir) / "checkpoints")
        self.permission_policy = PermissionPolicy(
            require_confirmation_for={SafetyLevel(level) for level in settings.require_confirmation_for},
            allow_destructive_without_confirmation=settings.allow_destructive_without_confirmation,
        )
        self.planner = PlannerAgent(settings, self.console)
        self.verifier = VerifierAgent()
        self.reporter = ReporterAgent(settings, self.artifact_store, self.console)

    def _render_plan(self, plan: Plan) -> None:
        table = Table(title=plan.title)
        table.add_column("Step")
        table.add_column("Safety")
        table.add_column("ETA (min)", justify="right")
        table.add_column("Preview")
        for step in plan.steps:
            table.add_row(step.name, step.safety_level.value, str(step.estimated_duration_minutes), step.command_preview)
        self.console.print(table)
        self.console.print(
            Panel(
                f"[bold]Objective:[/bold] {plan.objective}\n"
                f"[bold]Estimated duration:[/bold] {plan.estimated_duration_minutes} min\n"
                f"[bold]Estimated cost:[/bold] {plan.estimated_cost_label}\n"
                f"[bold]Context:[/bold] {plan.context.working_directory}",
                title="Plan Summary",
            )
        )
        if plan.risks:
            self.console.print(Panel("\n".join(f"- {risk}" for risk in plan.risks), title="Risks"))

    def _default_confirm(self, prompt: str) -> bool:
        return self.console.input(f"[bold yellow]{prompt} [/bold yellow][y/N] ").strip().lower() in {"y", "yes"}

    def run(
        self,
        objective: str,
        workflow: str = "rnaseq",
        working_directory: Path | None = None,
        dry_run: bool = False,
        resume: bool = False,
        confirm_callback: Callable[[str], bool] | None = None,
    ) -> dict[str, object]:
        working_directory = working_directory or Path.cwd()
        confirm = confirm_callback or self._default_confirm
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        context = self.planner.discover_context(working_directory)
        if resume:
            context.resume_token = run_id
            context.metadata["resume_requested"] = True

        plan = self.planner.create_plan(objective=objective, workflow=workflow, context=context, dry_run=dry_run)
        self._render_plan(plan)

        if dry_run:
            self.console.print("[green]Dry-run mode enabled. No execution will be performed.[/green]")
            self.checkpoints.save(run_id, {"stage": "dry-run", "plan": plan.model_dump(mode="json")})
            return {
                "run_id": run_id,
                "status": "dry-run",
                "plan": plan.model_dump(),
                "context": context.model_dump(),
            }

        if not confirm(f"Execute plan '{plan.title}' with {len(plan.steps)} steps?"):
            self.console.print("[yellow]Execution cancelled by user.[/yellow]")
            self.checkpoints.save(run_id, {"stage": "cancelled", "plan": plan.model_dump(mode="json")})
            return {"run_id": run_id, "status": "cancelled", "plan": plan.model_dump()}

        self.checkpoints.save(run_id, {"stage": "planned", "plan": plan.model_dump(mode="json")})
        executor = ExecutorAgent(
            backend_selector=self.backend_selector,
            tool_registry=self.tool_registry,
            console=self.console,
            permission_policy=self.permission_policy,
            confirm_callback=confirm,
            artifact_store=self.artifact_store,
        )
        execution_state = executor.execute(plan, backend_preference=self.settings.backend_preference, dry_run=False)
        self.checkpoints.save(
            run_id,
            {
                "stage": "executed",
                "plan": plan.model_dump(mode="json"),
                "outcomes": [outcome.model_dump() for outcome in execution_state.outcomes],
            },
        )

        verification: VerificationReport = self.verifier.verify(plan, execution_state.outcomes)
        self.checkpoints.save(
            run_id,
            {
                "stage": "verified",
                "plan": plan.model_dump(mode="json"),
                "verification": verification.model_dump(),
            },
        )

        report: ReportBundle = self.reporter.generate(
            plan,
            verification,
            [outcome.model_dump() for outcome in execution_state.outcomes],
        )
        self.checkpoints.save(
            run_id,
            {
                "stage": "reported",
                "plan": plan.model_dump(mode="json"),
                "verification": verification.model_dump(),
                "report": report.model_dump(),
            },
        )

        self.console.print(Panel(report.markdown_summary, title="Run Summary"))
        self.console.print(f"[bold green]Report written:[/bold green] {report.html_report}")

        return {
            "run_id": run_id,
            "status": "complete" if verification.passed else "completed-with-warnings",
            "plan": plan.model_dump(),
            "verification": verification.model_dump(),
            "report": report.model_dump(),
            "outcomes": [outcome.model_dump() for outcome in execution_state.outcomes],
            "checkpoint": str(self.checkpoints.state_for(run_id).checkpoint_file),
        }
