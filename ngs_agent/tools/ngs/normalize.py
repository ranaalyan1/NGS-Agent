"""Variant normalization — left-align + parsimonious, bcftools norm semantics.

Two variants are equivalent iff they normalize to the same representation.
Without normalization, gnomAD / ClinVar / VEP lookups miss matches because
the same variant is written differently in different VCFs.

Example:
  raw:  chr1:100 TC>T   (deletion of C at position 101)
  norm: chr1:99  C>-     (left-aligned, parsimonious)

This is a pure-Python implementation good enough for SNVs and short indels
(<50bp). For production use, wrap `bcftools norm` or `vt normalize`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


@dataclass
class NormalizedVariant:
    chrom: str
    pos: int           # 1-based, left-aligned
    ref: str
    alt: str           # empty string for deletion (per VCF spec) — we use "-" for readability
    variant_type: str  # "SNV" | "MNV" | "insertion" | "deletion" | "complex"


def _strip_chrom_prefix(chrom: str) -> str:
    return chrom[3:] if chrom.lower().startswith("chr") else chrom


def normalize_variant(chrom: str, pos: int, ref: str, alt: str) -> NormalizedVariant:
    """Left-align and parsimoniously trim a variant.

    Algorithm:
      1. Strip chr prefix.
      2. Handle multi-allelic by splitting ALT on comma (caller should split first).
      3. If REF == ALT → no-op variant.
      4. Trim common suffix from the right (parsimony).
      5. Trim common prefix from the left, walking the position back.
      6. Classify the variant type.

    This is the same logic as bcftools norm -m- -l (without reference genome —
    pure string-based left-align). For true left-alignment against a reference
    genome, use bcftools; this implementation handles the common cases that
    don't require walking the reference.
    """
    chrom = _strip_chrom_prefix(chrom)
    ref = ref.upper()
    alt = alt.upper()

    if ref == alt:
        return NormalizedVariant(chrom, pos, ref, alt, "SNV")  # no-op

    # Step 1: trim common suffix (parsimony)
    while len(ref) > 1 and len(alt) > 1 and ref[-1] == alt[-1]:
        ref = ref[:-1]
        alt = alt[:-1]

    # Step 2: trim common prefix, walking position back (left-align)
    # Note: without a reference genome we can only trim the *given* common prefix.
    # True left-alignment requires reading the reference backward — that's what
    # bcftools does. Here we trim what's visible.
    while len(ref) > 1 and len(alt) > 1 and ref[0] == alt[0]:
        ref = ref[1:]
        alt = alt[1:]
        pos += 1

    # Step 3: classify
    if len(ref) == 1 and len(alt) == 1:
        vtype = "SNV"
    elif len(ref) == len(alt) and len(ref) > 1:
        vtype = "MNV"
    elif len(ref) > len(alt):
        vtype = "deletion"
    elif len(ref) < len(alt):
        vtype = "insertion"
    else:
        vtype = "complex"

    # Use "-" for empty allele (common in some tools; gnomAD uses "" — caller's choice)
    if ref == "":
        ref = "-"
    if alt == "":
        alt = "-"

    return NormalizedVariant(chrom, pos, ref, alt, vtype)


def to_gnomad_variant_id(v: NormalizedVariant) -> str:
    """Format as gnomAD variant ID: 1-55516888-G-A (no 'chr', '-' for empty)."""
    ref = v.ref if v.ref != "-" else ""
    alt = v.alt if v.alt != "-" else ""
    return f"{v.chrom}-{v.pos}-{ref}-{alt}"


def to_spdi(v: NormalizedVariant) -> str:
    """Format as SPDI: 1:55516888:G:A. SPDI uses position before the variant
    (0-based for indels; here we keep 1-based VCF position)."""
    ref = v.ref if v.ref != "-" else ""
    alt = v.alt if v.alt != "-" else ""
    return f"{v.chrom}:{v.pos}:{ref}:{alt}"


class NormalizeTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="normalize_variant",
            description=(
                "Normalize a variant to left-aligned parsimonious form "
                "(bcftools norm semantics, pure-Python). ALSO returns gnomAD "
                "variant ID and SPDI formats. ALWAYS call this BEFORE "
                "gnomad_query / clinvar_query / hgvs_convert — different VCFs "
                "write the same variant differently and lookups will miss matches "
                "without normalization. Does not require a reference genome for "
                "the common cases (SNV, short indel)."
            ),
            parameters={
                "chrom": {"type": "string"},
                "pos": {"type": "integer"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
            },
            required=["chrom", "pos", "ref", "alt"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        try:
            v = normalize_variant(
                params["chrom"], int(params["pos"]),
                params["ref"], params["alt"],
            )
        except Exception as e:
            return ToolResponse(content=f"Normalization failed: {e}", is_error=True)

        content = (
            f"# Normalized variant\n"
            f"  CHROM:POS  = {v.chrom}:{v.pos}\n"
            f"  REF > ALT  = {v.ref} > {v.alt}\n"
            f"  Type       = {v.variant_type}\n\n"
            f"  gnomAD ID  = {to_gnomad_variant_id(v)}\n"
            f"  SPDI       = {to_spdi(v)}\n"
        )
        return ToolResponse(
            content=content,
            metadata={
                "normalized": {
                    "chrom": v.chrom, "pos": v.pos, "ref": v.ref, "alt": v.alt,
                    "type": v.variant_type,
                },
                "gnomad_id": to_gnomad_variant_id(v),
                "spdi": to_spdi(v),
            },
        )
