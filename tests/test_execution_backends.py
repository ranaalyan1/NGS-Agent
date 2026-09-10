"""Tests for the execution backends: native, docker, apptainer, slurm, pbs, selector."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from ngs_agent.execution.backends.apptainer_backend import ApptainerBackend
from ngs_agent.execution.backends.containers import (
    BIOCONTAINER_IMAGES,
    collect_bind_roots,
    resolve_image,
)
from ngs_agent.execution.backends.docker_backend import DockerBackend
from ngs_agent.execution.backends.native_backend import NativeBackend
from ngs_agent.execution.backends.pbs_backend import PBSBackend
from ngs_agent.execution.backends.slurm_backend import SlurmBackend
from ngs_agent.execution.models import CommandSpec
from ngs_agent.execution.selector import BackendSelector

quiet_console = Console(quiet=True)


class TestNativeBackend:
    def test_runs_a_command_and_streams_output(self) -> None:
        backend = NativeBackend()
        result = backend.run_command(
            CommandSpec(argv=["echo", "hello-native"], stream_output=False), quiet_console
        )
        assert result.backend == "native"
        assert result.returncode == 0
        assert "hello-native" in result.stdout

    def test_nonzero_exit_code_is_reported(self) -> None:
        result = NativeBackend().run_command(
            CommandSpec(argv=["sh", "-c", "exit 3"], stream_output=False), quiet_console
        )
        assert result.returncode == 3

    def test_timeout_raises(self) -> None:
        with pytest.raises(RuntimeError, match="timed out"):
            NativeBackend().run_command(
                CommandSpec(argv=["sleep", "5"], timeout_seconds=1, stream_output=False),
                quiet_console,
            )


class TestImageResolution:
    def test_pinned_biocontainer_map_has_core_tools(self) -> None:
        for tool in ("fastqc", "trimmomatic", "hisat2", "samtools", "featureCounts", "multiqc"):
            assert tool in BIOCONTAINER_IMAGES
            assert BIOCONTAINER_IMAGES[tool].startswith("quay.io/biocontainers/")

    def test_metadata_image_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NGS_CONTAINER_IMAGE", "example.com/fallback:1")
        spec = CommandSpec(argv=["fastqc", "x.fastq"], metadata={"image": "example.com/other:2"})
        assert resolve_image(spec) == "example.com/other:2"

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NGS_CONTAINER_IMAGE", "example.com/fallback:1")
        spec = CommandSpec(argv=["some-unknown-tool", "x"])
        assert resolve_image(spec) == "example.com/fallback:1"

    def test_tool_specific_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NGS_CONTAINER_IMAGE", "example.com/fallback:1")
        monkeypatch.setenv("NGS_CONTAINER_IMAGE_HISAT2", "example.com/hisat2:9")
        assert resolve_image(CommandSpec(argv=["hisat2", "-x", "idx"])) == "example.com/hisat2:9"

    def test_unresolvable_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NGS_CONTAINER_IMAGE", raising=False)
        assert resolve_image(CommandSpec(argv=["totally-unknown-tool"])) is None


class TestBindRoots:
    def test_existing_input_paths_are_mounted(self, tmp_path: Path) -> None:
        fastq = tmp_path / "reads" / "a.fastq.gz"
        fastq.parent.mkdir()
        fastq.touch()
        roots = collect_bind_roots(CommandSpec(argv=["fastqc", str(fastq)], cwd=str(tmp_path)))
        assert str(tmp_path) in roots
        assert str(tmp_path / "reads") in roots

    def test_output_parent_directories_are_mounted(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "results" / "sample"
        out_dir.mkdir(parents=True)
        roots = collect_bind_roots(
            CommandSpec(argv=["trimmomatic", "PE", "in", "in2", str(out_dir / "o1.fq")])
        )
        assert str(out_dir) in roots


class TestDockerBackend:
    def test_build_command_wraps_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NGS_CONTAINER_IMAGE", raising=False)
        backend = DockerBackend()
        reads = tmp_path / "a.fastq.gz"
        reads.touch()
        spec = CommandSpec(
            argv=["fastqc", "--outdir", str(tmp_path), str(reads)], cwd=str(tmp_path)
        )
        wrapped = backend.build_command(spec)
        assert wrapped[0:3] == ["docker", "run", "--rm"]
        assert wrapped[-len(spec.argv) :] == spec.argv
        assert f"-v {tmp_path}:{tmp_path}" in " ".join(wrapped[1:])
        assert "-w" in wrapped
        image = wrapped[-len(spec.argv) - 1]
        assert image == BIOCONTAINER_IMAGES["fastqc"]

    def test_missing_image_raises_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NGS_CONTAINER_IMAGE", raising=False)
        backend = DockerBackend()
        with pytest.raises(RuntimeError, match="No container image configured"):
            backend.build_command(CommandSpec(argv=["mystery-tool"]))

    def test_env_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NGS_CONTAINER_IMAGE", "example.com/x:1")
        wrapped = DockerBackend().build_command(CommandSpec(argv=["tool"], env={"THREADS": "8"}))
        assert "-e" in wrapped and "THREADS=8" in wrapped


class TestApptainerBackend:
    def test_build_command_uses_exec_and_binds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NGS_CONTAINER_IMAGE", raising=False)
        backend = ApptainerBackend(apptainer_binary="apptainer")
        reads = tmp_path / "a.fq"
        reads.touch()
        spec = CommandSpec(argv=["fastqc", str(reads)], cwd=str(tmp_path))
        wrapped = backend.build_command(spec)
        assert wrapped[0:2] == ["apptainer", "exec"]
        # The working directory and input paths are bind-mounted at the same
        # absolute path, either as separate src:dst entries or in the
        # comma-separated short form apptainer accepts.
        bind_index = wrapped.index("--bind")
        bind_value = wrapped[bind_index + 1]
        assert str(tmp_path) in bind_value
        assert "--pwd" in wrapped
        assert wrapped[-len(spec.argv) :] == spec.argv

    def test_singularity_binary_is_used_when_only_it_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "shutil.which", lambda name: "/usr/bin/singularity" if name == "singularity" else None
        )
        backend = ApptainerBackend()
        assert backend._binary == "singularity"
        assert backend.is_available()


class TestSlurmBackend:
    def test_batch_script_includes_directives_and_command(self) -> None:
        spec = CommandSpec(
            argv=["hisat2", "-p", "4", "-x", "idx", "-1", "r1.fq"],
            cwd="/data",
            env={"OMP_NUM_THREADS": "4"},
            metadata={"slurm": {"partition": "general", "mem": "32G", "cpus_per_task": "4"}},
        )
        script = SlurmBackend.build_batch_script(spec)
        assert "#SBATCH --partition=general" in script
        assert "#SBATCH --mem=32G" in script
        assert "#SBATCH --cpus-per-task=4" in script
        assert "export OMP_NUM_THREADS=4" in script
        assert "cd /data" in script
        assert script.rstrip().endswith("hisat2 -p 4 -x idx -1 r1.fq")

    def test_env_directives_merge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NGS_SLURM_PARTITION", "fromenv")
        monkeypatch.setenv("NGS_SLURM_TIME", "04:00:00")
        options = SlurmBackend.slurm_options(CommandSpec(argv=["tool"]))
        assert options["partition"] == "fromenv"
        assert options["time"] == "04:00:00"

    def test_metadata_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NGS_SLURM_PARTITION", "fromenv")
        options = SlurmBackend.slurm_options(
            CommandSpec(argv=["tool"], metadata={"slurm": {"partition": "override"}})
        )
        assert options["partition"] == "override"

    def test_parse_job_id_parsable_format(self) -> None:
        assert SlurmBackend.parse_job_id("12345;cluster-name\n") == "12345"
        assert SlurmBackend.parse_job_id("12345\n") == "12345"

    def test_parse_exit_code(self) -> None:
        sacct = "12345|0:0|COMPLETED|01:02:03\n12345.batch|0:0|COMPLETED|01:02:03\n"
        assert SlurmBackend.parse_exit_code(sacct, "12345") == 0
        assert SlurmBackend.parse_exit_code("12345|2:0|FAILED|00:01:00\n", "12345") == 2
        assert SlurmBackend.parse_exit_code("", "12345") is None


class TestPBSBackend:
    def test_batch_script_directives(self) -> None:
        spec = CommandSpec(
            argv=["bwa-mem2", "mem", "ref", "r1.fq"],
            cwd="/work",
            metadata={"pbs": {"queue": "long", "walltime": "08:00:00", "mem": "16gb", "cpus": "8"}},
        )
        script = PBSBackend.build_batch_script(spec)
        assert "#PBS -q long" in script
        assert "#PBS -l walltime=08:00:00" in script
        assert "#PBS -l select=1:ncpus=8:mem=16gb" in script
        assert "#PBS -j oe" in script

    def test_parse_job_id_and_exit_status(self) -> None:
        assert PBSBackend.parse_job_id("12345.server.example.com\n") == "12345"
        qstat = "    Exit_status = 0\n    job_state = C\n"
        assert PBSBackend.parse_exit_status(qstat) == 0
        assert PBSBackend.parse_job_state(qstat) == "C"


class TestBackendSelector:
    @staticmethod
    def _install_fake_which(
        monkeypatch: pytest.MonkeyPatch,
        *,
        native_tools: bool = False,
        docker: bool = False,
        apptainer: bool = False,
        sbatch: bool = False,
        qsub: bool = False,
    ) -> None:
        from ngs_agent.execution.selector import NATIVE_PROBE_BINARIES

        available = {
            "docker": docker,
            "apptainer": apptainer,
            "singularity": apptainer,
            "sbatch": sbatch,
            "qsub": qsub,
            **{probe: native_tools for probe in NATIVE_PROBE_BINARIES},
        }
        monkeypatch.setattr(
            "shutil.which", lambda name: f"/usr/bin/{name}" if available.get(name) else None
        )

    def test_explicit_preferences(self) -> None:
        selector = BackendSelector()
        assert selector.select("native").backend.name == "native"

    def test_unknown_preference_raises_instead_of_silent_auto(self) -> None:
        """A typo like 'dockerr' must fail loudly, not fall through to auto."""
        with pytest.raises(ValueError, match="Unknown backend preference 'dockerr'"):
            BackendSelector().select("dockerr")

    def test_explicit_unavailable_backend_raises(self) -> None:
        with pytest.raises(RuntimeError, match="SLURM backend requested"):
            BackendSelector().select("slurm")

    def test_auto_prefers_scheduler_on_hpc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On an HPC login node with tools installed AND sbatch available, the
        # scheduler wins because heavy compute must not run on login nodes.
        self._install_fake_which(monkeypatch, native_tools=True, sbatch=True)
        selected = BackendSelector().select("auto")
        assert selected.backend.name == "slurm"
        assert "sbatch" in selected.reason

    def test_auto_prefers_pbs_when_only_scheduler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_which(monkeypatch, qsub=True)
        assert BackendSelector().select("auto").backend.name == "pbs"

    def test_auto_prefers_native_when_tools_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install_fake_which(monkeypatch, native_tools=True, docker=True, apptainer=True)
        selected = BackendSelector().select("auto")
        assert selected.backend.name == "native"

    def test_auto_prefers_apptainer_over_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Docker was previously picked before apptainer although docker is
        # wrong for shared/HPC systems.
        self._install_fake_which(monkeypatch, docker=True, apptainer=True)
        selected = BackendSelector().select("auto")
        assert selected.backend.name == "apptainer"

    def test_auto_falls_back_to_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_which(monkeypatch, docker=True)
        assert BackendSelector().select("auto").backend.name == "docker"

    def test_auto_last_resort_is_native(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_which(monkeypatch)
        selected = BackendSelector().select("auto")
        assert selected.backend.name == "native"
        assert "fallback" in selected.reason
