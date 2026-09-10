"""Tests for the parallel DAG executor: concurrency, ordering, failure handling."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from rich.console import Console

from ngs_agent.agent.executor import ExecutorAgent, _validate_dag
from ngs_agent.agent.models import ExperimentContext, Plan, PlanStep
from ngs_agent.artifacts.store import LocalArtifactStore
from ngs_agent.execution.models import CommandSpec
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.tools.base import Tool, ToolContext
from ngs_agent.tools.permissions import PermissionPolicy, SafetyLevel, StepStatus
from ngs_agent.tools.registry import ToolRegistry


class SleepInput(BaseModel):
    seconds: float = 0.05


class SleepOutput(BaseModel):
    tool_name: str = "sleep"
    slept: float = 0.0
    returncode: int = 0


class SleepTool(Tool[SleepInput, SleepOutput]):
    name = "sleep"
    description = "Sleep for a while (test double)."
    safety_level = SafetyLevel.READ
    estimated_cost = "low"
    dry_run_support = True
    input_model = SleepInput
    output_model = SleepOutput

    def execute(self, payload: SleepInput, context: ToolContext) -> SleepOutput:
        time.sleep(payload.seconds)
        return SleepOutput(slept=payload.seconds)


class CrashInput(BaseModel):
    message: str = "boom"
    delay: float = 0.0  # optional sleep before raising (for race tests)


class CrashOutput(BaseModel):
    tool_name: str = "crash"


class CrashTool(Tool[CrashInput, CrashOutput]):
    name = "crash"
    description = "Always raises (test double)."
    safety_level = SafetyLevel.READ
    estimated_cost = "low"
    dry_run_support = True
    input_model = CrashInput
    output_model = CrashOutput

    def execute(self, payload: CrashInput, context: ToolContext) -> CrashOutput:
        if payload.delay > 0:
            time.sleep(payload.delay)
        raise RuntimeError(payload.message)


class FailingShellInput(BaseModel):
    pass


class FailingShellOutput(BaseModel):
    returncode: int = 1
    stdout: str = ""
    stderr: str = ""


class FailingShellTool(Tool[FailingShellInput, FailingShellOutput]):
    name = "failing-shell"
    description = "Exits nonzero via the native backend (test double)."
    safety_level = SafetyLevel.READ
    estimated_cost = "low"
    dry_run_support = True
    input_model = FailingShellInput
    output_model = FailingShellOutput

    def execute(self, payload: FailingShellInput, context: ToolContext) -> FailingShellOutput:
        backend = context.backend_selector.select(context.backend_preference).backend
        result = backend.run_command(
            CommandSpec(argv=["sh", "-c", "echo oops >&2; exit 1"]), Console(quiet=True)
        )
        return FailingShellOutput(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )


def make_executor(tmp_path: Path, tools: list[Tool[Any, Any]] | None = None) -> ExecutorAgent:
    registry = ToolRegistry()
    for tool in tools or []:
        registry.register(tool)
    return ExecutorAgent(
        backend_selector=BackendSelector(),
        tool_registry=registry,
        console=Console(quiet=True, width=200),
        permission_policy=PermissionPolicy(require_confirmation_for=set()),
        confirm_callback=lambda _prompt: True,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )


def make_plan(steps: list[PlanStep], context_dir: Path) -> Plan:
    return Plan(
        title="test plan",
        objective="test",
        workflow="rnaseq",
        summary="",
        estimated_duration_minutes=1,
        estimated_cost_label="low",
        context=ExperimentContext(working_directory=context_dir),
        steps=steps,
        max_parallel_steps=8,
    )


def step(
    name: str,
    dependencies: list[str] | None = None,
    tool: str | None = None,
    payload: dict | None = None,
) -> PlanStep:
    return PlanStep(
        name=name,
        description=name,
        command_preview=name,
        tool_name=tool,
        tool_payload=payload or {},
        dependencies=dependencies or [],
    )


class TestDagValidation:
    def test_cycle_detected(self, tmp_path: Path) -> None:
        steps = [
            step("a", ["b"]),
            step("b", ["a"]),
        ]
        with pytest.raises(ValueError, match="cycle"):
            _validate_dag(steps)

    def test_unknown_dependency_detected(self) -> None:
        with pytest.raises(ValueError, match="unknown dependencies"):
            _validate_dag([step("a", ["ghost"])])

    def test_duplicate_names_detected(self) -> None:
        with pytest.raises(ValueError, match="duplicate step names"):
            _validate_dag([step("a"), step("a")])


class TestParallelExecution:
    def test_independent_steps_run_concurrently(self, tmp_path: Path) -> None:
        """6 samples x sleep(0.3) must take far less than 6 x 0.3s serially.

        Mirrors planner output: a discover-context root, then one branch per
        sample whose only dependency is the root step.
        """
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [step("discover-context", dependencies=[])] + [
            step(
                f"qc-S{i}",
                dependencies=["discover-context"],
                tool="sleep",
                payload={"seconds": 0.3},
            )
            for i in range(6)
        ]
        plan = make_plan(steps, tmp_path)
        state = executor.execute(plan, backend_preference="native")
        assert all(outcome.status == StepStatus.OK for outcome in state.outcomes)
        assert state.max_observed_parallelism >= 4
        assert state.wall_clock_seconds < 6 * 0.3  # would be ~1.8s serial

    def test_worker_budget_caps_real_concurrency(self, tmp_path: Path) -> None:
        """More ready steps than workers: reported peak parallelism must
        reflect EXECUTION, not the number of submitted futures."""
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [step("discover-context", dependencies=[])] + [
            step(
                f"lane-{i}",
                dependencies=["discover-context"],
                tool="sleep",
                payload={"seconds": 0.05},
            )
            for i in range(8)
        ]
        plan = make_plan(steps, tmp_path).model_copy(update={"max_parallel_steps": 2})
        state = executor.execute(plan, backend_preference="native")
        assert all(outcome.status == StepStatus.OK for outcome in state.outcomes)
        assert state.max_observed_parallelism == 2  # never above the budget

    def test_dependent_steps_wait_for_dependencies(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [
            step("first", [], tool="sleep", payload={"seconds": 0.2}),
            step("second", ["first"], tool="sleep", payload={"seconds": 0.0}),
        ]
        plan = make_plan(steps, tmp_path)
        state = executor.execute(plan, backend_preference="native")
        assert state.max_observed_parallelism == 1
        assert [outcome.step_name for outcome in state.outcomes] == ["first", "second"]

    def test_plan_without_dependencies_runs_in_document_order(self, tmp_path: Path) -> None:
        # Safety rule: legacy plans (no dependencies declared anywhere) must
        # keep sequential semantics.
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [
            step("a", tool="sleep", payload={"seconds": 0.15}),
            step("b", tool="sleep", payload={"seconds": 0.0}),
            step("c", tool="sleep", payload={"seconds": 0.0}),
        ]
        plan = make_plan(steps, tmp_path)
        state = executor.execute(plan, backend_preference="native")
        assert state.max_observed_parallelism == 1

    def test_fan_in_waits_for_all_branches(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [
            step("s1", [], tool="sleep", payload={"seconds": 0.1}),
            step("s2", [], tool="sleep", payload={"seconds": 0.3}),
            step("merge", ["s1", "s2"]),
        ]
        state = executor.execute(make_plan(steps, tmp_path), backend_preference="native")
        assert [o.step_name for o in state.outcomes][-1] == "merge"


class TestFailureHandling:
    def test_exception_is_captured_not_swallowed(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [CrashTool()])
        steps = [
            step("crashy", [], tool="crash", payload={"message": "kaboom"}),
            step("independent", []),
        ]
        state = executor.execute(make_plan(steps, tmp_path), backend_preference="native")
        failed = next(o for o in state.outcomes if o.step_name == "crashy")
        assert failed.status == StepStatus.FAILED
        assert "kaboom" in failed.stderr
        assert "traceback" in failed.details
        assert failed.returncode == -1

    def test_stop_on_error_skips_downstream_but_runs_independent(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [CrashTool(), SleepTool()])
        steps = [
            # The independent step finishes well before the crash, so its
            # success is recorded before the abort decision (deterministic).
            step("crashy", [], tool="crash", payload={"delay": 0.25}),
            step("downstream", ["crashy"], tool="sleep", payload={"seconds": 0.0}),
            step("independent", [], tool="sleep", payload={"seconds": 0.05}),
        ]
        state = executor.execute(
            make_plan(steps, tmp_path), backend_preference="native", stop_on_error=True
        )
        by_name = {o.step_name: o for o in state.outcomes}
        assert by_name["crashy"].status == StepStatus.FAILED
        assert by_name["downstream"].status == StepStatus.SKIPPED
        assert by_name["independent"].status == StepStatus.OK
        assert state.failure is not None

    def test_inflight_failure_is_not_masked_as_skipped(self, tmp_path: Path) -> None:
        """When a failure triggers the abort, a sibling step that is itself
        failing (completing just after the abort decision) must be recorded
        with its TRUE failed outcome, not as skipped."""
        executor = make_executor(tmp_path, [SleepTool(), CrashTool()])
        steps = [
            step("root", [], tool="sleep", payload={"seconds": 0.0}),
            # Sibling A fails at t=0.15s, B fails at t=0.35s: B is still
            # in flight when A's failure triggers the abort, and must keep
            # its true FAILED outcome.
            step("fails-fast", ["root"], tool="crash", payload={"delay": 0.15}),
            step("fails-slow", ["root"], tool="crash", payload={"delay": 0.35}),
            step("needs-b", ["fails-slow"], tool="sleep", payload={"seconds": 0.0}),
        ]
        state = executor.execute(
            make_plan(steps, tmp_path), backend_preference="native", stop_on_error=True
        )
        by_name = {o.step_name: o for o in state.outcomes}
        assert by_name["fails-fast"].status == StepStatus.FAILED
        assert by_name["fails-slow"].status == StepStatus.FAILED  # true outcome kept
        assert "boom" in by_name["fails-slow"].stderr
        assert by_name["needs-b"].status == StepStatus.SKIPPED  # never started
        assert state.failure is not None

    def test_abort_cancels_queued_steps_beyond_worker_budget(self, tmp_path: Path) -> None:
        """With more ready steps than workers, the abort must cancel steps
        that are still queued (never started) while in-flight steps keep
        their true outcomes."""
        executor = make_executor(tmp_path, [CrashTool(), SleepTool()])
        steps = [
            step("root", [], tool="sleep", payload={"seconds": 0.0}),
            step("boom", ["root"], tool="crash", payload={"delay": 0.2}),
            step("lane-1", ["root"], tool="sleep", payload={"seconds": 0.5}),
            step("lane-2", ["root"], tool="sleep", payload={"seconds": 0.5}),
            step("lane-3", ["root"], tool="sleep", payload={"seconds": 0.5}),
        ]
        plan = make_plan(steps, tmp_path).model_copy(update={"max_parallel_steps": 2})
        start = time.perf_counter()
        state = executor.execute(plan, backend_preference="native", stop_on_error=True)
        elapsed = time.perf_counter() - start
        by_name = {o.step_name: o for o in state.outcomes}
        assert by_name["boom"].status == StepStatus.FAILED
        assert by_name["lane-1"].status == StepStatus.OK  # was in flight; joined truly
        assert by_name["lane-2"].status == StepStatus.SKIPPED  # queued; cancelled
        assert by_name["lane-3"].status == StepStatus.SKIPPED  # queued; cancelled
        assert elapsed < 1.4  # only one 0.5s lane actually ran

    def test_without_stop_on_error_dependents_are_skipped_and_run_continues(
        self, tmp_path: Path
    ) -> None:
        executor = make_executor(tmp_path, [CrashTool(), SleepTool()])
        steps = [
            step("crashy", [], tool="crash", payload={}),
            step("needs-crash", ["crashy"], tool="sleep", payload={}),
            step("fine", [], tool="sleep", payload={"seconds": 0.0}),
            step("after-fine", ["fine"], tool="sleep", payload={"seconds": 0.0}),
        ]
        state = executor.execute(
            make_plan(steps, tmp_path), backend_preference="native", stop_on_error=False
        )
        by_name = {o.step_name: o for o in state.outcomes}
        assert by_name["crashy"].status == StepStatus.FAILED
        assert by_name["needs-crash"].status == StepStatus.SKIPPED
        assert by_name["fine"].status == StepStatus.OK
        assert by_name["after-fine"].status == StepStatus.OK

    def test_tool_nonzero_exit_marks_step_failed(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [FailingShellTool()])
        state = executor.execute(
            make_plan([step("bad", [], tool="failing-shell", payload={})], tmp_path),
            backend_preference="native",
        )
        assert state.outcomes[0].status == StepStatus.FAILED
        assert "oops" in state.outcomes[0].stderr

    def test_outcomes_are_returned_in_plan_order(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [
            step("slow", [], tool="sleep", payload={"seconds": 0.2}),
            step("fast", [], tool="sleep", payload={"seconds": 0.0}),
        ]
        state = executor.execute(make_plan(steps, tmp_path), backend_preference="native")
        assert [o.step_name for o in state.outcomes] == ["slow", "fast"]


class TestCheckpoints:
    def test_checkpoint_written_per_step(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [step("a", tool="sleep", payload={"seconds": 0.0})]
        executor.execute(make_plan(steps, tmp_path), backend_preference="native")
        checkpoint = tmp_path / "artifacts" / "checkpoints" / "a.json"
        assert checkpoint.exists()
        import json

        payload = json.loads(checkpoint.read_text())
        assert payload["latest_step"]["step_name"] == "a"

    def test_confirmation_declined_raises(self, tmp_path: Path) -> None:
        executor = ExecutorAgent(
            backend_selector=BackendSelector(),
            tool_registry=ToolRegistry(),
            console=Console(quiet=True),
            permission_policy=PermissionPolicy(require_confirmation_for={SafetyLevel.EXPENSIVE}),
            confirm_callback=lambda _prompt: False,
            artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        )
        expensive = PlanStep(
            name="expensive-step",
            description="",
            command_preview="",
            safety_level=SafetyLevel.EXPENSIVE,
        )
        with pytest.raises(PermissionError, match="cancelled"):
            executor.execute(make_plan([expensive], tmp_path), backend_preference="native")


class TestDryRun:
    def test_dry_run_records_previews_without_executing(self, tmp_path: Path) -> None:
        executor = make_executor(tmp_path, [SleepTool()])
        steps = [
            step("a", [], tool="sleep", payload={"seconds": 5}),
        ]
        started = time.perf_counter()
        state = executor.execute(
            make_plan(steps, tmp_path), backend_preference="native", dry_run=True
        )
        assert time.perf_counter() - started < 2  # did not sleep
        assert state.outcomes[0].status == StepStatus.DRY_RUN
        assert state.outcomes[0].details["tool_payload"] == {"seconds": 5}
