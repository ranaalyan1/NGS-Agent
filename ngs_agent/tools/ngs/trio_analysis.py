"""Trio analysis + compound heterozygote detection.

Detects:
  - De novo variants (present in proband, absent in both parents) → PS2
  - Compound heterozygotes (two different pathogenic variants in the same
    gene in trans) → PM3
  - Homozygous recessive (one variant, homozygous in proband, heterozygous
    in both parents) → supports AR inheritance

Input: three VCFs (proband, mother, father) OR a single multi-sample VCF
with FORMAT/GT fields per sample.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse
from .vcf_parse import Variant


@dataclass
class TrioVariant:
    variant: Variant
    proband_gt: str       # "0/1", "1/1", "0/0", "./."
    mother_gt: str
    father_gt: str
    inheritance: str      # "de_novo" | "compound_het" | "autosomal_recessiveive" | "x_linked" | "mitochondrial" | "unknown"


def _parse_vcf_with_genotypes(path: Path) -> dict[tuple[str, int, str, str], tuple[Variant, str]]:
    """Parse VCF and return a dict keyed by (chrom, pos, ref, alt) → (Variant, GT)."""
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 10:
                continue
            chrom, pos, _id, ref, alt, _q, _filt, info = cols[:8]
            fmt = cols[8].split(":")
            sample = cols[9].split(":")
            try:
                gt_idx = fmt.index("GT")
                gt = sample[gt_idx]
            except (ValueError, IndexError):
                gt = "./."
            for alt_allele in alt.split(","):
                v = Variant(
                    chrom=chrom, pos=int(pos), ref=ref, alt=alt_allele,
                    gene=None, consequence=None, clinvar=None,
                )
                out[(chrom, int(pos), ref, alt_allele)] = (v, gt)
    return out


def analyze_trio(proband_vcf: Path, mother_vcf: Path, father_vcf: Path) -> list[TrioVariant]:
    """Analyze a trio for inheritance patterns."""
    p = _parse_vcf_with_genotypes(proband_vcf)
    m = _parse_vcf_with_genotypes(mother_vcf)
    f = _parse_vcf_with_genotypes(father_vcf)

    # Use proband variants as the index
    out: list[TrioVariant] = []
    for key, (variant, p_gt) in p.items():
        m_gt = m.get(key, (None, "./."))[1]
        f_gt = f.get(key, (None, "./."))[1]
        inheritance = _classify_inheritance(p_gt, m_gt, f_gt, variant.chrom)
        out.append(TrioVariant(
            variant=variant, proband_gt=p_gt, mother_gt=m_gt, father_gt=f_gt,
            inheritance=inheritance,
        ))
    return out


def _classify_inheritance(p_gt: str, m_gt: str, f_gt: str, chrom: str) -> str:
    """Classify inheritance pattern from genotypes."""
    # De novo: proband het, both parents homozygous ref
    if p_gt == "0/1" and m_gt == "0/0" and f_gt == "0/0":
        return "de_novo"
    # AR homozygous: proband hom, both parents het
    if p_gt == "1/1" and m_gt == "0/1" and f_gt == "0/1":
        return "autosomal_recessiveive"
    # X-linked (proband male, mother het, father ref)
    if chrom in ("X", "chrX") and p_gt in ("1/1", "1") and m_gt == "0/1" and f_gt in ("0/0", "0"):
        return "x_linked"
    return "unknown"


def find_compound_hets(variants: list[TrioVariant]) -> list[list[TrioVariant]]:
    """Find compound heterozygote pairs: two different variants in the same
    gene, both heterozygous in proband, in trans (one from each parent)."""
    by_gene: dict[str, list[TrioVariant]] = {}
    for v in variants:
        if v.proband_gt != "0/1":
            continue
        if not v.variant.gene:
            continue
        by_gene.setdefault(v.variant.gene, []).append(v)

    pairs = []
    for gene, vars_in_gene in by_gene.items():
        if len(vars_in_gene) < 2:
            continue
        for i, v1 in enumerate(vars_in_gene):
            for v2 in vars_in_gene[i + 1:]:
                # In trans: v1 from mother, v2 from father (or vice versa)
                v1_from_mother = v1.mother_gt == "0/1" and v1.father_gt == "0/0"
                v1_from_father = v1.father_gt == "0/1" and v1.mother_gt == "0/0"
                v2_from_mother = v2.mother_gt == "0/1" and v2.father_gt == "0/0"
                v2_from_father = v2.father_gt == "0/1" and v2.mother_gt == "0/0"
                if (v1_from_mother and v2_from_father) or (v1_from_father and v2_from_mother):
                    pairs.append([v1, v2])
    return pairs


def _format(trio_variants: list[TrioVariant], compound_hets: list[list[TrioVariant]]) -> str:
    de_novo = [v for v in trio_variants if v.inheritance == "de_novo"]
    ar = [v for v in trio_variants if v.inheritance == "autosomal_recessiveive"]
    xl = [v for v in trio_variants if v.inheritance == "x_linked"]

    out = ["# Trio analysis\n"]
    out.append(f"Total variants in proband: {len(trio_variants)}\n")

    if de_novo:
        out.append(f"## De novo variants (PS2 evidence): {len(de_novo)}")
        for v in de_novo:
            out.append(f"  - {v.variant.chrom}:{v.variant.pos} {v.variant.ref}>{v.variant.alt} "
                       f"(proband={v.proband_gt}, mother={v.mother_gt}, father={v.father_gt})")
        out.append("")

    if ar:
        out.append(f"## Autosomal recessive (PM3 evidence): {len(ar)}")
        for v in ar:
            out.append(f"  - {v.variant.chrom}:{v.variant.pos} {v.variant.ref}>{v.variant.alt}")
        out.append("")

    if xl:
        out.append(f"## X-linked: {len(xl)}")
        for v in xl:
            out.append(f"  - {v.variant.chrom}:{v.variant.pos} {v.variant.ref}>{v.variant.alt}")
        out.append("")

    if compound_hets:
        out.append(f"## Compound heterozygotes (PM3 evidence): {len(compound_hets)} pair(s)")
        for pair in compound_hets:
            v1, v2 = pair
            out.append(f"  - Gene {v1.variant.gene}: "
                       f"{v1.variant.chrom}:{v1.variant.pos} "
                       f"(mother={v1.mother_gt}, father={v1.father_gt}) + "
                       f"{v2.variant.chrom}:{v2.variant.pos} "
                       f"(mother={v2.mother_gt}, father={v2.father_gt})")
        out.append("")

    if not (de_novo or ar or xl or compound_hets):
        out.append("No significant inheritance patterns detected.")

    return "\n".join(out)


class TrioAnalysisTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="trio_analysis",
            description=(
                "Analyze a proband-mother-father trio VCF set for inheritance "
                "patterns: de novo (PS2), autosomal recessive homozygous (PM3), "
                "X-linked, and compound heterozygotes in trans (PM3). Pass three "
                "VCF paths. Returns variants grouped by inheritance pattern. "
                "Use this when family data is available — de novo evidence is "
                "very strong (PS2) and compound het evidence supports autosomal "
                "recessive disease."
            ),
            parameters={
                "proband_vcf": {"type": "string"},
                "mother_vcf": {"type": "string"},
                "father_vcf": {"type": "string"},
            },
            required=["proband_vcf", "mother_vcf", "father_vcf"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        p_path = Path(params["proband_vcf"])
        m_path = Path(params["mother_vcf"])
        f_path = Path(params["father_vcf"])
        if not p_path.is_absolute():
            p_path = Path(ctx.cwd) / p_path
        if not m_path.is_absolute():
            m_path = Path(ctx.cwd) / m_path
        if not f_path.is_absolute():
            f_path = Path(ctx.cwd) / f_path

        for label, p in [("proband", p_path), ("mother", m_path), ("father", f_path)]:
            if not p.exists():
                return ToolResponse(
                    content=f"{label} VCF not found: {p}", is_error=True
                )

        trio = analyze_trio(p_path, m_path, f_path)
        ch = find_compound_hets(trio)

        return ToolResponse(
            content=_format(trio, ch),
            metadata={
                "total_variants": len(trio),
                "de_novo_count": sum(1 for v in trio if v.inheritance == "de_novo"),
                "ar_count": sum(1 for v in trio if v.inheritance == "autosomal_recessiveive"),
                "compound_het_count": len(ch),
                "x_linked_count": sum(1 for v in trio if v.inheritance == "x_linked"),
            },
        )
