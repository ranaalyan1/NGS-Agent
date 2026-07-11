"""VCF parser, QC scanner, and report rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


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


@dataclass
class QCMetric:
    name: str
    value: str
    status: str  # pass, warn, fail


def parse_vcf(path: Path) -> list[Variant]:
    variants: list[Variant] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            parts = line.strip().split("\t")
            if len(parts) < 8:
                continue
            chrom, pos_s, _id, ref, alt = parts[0], parts[1], parts[2], parts[3], parts[4]
            info = parts[7]
            fmt = parts[9] if len(parts) > 9 else ""
            format_keys = parts[8].split(":") if len(parts) > 8 else []

            gene = _info_field(info, "GENE") or _info_field(info, "SYMBOL") or "."
            consequence = _info_field(info, "CSQ") or _info_field(info, "ANN") or "."
            clinvar = _info_field(info, "CLNSIG") or _info_field(info, "CLINVAR") or "."
            af = _parse_float(_info_field(info, "AF"))
            depth, vaf = _parse_sample(fmt, format_keys)

            clinvar_lower = clinvar.lower()
            is_pathogenic = "pathogenic" in clinvar_lower and "conflict" not in clinvar_lower
            is_vus = (
                "uncertain" in clinvar_lower
                or "vus" in clinvar_lower
                or "unknown significance" in clinvar_lower
            )

            variants.append(
                Variant(
                    chrom=chrom,
                    pos=int(pos_s),
                    ref=ref,
                    alt=alt,
                    gene=gene,
                    consequence=consequence.split("|")[0] if "|" in consequence else consequence,
                    clinvar=clinvar,
                    af=af,
                    depth=depth,
                    vaf=vaf,
                    is_pathogenic=is_pathogenic,
                    is_vus=is_vus,
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
        except ValueError:
            depth = None
    if "AD" in fields and depth:
        try:
            ads = [int(x) for x in fields["AD"].split(",")]
            if len(ads) >= 2 and sum(ads) > 0:
                vaf = ads[1] / sum(ads)
        except ValueError:
            vaf = None
    return depth, vaf


def scan_qc(path: Path) -> list[QCMetric]:
    """Scan a MultiQC-style or plain-text QC summary file."""
    metrics: list[QCMetric] = []
    if not path.exists():
        return metrics
    text = path.read_text(encoding="utf-8", errors="replace")
    rules = [
        ("Mapping rate", r"mapping\s+rate[:\s]+(\d+\.?\d*)%?", lambda v: v >= 90, lambda v: v >= 75),
        ("Mean coverage", r"mean\s+coverage[:\s]+(\d+\.?\d*)", lambda v: v >= 30, lambda v: v >= 15),
        ("Duplication", r"duplicat(?:ion|e)\s+rate[:\s]+(\d+\.?\d*)%?", lambda v: v <= 20, lambda v: v <= 40),
        ("Q30", r"Q30[:\s]+(\d+\.?\d*)%?", lambda v: v >= 85, lambda v: v >= 70),
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
        metrics.append(QCMetric(name=name, value=f"{value:.1f}", status=status))
    return metrics


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
