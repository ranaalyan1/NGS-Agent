from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ngs_agent.agent.models import Plan, PlanStep, StepOutcome
from ngs_agent.artifacts.store import LocalArtifactStore
from ngs_agent.execution.models import CommandSpec
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.provenance.manifest import RunManifest
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.permissions import PermissionPolicy, StepStatus
from ngs_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class ExecutionState:
    plan: Plan
    outcomes: list[StepOutcome]
    resumed_from_checkpoint: bool = False
    wall_clock_seconds: float = 0.0
    max_observed_parallelism: int = 0
    failure: Exception | None = field(default=None)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _validate_dag(steps: list[PlanStep]) -> None:
    """Ensure step names are unique and dependencies form a DAG (no cycles)."""
    names = {step.name for step in steps}
    if len(names) != len(steps):
        duplicates = sorted(
            {step.name for step in steps if [s.name for s in steps].count(step.name) > 1}
        )
        raise ValueError(f"Plan contains duplicate step names: {', '.join(duplicates)}")
    unknown = {dep for step in steps for dep in step.dependencies if dep not in names}
    if unknown:
        raise ValueError(f"Plan steps reference unknown dependencies: {', '.join(sorted(unknown))}")

    # Kahn's algorithm for cycle detection.
    remaining = {step.name: set(step.dependencies) for step in steps}
    while remaining:
        ready = [name for name, deps in remaining.items() if not (deps & remaining.keys())]
        if not ready:
            raise ValueError(
                f"Dependency cycle detected among steps: {', '.join(sorted(remaining))}"
            )
        for name in ready:
            del remaining[name]


def _dependency_map(plan: Plan) -> dict[str, set[str]]:
    """Resolve each step's effective dependencies.

    When a plan declares no dependencies at all (e.g. plans from older
    planners or plain AI output), document order is used as an implicit chain
    so execution semantics stay safe. Plans that declare dependencies honour
    them exactly, which lets per-sample branches run concurrently.
    """
    if any(step.dependencies for step in plan.steps):
        return {step.name: set(step.dependencies) for step in plan.steps}
    chain: dict[str, set[str]] = {}
    for index, step in enumerate(plan.steps):
        chain[step.name] = {plan.steps[index - 1].name} if index > 0 else set()
    return chain


