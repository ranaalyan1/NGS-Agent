from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ngs_agent.agent.models import Plan, StepOutcome, VerificationIssue, VerificationReport
from ngs_agent.tools.permissions import IssueSeverity, StepStatus

logger = logging.getLogger(__name__)


def _collect_paths(value: Any) -> list[Path]:
    """Recursively collect path-like strings from a step outcome payload."""
    found: list[Path] = []
    if isinstance(value, str):
        candidate = Path(value)
        if value.startswith(("/", "./", "../", "~")) and candidate.suffix:
            found.append(candidate)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_collect_paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_collect_paths(item))
    return found


class VerifierAgent:
    """Verifies that each step's expected outputs actually exist on disk."""

    def verify(
        self,
        plan: Plan,
        outcomes: list[StepOutcome],
        search_roots: list[Path] | None = None,
    ) -> VerificationReport:
        issues: list[VerificationIssue] = []
        expected_outputs = {output for step in plan.steps for output in step.expected_outputs}
        produced_paths: list[Path] = []

        roots = [Path.cwd()]
        if plan.context.working_directory:
            roots.append(Path(plan.context.working_directory))
        roots.extend(search_roots or [])

        for outcome in outcomes:
            if outcome.status == StepStatus.FAILED:
                issues.append(
                    VerificationIssue(
                        severity=IssueSeverity.ERROR,
                        message=f"Step {outcome.step_name} failed with return code"
                            f"{outcome.returncode}.",
                        remediation="Inspect stderr, rerun the failed step, or resume from the"
                            "checkpoint.",
                    )
                )
            if outcome.status == StepStatus.SKIPPED:
                issues.append(
                    VerificationIssue(
                        severity=IssueSeverity.WARNING,
                        message=(
                            f"Step {outcome.step_name} was skipped "
                            f"({outcome.details.get('reason', 'unknown reason')})."
                        ),
                        remediation="Resolve the upstream failure and re-run; skipped steps"
                            "produced no outputs.",
                    )
                )
            produced_paths.extend(_collect_paths(outcome.artifacts))

        # An expected output counts as produced when a file with that name was
        # emitted by some step AND exists on disk (either at the recorded
        # path or anywhere under one of the search roots).
        missing_artifacts: list[str] = []
        for name in sorted(expected_outputs):
            if any(
                path.name == name or path.stem == Path(name).stem
                for path in produced_paths
                if path.exists()
            ):
                continue
            if any(next(root.rglob(name), None) is not None for root in roots):
                continue
            missing_artifacts.append(name)

        for item in missing_artifacts:
            issues.append(
                VerificationIssue(
                    severity=IssueSeverity.WARNING,
                    message=f"Expected artifact not observed on disk: {item}",
                    remediation="Check the selected backend, tool availability, and working"
                        "directory mounts.",
                )
            )

        passed = not any(issue.severity == IssueSeverity.ERROR for issue in issues)
        metrics = {
            "planned_steps": len(plan.steps),
            "executed_steps": len(outcomes),
            "failed_steps": sum(1 for outcome in outcomes if outcome.status == StepStatus.FAILED),
            "skipped_steps": sum(1 for outcome in outcomes if outcome.status == StepStatus.SKIPPED),
            "expected_artifacts": len(expected_outputs),
            "observed_artifacts": len(expected_outputs) - len(missing_artifacts),
        }

        if issues:
            logger.warning(
                "Verification found %d issues: %s", len(issues), [issue.message for issue in issues]
            )

        return VerificationReport(
            passed=passed,
            issues=issues,
            missing_artifacts=missing_artifacts,
            metrics=metrics,
        )
