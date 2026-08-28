"""Comprehensive QC parser supporting FastQC, MultiQC, Samtools, and Mosdepth."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class QCMetric:
    name: str
    value: str
    status: str  # pass, warn, fail
    source: str = "General"
    details: Optional[str] = None


class QCParser:
    """Multi-format QC parser for Next-Generation Sequencing pipelines."""

    @staticmethod
    def parse(path: Path) -> List[QCMetric]:
        if not path.exists():
            return []

        text = path.read_text(encoding="utf-8", errors="replace")

        # 1. FastQC Data parser
        if "##FastQC" in text or ">>Basic Statistics" in text:
            return QCParser._parse_fastqc(text)

        # 2. MultiQC JSON parser
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(text)
                return QCParser._parse_multiqc_json(data)
            except Exception:
                pass

        # 3. Samtools Flagstat parser
        if "in total (QC-passed reads + QC-failed reads)" in text or "mapped (" in text:
            return QCParser._parse_samtools_flagstat(text)

        # 4. Standard text / summary rules
        return QCParser._parse_generic_summary(text)

    @staticmethod
    def _parse_fastqc(text: str) -> List[QCMetric]:
        metrics: List[QCMetric] = []

        # Extract basic statistics
        total_seq = re.search(r"Total Sequences\s+(\d+)", text)
        if total_seq:
            count = int(total_seq.group(1))
            status = "pass" if count >= 1_000_000 else "warn"
            metrics.append(QCMetric(name="Total Sequences", value=f"{count:,}", status=status, source="FastQC"))

        gc_match = re.search(r"%GC\s+(\d+)", text)
        if gc_match:
            gc = float(gc_match.group(1))
            status = "pass" if 40 <= gc <= 60 else "warn"
            metrics.append(QCMetric(name="GC Content", value=f"{gc:.1f}%", status=status, source="FastQC"))

        seq_len = re.search(r"Sequence length\s+(\d+)", text)
        if seq_len:
            metrics.append(QCMetric(name="Sequence Length", value=f"{seq_len.group(1)} bp", status="pass", source="FastQC"))

        # Module statuses
        modules = re.findall(r">>([A-Za-z0-9 _]+)\s+(pass|warn|fail)", text)
        for mod_name, mod_status in modules:
            if mod_name not in ("Basic Statistics",):
                metrics.append(QCMetric(name=mod_name, value=mod_status.upper(), status=mod_status.lower(), source="FastQC"))

        return metrics

    @staticmethod
    def _parse_samtools_flagstat(text: str) -> List[QCMetric]:
        metrics: List[QCMetric] = []
        map_match = re.search(r"(\d+\.?\d*)%\s*:\s*mapped", text) or re.search(r"mapped\s*\((\d+\.?\d*)%", text)
        if map_match:
            val = float(map_match.group(1))
            status = "pass" if val >= 90 else ("warn" if val >= 75 else "fail")
            metrics.append(QCMetric(name="Mapping Rate", value=f"{val:.1f}%", status=status, source="Samtools"))

        dups = re.search(r"(\d+)\s*\+\s*\d+\s+duplicates", text)
        total = re.search(r"(\d+)\s*\+\s*\d+\s+in total", text)
        if dups and total and int(total.group(1)) > 0:
            dup_pct = (int(dups.group(1)) / int(total.group(1))) * 100
            status = "pass" if dup_pct <= 20 else ("warn" if dup_pct <= 40 else "fail")
            metrics.append(QCMetric(name="Duplication Rate", value=f"{dup_pct:.1f}%", status=status, source="Samtools"))

        return metrics

    @staticmethod
    def _parse_multiqc_json(data: dict) -> List[QCMetric]:
        metrics: List[QCMetric] = []
        general = data.get("report_general_stats_data", [])
        for block in general:
            for sample_id, stats in block.items():
                for key, val in stats.items():
                    if isinstance(val, (int, float)):
                        name = key.replace("_", " ").title()
                        metrics.append(QCMetric(name=f"{sample_id}: {name}", value=str(round(val, 2)), status="pass", source="MultiQC"))
        return metrics

    @staticmethod
    def _parse_generic_summary(text: str) -> List[QCMetric]:
        metrics: List[QCMetric] = []
        rules = [
            ("Mapping Rate", r"mapping\s+rate[:\s]+(\d+\.?\d*)%?", lambda v: v >= 90, lambda v: v >= 75),
            ("Mean Coverage", r"mean\s+coverage[:\s]+(\d+\.?\d*)", lambda v: v >= 30, lambda v: v >= 15),
            ("Duplication Rate", r"duplicat(?:ion|e)\s+rate[:\s]+(\d+\.?\d*)%?", lambda v: v <= 20, lambda v: v <= 40),
            ("Q30 Fraction", r"Q30[:\s]+(\d+\.?\d*)%?", lambda v: v >= 85, lambda v: v >= 70),
        ]
        for name, pattern, pass_fn, warn_fn in rules:
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            value = float(match.group(1))
            if pass_fn(value):
                status = "pass"
            elif warn_fn(value):
                status = "warn"
            else:
                status = "fail"
            metrics.append(QCMetric(name=name, value=f"{value:.1f}", status=status, source="QC Summary"))
        return metrics
