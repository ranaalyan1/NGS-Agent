"""MultiQC summary parser — extracts QC metrics from a MultiQC text dump."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse

# Regex patterns for common QC metrics
_PATTERNS = {
    "mapping_rate": (r"mapping[_\s]rate[:\s]+([\d.]+)\s*%", float, (80.0, 90.0)),
    "mean_coverage": (r"(?:mean[_\s])?coverage[:\s]+([\d.]+)\s*x?", float, (20.0, 30.0)),
    "duplication_rate": (r"duplication[_\s]rate[:\s]+([\d.]+)\s*%", float, (30.0, 50.0)),
    "q30_rate": (r"q30[_\s]rate[:\s]+([\d.]+)\s*%?", float, (85.0, 90.0)),
    "total_reads": (r"total[_\s]reads[:\s]+([\d,]+)", lambda s: int(s.replace(",", "")), None),
    "paired_reads": (r"paired[_\s]reads[:\s]+([\d,]+)", lambda s: int(s.replace(",", "")), None),
    "gc_content": (r"gc[_\s]content[:\s]+([\d.]+)\s*%?", float, (40.0, 60.0)),
    "insert_size": (r"insert[_\s]size[:\s]+([\d.]+)", float, (150.0, 300.0)),
}


def parse_multiqc(text: str) -> list[dict]:
    metrics = []
    for name, (pattern, cast, thresholds) in _PATTERNS.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        try:
            value = cast(m.group(1))
        except (ValueError, TypeError):
            continue
        grade = "pass"
        if thresholds:
            low, high = thresholds
            if value < low:
                grade = "fail"
            elif value < high:
                grade = "warn"
            elif name == "duplication_rate" and value > high:
                grade = "fail"
        metrics.append({
            "name": name,
            "value": value,
            "grade": grade,
            "thresholds": thresholds,
        })
    return metrics


def _format(metrics: list[dict]) -> str:
    if not metrics:
        return "No QC metrics recognized in the input file."
    out = ["# QC Metrics\n"]
    out.append("| Metric | Value | Grade |")
    out.append("|---|---|---|")
    for m in metrics:
        out.append(f"| {m['name']} | {m['value']} | {m['grade'].upper()} |")
    failed = [m for m in metrics if m["grade"] == "fail"]
    warned = [m for m in metrics if m["grade"] == "warn"]
    out.append("")
    if failed:
        out.append(f"## FAILED ({len(failed)})")
        for m in failed:
            out.append(f"- {m['name']} = {m['value']} (expected >= {m['thresholds'][0]})")
    if warned:
        out.append(f"## WARNED ({len(warned)})")
        for m in warned:
            out.append(f"- {m['name']} = {m['value']}")
    if not failed and not warned:
        out.append("All metrics passed.")
    return "\n".join(out)


class MultiQcParseTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="multiqc_parse",
            description=(
                "Parse a MultiQC summary text file and extract QC metrics: "
                "mapping rate, mean coverage, duplication rate, Q30 rate, total reads, "
                "GC content, insert size. Each metric is graded pass/warn/fail against "
                "standard thresholds. Use this when triaging a pipeline failure or QC report."
            ),
            parameters={
                "path": {"type": "string", "description": "Path to MultiQC summary text file"},
            },
            required=["path"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        path = Path(params["path"])
        if not path.is_absolute():
            path = Path(ctx.cwd) / path
        if not path.exists():
            return ToolResponse(content=f"QC file not found: {path}", is_error=True)

        text = path.read_text()
        if ctx.file_tracker:
            ctx.file_tracker.record_read(str(path))

        metrics = parse_multiqc(text)
        return ToolResponse(
            content=_format(metrics),
            metadata={"qc_metrics": metrics, "source": str(path)},
        )