class ExecutorAgent:
    """Executes plan steps as a dependency graph with bounded parallelism.

    Steps whose dependencies are satisfied run concurrently (up to
    ``max_parallel``), so per-sample work (6 samples x qc/trim/align/sort) is
    no longer forced through a serial bottleneck. Failures are never
    swallowed: exceptions are logged with full tracebacks and captured in the
    step outcome, downstream steps are marked SKIPPED, and independent
    branches continue (or the run aborts when ``stop_on_error`` is set).
    """

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

    # ------------------------------------------------------------------
    # Single-step execution
    # ------------------------------------------------------------------
    def _fallback_execute(self, step: PlanStep, backend_name: str) -> StepOutcome:
        selected = self.backend_selector.select(backend_name)
        result = selected.backend.run_command(
            CommandSpec(
                argv=["echo", step.command_preview],
                description=step.description,
                stream_output=True,
            ),
            self.console,
        )
        status = StepStatus.OK if result.returncode == 0 else StepStatus.FAILED
        return StepOutcome(
            step_name=step.name,
            status=status,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            duration_seconds=result.duration_seconds,
            details={"backend": selected.backend.name, "command": result.command},
        )

    def _execute_step(self, step: PlanStep, context: ToolContext, backend_name: str) -> StepOutcome:
        started_at = _now()
        if step.tool_name:
            tool_result = self.tool_registry.execute(
                step.tool_name,
                step.tool_payload,
                context,
                lambda _message: True,
            )
            payload = (
                tool_result.model_dump(mode="json")
                if hasattr(tool_result, "model_dump")
                else dict(tool_result)
            )
            returncode = int(payload.get("returncode", 0) or 0)
            status = StepStatus.OK if returncode == 0 else StepStatus.FAILED
            return StepOutcome(
                step_name=step.name,
                status=status,
                stdout=str(payload.get("stdout", "")),
                stderr=str(payload.get("stderr", "")),
                returncode=returncode,
                artifacts=payload,
                duration_seconds=float(payload.get("duration_seconds", 0.0) or 0.0),
                started_at=started_at,
                finished_at=_now(),
                details={"tool_name": step.tool_name, "expected_outputs": step.expected_outputs},
            )
        return self._fallback_execute(step, backend_name)

    def _safe_execute_step(
        self, step: PlanStep, context: ToolContext, backend_name: str
    ) -> StepOutcome:
        """Run one step, converting any exception into a FAILED outcome.

        Exceptions are logged with full tracebacks (never silently ignored)
        and captured in the outcome so the verifier and report surface them.
        """
        self.console.print(f"[bold cyan]Step:[/bold cyan] {step.name} - {step.description}")
        try:
            outcome = self._execute_step(step, context, backend_name)
            if outcome.status == StepStatus.FAILED:
                logger.error(
                    "Step %s failed (rc=%s): %s",
                    step.name,
                    outcome.returncode,
                    (outcome.stderr or outcome.stdout)[-500:],
                )
            return outcome
        except Exception as exc:
            logger.exception("Step %s raised an exception", step.name)
            return StepOutcome(
                step_name=step.name,
                status=StepStatus.FAILED,
                stderr=f"{type(exc).__name__}: {exc}",
                returncode=-1,
                details={"error": str(exc), "traceback": traceback.format_exc()},
                started_at=_now(),
                finished_at=_now(),
            )

    # ------------------------------------------------------------------
    # Confirmation (pre-flight, in plan order, before any parallelism)
    # ------------------------------------------------------------------
    def _pre_flight_confirmations(self, plan: Plan) -> None:
        for step in plan.steps:
            needs = step.requires_confirmation or self.permission_policy.requires_confirmation(
                step.safety_level
            )
            if not needs:
                continue
            if not self.confirm_callback(
                f"Step '{step.name}' is {step.safety_level.value}. Continue?"
            ):
                raise PermissionError(f"Execution cancelled before step '{step.name}'")

    # ------------------------------------------------------------------
    # DAG execution
    # ------------------------------------------------------------------
    def execute(
        self,
        plan: Plan,
        backend_preference: str = "auto",
        dry_run: bool = False,
        max_parallel: int | None = None,
        stop_on_error: bool = False,
        manifest: RunManifest | None = None,
    ) -> ExecutionState:
        _validate_dag(plan.steps)
        selected_backend = self.backend_selector.select(backend_preference)
        max_parallel = max(1, max_parallel or plan.max_parallel_steps or 1)
        dependencies = _dependency_map(plan)

        if dry_run:
            return self._dry_run(plan, selected_backend.backend.name)

        self._pre_flight_confirmations(plan)

        outcomes: dict[str, StepOutcome] = {}
        pending: dict[str, set[str]] = {
            step.name: set(deps)
            for step, deps in zip(plan.steps, dependencies.values(), strict=False)
        }
        steps_by_name = {step.name: step for step in plan.steps}
        running: dict[Future[StepOutcome], str] = {}
        checkpoint_lock = threading.Lock()
        manifest_lock = threading.Lock()
        wall_start = time.perf_counter()
        aborted = False
        failure: Exception | None = None
        peak_parallelism = 0

        context = ToolContext(
            dry_run=False,
            permission_policy=self.permission_policy,
            backend_selector=self.backend_selector,
            backend_preference=backend_preference,
            console=self.console,
        )

        def _schedule_ready() -> None:
            nonlocal peak_parallelism
            if aborted:
                return
            for name, deps in list(pending.items()):
                if deps:
                    continue
                if len(running) >= max_parallel:
                    # Respect the worker budget: never submit more steps
                    # than can actually execute. Extra ready steps stay
                    # pending and are scheduled as slots free up, so
                    # peak_parallelism reflects real concurrency and an
                    # abort never runs queued-but-unstarted work.
                    break
                del pending[name]
                task_id = progress.add_task(f"Executing {name}", total=None)
                task_ids[name] = task_id
                future = pool.submit(
                    self._safe_execute_step,
                    steps_by_name[name],
                    context,
                    selected_backend.backend.name,
                )
                running[future] = name
                peak_parallelism = max(peak_parallelism, len(running))

        def _record(outcome: StepOutcome) -> None:
            outcomes[outcome.step_name] = outcome
            with checkpoint_lock:
                self.artifact_store.write_json(
                    f"checkpoints/{outcome.step_name}.json",
                    {
                        "plan": plan.model_dump(mode="json"),
                        "latest_step": outcome.model_dump(mode="json"),
                        "outcomes": [outcomes[name].model_dump(mode="json") for name in outcomes],
                    },
                )
            if manifest is not None:
                with manifest_lock:
                    manifest.record_outcome(
                        outcome.step_name,
                        outcome.artifacts,
                        tool=str(outcome.details.get("tool_name", "")),
                    )
            if outcome.step_name in task_ids:
                progress.update(
                    task_ids[outcome.step_name], description=f"Finished {outcome.step_name}"
                )
                progress.remove_task(task_ids[outcome.step_name])

        def _mark_skipped(name: str, reason: str) -> None:
            if name in outcomes:
                return
            pending.pop(name, None)
            outcomes[name] = StepOutcome(
                step_name=name,
                status=StepStatus.SKIPPED,
                details={"reason": reason},
                started_at=_now(),
                finished_at=_now(),
            )
            if name in task_ids:
                progress.remove_task(task_ids[name])

        with (
            Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress,
            ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="ngs-step") as pool,
        ):
            task_ids: dict[str, Any] = {}

            _schedule_ready()
            while running or pending:
                if not running:
                    # Nothing is running but steps remain pending: they can
                    # never become ready because an upstream dependency
                    # failed or was skipped.
                    for name in list(pending):
                        _mark_skipped(name, "Upstream dependencies did not complete successfully.")
                    break

                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                # Drain every completed future before considering an abort,
                # so a step that already finished successfully in the same
                # wave keeps its real outcome instead of being discarded.
                failed_step: str | None = None
                for future in done:
                    name = running.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:  # defensive: _safe_execute_step converts to outcomes
                        outcome = StepOutcome(
                            step_name=name,
                            status=StepStatus.FAILED,
                            stderr=str(exc),
                            returncode=-1,
                            details={"error": str(exc), "traceback": traceback.format_exc()},
                            started_at=_now(),
                            finished_at=_now(),
                        )
                    _record(outcome)
                    if (
                        outcome.status == StepStatus.FAILED
                        and stop_on_error
                        and failed_step is None
                    ):
                        failed_step = name

                if failed_step is not None:
                    aborted = True
                    failure = RuntimeError(
                        f"Step '{failed_step}' failed; aborting remaining "
                        "steps (stop_on_error=True)."
                    )
                    logger.error("Step %s failed; skipping remaining steps.", failed_step)
                    # Steps that never started are skipped; in-flight steps
                    # are joined and keep their TRUE outcomes — a step that
                    # is itself failing must not be masked as "skipped", and
                    # one that already succeeded keeps its success. (The
                    # thread pool joins at shutdown regardless, so this
                    # changes provenance, not wall-clock time.)
                    for name in list(pending):
                        _mark_skipped(name, "Aborted after an upstream failure (stop_on_error).")
                    for future, name in list(running.items()):
                        running.pop(future)
                        if future.cancel():
                            # Was still queued and never started.
                            _mark_skipped(
                                name, "Aborted after an upstream failure (stop_on_error)."
                            )
                            continue
                        try:
                            outcome = future.result()
                        except Exception as exc:  # defensive: wrapper converts to outcomes
                            outcome = StepOutcome(
                                step_name=name,
                                status=StepStatus.FAILED,
                                stderr=str(exc),
                                returncode=-1,
                                details={
                                    "error": str(exc),
                                    "traceback": traceback.format_exc(),
                                },
                                started_at=_now(),
                                finished_at=_now(),
                            )
                        _record(outcome)
                    break

                # Release dependencies satisfied by successful steps.
                for finished in [n for n, o in outcomes.items() if o.status == StepStatus.OK]:
                    for deps in pending.values():
                        deps.discard(finished)

                _schedule_ready()

        ordered = [outcomes[step.name] for step in plan.steps if step.name in outcomes]
        return ExecutionState(
            plan=plan,
            outcomes=ordered,
            wall_clock_seconds=time.perf_counter() - wall_start,
            max_observed_parallelism=peak_parallelism,
            failure=failure,
        )

    def _dry_run(self, plan: Plan, backend_name: str) -> ExecutionState:
        outcomes: list[StepOutcome] = []
        for step in plan.steps:
            outcome = StepOutcome(
                step_name=step.name,
                status=StepStatus.DRY_RUN,
                details={
                    "backend": backend_name,
                    "command_preview": step.command_preview,
                    "expected_outputs": step.expected_outputs,
                    "tool_name": step.tool_name,
                    "tool_payload": step.tool_payload,
                    "dependencies": step.dependencies,
                },
            )
            outcomes.append(outcome)
            self.artifact_store.write_json(f"dry-run/{step.name}.json", outcome.model_dump())
        return ExecutionState(plan=plan, outcomes=outcomes, resumed_from_checkpoint=False)
