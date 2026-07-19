from __future__ import annotations

import logging
from pathlib import Path

from ngs_agent.agent.models import Plan, StepOutcome, VerificationIssue, VerificationReport
from ngs_agent.tools.permissions import IssueSeverity, StepStatus

logger = logging.getLogger(__name__)


class VerifierAgent:
    def verify(self, plan: Plan, outcomes: list[StepOutcome]) -> VerificationReport:
        issues: list[VerificationIssue] = []
        expected_outputs = {output for step in plan.steps for output in step.expected_outputs}
        produced_outputs: set[str] = set()

        for outcome in outcomes:
            if outcome.returncode != 0:
                issues.append(
                    VerificationIssue(
                        severity=IssueSeverity.ERROR,
                        message=f"Step {outcome.step_name} failed with return code {outcome.returncode}.",
                        remediation="Inspect stderr, rerun the failed step, or resume from the checkpoint.",
                    )
                )
            produced_outputs.update(outcome.artifacts.keys())
            details = outcome.details.get("expected_outputs") if isinstance(outcome.details, dict) else None
            if isinstance(details, list):
                produced_outputs.update(str(item) for item in details)

        missing_artifacts = sorted(expected_outputs - produced_outputs)
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
            "executed_steps": len(outcomes),
            "expected_artifacts": len(expected_outputs),
            "observed_artifacts": len(produced_outputs),
        }
        
        if issues:
            logger.warning(f"Verification found {len(issues)} issues: {[i.message for i in issues]}")
        
        return VerificationReport(
            passed=passed,
            issues=issues,
            missing_artifacts=missing_artifacts,
            metrics=metrics,
        )
