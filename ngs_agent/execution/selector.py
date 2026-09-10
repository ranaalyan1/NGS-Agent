from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ngs_agent.execution.backends.apptainer_backend import ApptainerBackend
from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.backends.docker_backend import DockerBackend
from ngs_agent.execution.backends.native_backend import NativeBackend
from ngs_agent.execution.backends.pbs_backend import PBSBackend
from ngs_agent.execution.backends.slurm_backend import SlurmBackend

BackendPreference = Literal["auto", "native", "docker", "apptainer", "slurm", "pbs"]

VALID_BACKEND_PREFERENCES: tuple[BackendPreference, ...] = (
    "auto",
    "native",
    "docker",
    "apptainer",
    "slurm",
    "pbs",
)

# Binaries probed to decide whether the native environment is usable. If all
# core pipeline tools resolve on PATH, native execution is preferred because
# it is the fastest and the path the project tests.
NATIVE_PROBE_BINARIES = ("samtools", "fastqc", "hisat2", "featureCounts")


@dataclass(frozen=True)
class SelectedBackend:
    backend: ExecutionBackend
    reason: str


class BackendSelector:
    """Chooses the execution backend.

    Auto-selection policy (in order):

    1. **SLURM/PBS** when a batch scheduler is installed (``sbatch``/``qsub``).
       On HPC clusters heavy compute on login/shared nodes is prohibited, so
       submitting through the scheduler is the safest default; set
       ``backend_preference = "native"`` to override.
    2. **Native** when the core pipeline tools (samtools, fastqc, hisat2,
       featureCounts) are on PATH — e.g. from the project conda environment.
    3. **Apptainer** when installed — user-space, no daemon, and the standard
       container runtime on shared/HPC systems.
    4. **Docker** when installed (least preferred container runtime because it
       requires a root daemon and does not work on most HPC clusters).
    5. **Native** as a last resort so commands fail with actionable tool
       errors instead of the agent refusing to start.
    """

    def __init__(self) -> None:
        self._native = NativeBackend()
        self._docker = DockerBackend()
        self._apptainer = ApptainerBackend()
        self._slurm = SlurmBackend()
        self._pbs = PBSBackend()

    def select(self, preference: str = "auto") -> SelectedBackend:
        if preference not in VALID_BACKEND_PREFERENCES:
            # A typo like "dockerr" must fail loudly, not silently fall
            # through to auto-selection.
            raise ValueError(
                f"Unknown backend preference '{preference}'. "
                f"Valid preferences: {', '.join(VALID_BACKEND_PREFERENCES)}."
            )
        if preference == "native":
            return SelectedBackend(self._native, "User preference: native backend")
        if preference == "docker":
            return self._select_explicit(self._docker, "docker", "Docker")
        if preference == "apptainer":
            return self._select_explicit(self._apptainer, "apptainer/singularity", "Apptainer")
        if preference == "slurm":
            return self._select_explicit(self._slurm, "sbatch", "SLURM")
        if preference == "pbs":
            return self._select_explicit(self._pbs, "qsub", "PBS")

        # ---- auto selection -------------------------------------------------
        if self._slurm.is_available():
            return SelectedBackend(
                self._slurm,
                "Auto-selected SLURM backend: 'sbatch' found (HPC scheduler detected; "
                "set backend_preference='native' to run on this host instead)",
            )
        if self._pbs.is_available():
            return SelectedBackend(
                self._pbs,
                "Auto-selected PBS backend: 'qsub' found (HPC scheduler detected; "
                "set backend_preference='native' to run on this host instead)",
            )
        if self._native_tools_available():
            return SelectedBackend(
                self._native,
                "Auto-selected native backend (core tools found on PATH)",
            )
        if self._apptainer.is_available():
            return SelectedBackend(
                self._apptainer,
                "Auto-selected apptainer backend (no local tools on PATH; "
                "apptainer preferred over docker for shared/HPC systems)",
            )
        if self._docker.is_available():
            return SelectedBackend(
                self._docker,
                "Auto-selected docker backend (no local tools or apptainer available)",
            )
        return SelectedBackend(
            self._native,
            "Auto-selected native backend as fallback; no core tools, apptainer, docker, "
            "or scheduler detected. Install tools with `mamba env create -f environment.yml`.",
        )

    def _select_explicit(
        self, backend: ExecutionBackend, binaries: str, label: str
    ) -> SelectedBackend:
        if backend.is_available():
            return SelectedBackend(backend, f"User preference: {label.lower()} backend")
        raise RuntimeError(
            f"{label} backend requested but {binaries} was not found. "
            f"Install {label} or switch backend_preference in ngs.toml."
        )

    def _native_tools_available(self) -> bool:
        import shutil

        return all(shutil.which(binary) is not None for binary in NATIVE_PROBE_BINARIES)
