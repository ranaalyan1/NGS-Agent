"""Tests for the report template and reporter agent."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from ngs_agent.agent.models import (
    ExperimentContext,
    Plan,
    PlanStep,
    StepOutcome,
    VerificationIssue,
    VerificationReport,
)
from ngs_agent.agent.report_template import (
    ProvenanceSummary,
    RunMetadata,
    StepRow,
    VerificationSummary,
    render_html,
    render_markdown,
)
from ngs_agent.agent.reporter import ReporterAgent
from ngs_agent.artifacts.store import LocalArtifactStore
from ngs_agent.config.settings import NGSSettings
from ngs_agent.provenance.manifest import RunManifest
from ngs_agent.tools.permissions import IssueSeverity, StepStatus


def sample_inputs() -> tuple:
    metadata = RunMetadata(
        workflow="rnaseq",
        objective='Find DE genes <script>alert("x")</script>',
        status="passed",
        generated_at="2026-01-01 00:00:00Z",
        run_id="run-abc",
        backend="native",
    )
    steps = [
        StepRow(
            name="qc-S1",
            status="ok",
            returncode=0,
            duration="1.2s",
            tool="fastqc",
            command="fastqc --outdir results/S1/qc S1.fastq.gz",
        ),
        StepRow(
            name="align-S1",
            status="failed",
            returncode=1,
            duration="0.5s",
            tool="hisat2",
            command="hisat2 -x genome <img src=x onerror=alert(1)>",
        ),
    ]
    verification = VerificationSummary(
        passed=False,
        planned_steps=2,
        executed_steps=2,
        missing_artifacts=["aligned.bam"],
        issues=[("error", "Step align-S1 failed with return code 1.")],
    )
    provenance = ProvenanceSummary(
        manifest_path="artifacts/manifest.jsonl",
        artifact_count=7,
        total_bytes=12345,
        tool_versions={"fastqc": "FastQC v0.12.1"},
    )
    return metadata, steps, verification, provenance


class TestTemplateEscaping:
    def test_all_interpolated_values_are_html_escaped(self) -> None:
        metadata, steps, verification, provenance = sample_inputs()
        html = render_html(
            metadata,
            steps,
            verification,
            provenance,
            ai_insights="Summary with <script>alert('evil')</script> and <b>tags</b>",
            risks=["<iframe src=evil>"],
            next_actions=["Re-run <-sample>"],
        )
        assert "<script>alert" not in html
        assert "<iframe" not in html
        assert "&lt;script&gt;" in html
        assert "&lt;img src=x" in html

    def test_template_sections_present(self) -> None:
        metadata, steps, verification, provenance = sample_inputs()
        html = render_html(metadata, steps, verification, provenance, "insights", [], [])
        for section in (
            "NGS Run Report",
            "AI Insights",
            "Steps Executed",
            "Verification",
            "Provenance",
            "Risks",
            "Next Actions",
        ):
            assert section in html
        assert "manifest.jsonl" in html  # provenance manifest is linked
        assert "FastQC v0.12.1" in html  # tool versions are auditable
        assert "fastqc --outdir" in html  # exact commands are shown

    def test_markdown_mirror(self) -> None:
        metadata, steps, verification, provenance = sample_inputs()
        markdown = render_markdown(
            metadata, steps, verification, provenance, "insights", ["r1"], ["a1"]
        )
        assert "# NGS Run Report" in markdown
        assert "| qc-S1 | ok | 0 |" in markdown
        assert "manifest.jsonl" in markdown


class TestReporterAgent:
    @pytest.fixture()
    def reporter(self, tmp_path: Path) -> ReporterAgent:
        return ReporterAgent(
            NGSSettings(artifacts_dir=str(tmp_path / "artifacts")),
            LocalArtifactStore(tmp_path / "artifacts"),
            Console(quiet=True),
        )

    @staticmethod
    def _plan(tmp_path: Path) -> Plan:
        return Plan(
            title="t",
            objective="o",
            workflow="rnaseq",
            summary="s",
            estimated_duration_minutes=1,
            estimated_cost_label="low",
            context=ExperimentContext(working_directory=tmp_path),
            steps=[PlanStep(name="qc", description="d", command_preview="c")],
            risks=["risk-1"],
            next_actions=["action-1"],
        )

    def test_generates_html_markdown_and_json(
        self, reporter: ReporterAgent, tmp_path: Path
    ) -> None:
        plan = self._plan(tmp_path)
        verification = VerificationReport(
            passed=True, metrics={"planned_steps": 1, "executed_steps": 1}
        )
        outcomes = [
            StepOutcome(
                step_name="qc",
                status=StepStatus.OK,
                artifacts={
                    "provenance": {"command": ["fastqc", "x"], "tool_version": "FastQC v0.12.1"}
                },
                details={"tool_name": "fastqc"},
            )
        ]
        manifest = RunManifest(run_id="run-x")
        artifact = reporter.artifact_store.root / "counts.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("g1,1\n", encoding="utf-8")
        manifest.record(artifact)
        bundle = reporter.generate(
            plan, verification, outcomes, run_id="run-x", backend_name="native", manifest=manifest
        )
        assert Path(bundle.html_report).exists()
        assert bundle.manifest_path
        assert "manifest.jsonl" in bundle.manifest_path
        assert (tmp_path / "artifacts" / "reports" / "report.md").exists()
        assert (tmp_path / "artifacts" / "reports" / "report.json").exists()
        html = Path(bundle.html_report).read_text(encoding="utf-8")
        assert "fastqc x" in html  # command from provenance rendered
        assert "FastQC v0.12.1" in html

    def test_ai_insights_without_key_is_static(self, reporter: ReporterAgent) -> None:
        plan = self._plan(Path("/tmp"))
        verification = VerificationReport(passed=True)
        insights = reporter._ai_insights(plan, verification)
        assert "Workflow: rnaseq" in insights

    def test_failed_step_row_shows_nonzero_rc(self, reporter: ReporterAgent) -> None:
        rows = reporter._step_rows(
            [
                StepOutcome(step_name="bad", status=StepStatus.FAILED, returncode=2, stderr="boom"),
                StepOutcome(
                    step_name="skip", status=StepStatus.SKIPPED, details={"reason": "upstream"}
                ),
            ]
        )
        by_name = {row.name: row for row in rows}
        assert by_name["bad"].returncode == 2
        assert by_name["bad"].status == "failed"
        assert by_name["skip"].returncode == "-"

    def test_verification_issues_rendered(self, reporter: ReporterAgent, tmp_path: Path) -> None:
        plan = self._plan(tmp_path)
        verification = VerificationReport(
            passed=False,
            issues=[VerificationIssue(severity=IssueSeverity.ERROR, message="<bad> step failed")],
            missing_artifacts=["report.html"],
        )
        bundle = reporter.generate(plan, verification, [], run_id="r", backend_name="native")
        html = Path(bundle.html_report).read_text(encoding="utf-8")
        assert "&lt;bad&gt; step failed" in html
