"""Tests for the verifier: expected outputs are checked against disk."""

from __future__ import annotations

from pathlib import Path

from ngs_agent.agent.models import ExperimentContext, Plan, PlanStep, StepOutcome
from ngs_agent.agent.verifier import VerifierAgent
from ngs_agent.tools.permissions import StepStatus


def make_plan(tmp_path: Path, expected: list[str]) -> Plan:
    return Plan(
        title="t",
        objective="o",
        workflow="rnaseq",
        summary="s",
        estimated_duration_minutes=1,
        estimated_cost_label="low",
        context=ExperimentContext(working_directory=tmp_path),
        steps=[
            PlanStep(name="step", description="d", command_preview="c", expected_outputs=expected)
        ],
    )


class TestVerifier:
    def test_artifact_on_disk_counts_as_produced(self, tmp_path: Path) -> None:
        (tmp_path / "aligned.sorted.bam").touch()
        plan = make_plan(tmp_path, ["aligned.sorted.bam"])
        report = VerifierAgent().verify(plan, [])
        assert report.missing_artifacts == []
        assert report.passed

    def test_artifact_in_outcome_payload_counts(self, tmp_path: Path) -> None:
        bam = tmp_path / "results" / "aligned.sorted.bam"
        bam.parent.mkdir()
        bam.touch()
        plan = make_plan(tmp_path, ["aligned.sorted.bam"])
        outcome = StepOutcome(step_name="step", artifacts={"bam_path": str(bam)})
        report = VerifierAgent().verify(plan, [outcome])
        assert "aligned.sorted.bam" not in report.missing_artifacts

    def test_missing_artifact_flagged_with_warning(self, tmp_path: Path) -> None:
        plan = make_plan(tmp_path, ["counts.txt"])
        report = VerifierAgent().verify(plan, [])
        assert report.missing_artifacts == ["counts.txt"]
        assert report.passed  # warnings do not fail the run, errors do

    def test_failed_step_is_an_error(self, tmp_path: Path) -> None:
        plan = make_plan(tmp_path, [])
        outcome = StepOutcome(step_name="step", status=StepStatus.FAILED, returncode=2)
        report = VerifierAgent().verify(plan, [outcome])
        assert not report.passed
        assert report.issues[0].severity.value == "error"

    def test_skipped_step_is_a_warning(self, tmp_path: Path) -> None:
        plan = make_plan(tmp_path, [])
        outcome = StepOutcome(
            step_name="step", status=StepStatus.SKIPPED, details={"reason": "upstream failed"}
        )
        report = VerifierAgent().verify(plan, [outcome])
        assert report.passed
        assert any("skipped" in issue.message for issue in report.issues)
        assert report.metrics["skipped_steps"] == 1

    def test_search_roots_include_nested_directories(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        (root / "reports").mkdir(parents=True)
        (root / "reports" / "report.html").touch()
        plan = make_plan(tmp_path, ["report.html"])
        report = VerifierAgent().verify(plan, [], search_roots=[root])
        assert report.missing_artifacts == []
