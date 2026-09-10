from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ngs_agent.agentic.agent.models import Plan, StepOutcome, VerificationIssue, VerificationReport
from ngs_agent.agentic.tools.permissions import IssueSeverity, StepStatus

logger = logging.getLogger(__name__)


def _collect_artifact_paths(value: Any, into: set[str]) -> None:
    """Recursively collect string values that look like file paths from tool outputs."""
    if isinstance(value, str):
        if value:
            into.add(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_artifact_paths(item, into)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_artifact_paths(item, into)


class VerifierAgent:
    def verify(self, plan: Plan, outcomes: list[StepOutcome]) -> VerificationReport:
        issues: list[VerificationIssue] = []
        outcome_by_step = {outcome.step_name: outcome for outcome in outcomes}

        skipped_steps = {
            outcome.step_name for outcome in outcomes if outcome.status == StepStatus.SKIPPED
        }
        dry_run = any(outcome.status == StepStatus.DRY_RUN for outcome in outcomes)

        # Only verify expected outputs for steps that actually executed.
        expected_outputs = {
            output
            for step in plan.steps
            if step.name not in skipped_steps and step.name in outcome_by_step
            for output in step.expected_outputs
        }

        produced_paths: set[str] = set()
        executed_steps = 0
        for outcome in outcomes:
            if outcome.status == StepStatus.SKIPPED:
                reason = outcome.details.get("skip_reason", "unknown") if isinstance(outcome.details, dict) else "unknown"
                issues.append(
                    VerificationIssue(
                        severity=IssueSeverity.WARNING,
                        message=f"Step {outcome.step_name} was skipped: {reason}",
                        remediation="Provide the missing inputs (reads, index, annotation, or samplesheet) and re-run.",
                    )
                )
                continue
            executed_steps += 1
            if outcome.status == StepStatus.FAILED or outcome.returncode != 0:
                issues.append(
                    VerificationIssue(
                        severity=IssueSeverity.ERROR,
                        message=f"Step {outcome.step_name} failed with return code {outcome.returncode}.",
                        remediation="Inspect stderr, rerun the failed step, or resume from the checkpoint.",
                    )
                )
            _collect_artifact_paths(outcome.artifacts, produced_paths)
            details = outcome.details.get("expected_outputs") if isinstance(outcome.details, dict) else None
            if isinstance(details, list):
                produced_paths.update(str(item) for item in details)

        # An expected output is satisfied if a produced path ends with it or it exists on disk.
        missing_artifacts: list[str] = []
        if not dry_run:
            produced_names = {Path(p).name for p in produced_paths}
            for expected in sorted(expected_outputs):
                expected_name = Path(expected).name
                if expected_name in produced_names:
                    matching = [p for p in produced_paths if Path(p).name == expected_name]
                    if any(Path(p).exists() for p in matching):
                        continue
                if Path(expected).exists():
                    continue
                missing_artifacts.append(expected)

        for item in missing_artifacts:
            issues.append(
                VerificationIssue(
                    severity=IssueSeverity.WARNING,
                    message=f"Expected artifact not observed: {item}",
                    remediation="Check the selected backend, tool availability, and working directory mounts.",
                )
            )

        passed = not any(issue.severity == IssueSeverity.ERROR for issue in issues)
        metrics = {
            "planned_steps": len(plan.steps),
            "executed_steps": executed_steps,
            "skipped_steps": len(skipped_steps),
            "expected_artifacts": len(expected_outputs),
            "observed_artifacts": len(produced_paths),
        }

        if issues:
            logger.warning(f"Verification found {len(issues)} issues: {[i.message for i in issues]}")

        return VerificationReport(
            passed=passed,
            issues=issues,
            missing_artifacts=missing_artifacts,
            metrics=metrics,
        )
