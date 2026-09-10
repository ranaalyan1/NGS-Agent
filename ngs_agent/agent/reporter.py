from __future__ import annotations

import logging
from datetime import UTC, datetime

from rich.console import Console

from ngs_agent.agent.models import Plan, ReportBundle, StepOutcome, VerificationReport
from ngs_agent.agent.report_template import (
    ProvenanceSummary,
    RunMetadata,
    StepRow,
    VerificationSummary,
    render_html,
    render_markdown,
)
from ngs_agent.artifacts.store import LocalArtifactStore
from ngs_agent.config.settings import NGSSettings
from ngs_agent.provenance.manifest import RunManifest

logger = logging.getLogger(__name__)


class ReporterAgent:
    """Renders the run report through the explicit template in report_template.py.

    The HTML is generated only from the plan, step outcomes, verification
    report, and provenance manifest — see report_template.py for the mapping
    of every rendered claim to its source.
    """

    def __init__(
        self, settings: NGSSettings, artifact_store: LocalArtifactStore, console: Console
    ) -> None:
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
            from anthropic import Anthropic
        except ImportError:
            logger.debug("anthropic SDK not installed; using static insights")
            return "AI insights unavailable: the anthropic package is not installed."

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
                            "You are an expert bioinformatics reporter. Summarize the run,"
                                "identify risks,  and recommend next actions in concise markdown. "
                            f"Plan: {plan.model_dump(mode='json')}\n"
                            f"Verification: {verification.model_dump(mode='json')}"
                        ),
                    }
                ],
            )
            return "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
        except Exception as exc:
            logger.warning("AI insights generation failed: %s", exc)
            return f"AI summary unavailable: {exc}"

    @staticmethod
    def _step_rows(outcomes: list[StepOutcome]) -> list[StepRow]:
        rows: list[StepRow] = []
        for outcome in outcomes:
            command = ""
            tool = str(outcome.details.get("tool_name", ""))
            artifacts = outcome.artifacts if isinstance(outcome.artifacts, dict) else {}
            provenance = (
                artifacts.get("provenance") if isinstance(artifacts.get("provenance"), dict) else {}
            )
            if isinstance(provenance, dict) and isinstance(provenance.get("command"), list):
                command = " ".join(str(part) for part in provenance["command"])
            elif outcome.details.get("command"):
                command = " ".join(str(part) for part in outcome.details["command"])
            elif outcome.details.get("command_preview"):
                command = str(outcome.details["command_preview"])
            duration = f"{outcome.duration_seconds:.1f}s" if outcome.duration_seconds else "-"
            rows.append(
                StepRow(
                    name=outcome.step_name,
                    status=outcome.status.value,
                    returncode=outcome.returncode if outcome.status.value != "skipped" else "-",
                    duration=duration,
                    tool=tool or "-",
                    command=command or outcome.details.get("command_preview", "-") or "-",
                )
            )
        return rows

    @staticmethod
    def _tool_versions(outcomes: list[StepOutcome]) -> dict[str, str]:
        versions: dict[str, str] = {}
        for outcome in outcomes:
            artifacts = outcome.artifacts if isinstance(outcome.artifacts, dict) else {}
            provenance = (
                artifacts.get("provenance") if isinstance(artifacts.get("provenance"), dict) else {}
            )
            tool = str(outcome.details.get("tool_name", "") or artifacts.get("tool_name", ""))
            version = provenance.get("tool_version") if isinstance(provenance, dict) else None
            if tool and version and version not in {"unknown", "unavailable"}:
                versions.setdefault(tool, str(version))
        return versions

    def generate(
        self,
        plan: Plan,
        verification: VerificationReport,
        outcomes: list[StepOutcome],
        run_id: str = "-",
        backend_name: str = "-",
        manifest: RunManifest | None = None,
    ) -> ReportBundle:
        timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        ai_insights = self._ai_insights(plan, verification)

        metadata = RunMetadata(
            workflow=plan.workflow,
            objective=plan.objective,
            status="passed" if verification.passed else "needs attention",
            generated_at=timestamp,
            run_id=run_id,
            backend=backend_name,
        )
        verification_summary = VerificationSummary(
            passed=verification.passed,
            planned_steps=int(verification.metrics.get("planned_steps", 0)),
            executed_steps=int(verification.metrics.get("executed_steps", 0)),
            missing_artifacts=verification.missing_artifacts,
            issues=[(issue.severity.value, issue.message) for issue in verification.issues],
        )
        provenance_summary = ProvenanceSummary(
            manifest_path=str(self.artifact_store.root / "manifest.jsonl")
            if manifest is not None
            else "",
            artifact_count=len(manifest.records) if manifest is not None else 0,
            total_bytes=sum(record.size_bytes for record in manifest.records)
            if manifest is not None
            else 0,
            tool_versions=self._tool_versions(outcomes),
        )

        html_report = render_html(
            metadata,
            self._step_rows(outcomes),
            verification_summary,
            provenance_summary,
            ai_insights,
            plan.risks,
            plan.next_actions,
        )
        markdown_summary = render_markdown(
            metadata,
            self._step_rows(outcomes),
            verification_summary,
            provenance_summary,
            ai_insights,
            plan.risks,
            plan.next_actions,
        )

        report_path = self.artifact_store.write_text("reports/report.html", html_report)
        markdown_path = self.artifact_store.write_text("reports/report.md", markdown_summary)
        manifest_path = self.artifact_store.write_json(
            "reports/report.json",
            {
                "run_id": run_id,
                "workflow": plan.workflow,
                "objective": plan.objective,
                "generated_at": timestamp,
                "verification": verification.model_dump(),
                "outcomes": [outcome.model_dump(mode="json") for outcome in outcomes],
                "tool_versions": provenance_summary.tool_versions,
            },
        )
        return ReportBundle(
            markdown_summary=markdown_summary,
            html_report=str(report_path),
            ai_insights=ai_insights,
            artifact_paths=[str(report_path), str(markdown_path), str(manifest_path)],
            next_actions=plan.next_actions,
            manifest_path=provenance_summary.manifest_path,
        )
