"""Shared helpers for the container backends (Docker and Apptainer).

Includes the pinned BioContainers image map used as a default when the user
has not configured one, and bind-mount resolution so that input/output paths
appearing on the command line are visible inside the container at the *same*
absolute path (the same strategy nf-core uses for its singularity integration).
"""

from __future__ import annotations

import os
from pathlib import Path

from ngs_agent.execution.models import CommandSpec

# Pinned BioContainers images for the core pipeline tools. Versions match the
# pins in environment.yml so container and native runs use the same tool
# versions. Tags verified against quay.io/biocontainers.
BIOCONTAINER_IMAGES: dict[str, str] = {
    "fastqc": "quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0",
    "trimmomatic": "quay.io/biocontainers/trimmomatic:0.39--hdfd78af_2",
    "hisat2": "quay.io/biocontainers/hisat2:2.2.1--h503566f_8",
    "hisat2_extract_splice_sites.py": "quay.io/biocontainers/hisat2:2.2.1--h503566f_8",
    "samtools": "quay.io/biocontainers/samtools:1.19.2--h50ea8bc_1",
    "featureCounts": "quay.io/biocontainers/subread:2.0.6--he4a0461_0",
    "multiqc": "quay.io/biocontainers/multiqc:1.21--pyhdfd78af_0",
    "Rscript": "bioconductor/bioconductor_docker:RELEASE_3_18",
}

# Environment variables that let users override the image for a whole run or
# for a single tool, e.g. NGS_CONTAINER_IMAGE or NGS_CONTAINER_IMAGE_HISAT2.
CONTAINER_IMAGE_ENV = "NGS_CONTAINER_IMAGE"
CONTAINER_IMAGE_TOOL_ENV_PREFIX = "NGS_CONTAINER_IMAGE_"


def resolve_image(spec: CommandSpec) -> str | None:
    """Resolve the container image for a command.

    Priority:
      1. ``spec.metadata["image"]`` (explicit per-command override)
      2. ``NGS_CONTAINER_IMAGE_<TOOL>`` (e.g. NGS_CONTAINER_IMAGE_HISAT2)
      3. ``NGS_CONTAINER_IMAGE`` (single image for every command)
      4. The pinned BioContainers map keyed by the tool binary (``argv[0]``)
    """
    explicit = spec.metadata.get("image")
    if isinstance(explicit, str) and explicit:
        return explicit

    binary = Path(spec.argv[0]).name if spec.argv else ""
    if binary:
        tool_env = os.environ.get(
            f"{CONTAINER_IMAGE_TOOL_ENV_PREFIX}{binary.upper().replace('.', '_').replace('-', '_')}"
        )
        if tool_env:
            return tool_env
    default_env = os.environ.get(CONTAINER_IMAGE_ENV)
    if default_env:
        return default_env

    return BIOCONTAINER_IMAGES.get(binary)


def _existing_ancestor(path: Path) -> Path | None:
    """Return the closest existing ancestor of ``path`` (or the path itself)."""
    current = path
    while current != current.parent:
        if current.exists():
            return current
        current = current.parent
    return current if current.exists() else None


def collect_bind_roots(spec: CommandSpec) -> list[str]:
    """Collect directory roots that must be mounted into the container.

    Every absolute path appearing in ``argv`` (plus ``spec.cwd`` and any
    ``spec.metadata["mounts"]``) is reduced to its longest *existing*
    ancestor. Container tools create output directories before the command
    runs, so input paths and output parents normally exist by execution time.
    Mounting at identical paths means the command needs no rewriting.
    """
    roots: list[str] = []
    candidates: list[str] = [spec.cwd] if spec.cwd else []
    candidates.extend(
        str(mount) for mount in spec.metadata.get("mounts", []) if isinstance(mount, str)
    )
    for token in spec.argv[1:]:
        token = token.strip()
        if token.startswith("/") and not token.startswith("//"):
            candidates.append(token)

    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            continue
        ancestor = _existing_ancestor(path)
        if ancestor is None:
            continue
        root = str(ancestor if ancestor.is_dir() else ancestor.parent)
        if root not in roots and root != "/":
            roots.append(root)
    return sorted(roots, key=len)
