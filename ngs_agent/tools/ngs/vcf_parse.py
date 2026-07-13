"""VCF parser tool — parses VCF v4.1+ and extracts structured variant info.

Reads INFO fields (GENE, CSQ, CLNSIG, AF, DP) and FORMAT/sample fields.
Classifies variants as Pathogenic / VUS / Other based on ClinVar CLNSIG.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


@dataclass
class Variant:
    chrom: str
    pos: int
    ref: str
    alt: str
    gene: str | None = None
    consequence: str | None = None
    clinvar: str | None = None
    af: float | None = None
    depth: int | None = None
    vaf: float | None = None
    classification: str = "Other"   # Pathogenic / VUS / Other

    @property
    def is_vus(self) -> bool:
        return self.classification == "VUS"

    @property
    def hgvs_short(self) -> str:
        return f"{self.chrom}:{self.pos}{self.ref}>{self.alt}"


_INFO_RE = re.compile(r"([^=;]+)=([^;]*)")
_CSQ_RE = re.compile(r"Consequence=([^|]+)\|Symbol=([^|]+)", re.IGNORECASE)


def parse_info(info_str: str) -> dict[str, str]:
    return dict(_INFO_RE.findall(info_str))


def parse_csq(csq_field: str) -> tuple[str | None, str | None]:
    """Extract consequence + gene from a CSQ allele string.
    Format: Consequence|Symbol|... (VEP-style, but loosely handled)."""
    if not csq_field:
        return None, None
    parts = csq_field.split("|")
    if len(parts) >= 2:
        return parts[0] or None, parts[1] or None
    return parts[0] if parts else None, None


def classify(clinvar: str | None) -> str:
    if not clinvar:
        return "Other"
    c = clinvar.lower()
    if "pathogenic" in c and "conflicting" not in c and "likely benign" not in c:
        return "Pathogenic"
    if "uncertain" in c or "vus" in c or "unknown significance" in c:
        return "VUS"
    return "Other"


def parse_vcf_file(path: Path) -> list[Variant]:
    """Parse a VCF file and return a list of Variant objects."""
    variants: list[Variant] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 8:
                continue
            chrom, pos, _id, ref, alt, _qual, _filter, info = cols[:8]
            # Handle multi-allelic by splitting ALT
            for alt_allele in alt.split(","):
                v = _make_variant(chrom, pos, ref, alt_allele, info, cols)
                variants.append(v)
    return variants


def _make_variant(chrom: str, pos: str, ref: str, alt: str, info: str, cols: list[str]) -> Variant:
    info_dict = parse_info(info)

    # Gene / consequence
    gene = info_dict.get("GENE") or info_dict.get("SYMBOL")
    consequence = info_dict.get("CONSEQUENCE") or info_dict.get("ANN")
    if consequence and not gene:
        cons, g = parse_csq(consequence)
        consequence = cons
        gene = gene or g

    # ClinVar
    clinvar = info_dict.get("CLNSIG") or info_dict.get("CLNREVSTAT")

    # AF
    af = None
    if "AF" in info_dict:
        try:
            af = float(info_dict["AF"])
        except ValueError:
            pass

    # Depth + VAF from FORMAT/sample
    depth = None
    vaf = None
    if "DP" in info_dict:
        try:
            depth = int(info_dict["DP"])
        except ValueError:
            pass
    if len(cols) >= 10:
        fmt = cols[8].split(":")
        sample = cols[9].split(":")
        try:
            dp_idx = fmt.index("DP")
            if dp_idx < len(sample):
                depth = int(sample[dp_idx])
        except (ValueError, IndexError):
            pass
        try:
            ad_idx = fmt.index("AD")
            if ad_idx < len(sample):
                ad_parts = sample[ad_idx].split(",")
                if len(ad_parts) == 2:
                    ref_d, alt_d = int(ad_parts[0]), int(ad_parts[1])
                    depth = depth or (ref_d + alt_d)
                    if (ref_d + alt_d) > 0:
                        vaf = alt_d / (ref_d + alt_d)
        except (ValueError, IndexError):
            pass

    return Variant(
        chrom=chrom,
        pos=int(pos),
        ref=ref,
        alt=alt,
        gene=gene,
        consequence=consequence,
        clinvar=clinvar,
        af=af,
        depth=depth,
        vaf=vaf,
        classification=classify(clinvar),
    )


class VcfParseTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="vcf_parse",
            description=(
                "Parse a VCF file and return a structured list of variants with "
                "gene, consequence, ClinVar classification, allele frequency, depth, "
                "and VAF. Use this FIRST when given a VCF to interpret."
            ),
            parameters={
                "path": {"type": "string", "description": "Path to the VCF file"},
                "filter": {
                    "type": "string",
                    "enum": ["all", "vus", "pathogenic"],
                    "default": "all",
                    "description": "Filter variants by classification",
                },
            },
            required=["path"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        path = Path(params["path"])
        if not path.is_absolute():
            path = Path(ctx.cwd) / path
        if not path.exists():
            return ToolResponse(
                content=f"VCF file not found: {path}", is_error=True
            )

        try:
            variants = parse_vcf_file(path)
        except Exception as e:
            return ToolResponse(
                content=f"Failed to parse VCF: {e}", is_error=True
            )

        filt = params.get("filter", "all")
        if filt == "vus":
            variants = [v for v in variants if v.is_vus]
        elif filt == "pathogenic":
            variants = [v for v in variants if v.classification == "Pathogenic"]

        # Record file read in tracker
        if ctx.file_tracker:
            ctx.file_tracker.record_read(str(path))

        # Format as compact table for the LLM
        lines = [f"# {len(variants)} variant(s) from {path.name} (filter={filt})\n"]
        lines.append("| # | CHROM:POS | REF>ALT | GENE | CSQ | ClinVar | AF | DP | VAF | Class |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, v in enumerate(variants, 1):
            lines.append(
                f"| {i} | {v.chrom}:{v.pos} | {v.ref}>{v.alt} | {v.gene or '-'} | "
                f"{(v.consequence or '-')[:30]} | {v.clinvar or '-'} | "
                f"{v.af if v.af is not None else '-'} | {v.depth or '-'} | "
                f"{f'{v.vaf:.2%}' if v.vaf is not None else '-'} | {v.classification} |"
            )

        return ToolResponse(
            content="\n".join(lines),
            metadata={
                "variant_count": len(variants),
                "variants": [asdict(v) for v in variants[:50]],  # cap metadata
                "source": str(path),
            },
        )
