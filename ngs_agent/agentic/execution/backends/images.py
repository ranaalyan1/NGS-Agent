"""Container image resolution for tool commands.

Maps the first element of a command's argv (the tool binary) to a container
image. Users can override any mapping with environment variables of the form
``NGS_IMAGE_<TOOL>`` (e.g. ``NGS_IMAGE_FASTQC``), or set a global fallback
image with ``NGS_CONTAINER_IMAGE``.
"""
from __future__ import annotations

import os
import re

# Public biocontainers images for the built-in tools.
DEFAULT_TOOL_IMAGES: dict[str, str] = {
    "fastqc": "quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0",
    "trimmomatic": "quay.io/biocontainers/trimmomatic:0.39--hdfd78af_2",
    "hisat2": "quay.io/biocontainers/hisat2:2.2.1--hdbdd923_6",
    "samtools": "quay.io/biocontainers/samtools:1.19.2--h50ea8bc_1",
    "featureCounts": "quay.io/biocontainers/subread:2.0.6--he4a0461_2",
    "multiqc": "quay.io/biocontainers/multiqc:1.21--pyhdfd78af_0",
    "Rscript": "quay.io/biocontainers/bioconductor-deseq2:1.42.0--r43hf17093f_0",
}

FALLBACK_IMAGE = "quay.io/biocontainers/samtools:1.19.2--h50ea8bc_1"


def resolve_image(argv: list[str]) -> str:
    """Resolve the container image to use for a command."""
    binary = argv[0] if argv else ""
    env_key = "NGS_IMAGE_" + re.sub(r"[^A-Za-z0-9]", "_", binary).upper()
    if os.environ.get(env_key):
        return os.environ[env_key]
    if binary in DEFAULT_TOOL_IMAGES:
        return DEFAULT_TOOL_IMAGES[binary]
    return os.environ.get("NGS_CONTAINER_IMAGE", FALLBACK_IMAGE)
