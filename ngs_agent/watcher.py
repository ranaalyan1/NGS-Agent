"""Pipeline log watcher with regex/threshold signature matching."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml


@dataclass
class Signature:
    id: str
    name: str
    severity: str
    patterns: list[str]
    threshold: float | None
    threshold_field: str | None
    threshold_op: str | None  # "lt" or "gt" — omit for pattern-only
    explanation: str
    suggested_fix: str
    _compiled: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    def compile(self) -> None:
        self._compiled = [re.compile(p, re.IGNORECASE) for p in self.patterns]


@dataclass
class Match:
    signature: Signature
    line: str
    line_no: int
    value: float | None = None
    sample_id: str | None = None


def signatures_dir() -> Path:
    return Path(__file__).parent / "signatures"


def load_signatures(path: Path | None = None) -> list[Signature]:
    root = path or signatures_dir()
    sigs: list[Signature] = []
    for yaml_path in sorted(root.glob("*.yaml")):
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        sig = Signature(
            id=data["id"],
            name=data["name"],
            severity=data.get("severity", "warning"),
            patterns=data.get("patterns", []),
            threshold=data.get("threshold"),
            threshold_field=data.get("threshold_field"),
            threshold_op=data.get("threshold_op"),
            explanation=data["explanation"],
            suggested_fix=data["suggested_fix"],
        )
        sig.compile()
        sigs.append(sig)
    return sigs


def _extract_value(line: str, field_name: str | None) -> float | None:
    if not line:
        return None
    num_pattern = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
    if field_name:
        match = re.search(rf"{re.escape(field_name)}\s*[=:]\s*({num_pattern})", line, re.I)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
    numbers = re.findall(num_pattern, line)
    valid_nums: list[float] = []
    for n in numbers:
        try:
            valid_nums.append(float(n))
        except ValueError:
            continue
    return valid_nums[-1] if valid_nums else None


def match_line(line: str, line_no: int, signatures: list[Signature]) -> list[Match]:
    """Return all signature matches for a single log line."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return []
    matches: list[Match] = []
    for sig in signatures:
        if not any(p.search(line) for p in sig._compiled):
            continue
        value = _extract_value(line, sig.threshold_field)
        if sig.threshold is not None and value is not None and sig.threshold_op:
            if sig.threshold_op == "lt" and value >= sig.threshold:
                continue
            if sig.threshold_op == "gt" and value <= sig.threshold:
                continue
        matches.append(Match(signature=sig, line=line.rstrip(), line_no=line_no, value=value))
    return matches


def scan_file(path: Path, signatures: list[Signature] | None = None) -> list[Match]:
    sigs = signatures or load_signatures()
    all_matches: list[Match] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            all_matches.extend(match_line(line, line_no, sigs))
    return all_matches


def tail_file(
    path: Path,
    signatures: list[Signature] | None = None,
    poll_interval: float = 0.5,
) -> Iterator[Match]:
    """Tail a log file and yield matches as new lines appear."""
    sigs = signatures or load_signatures()
    with path.open(encoding="utf-8", errors="replace") as fh:
        fh.seek(0, 2)
        while True:
            line = fh.readline()
            if not line:
                time.sleep(poll_interval)
                continue
            line_no = fh.tell()
            for match in match_line(line, line_no, sigs):
                yield match
