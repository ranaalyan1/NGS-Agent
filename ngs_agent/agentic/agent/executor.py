from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ngs_agent.agentic.agent.models import Plan, PlanStep, StepOutcome
from ngs_agent.agentic.artifacts.store import LocalArtifactStore
from ngs_agent.agentic.execution.selector import BackendSelector
from ngs_agent.agentic.tools.base import ToolContext
from ngs_agent.agentic.tools.permissions import PermissionPolicy, StepStatus
from ngs_agent.agentic.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Steps that are performed by the agents themselves (planner/reporter) rather
# than by an external tool; they succeed without invoking a backend command.
_INTERNAL_STEPS = {"discover-context", "report"}


@dataclass
class ExecutionState:
    plan: Plan
    outcomes: list[StepOutcome]
    resumed_from_checkpoint: bool = False


class ExecutorAgent:
    """Executes pipeline steps with proper permission handling and checkpointing."""

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

    def _execute_tool_step(self, step: PlanStep, context: ToolContext) -> StepOutcome:
        try:
            # Step-level confirmation already happened in execute(); pass a
            # no-op confirm callback so the tool does not double-prompt.
            tool_result = self.tool_registry.execute(
                step.tool_name,
                step.tool_payload,
                context,
                lambda _message: True,
            )
        except Exception as exc:
            logger.error(f"Tool '{step.tool_name}' failed for step '{step.name}': {exc}", exc_info=True)
            return StepOutcome(
                step_name=step.name,
                status=StepStatus.FAILED,
                stderr=str(exc),
                returncode=1,
                details={"tool_name": step.tool_name, "error": str(exc)},
            )

        result_data = tool_result.model_dump(mode="json") if hasattr(tool_result, "model_dump") else {}
        returncode = int(result_data.get("returncode", 0))
        status = StepStatus.OK if returncode == 0 else StepStatus.FAILED
        return StepOutcome(
            step_name=step.name,
            status=status,
            stdout=str(result_data.get("stdout", "")),
            stderr=str(result_data.get("stderr", "")),
            returncode=returncode,
            artifacts=result_data,
            details={"tool_name": step.tool_name},
        )

    def _internal_step_outcome(self, step: PlanStep) -> StepOutcome:
        if step.name in _INTERNAL_STEPS:
            return StepOutcome(
                step_name=step.name,
                status=StepStatus.OK,
                details={"note": "Handled internally by the agent (no external tool invocation)."},
            )
        logger.warning(
            f"Step '{step.name}' has no tool bound and is not a known internal step; marking as skipped."
        )
        return StepOutcome(
            step_name=step.name,
            status=StepStatus.SKIPPED,
            details={"note": "No tool bound to this step; nothing was executed."},
        )

    def execute(self, plan: Plan, backend_preference: str = "auto", dry_run: bool = False) -> ExecutionState:
        selected_backend = self.backend_selector.select(backend_preference)
        outcomes: list[StepOutcome] = []
        failed_steps: set[str] = set()
        skipped_steps: set[str] = set()
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

                # Planner marked this step as not runnable in the current context.
                if step.skip_reason:
                    self.console.print(f"[yellow]Skipping {step.name}: {step.skip_reason}[/yellow]")
                    outcome = StepOutcome(
                        step_name=step.name,
                        status=StepStatus.SKIPPED,
                        details={"skip_reason": step.skip_reason},
                    )
                    skipped_steps.add(step.name)
                    outcomes.append(outcome)
                    progress.remove_task(task)
                    continue

                # Skip steps whose dependencies failed or were skipped.
                blocked_by = [dep for dep in step.dependencies if dep in failed_steps or dep in skipped_steps]
                if blocked_by:
                    reason = f"Dependency not satisfied: {', '.join(blocked_by)}"
                    self.console.print(f"[yellow]Skipping {step.name}: {reason}[/yellow]")
                    outcome = StepOutcome(
                        step_name=step.name,
                        status=StepStatus.SKIPPED,
                        details={"skip_reason": reason},
                    )
                    skipped_steps.add(step.name)
                    outcomes.append(outcome)
                    progress.remove_task(task)
                    continue

                if step.requires_confirmation or self.permission_policy.requires_confirmation(step.safety_level):
                    if not self.confirm_callback(
                        f"Step '{step.name}' is {step.safety_level.value}. Continue?"
                    ):
                        raise PermissionError(f"Execution cancelled before step '{step.name}'")

                if dry_run:
                    outcome = StepOutcome(
                        step_name=step.name,
                        status=StepStatus.DRY_RUN,
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
                    outcome = self._execute_tool_step(step, context)
                else:
                    outcome = self._internal_step_outcome(step)

                if outcome.status == StepStatus.FAILED:
                    failed_steps.add(step.name)
                    self.console.print(f"[bold red]Step {step.name} failed (rc={outcome.returncode}).[/bold red]")
                elif outcome.status == StepStatus.SKIPPED:
                    skipped_steps.add(step.name)

                outcomes.append(outcome)
                checkpoint_payload = {
                    "plan": plan.model_dump(mode="json"),
                    "latest_step": outcome.model_dump(),
                    "outcomes": [item.model_dump() for item in outcomes],
                }
                self.artifact_store.write_json(f"checkpoints/{step.name}.json", checkpoint_payload)
                progress.update(task, description=f"Finished {step.name}")
                progress.remove_task(task)

        return ExecutionState(plan=plan, outcomes=outcomes, resumed_from_checkpoint=False)
