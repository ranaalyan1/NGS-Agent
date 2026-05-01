from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from rich.console import Console

from ngs_agent.agent.models import Plan, ReportBundle, VerificationReport
from ngs_agent.artifacts.store import LocalArtifactStore
from ngs_agent.config.settings import NGSSettings


class ReporterAgent:
    def __init__(self, settings: NGSSettings, artifact_store: LocalArtifactStore, console: Console) -> None:
        self.settings = settings
        self.artifact_store = artifact_store
        self.console = console

    def _ai_insights(self, plan: Plan, verification: VerificationReport) -> str:
        if not self.settings.anthropic_api_key:
            lines = [
                f"Workflow: {plan.workflow}",
                f"Status: {'passed' if verification.passed else 'needs attention'}",
                f"Planned duration: {plan.estimated_duration_minutes} minutes",
            ]
            if verification.missing_artifacts:
                lines.append(f"Missing artifacts: {', '.join(verification.missing_artifacts)}")
            return "\n".join(lines)

        try:
            client = Anthropic(api_key=self.settings.anthropic_api_key)
            response = client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=700,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "You are an expert bioinformatics reporter. Summarize the run, identify risks, "
                            "and recommend next actions in concise markdown. "
                            f"Plan: {plan.model_dump(mode='json')}\n"
                            f"Verification: {verification.model_dump(mode='json')}"
                        ),
                    }
                ],
            )
            return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        except Exception as exc:
            return f"AI summary unavailable: {exc}"

    def generate(self, plan: Plan, verification: VerificationReport, outcomes: list[dict[str, object]]) -> ReportBundle:
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        markdown_summary = [
            f"# NGS Run Report",
            f"- Workflow: {plan.workflow}",
            f"- Objective: {plan.objective}",
            f"- Status: {'passed' if verification.passed else 'needs attention'}",
            f"- Generated: {timestamp}",
            "",
            "## Verification",
            f"- Planned steps: {verification.metrics.get('planned_steps', 0)}",
            f"- Executed steps: {verification.metrics.get('executed_steps', 0)}",
            f"- Missing artifacts: {', '.join(verification.missing_artifacts) if verification.missing_artifacts else 'none'}",
            "",
            "## Risks",
            *[f"- {risk}" for risk in plan.risks],
            "",
            "## Next Actions",
            *[f"- {action}" for action in plan.next_actions],
        ]
        ai_insights = self._ai_insights(plan, verification)
        html_report = f"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>NGS Report - {plan.workflow}</title>
    <style>
      body {{ font-family: Inter, Arial, sans-serif; margin: 2rem; color: #e2e8f0; background: #0f172a; }}
      .card {{ background: #111827; border: 1px solid #334155; border-radius: 16px; padding: 1.5rem; margin-bottom: 1rem; }}
      h1, h2 {{ color: #f8fafc; }}
      code {{ background: #1e293b; padding: 0.2rem 0.4rem; border-radius: 6px; }}
      ul {{ line-height: 1.7; }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>NGS Report</h1>
      <p><strong>Workflow:</strong> {plan.workflow}</p>
      <p><strong>Status:</strong> {'passed' if verification.passed else 'needs attention'}</p>
      <p><strong>Generated:</strong> {timestamp}</p>
    </div>
    <div class="card">
      <h2>AI Insights</h2>
      <pre>{ai_insights}</pre>
    </div>
    <div class="card">
      <h2>Verification</h2>
      <p>Planned steps: {verification.metrics.get('planned_steps', 0)}</p>
      <p>Executed steps: {verification.metrics.get('executed_steps', 0)}</p>
      <p>Missing artifacts: {', '.join(verification.missing_artifacts) if verification.missing_artifacts else 'none'}</p>
    </div>
  </body>
</html>
""".strip()

        report_path = self.artifact_store.write_text("reports/report.html", html_report)
        manifest_path = self.artifact_store.write_json(
            "reports/report.json",
            {
                "workflow": plan.workflow,
                "objective": plan.objective,
                "verification": verification.model_dump(),
                "outcomes": outcomes,
            },
        )
        return ReportBundle(
            markdown_summary="\n".join(markdown_summary),
            html_report=str(report_path),
            ai_insights=ai_insights,
            artifact_paths=[str(report_path), str(manifest_path)],
            next_actions=plan.next_actions,
        )
