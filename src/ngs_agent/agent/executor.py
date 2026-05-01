from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ngs_agent.agent.models import Plan, PlanStep, StepOutcome
from ngs_agent.artifacts.store import LocalArtifactStore
from ngs_agent.execution.models import CommandSpec
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.permissions import PermissionPolicy
from ngs_agent.tools.registry import ToolRegistry


@dataclass
class ExecutionState:
    plan: Plan
    outcomes: list[StepOutcome]
    resumed_from_checkpoint: bool = False


class ExecutorAgent:
    def __init__(
        self,
        backend_selector: BackendSelector,
        tool_registry: ToolRegistry,
        console: Console,
        permission_policy: PermissionPolicy,
        confirm_callback: Callable[[str], bool],
        artifact_store: LocalArtifactStore,
    ) -> None:
        self.backend_selector = backend_selector
        self.tool_registry = tool_registry
        self.console = console
        self.permission_policy = permission_policy
        self.confirm_callback = confirm_callback
        self.artifact_store = artifact_store

    def _fallback_execute(self, step: PlanStep, backend_name: str) -> StepOutcome:
        result = self.backend_selector.select(backend_name).backend.run_command(
            CommandSpec(argv=["echo", step.command_preview], description=step.description, stream_output=True),
            self.console,
        )
        return StepOutcome(
            step_name=step.name,
            status="ok" if result.returncode == 0 else "failed",
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            details={"backend": backend_name, "command": result.command},
        )

    def execute(self, plan: Plan, backend_preference: str = "auto", dry_run: bool = False) -> ExecutionState:
        selected_backend = self.backend_selector.select(backend_preference)
        outcomes: list[StepOutcome] = []
        context = ToolContext(
            dry_run=dry_run,
            permission_policy=self.permission_policy,
            backend_selector=self.backend_selector,
            backend_preference=backend_preference,
            console=self.console,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
        ) as progress:
            for step in plan.steps:
                task = progress.add_task(f"Executing {step.name}", total=None)
                self.console.print(f"[bold cyan]Step:[/bold cyan] {step.name} - {step.description}")

                if step.requires_confirmation or self.permission_policy.requires_confirmation(step.safety_level):
                    if not self.confirm_callback(
                        f"Step '{step.name}' is {step.safety_level.value}. Continue?"
                    ):
                        raise PermissionError(f"Execution cancelled before step '{step.name}'")

                if dry_run:
                    outcome = StepOutcome(
                        step_name=step.name,
                        status="dry-run",
                        details={
                            "backend": selected_backend.backend.name,
                            "command_preview": step.command_preview,
                            "expected_outputs": step.expected_outputs,
                        },
                    )
                    outcomes.append(outcome)
                    self.artifact_store.write_json(f"dry-run/{step.name}.json", outcome.model_dump())
                    progress.update(task, description=f"Dry-run {step.name} complete")
                    progress.remove_task(task)
                    continue

                if step.tool_name:
                    tool_result = self.tool_registry.execute(
                        step.tool_name,
                        step.tool_payload,
                        context,
                        lambda _message: True,
                    )
                    outcome = StepOutcome(
                        step_name=step.name,
                        status="ok",
                        artifacts=tool_result.model_dump() if hasattr(tool_result, "model_dump") else {},
                        details={"tool_name": step.tool_name},
                    )
                else:
                    outcome = self._fallback_execute(step, selected_backend.backend.name)

                checkpoint_payload = {
                    "plan": plan.model_dump(mode="json"),
                    "latest_step": outcome.model_dump(),
                    "outcomes": [item.model_dump() for item in outcomes + [outcome]],
                }
                self.artifact_store.write_json(f"checkpoints/{step.name}.json", checkpoint_payload)
                outcomes.append(outcome)
                progress.update(task, description=f"Finished {step.name}")
                progress.remove_task(task)

        return ExecutionState(plan=plan, outcomes=outcomes, resumed_from_checkpoint=False)
