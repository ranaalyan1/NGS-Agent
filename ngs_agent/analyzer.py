"""VCF parser, QC scanner, and report rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ngs_agent.qc import QCMetric, QCParser


@dataclass
class Variant:
    chrom: str
    pos: int
    ref: str
    alt: str
    gene: str
    consequence: str
    clinvar: str
    af: float | None
    depth: int | None
    vaf: float | None
    is_pathogenic: bool = False
    is_vus: bool = False
    samples: dict[str, dict[str, Any]] = field(default_factory=dict)


def _parse_consequence(csq_or_ann: str | None) -> str:
    if not csq_or_ann or csq_or_ann == ".":
        return "."
    first_allele_csq = csq_or_ann.split(",")[0]
    if "|" in first_allele_csq:
        parts = first_allele_csq.split("|")
        if len(parts) > 1 and parts[1]:
            return parts[1]
        return parts[0]
    return first_allele_csq


def parse_vcf(path: Path) -> list[Variant]:
    variants: list[Variant] = []
    with path.open(encoding="utf-8") as fh:
        sample_names: list[str] = []
        for line in fh:
            if line.startswith("##"):
                continue
            if line.startswith("#CHROM"):
                headers = line.strip().split("\t")
                if len(headers) > 9:
                    sample_names = headers[9:]
                continue

            parts = line.strip().split("\t")
            if len(parts) < 8:
                continue
            chrom, pos_s, _id, ref, alt = parts[0], parts[1], parts[2], parts[3], parts[4]
            info = parts[7]
            format_keys = parts[8].split(":") if len(parts) > 8 else []
            fmt = parts[9] if len(parts) > 9 else ""

            csq_raw = _info_field(info, "CSQ") or _info_field(info, "ANN") or _info_field(info, "CONSEQUENCE") or "."
            consequence = _parse_consequence(csq_raw)

            gene = _info_field(info, "GENE") or _info_field(info, "SYMBOL")
            if not gene or gene == ".":
                if csq_raw != ".":
                    first_csq = csq_raw.split(",")[0]
                    csq_parts = first_csq.split("|")
                    if len(csq_parts) >= 4 and csq_parts[3] and csq_parts[3] != ".":
                        gene = csq_parts[3]
                    elif len(csq_parts) >= 5 and csq_parts[4] and csq_parts[4] != ".":
                        gene = csq_parts[4]
            if not gene:
                gene = "."

            clinvar = _info_field(info, "CLNSIG") or _info_field(info, "CLINVAR") or "."
            af = _parse_float(_info_field(info, "AF"))
            depth, vaf = _parse_sample(fmt, format_keys)

            clinvar_lower = clinvar.lower()
            is_pathogenic = "pathogenic" in clinvar_lower and "conflict" not in clinvar_lower
            is_vus = (
                "uncertain" in clinvar_lower
                or "vus" in clinvar_lower
                or "unknown significance" in clinvar_lower
                or "unknown_significance" in clinvar_lower
            )

            # Parse multisample columns if available
            sample_data = {}
            if sample_names and len(parts) > 9:
                for idx, sname in enumerate(sample_names):
                    if 9 + idx < len(parts):
                        sfmt = parts[9 + idx]
                        s_dp, s_vaf = _parse_sample(sfmt, format_keys)
                        sample_data[sname] = {"dp": s_dp, "vaf": s_vaf, "raw": sfmt}

            variants.append(
                Variant(
                    chrom=chrom,
                    pos=int(pos_s),
                    ref=ref,
                    alt=alt,
                    gene=gene,
                    consequence=consequence,
                    clinvar=clinvar,
                    af=af,
                    depth=depth,
                    vaf=vaf,
                    is_pathogenic=is_pathogenic,
                    is_vus=is_vus,
                    samples=sample_data,
                )
            )
    return variants


def _info_field(info: str, key: str) -> str | None:
    for part in info.split(";"):
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1]
    return None


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.split(",")[0])
    except ValueError:
        return None


def _parse_sample(fmt: str, format_keys: list[str]) -> tuple[int | None, float | None]:
    if not fmt or fmt == ".":
        return None, None
    values = fmt.split(":")
    fields = dict(zip(format_keys, values, strict=False))

    depth = None
    vaf = None
    if "DP" in fields:
        try:
            depth = int(float(fields["DP"]))
        except (ValueError, TypeError):
            depth = None
    if "AD" in fields:
        try:
            ad_parts = fields["AD"].split(",")
            ads = [int(float(x)) for x in ad_parts if x not in (".", "")]
            if len(ads) >= 2 and sum(ads) > 0:
                vaf = ads[1] / sum(ads)
                if depth is None:
                    depth = sum(ads)
        except (ValueError, TypeError):
            vaf = None
    return depth, vaf


def scan_qc(path: Path) -> list[QCMetric]:
    """Scan any QC file (FastQC, MultiQC, Samtools, or plain text)."""
    return QCParser.parse(path)


def render_report(
    variants: list[Variant],
    qc_metrics: list[QCMetric] | None = None,
    console: Console | None = None,
) -> None:
    con = console or Console()
    pathogenic = [v for v in variants if v.is_pathogenic]
    vus = [v for v in variants if v.is_vus]
    other = [v for v in variants if not v.is_pathogenic and not v.is_vus]

    con.print(Panel("[bold]NGS-Agent Variant Report[/bold]", style="blue"))

    if qc_metrics:
        qc_table = Table(title="QC Summary", show_header=True)
        qc_table.add_column("Metric")
        qc_table.add_column("Value")
        qc_table.add_column("Status")
        for m in qc_metrics:
            style = {"pass": "green", "warn": "yellow", "fail": "red"}.get(m.status, "white")
            qc_table.add_row(m.name, m.value, f"[{style}]{m.status.upper()}[/{style}]")
        con.print(qc_table)
        con.print()

    def _variant_table(title: str, rows: list[Variant], style: str) -> None:
        if not rows:
            return
        table = Table(title=title, show_header=True, header_style=style)
        table.add_column("Gene")
        table.add_column("Variant")
        table.add_column("Consequence")
        table.add_column("ClinVar")
        table.add_column("Depth/VAF")
        for v in rows:
            loc = f"{v.chrom}:{v.pos} {v.ref}>{v.alt}"
            dv = f"{v.depth or '—'}/{f'{v.vaf:.0%}' if v.vaf is not None else '—'}"
            table.add_row(v.gene, loc, v.consequence[:40], v.clinvar, dv)
        con.print(table)
        con.print()

    _variant_table("Pathogenic / Likely Pathogenic", pathogenic, "bold red")
    _variant_table("Variants of Uncertain Significance (VUS)", vus, "bold yellow")
    _variant_table("Other Variants", other, "bold")

    con.print(
        Panel(
            f"Total: {len(variants)} variants | "
            f"Pathogenic: {len(pathogenic)} | VUS: {len(vus)} | Other: {len(other)}\n"
            "Run [bold]ngsagent debate[/bold] on VUS entries (requires LLM).",
            title="Summary",
        )
    )
