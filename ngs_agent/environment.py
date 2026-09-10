"""Tool environment verification and platform compatibility checks.

Guards against two classes of silent failure:

1. **Shadowed tool installs** — a tool resolves to a system binary that
   shadows the conda-environment install (e.g. ``/usr/bin/samtools`` instead
   of ``$CONDA_PREFIX/bin/samtools``), so a different version runs than the
   one the environment pins.
2. **Incompatible platforms** — Apple Silicon macOS, where several bioconda
   recipes (gatk4, snpeff, some R/Bioconductor builds) still lack ``osx-arm64``
   builds and silently install (or fail) under Rosetta.
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Tools the RNA-Seq pipeline requires at execution time.
CORE_PIPELINE_TOOLS = ("fastqc", "trimmomatic", "hisat2", "samtools", "featureCounts", "multiqc")


@dataclass(frozen=True)
class ToolCheck:
    name: str
    path: str | None
    status: str  # "ok" | "missing" | "conflict"
    detail: str = ""


@dataclass
class ToolEnvironmentReport:
    expected_prefix: str | None
    checks: list[ToolCheck] = field(default_factory=list)

    @property
    def missing(self) -> list[ToolCheck]:
        return [check for check in self.checks if check.status == "missing"]

    @property
    def conflicts(self) -> list[ToolCheck]:
        return [check for check in self.checks if check.status == "conflict"]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.conflicts

    def summary(self) -> str:
        parts: list[str] = []
        if self.conflicts:
            names = ", ".join(f"{c.name} -> {c.path}" for c in self.conflicts)
            parts.append(f"Tools shadowing the expected environment: {names}")
        if self.missing:
            parts.append(f"Missing tools: {', '.join(c.name for c in self.missing)}")
        if not parts:
            parts.append(f"All {len(self.checks)} tools resolve from {self.expected_prefix}")
        return "; ".join(parts)


def resolve_expected_prefix() -> str | None:
    """Return the prefix tools are expected to come from.

    Priority: ``NGS_TOOL_PREFIX`` env var → ``CONDA_PREFIX`` (active conda
    env) → ``sys.prefix`` (the interpreter's own prefix).
    """
    import os

    for variable in ("NGS_TOOL_PREFIX", "CONDA_PREFIX"):
        value = os.environ.get(variable)
        if value:
            return str(Path(value).resolve())
    return str(Path(sys.prefix).resolve())


def verify_tool_environment(
    tools: tuple[str, ...] = CORE_PIPELINE_TOOLS,
    expected_prefix: str | None = None,
) -> ToolEnvironmentReport:
    """Check that each tool exists and resolves from the expected prefix.

    A tool is a *conflict* when it is on PATH but lives outside the expected
    prefix — meaning a system install shadows (or is shadowed by) the managed
    environment, which is exactly how wrong tool versions sneak into
    pipelines.
    """
    prefix = expected_prefix or resolve_expected_prefix()
    checks: list[ToolCheck] = []
    for name in tools:
        found = shutil.which(name)
        if found is None:
            checks.append(
                ToolCheck(name=name, path=None, status="missing", detail="not found on PATH")
            )
            continue
        resolved = str(Path(found).resolve())
        if prefix and not resolved.startswith(str(Path(prefix))):
            checks.append(
                ToolCheck(
                    name=name,
                    path=resolved,
                    status="conflict",
                    detail=f"resolves outside expected prefix {prefix}",
                )
            )
        else:
            checks.append(ToolCheck(name=name, path=resolved, status="ok", detail=f"from {prefix}"))
    return ToolEnvironmentReport(expected_prefix=prefix, checks=checks)


def platform_warnings() -> list[str]:
    """Return platform-specific compatibility warnings for this host."""
    warnings: list[str] = []
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        warnings.append(
            "Running on Apple Silicon (arm64) macOS: some bioconda recipes "
            "(notably gatk4, snpeff, and some R/Bioconductor builds) lack "
            "osx-arm64 builds. Prefer the Apptainer/Docker backend, or create "
            "the environment with CONDA_SUBDIR=osx-64 under Rosetta."
        )
    if system == "Windows":
        warnings.append(
            "Running on Windows: most bioinformatics tools used by this "
            "pipeline are Linux/macOS only. Use the docker backend or WSL2."
        )
    return warnings
