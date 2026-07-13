"""Log diagnosis tool — scans pipeline logs for known failure signatures.

Successor to v0.2's `watch` command, refactored as a BaseTool so the agent
loop can call it during QC triage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


@dataclass
class Signature:
    name: str
    severity: str          # "critical" | "warning" | "info"
    pattern: str           # regex
    explanation: str
    suggested_fix: str

    def match(self, line: str) -> re.Match | None:
        return re.search(self.pattern, line, re.IGNORECASE)


# Built-in signatures (mirrors v0.2)
BUILTIN_SIGNATURES = [
    Signature(
        name="Adapter Contamination",
        severity="critical",
        pattern=r"adapter.*overrepresented|overrepresented.*adapter|adapter[._-]content.*high",
        explanation="Adapter sequences are overrepresented in reads, suggesting insufficient adapter trimming or library prep issue.",
        suggested_fix="Re-run Trimmomatic/Cutadapt with adapter sequences. Check library prep for low input DNA.",
    ),
    Signature(
        name="Low Alignment Rate",
        severity="critical",
        pattern=r"(?:mapping|alignment)[._\s-]?rate[:\s]+(\d+\.?\d*)\s*%?|overall[._\s]alignment[._\s]rate[:\s]+(\d+\.?\d*)\s*%?",
        explanation="Overall mapping rate below 80% indicates poor alignment — likely contamination, wrong reference, or low-quality reads.",
        suggested_fix="Verify reference genome matches sample. Check for contamination with Kraken. Re-trim low-quality bases.",
    ),
    Signature(
        name="Low Mean Coverage",
        severity="critical",
        pattern=r"(?:mean|average)[._\s]?coverage[:\s]+(\d+\.?\d*)|mean[._\s]depth[:\s]+(\d+\.?\d*)",
        explanation="Mean sequencing depth below 20x is inadequate for variant calling sensitivity.",
        suggested_fix="Re-sequence to higher depth, or narrow target panel. Check pool balancing if multiplexed.",
    ),
    Signature(
        name="High PCR Duplication",
        severity="warning",
        pattern=r"duplication[._\s]?rate[:\s]+(\d+\.?\d*)\s*%?|pcr[._\s]duplicate.*high",
        explanation="Duplication rate above 30% indicates PCR over-amplification — reduces effective coverage.",
        suggested_fix="Reduce PCR cycles. Use less starting material. Remove duplicates in downstream analysis (Picard MarkDuplicates).",
    ),
    Signature(
        name="Poor Insert Size",
        severity="warning",
        pattern=r"insert[._\s]?size[:\s]+(\d+\.?\d*)|median[._\s]insert[:\s]+(\d+\.?\d*)",
        explanation="Median insert size below 150bp suggests library fragmentation issue or adapter dimer contamination.",
        suggested_fix="Re-fragment library to 300-500bp. Check sonication settings. Re-purify to remove adapter dimers.",
    ),
    Signature(
        name="OOM Kill",
        severity="critical",
        pattern=r"out of memory|oom-?kill|killed process|cannot allocate memory",
        explanation="Process was killed by OOM. Tool exceeded available memory.",
        suggested_fix="Increase memory limit. Sort BAM before indexing. Use -Xmx flag for Java tools. Split input by chromosome.",
    ),
    Signature(
        name="Disk Full",
        severity="critical",
        pattern=r"no space left on device|disk full|errno 28",
        explanation="Disk ran out of space during pipeline run.",
        suggested_fix="Free up disk space. Clean intermediate files. Use larger temp volume. Stream output to object storage.",
    ),
    Signature(
        name="GATK Error",
        severity="critical",
        pattern=r"org\.broadinstitute\.gatk|gatk.*error|java\.lang\.(?:runtimeexception|exception)",
        explanation="GATK threw a Java exception — likely reference mismatch or malformed input.",
        suggested_fix="Check reference index matches VCF contig names. Verify sample sheet. Run GATK with --java-debug.",
    ),
    Signature(
        name="BWA Alignment Error",
        severity="critical",
        pattern=r"bwa.*error|fail.*to.*align|incorrect reference",
        explanation="BWA failed to align — reference index mismatch or corrupted FASTQ.",
        suggested_fix="Rebuild BWA index. Verify FASTQ integrity with md5sum. Check sample sheet.",
    ),
    Signature(
        name="FastQC Failure",
        severity="warning",
        pattern=r"fastqc.*fail|per[._\s]base[._\s]sequence[._\s]quality.*fail",
        explanation="FastQC flagged a quality module as failed.",
        suggested_fix="Inspect FastQC HTML report. Re-trim low-quality bases. Check base quality by cycle.",
    ),
]


@dataclass
class Match:
    line_no: int
    line: str
    signature: Signature


def scan_log(text: str, signatures: list[Signature] | None = None) -> list[Match]:
    sigs = signatures or BUILTIN_SIGNATURES
    matches: list[Match] = []
    for i, line in enumerate(text.splitlines(), 1):
        for sig in sigs:
            if sig.match(line):
                matches.append(Match(line_no=i, line=line, signature=sig))
                break  # one signature per line
    return matches


def _format(matches: list[Match], path: str) -> str:
    if not matches:
        return f"No failure signatures detected in {path}."
    out = [f"# Log Diagnosis — {path}\n"]
    out.append(f"Found {len(matches)} issue(s):\n")
    for m in matches:
        sev = m.signature.severity.upper()
        out.append(f"## [{sev}] {m.signature.name} (line {m.line_no})")
        out.append(f"  Matched: `{m.line.strip()[:200]}`")
        out.append(f"  Explanation: {m.signature.explanation}")
        out.append(f"  Suggested fix: {m.signature.suggested_fix}")
        out.append("")
    return "\n".join(out)


class LogDiagnoseTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="log_diagnose",
            description=(
                "Scan a pipeline log file for known failure signatures: adapter "
                "contamination, low mapping rate, low coverage, high duplication, "
                "OOM kills, disk full, GATK/BWA errors, FastQC failures. Returns "
                "matched lines with plain-English explanations and concrete fixes."
            ),
            parameters={
                "path": {"type": "string", "description": "Path to pipeline log file"},
            },
            required=["path"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        path = Path(params["path"])
        if not path.is_absolute():
            path = Path(ctx.cwd) / path
        if not path.exists():
            return ToolResponse(content=f"Log file not found: {path}", is_error=True)

        text = path.read_text(errors="replace")
        if ctx.file_tracker:
            ctx.file_tracker.record_read(str(path))

        matches = scan_log(text)
        return ToolResponse(
            content=_format(matches, str(path)),
            metadata={
                "match_count": len(matches),
                "matches": [
                    {
                        "line_no": m.line_no,
                        "name": m.signature.name,
                        "severity": m.signature.severity,
                        "line": m.line[:200],
                    }
                    for m in matches
                ],
                "source": str(path),
            },
        )


def load_signatures_dir(dir_path: Path) -> list[Signature]:
    """Load custom YAML signatures from a directory."""
    sigs: list[Signature] = []
    if not dir_path.exists():
        return sigs
    for yf in dir_path.glob("*.yaml"):
        data = yaml.safe_load(yf.read_text())
        if not isinstance(data, list):
            data = [data]
        for item in data:
            sigs.append(Signature(
                name=item["name"],
                severity=item.get("severity", "warning"),
                pattern=item["pattern"],
                explanation=item.get("explanation", ""),
                suggested_fix=item.get("suggested_fix", ""),
            ))
    return sigs
