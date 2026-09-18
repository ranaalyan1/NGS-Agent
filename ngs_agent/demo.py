"""Bundled demo files that work from an installed wheel, anywhere.

The README quickstart only works from a git checkout because wheels never
shipped `demo_data/`. These helpers resolve the packaged copies inside the
`ngs_agent` package (with a fallback to the repository layout), so
`ngsagent demo` works right after `pip install ngs-agent`, from any directory.
"""

from __future__ import annotations

from pathlib import Path


def _resolve(filename: str) -> Path:
    # 1. Packaged data (installed wheel / sdist): ngs_agent/demo_data/.
    sibling = Path(__file__).parent / "demo_data" / filename
    if sibling.is_file():
        return sibling
    # 2. Repository checkout layout: <repo>/demo_data/.
    repo = Path(__file__).parent.parent / "demo_data" / filename
    return repo


def demo_vcf() -> Path:
    return _resolve("sample.vcf")


def demo_log() -> Path:
    return _resolve("sample.log")
