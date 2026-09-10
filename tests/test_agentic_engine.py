"""Tests for the agentic engine (ngs_agent.agentic): packaging, planning, execution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("typer")
pytest.importorskip("pydantic")
pytest.importorskip("platformdirs")

from rich.console import Console  # noqa: E402

from ngs_agent.agentic.agent.models import Plan, PlanStep, StepOutcome  # noqa: E402
from ngs_agent.agentic.agent.planner import PlannerAgent  # noqa: E402
from ngs_agent.agentic.agent.verifier import VerifierAgent  # noqa: E402
from ngs_agent.agentic.config.settings import NGSSettings  # noqa: E402
from ngs_agent.agentic.tools.permissions import StepStatus  # noqa: E402
from ngs_agent.agentic.tools.registry import create_default_registry  # noqa: E402


@pytest.fixture()
def console() -> Console:
    return Console(force_terminal=False, width=120)


@pytest.fixture()
def rnaseq_workdir(tmp_path: Path) -> Path:
    (tmp_path / "sample_R1.fastq.gz").write_bytes(b"")
    (tmp_path / "sample_R2.fastq.gz").write_bytes(b"")
    (tmp_path / "genes.gtf").write_text("gene\n")
    (tmp_path / "samplesheet.csv").write_text("sample_id,condition\ns1,ctrl\ns2,treat\n")
    (tmp_path / "index.1.ht2").write_bytes(b"")
    return tmp_path


class TestImportsWithoutAnthropicAtModuleScope:
    def test_no_module_level_anthropic_import(self):
        """Core agentic modules must not import anthropic at module scope (optional [llm] extra)."""
        for module_name in [
            "ngs_agent.agentic.agent.planner",
            "ngs_agent.agentic.agent.reporter",
            "ngs_agent.agentic.tools.builtins.bioinformatics_tools",
        ]:
            module = sys.modules.get(module_name) or __import__(module_name, fromlist=["_"])
            assert not hasattr(module, "Anthropic"), (
                f"{module_name} imports anthropic at module scope; must be lazy"
            )

    def test_orchestrator_importable(self):
        from ngs_agent.agentic.agent.orchestrator import AgentOrchestrator  # noqa: F401


class TestToolRegistry:
    def test_all_eight_tools_registered(self):
        registry = create_default_registry()
        names = {meta.name for meta in registry.list_metadata()}
        assert names == {
            "fastqc",
            "trimmomatic",
            "hisat2",
            "samtools",
            "featureCounts",
            "multiqc",
            "deseq2",
            "go_enrichment",
        }


class TestPlannerToolBinding:
    def test_rnaseq_plan_binds_tools(self, rnaseq_workdir: Path, tmp_path: Path, console: Console):
        settings = NGSSettings(artifacts_dir=str(tmp_path / "artifacts"), logs_dir=str(tmp_path / "logs"))
        planner = PlannerAgent(settings, console)
        context = planner.discover_context(rnaseq_workdir)
        plan = planner.create_plan("Run RNA-seq", "rnaseq", context, dry_run=False)

        bound = {step.name: step.tool_name for step in plan.steps if step.tool_name}
        assert bound.get("qc") == "fastqc"
        assert bound.get("trim") == "trimmomatic"
        assert bound.get("align") == "hisat2"
        assert bound.get("quantify") == "featureCounts"
        assert bound.get("multiqc") == "multiqc"
        assert bound.get("differential-expression") == "deseq2"
        assert bound.get("go-enrichment") == "go_enrichment"

        # Tool payloads must be populated for bound steps.
        for step in plan.steps:
            if step.tool_name:
                assert step.tool_payload, f"step {step.name} bound to {step.tool_name} but payload empty"

    def test_empty_directory_yields_skips_not_fabricated_steps(self, tmp_path: Path, console: Console):
        settings = NGSSettings(artifacts_dir=str(tmp_path / "artifacts"), logs_dir=str(tmp_path / "logs"))
        planner = PlannerAgent(settings, console)
        workdir = tmp_path / "empty"
        workdir.mkdir()
        context = planner.discover_context(workdir)
        plan = planner.create_plan("Run RNA-seq", "rnaseq", context, dry_run=False)

        skip_reasons = {step.name: step.skip_reason for step in plan.steps}
        assert skip_reasons["qc"], "qc should be marked skipped when no FASTQ files exist"
        assert skip_reasons["align"], "align should be marked skipped when no index exists"

    def test_samplesheet_not_double_counted(self, rnaseq_workdir: Path, tmp_path: Path, console: Console):
        settings = NGSSettings(artifacts_dir=str(tmp_path / "artifacts"), logs_dir=str(tmp_path / "logs"))
        planner = PlannerAgent(settings, console)
        context = planner.discover_context(rnaseq_workdir)
        assert len(context.samplesheets) == 1


class TestVerifier:
    def _make_plan(self, tmp_path: Path, steps: list[PlanStep]) -> Plan:
        from ngs_agent.agentic.agent.models import ExperimentContext

        return Plan(
            title="t",
            objective="o",
            workflow="rnaseq",
            summary="s",
            estimated_duration_minutes=1,
            estimated_cost_label="low",
            context=ExperimentContext(working_directory=tmp_path),
            steps=steps,
        )

    def test_failed_step_fails_verification(self, tmp_path: Path):
        plan = self._make_plan(
            tmp_path,
            [PlanStep(name="a", description="d", command_preview="c")],
        )
        outcomes = [StepOutcome(step_name="a", status=StepStatus.FAILED, returncode=2)]
        report = VerifierAgent().verify(plan, outcomes)
        assert not report.passed

    def test_skipped_steps_do_not_count_missing_artifacts(self, tmp_path: Path):
        plan = self._make_plan(
            tmp_path,
            [PlanStep(name="a", description="d", command_preview="c", expected_outputs=["x.bam"])],
        )
        outcomes = [
            StepOutcome(step_name="a", status=StepStatus.SKIPPED, details={"skip_reason": "no input"})
        ]
        report = VerifierAgent().verify(plan, outcomes)
        assert report.passed
        assert report.missing_artifacts == []

    def test_existing_artifact_satisfies_expectation(self, tmp_path: Path):
        artifact = tmp_path / "out.bam"
        artifact.write_bytes(b"")
        plan = self._make_plan(
            tmp_path,
            [PlanStep(name="a", description="d", command_preview="c", expected_outputs=[str(artifact)])],
        )
        outcomes = [StepOutcome(step_name="a", status=StepStatus.OK)]
        report = VerifierAgent().verify(plan, outcomes)
        assert report.passed
        assert report.missing_artifacts == []


class TestBackendSelector:
    def test_native_always_available(self):
        from ngs_agent.agentic.execution.selector import BackendSelector

        selected = BackendSelector().select("native")
        assert selected.backend.name == "native"

    def test_docker_backend_run_command_is_implemented(self):
        from ngs_agent.agentic.execution.backends.docker_backend import DockerBackend

        backend = DockerBackend()
        assert backend.run_command.__qualname__.startswith("DockerBackend"), (
            "DockerBackend must implement run_command"
        )
        # Building the docker argv must not raise NotImplementedError.
        from ngs_agent.agentic.execution.models import CommandSpec

        argv = backend._build_docker_argv(CommandSpec(argv=["fastqc", "--version"]))
        assert argv[:3] == ["docker", "run", "--rm"]
        assert "fastqc" in argv

    def test_apptainer_backend_builds_argv(self):
        from ngs_agent.agentic.execution.backends.apptainer_backend import ApptainerBackend
        from ngs_agent.agentic.execution.models import CommandSpec

        backend = ApptainerBackend()
        argv = backend._build_argv(CommandSpec(argv=["samtools", "--version"]), "apptainer")
        assert argv[0] == "apptainer"
        assert "exec" in argv
        assert "samtools" in argv


class TestExecutorRunsRealTools:
    def test_executor_invokes_bound_tools(self, rnaseq_workdir: Path, tmp_path: Path, console: Console, monkeypatch):
        """End-to-end: the executor must call tools through the registry, not echo previews."""
        import stat

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log_file = tmp_path / "invocations.log"
        for tool in ["fastqc", "trimmomatic", "hisat2", "samtools", "featureCounts", "multiqc", "Rscript"]:
            script = bin_dir / tool
            script.write_text(
                "#!/bin/sh\n"
                f'echo "$(basename $0) $@" >> {log_file}\n'
                "exit 0\n"
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

        from ngs_agent.agentic.agent.orchestrator import AgentOrchestrator
        from ngs_agent.agentic.execution.selector import BackendSelector

        settings = NGSSettings(artifacts_dir=str(tmp_path / "artifacts"), logs_dir=str(tmp_path / "logs"))
        orchestrator = AgentOrchestrator(settings, BackendSelector(), create_default_registry(), console)
        result = orchestrator.run(
            objective="Run RNA-seq analysis",
            workflow="rnaseq",
            working_directory=rnaseq_workdir,
            dry_run=False,
            confirm_callback=lambda _msg: True,
        )
        invocations = log_file.read_text() if log_file.exists() else ""
        assert "fastqc" in invocations
        assert "trimmomatic" in invocations
        assert "hisat2" in invocations
        assert "featureCounts" in invocations
        assert "echo" not in invocations
        statuses = {o["step_name"]: o["status"] for o in result["outcomes"]}
        assert statuses["qc"] == StepStatus.OK

    def test_dry_run_executes_nothing(self, rnaseq_workdir: Path, tmp_path: Path, console: Console):
        from ngs_agent.agentic.agent.orchestrator import AgentOrchestrator
        from ngs_agent.agentic.execution.selector import BackendSelector

        settings = NGSSettings(artifacts_dir=str(tmp_path / "artifacts"), logs_dir=str(tmp_path / "logs"))
        orchestrator = AgentOrchestrator(settings, BackendSelector(), create_default_registry(), console)
        result = orchestrator.run(
            objective="Run RNA-seq analysis",
            workflow="rnaseq",
            working_directory=rnaseq_workdir,
            dry_run=True,
            confirm_callback=lambda _msg: pytest.fail("dry-run must not ask for confirmation"),
        )
        assert result["status"] == "dry-run"


class TestPackagedScripts:
    def test_r_scripts_ship_with_package(self):
        import ngs_agent.agentic as agentic_pkg

        scripts_dir = Path(agentic_pkg.__file__).parent / "scripts"
        assert (scripts_dir / "deseq2_analysis.R").exists()
        assert (scripts_dir / "enrichment_analysis.R").exists()

    def test_tool_points_at_existing_scripts(self, tmp_path: Path):
        from ngs_agent.agentic.tools.builtins.bioinformatics_tools import DESeq2Tool, DESeq2Input
        from ngs_agent.agentic.tools.base import ToolContext
        from ngs_agent.agentic.tools.permissions import PermissionPolicy

        tool = DESeq2Tool()
        payload = DESeq2Input(
            count_matrix=tmp_path / "counts.txt",
            sample_sheet=tmp_path / "samples.csv",
            output_dir=tmp_path / "out",
        )
        context = ToolContext(dry_run=True, permission_policy=PermissionPolicy(require_confirmation_for=set()))
        output = tool.execute(payload, context)
        script_path = Path(output.command[1])
        assert script_path.exists(), f"DESeq2 R script missing at {script_path}"
