from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ngs_agent.execution.backends.apptainer_backend import ApptainerBackend
from ngs_agent.execution.backends.base import ExecutionBackend
from ngs_agent.execution.backends.docker_backend import DockerBackend
from ngs_agent.execution.backends.native_backend import NativeBackend


BackendPreference = Literal["auto", "native", "docker", "apptainer"]


@dataclass(frozen=True)
class SelectedBackend:
    backend: ExecutionBackend
    reason: str


class BackendSelector:
    def __init__(self) -> None:
        self._native = NativeBackend()
        self._docker = DockerBackend()
        self._apptainer = ApptainerBackend()

    def select(self, preference: BackendPreference) -> SelectedBackend:
        if preference == "native":
            return SelectedBackend(self._native, "User preference: native backend")

        if preference == "docker":
            if self._docker.is_available():
                return SelectedBackend(self._docker, "User preference: docker backend")
            raise RuntimeError(
                "Docker backend requested but docker was not found. "
                "Install Docker or switch to native backend in ngs.toml."
            )

        if preference == "apptainer":
            if self._apptainer.is_available():
                return SelectedBackend(self._apptainer, "User preference: apptainer backend")
            raise RuntimeError(
                "Apptainer backend requested but apptainer/singularity was not found. "
                "Install Apptainer or switch to native backend in ngs.toml."
            )

        if self._native.is_available():
            return SelectedBackend(self._native, "Auto-selected native backend (default)")
        if self._apptainer.is_available():
            return SelectedBackend(self._apptainer, "Auto-selected apptainer backend")
        if self._docker.is_available():
            return SelectedBackend(self._docker, "Auto-selected docker backend")

        raise RuntimeError(
            "No execution backend is available. Install Conda/Mamba tools for native mode, "
            "or install Docker/Apptainer and update backend preference."
        )
