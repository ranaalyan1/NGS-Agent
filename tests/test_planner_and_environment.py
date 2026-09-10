"""Tests for the planner (per-sample DAG, env risks) and environment checks."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from ngs_agent.agent.planner import PlannerAgent, strandness_code
from ngs_agent.bioinformatics.workflows import WorkflowInferenceError
from ngs_agent.config.settings import NGSSettings
from ngs_agent.environment import platform_warnings, verify_tool_environment


@pytest.fixture()
def planner() -> PlannerAgent:
    return PlannerAgent(NGSSettings(), Console(quiet=True))


@pytest.fixture()
def experiment(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    for name in ("S1_R1.fastq.gz", "S1_R2.fastq.gz", "S2_R1.fastq.gz", "S2_R2.fastq.gz"):
        (data / name).touch()
    (data / "genes.gtf").touch()
    (tmp_path / "ref").mkdir()
    (tmp_path / "ref" / "genome.1.ht2").touch()
    (tmp_path / "ref" / "genome.2.ht2").touch()
    (data / "samplesheet.csv").write_text(
        "sample,fastq_1,fastq_2,strandedness,condition\n"
        "S1,S1_R1.fastq.gz,S1_R2.fastq.gz,forward,control\n"
        "S2,S2_R1.fastq.gz,S2_R2.fastq.gz,forward,treated\n",
        encoding="utf-8",
    )
    return tmp_path


class TestContextDiscovery:
    def test_samplesheet_found_one_level_down(
        self, planner: PlannerAgent, experiment: Path
    ) -> None:
        context = planner.discover_context(experiment)
        assert context.samplesheets, "samplesheet in data/ must be discovered"
        assert context.metadata["sample_count"] == 2
        assert {sample["sample"] for sample in context.samples} == {"S1", "S2"}

    def test_bad_samplesheet_does_not_break_planning(
        self, planner: PlannerAgent, tmp_path: Path
    ) -> None:
        (tmp_path / "samplesheet.csv").write_text("garbage,without,fastq\n", encoding="utf-8")
        context = planner.discover_context(tmp_path)
        assert context.samples == []

    def test_hisat2_index_discovered(self, planner: PlannerAgent, experiment: Path) -> None:
        context = planner.discover_context(experiment)
        assert planner._discover_hisat2_index(context) is not None


class TestHeuristicPlan:
    def test_rnaseq_plan_builds_per_sample_dag(
        self, planner: PlannerAgent, experiment: Path
    ) -> None:
        context = planner.discover_context(experiment)
        plan = planner.create_plan("differential expression", "rnaseq", context)
        names = {step.name for step in plan.steps}
        for expected in (
            "qc-S1",
            "trim-S1",
            "align-S1",
            "sort-S1",
            "index-S1",
            "qc-S2",
            "trim-S2",
            "align-S2",
            "sort-S2",
            "index-S2",
            "quantify",
            "aggregate-qc",
            "report",
        ):
            assert expected in names

        by_name = {step.name: step for step in plan.steps}
        # Per-sample chain.
        assert by_name["trim-S1"].dependencies == ["qc-S1"]
        assert "trim-S1" in by_name["align-S1"].dependencies
        # Quantify fans in every sample's sorted BAM.
        assert set(by_name["quantify"].dependencies) == {"sort-S1", "sort-S2"}
        # Align steps carry real tool payloads.
        assert by_name["align-S1"].tool_name == "hisat2"
        assert by_name["align-S1"].tool_payload["gtf"]
        assert by_name["quantify"].tool_payload["strandness"] == "forward"
        assert by_name["quantify"].tool_payload["min_mapq"] == 10

    def test_samplesheet_strandedness_flows_to_quantify(
        self, planner: PlannerAgent, experiment: Path
    ) -> None:
        (experiment / "data" / "samplesheet.csv").write_text(
            "sample,fastq_1,fastq_2,strandedness\n"
            "S1,S1_R1.fastq.gz,S1_R2.fastq.gz,reverse\n"
            "S2,S2_R1.fastq.gz,S2_R2.fastq.gz,reverse\n",
            encoding="utf-8",
        )
        context = planner.discover_context(experiment)
        plan = planner.create_plan("x", "rnaseq", context)
        quantify = next(s for s in plan.steps if s.name == "quantify")
        assert quantify.tool_payload["strandness"] == "reverse"

    def test_variant_plan_structure(self, planner: PlannerAgent, experiment: Path) -> None:
        context = planner.discover_context(experiment)
        plan = planner.create_plan("find variants", "variant", context)
        names = [step.name for step in plan.steps]
        assert "variant-call" in names
        assert "report" in names

    def test_unknown_workflow_rejected(self, planner: PlannerAgent, experiment: Path) -> None:
        context = planner.discover_context(experiment)
        with pytest.raises(WorkflowInferenceError):
            planner.create_plan("x", "metagenomics", context)

    def test_missing_tools_surface_as_risks(
        self, planner: PlannerAgent, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ngs_agent.agent.planner as planner_module
        import ngs_agent.environment as env_module

        monkeypatch.setattr(
            planner_module,
            "verify_tool_environment",
            lambda tools, expected_prefix=None: env_module.ToolEnvironmentReport(
                expected_prefix="/opt/conda",
                checks=[
                    env_module.ToolCheck("hisat2", "/usr/bin/hisat2", "conflict", "outside prefix")
                ],
            ),
        )
        context = planner.discover_context(experiment)
        plan = planner.create_plan("x", "rnaseq", context)
        assert any("hisat2" in risk and "different version" in risk for risk in plan.risks)


class TestStrandnessCode:
    def test_mapping(self) -> None:
        assert strandness_code("unstranded") == 0
        assert strandness_code("forward") == 1
        assert strandness_code("reverse") == 2
        assert strandness_code("nonsense") == 0


class TestToolEnvironment:
    def test_conflict_detected_when_tool_outside_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "samtools").touch()
        # samtools resolves from /usr/bin, outside the expected prefix.
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/samtools" if name == "samtools" else None
        )
        report = verify_tool_environment(("samtools", "fastqc"), expected_prefix=str(tmp_path))
        assert report.ok is False
        assert report.missing[0].name == "fastqc"
        conflict = report.conflicts[0]
        assert conflict.name == "samtools"
        assert "outside" in conflict.detail or "prefix" in conflict.detail

    def test_ok_when_tools_resolve_from_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda name: str(tmp_path / "bin" / name))
        (tmp_path / "bin").mkdir(parents=True)
        (tmp_path / "bin" / "samtools").touch()
        report = verify_tool_environment(("samtools",), expected_prefix=str(tmp_path))
        assert report.ok
        assert report.conflicts == []


class TestPlatformWarnings:
    def test_arm_macos_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        warnings = platform_warnings()
        assert any("Apple Silicon" in warning for warning in warnings)

    def test_linux_has_no_platform_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        assert platform_warnings() == []
