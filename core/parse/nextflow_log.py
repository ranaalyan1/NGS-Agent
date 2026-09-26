"""Nextflow log parser: a log file in, lines with numbers out.

The parser only splits text and notes where things are. Deciding *why* a run
died is the matcher's job (core/diagnose.py).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..models import LogFacts
from ..util import decode, read_gzip_head, read_head, sha256_file

VERSION_RE = re.compile(r"N E X T F L O W\s+~\s+version\s+([\d.]+)", re.IGNORECASE)

#: Markers that make a paragraph worth treating as an error block.
ERROR_MARKERS = (
    "ERROR ~",
    "Caused by:",
    "Command error:",
    "Command exit status:",
    "Execution cancelled",
    "EXITING because of FATAL ERROR",
    "Exception",
)


def read_log_text(path: str | Path, max_bytes: int = 32 * 1024 * 1024) -> str:
    """Read a log, transparently handling gzip. Huge logs are read whole: log
    diagnosis needs the tail as much as the head, and 32 MB is the cap."""
    p = Path(path)
    head = read_head(p, 2)
    if head[:2] == b"\x1f\x8b":
        data, _ = read_gzip_head(p, max_bytes)
        return decode(data)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def split_blocks(lines: list[str]) -> list[list[int]]:
    """Split a log into paragraphs. Returns lists of 1-based line numbers.

    Nextflow writes errors as a block of non-blank lines (the command, the
    cause, the exit status), so paragraphs are the natural unit to score.
    """
    blocks: list[list[int]] = []
    current: list[int] = []
    for index, line in enumerate(lines, start=1):
        if line.strip():
            current.append(index)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def parse_nextflow_log(path: str | Path, text_override: str | None = None) -> LogFacts:
    """Extract lines, version and error blocks; optional text supports read-only watch snapshots."""
    p = Path(path)
    text = read_log_text(p) if text_override is None else text_override
    lines = text.splitlines()
    facts = LogFacts(
        source_path=str(p),
        source_sha256=sha256_file(p)
        if text_override is None
        else hashlib.sha256(text.encode("utf-8")).hexdigest(),
        lines=lines,
    )
    match = VERSION_RE.search(text)
    if match:
        facts.nextflow_version = match.group(1)

    for block in split_blocks(lines):
        text_of_block = "\n".join(lines[i - 1] for i in block)
        if any(marker in text_of_block for marker in ERROR_MARKERS):
            facts.error_blocks.append(block)
    return facts


def run_failed(facts: LogFacts) -> bool:
    """Did this run die? Judged from lines that only appear on failure."""
    hard = (
        "Execution cancelled",
        "ERROR ~ Error executing process",
        "Execution failed",
        "Pipeline completed with errors",
    )
    text = "\n".join(facts.lines)
    if any(marker in text for marker in hard):
        return True
    return False


def run_succeeded(facts: LogFacts) -> bool:
    return any(
        marker in "\n".join(facts.lines)
        for marker in ("Pipeline completed successfully", "Execution complete", "Succeeded")
    )
