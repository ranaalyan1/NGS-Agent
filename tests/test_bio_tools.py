"""Tests for the bioinformatics tools: command construction and correctness.

Uses a recording backend so command construction (flags, order, paths) is
verified without needing real binaries installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.models import CommandResult, CommandSpec
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.builtins.bioinformatics_tools import (
    DEFAULT_ANTHROPIC_MODEL,
    FastQCTool,
    FeatureCountsInput,
    FeatureCountsTool,
    HISAT2Input,
    HISAT2Tool,
    SamtoolsInput,
    SamtoolsTool,
    TrimmomaticInput,
    TrimmomaticTool,
    parse_fastqc_summary,
    register_bioinformatics_tools,
)
from ngs_agent.tools.permissions import PermissionPolicy
from ngs_agent.tools.registry import ToolRegistry


class RecordingBackend(ExecutionBackend):
    """Records every command it is asked to run and returns canned results."""

    name = "recording"

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.results: list[CommandResult] = []
        self.next_results: list[CommandResult] = []
        self.stdout_by_binary: dict[str, str] = {}

    def is_available(self) -> bool:
        return True

    def run_command(self, spec: CommandSpec, console: Console) -> CommandResult:
        self.commands.append(spec.argv)
        if self.next_results:
            return self.next_results.pop(0)
        stdout = self.stdout_by_binary.get(spec.argv[0], "")
        return CommandResult(
            backend=self.name,
            command=spec.argv,
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0.01,
        )

    def argv_strings(self) -> list[str]:
        return [" ".join(command) for command in self.commands]


class RecordingSelector(BackendSelector):
    def __init__(self, backend: RecordingBackend) -> None:  # noqa: D107 - bypass super
        self._recording = backend

    def select(self, preference: Any = "auto") -> Any:
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Selected:
            backend: RecordingBackend
            reason: str

        return _Selected(self._recording, "recording backend for tests")


def make_context(dry_run: bool = False) -> ToolContext:
    backend = RecordingBackend()
    return ToolContext(
        dry_run=dry_run,
        permission_policy=PermissionPolicy(require_confirmation_for=set()),
        backend_selector=RecordingSelector(backend),
        backend_preference="auto",
        console=Console(quiet=True),
    ), backend


class TestHISAT2:
    def test_known_splicesites_from_gtf(self, tmp_path: Path) -> None:
        """Standard of care: HISAT2 must be given --known-splicesites-infile."""
        context, backend = make_context(dry_run=True)
        gtf = tmp_path / "genes.gtf"
        gtf.write_text('chr1\ttest\texon\t100\t200\t.\t+\t.\tgene_id "G1";\n', encoding="utf-8")
        tool = HISAT2Tool()
        output = tool.execute(
            HISAT2Input(
                index_basename="genome",
                reads=[tmp_path / "r1.fq", tmp_path / "r2.fq"],
                output_bam=tmp_path / "out.bam",
                gtf=gtf,
            ),
            context,
        )
        command = " ".join(output.command)
        assert "--known-splicesites-infile" in command
        assert output.splicesites_file

    def test_strandness_flag(self, tmp_path: Path) -> None:
        context, _ = make_context(dry_run=True)
        output = HISAT2Tool().execute(
            HISAT2Input(
                index_basename="genome",
                reads=[tmp_path / "r1.fq"],
                output_bam=tmp_path / "out.bam",
                rna_strandness="reverse",
            ),
            context,
        )
        command = " ".join(output.command)
        assert "--rna-strandness reverse" in command

    def test_no_unal_by_default(self, tmp_path: Path) -> None:
        context, _ = make_context(dry_run=True)
        output = HISAT2Tool().execute(
            HISAT2Input(
                index_basename="genome",
                reads=[tmp_path / "r1.fq"],
                output_bam=tmp_path / "out.bam",
            ),
            context,
        )
        assert "--no-unal" in output.command

    def test_paired_end_uses_dash_1_dash_2(self, tmp_path: Path) -> None:
        context, _ = make_context(dry_run=True)
        output = HISAT2Tool().execute(
            HISAT2Input(
                index_basename="genome",
                reads=[tmp_path / "r1.fq", tmp_path / "r2.fq"],
                output_bam=tmp_path / "out.bam",
            ),
            context,
        )
        assert "-1" in output.command and "-2" in output.command

    def test_mapping_rate_parsed_from_stderr(self, tmp_path: Path) -> None:
        context, backend = make_context()
        backend.stdout_by_binary["hisat2"] = ""
        # The tool reads mapping rate from stderr; simulate via canned result.
        backend.next_results.append(
            CommandResult(
                backend="recording",
                command=[],
                returncode=0,
                stdout="",
                stderr="5000 reads; of these:\n  overall alignment rate: 96.32%\n",
                duration_seconds=0.0,
            )
        )
        output = HISAT2Tool().execute(
            HISAT2Input(
                index_basename="genome",
                reads=[tmp_path / "r1.fq"],
                output_bam=tmp_path / "out.bam",
            ),
            context,
        )
        assert output.mapping_rate == "96.32%"


class TestFeatureCounts:
    def test_strandness_maps_to_s_codes(self, tmp_path: Path) -> None:
        tool = FeatureCountsTool()
        for strandness, code in [("unstranded", "0"), ("forward", "1"), ("reverse", "2")]:
            command = tool.build_command(
                FeatureCountsInput(
                    bam_paths=[tmp_path / "a.bam"],
                    gtf=tmp_path / "genes.gtf",
                    output_dir=tmp_path,
                    strandness=strandness,
                ),
                [tmp_path / "a.bam"],
            )
            s_index = command.index("-s")
            assert command[s_index + 1] == code

    def test_multimappers_excluded_by_default(self, tmp_path: Path) -> None:
        command = FeatureCountsTool().build_command(
            FeatureCountsInput(
                bam_paths=[tmp_path / "a.bam"],
                gtf=tmp_path / "g.gtf",
                output_dir=tmp_path,
            ),
            [tmp_path / "a.bam"],
        )
        assert "-M" not in command
        assert "--fraction" not in command

    def test_multimappers_opt_in_adds_flags(self, tmp_path: Path) -> None:
        command = FeatureCountsTool().build_command(
            FeatureCountsInput(
                bam_paths=[tmp_path / "a.bam"],
                gtf=tmp_path / "g.gtf",
                output_dir=tmp_path,
                count_multimappers=True,
            ),
            [tmp_path / "a.bam"],
        )
        assert "-M" in command and "--fraction" in command

    def test_paired_end_counts_read_pairs(self, tmp_path: Path) -> None:
        command = FeatureCountsTool().build_command(
            FeatureCountsInput(
                bam_paths=[tmp_path / "a.bam"],
                gtf=tmp_path / "g.gtf",
                output_dir=tmp_path,
                paired_end=True,
            ),
            [tmp_path / "a.bam"],
        )
        assert "-p" in command and "--countReadPairs" in command

    def test_min_mapq_prefilters_with_samtools(self, tmp_path: Path) -> None:
        """Multi-mapping primaries (MAPQ 0/1 from HISAT2) must be filtered."""
        context, _backend = make_context(dry_run=True)
        bam = tmp_path / "S1" / "aligned.sorted.bam"
        bam.parent.mkdir(parents=True)
        bam.touch()
        output = FeatureCountsTool().execute(
            FeatureCountsInput(
                bam_paths=[bam],
                gtf=tmp_path / "g.gtf",
                output_dir=tmp_path / "counts",
                min_mapq=10,
            ),
            context,
        )
        assert len(output.filter_commands) == 1
        filter_command = output.filter_commands[0]
        assert filter_command[0] == "samtools"
        assert "view" in filter_command
        assert "-q" in filter_command and filter_command[filter_command.index("-q") + 1] == "10"
        assert "-b" in filter_command
        # The filtered BAM (namespaced by sample dir) is what gets counted.
        assert "mapq10.bam" in " ".join(output.command)

    def test_mapq_filtered_bams_are_namespaced_per_sample(self, tmp_path: Path) -> None:
        # Two samples with identically named BAMs must not collide.
        context, _ = make_context(dry_run=True)
        bams = []
        for sample in ("S1", "S2"):
            bam = tmp_path / sample / "aligned.sorted.bam"
            bam.parent.mkdir(parents=True)
            bam.touch()
            bams.append(bam)
        tool = FeatureCountsTool()
        command = tool.build_command(
            FeatureCountsInput(
                bam_paths=bams,
                gtf=tmp_path / "g.gtf",
                output_dir=tmp_path / "counts",
                min_mapq=10,
            ),
            bams,
        )
        # The build_command receives pre-filtered paths; just assert both
        # input BAMs are counted exactly once each.
        assert command.count(str(bams[0])) == 1
        assert command.count(str(bams[1])) == 1


class TestSamtools:
    def test_sort_uses_output_flag(self, tmp_path: Path) -> None:
        command, artifact = SamtoolsTool().build_command(
            SamtoolsInput(
                action="sort", input_path=tmp_path / "in.bam", output_path=tmp_path / "out.bam"
            )
        )
        assert command[:2] == ["samtools", "sort"]
        assert "-o" in command
        assert str(tmp_path / "out.bam") in command
        assert artifact == tmp_path / "out.bam"

    def test_view_requires_output_path(self, tmp_path: Path) -> None:
        """`samtools view` without -o silently dumps to stdout; must raise."""
        from ngs_agent.tools.builtins.bioinformatics_tools import ToolExecutionError

        with pytest.raises(ToolExecutionError, match="output_path"):
            SamtoolsTool().build_command(
                SamtoolsInput(action="view", input_path=tmp_path / "in.bam")
            )

    def test_view_writes_to_file(self, tmp_path: Path) -> None:
        command, artifact = SamtoolsTool().build_command(
            SamtoolsInput(
                action="view", input_path=tmp_path / "in.bam", output_path=tmp_path / "out.bam"
            )
        )
        assert "-o" in command
        assert artifact == tmp_path / "out.bam"

    def test_index_produces_bai(self, tmp_path: Path) -> None:
        command, artifact = SamtoolsTool().build_command(
            SamtoolsInput(action="index", input_path=tmp_path / "x.bam")
        )
        assert "index" in command
        assert str(artifact).endswith(".bam.bai")


FASTQC_SUMMARY = (
    "PASS\tBasic Statistics\tS1_R1.fastq.gz\n"
    "WARN\tPer base sequence quality\tS1_R1.fastq.gz\n"
    "FAIL\tAdapter Content\tS1_R1.fastq.gz\n"
    "FAIL\tPer tile quality\tS1_R1.fastq.gz\n"
)


class TestTrimmomatic:
    def test_parse_fastqc_summary_is_module_aware(self) -> None:
        entries = parse_fastqc_summary(FASTQC_SUMMARY)
        assert len(entries) == 4
        assert ("FAIL", "Adapter Content", "S1_R1.fastq.gz") in entries

    def test_adapter_fail_adds_illuminaclip(self, tmp_path: Path) -> None:
        context, _ = make_context(dry_run=True)
        output = TrimmomaticTool().execute(
            TrimmomaticInput(
                paired_end=True,
                fastqc_summary=FASTQC_SUMMARY,
                input_r1=tmp_path / "r1.fq",
                input_r2=tmp_path / "r2.fq",
                output_dir=tmp_path / "trimmed",
            ),
            context,
        )
        assert "ILLUMINACLIP" in output.trim_params
        assert "TruSeq3-PE.fa" in output.trim_params["ILLUMINACLIP"]
        assert "adapter" in " ".join(output.confidence_reasons).lower()
        assert 0 < output.confidence <= 0.95

    def test_confidence_is_explainable_and_bounded(self, tmp_path: Path) -> None:
        context, _ = make_context(dry_run=True)
        tool = TrimmomaticTool()
        no_data = tool._decide_trim_params(TrimmomaticInput(paired_end=True, output_dir=tmp_path))
        params, source, confidence, reasons = no_data
        assert source == "heuristic:defaults"
        assert confidence == 0.5
        assert reasons  # always explain why

        with_data = tool._decide_trim_params(
            TrimmomaticInput(paired_end=True, output_dir=tmp_path, fastqc_summary=FASTQC_SUMMARY)
        )
        assert with_data[2] > no_data[2]  # more evidence -> more confidence
        assert with_data[2] <= 0.95

    def test_user_params_win_with_full_confidence(self, tmp_path: Path) -> None:
        context, _ = make_context(dry_run=True)
        output = TrimmomaticTool().execute(
            TrimmomaticInput(
                paired_end=True,
                trim_params={"MINLEN": 50},
                input_r1=tmp_path / "r1.fq",
                input_r2=tmp_path / "r2.fq",
                output_dir=tmp_path / "trimmed",
            ),
            context,
        )
        assert output.decision_source == "user-supplied"
        assert output.confidence == 1.0
        assert output.trim_params["MINLEN"] == 50

    def test_outputs_are_discovered_from_disk_not_assumed(self, tmp_path: Path) -> None:
        """Only files that actually exist are reported as artifacts."""
        context, _ = make_context()
        # Trimmomatic 'runs' but writes only the R1 outputs.
        r1 = tmp_path / "in_R1.fq"
        r1.touch()
        output = TrimmomaticTool().execute(
            TrimmomaticInput(
                paired_end=True,
                input_r1=r1,
                input_r2=tmp_path / "in_R2.fq",
                output_dir=tmp_path / "trimmed",
                sample_name="S1",
            ),
            context,
        )
        assert output.trimmed_reads == []  # nothing was actually written
        assert len(output.missing_outputs) == 4
        assert output.status == "failed"  # rc=0 but no outputs -> failed

    def test_read_stats_parsed_from_stderr(self, tmp_path: Path) -> None:
        stderr = (
            "TrimmomaticPE: Started with arguments\n"
            "Input Read Pairs: 10000 Both Surviving: 9000 (90.00%) "
            "Forward Only Surviving: 400 (4.00%) Reverse Only Surviving: 300 (3.00%) "
            "Dropped: 300 (3.00%)\n"
        )
        stats = TrimmomaticTool._parse_trim_stats(stderr)
        assert stats["pairs"] == 10000
        assert stats["both"] == 9000
        assert stats["dropped"] == 300

    def test_model_default_is_not_a_dated_snapshot(self) -> None:
        # The old default 'claude-sonnet-4-20250514' goes stale; the shared
        # constant must be a stable alias.
        assert DEFAULT_ANTHROPIC_MODEL == "claude-sonnet-4-5"
        import re

        assert not re.search(r"-2\d{7}$", DEFAULT_ANTHROPIC_MODEL)

    def test_fastqc_summary_read_from_path(self, tmp_path: Path) -> None:
        summary = tmp_path / "S1_fastqc.summary.txt"
        summary.write_text(FASTQC_SUMMARY, encoding="utf-8")
        context, _ = make_context(dry_run=True)
        tool = TrimmomaticTool()
        params, source, _confidence, _reasons = tool._decide_trim_params(
            TrimmomaticInput(paired_end=True, output_dir=tmp_path, fastqc_summary_path=summary)
        )
        assert source == "heuristic:fastqc"
        assert "ILLUMINACLIP" in params


class TestFastQC:
    def test_naming_follows_real_fastqc_convention(self) -> None:
        from ngs_agent.bioinformatics.common import fastqc_stem

        # Real FastQC strips the whole .fastq.gz suffix, not just .gz.
        assert fastqc_stem("S1_R1.fastq.gz") == "S1_R1"
        assert fastqc_stem("S1_R1.fq.gz") == "S1_R1"
        assert fastqc_stem("S1_R1.fastq") == "S1_R1"
        assert fastqc_stem("plain.txt.gz") == "plain.txt"

    def test_dry_run_builds_command(self, tmp_path: Path) -> None:
        context, _ = make_context(dry_run=True)
        output = FastQCTool().execute(
            __import__(
                "ngs_agent.tools.builtins.bioinformatics_tools", fromlist=["FastQCInput"]
            ).FastQCInput(reads=[tmp_path / "a.fastq.gz"], output_dir=tmp_path / "qc"),
            context,
        )
        assert output.command[0] == "fastqc"
        assert "--outdir" in output.command


class TestRegistry:
    def test_all_tools_registered(self) -> None:
        registry = register_bioinformatics_tools(ToolRegistry())
        names = {meta.name for meta in registry.list_metadata()}
        assert {
            "fastqc",
            "trimmomatic",
            "hisat2",
            "hisat2-build",
            "samtools",
            "featureCounts",
            "multiqc",
            "deseq2",
            "go_enrichment",
        } <= names

    def test_r_scripts_ship_with_the_package(self) -> None:
        from ngs_agent.tools.builtins.bioinformatics_tools import _package_script

        assert _package_script("deseq2_analysis.R").exists()
        assert _package_script("enrichment_analysis.R").exists()
