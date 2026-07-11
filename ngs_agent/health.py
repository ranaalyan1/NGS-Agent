"""Run health scoring and sample-aware match aggregation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ngs_agent.watcher import Match

SEVERITY_WEIGHT = {"critical": 25, "warning": 10, "info": 3}
SAMPLE_RE = re.compile(r"(SAMPLE[-_]\w+|[A-Z]{2,}-\d{3,})", re.I)


@dataclass
class SampleHealth:
    sample_id: str
    matches: list[Match] = field(default_factory=list)
    score: int = 100
    grade: str = "A"

    @property
    def critical_count(self) -> int:
        return sum(1 for m in self.matches if m.signature.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for m in self.matches if m.signature.severity == "warning")


@dataclass
class RunHealth:
    score: int
    grade: str
    samples: list[SampleHealth]
    action_items: list[str]
    total_issues: int


def extract_sample_id(line: str) -> str | None:
    match = SAMPLE_RE.search(line)
    return match.group(1).upper() if match else None


def dedupe_matches(matches: list[Match]) -> list[Match]:
    """Keep one match per (sample, signature) pair — highest severity wins."""
    best: dict[tuple[str | None, str], Match] = {}
    for m in matches:
        key = (m.sample_id, m.signature.id)
        existing = best.get(key)
        if existing is None:
            best[key] = m
            continue
        w_new = SEVERITY_WEIGHT.get(m.signature.severity, 0)
        w_old = SEVERITY_WEIGHT.get(existing.signature.severity, 0)
        if w_new > w_old:
            best[key] = m
    return sorted(best.values(), key=lambda m: (m.sample_id or "", m.line_no))


def annotate_samples(matches: list[Match]) -> list[Match]:
    for m in matches:
        if m.sample_id is None:
            m.sample_id = extract_sample_id(m.line)
    return matches


def compute_run_health(matches: list[Match]) -> RunHealth:
    annotated = annotate_samples(dedupe_matches(matches))
    by_sample: dict[str, list[Match]] = {}
    for m in annotated:
        sid = m.sample_id or "UNKNOWN"
        by_sample.setdefault(sid, []).append(m)

    samples: list[SampleHealth] = []
    for sid, smatches in sorted(by_sample.items()):
        penalty = sum(SEVERITY_WEIGHT.get(m.signature.severity, 5) for m in smatches)
        score = max(0, 100 - penalty)
        samples.append(
            SampleHealth(
                sample_id=sid,
                matches=smatches,
                score=score,
                grade=_grade(score),
            )
        )

    overall_penalty = sum(SEVERITY_WEIGHT.get(m.signature.severity, 5) for m in annotated)
    overall_score = max(0, 100 - overall_penalty // max(len(by_sample), 1))
    actions = _build_action_checklist(annotated)

    return RunHealth(
        score=overall_score,
        grade=_grade(overall_score),
        samples=samples,
        action_items=actions,
        total_issues=len(annotated),
    )


def _grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _build_action_checklist(matches: list[Match]) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    ordered = sorted(
        matches,
        key=lambda m: SEVERITY_WEIGHT.get(m.signature.severity, 0),
        reverse=True,
    )
    for m in ordered:
        fix = m.signature.suggested_fix.strip().split(".")[0]
        key = f"{m.sample_id}:{m.signature.id}"
        if key in seen:
            continue
        seen.add(key)
        prefix = f"[{m.sample_id}] " if m.sample_id else ""
        items.append(f"{prefix}{m.signature.name}: {fix}.")
    return items[:8]
