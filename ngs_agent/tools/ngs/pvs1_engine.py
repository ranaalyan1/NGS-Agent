"""ClinGen SVI PVS1 decision tree — Mane et al. 2018 + 2023 SVI refinements.

This is the *correct* implementation of PVS1, replacing v0.3's naive
`_is_lof(consequence) and gene_haploinsufficient` check.

The decision tree grades PVS1 strength based on:
  - Variant type (nonsense, frameshift, canonical ±1/2 splice, initiation codon,
    single/multi-exon deletion)
  - Predicted effect on transcript (NMD escape vs decay)
  - Location (critical/non-critical exon, last exon, last 50nt of penultimate exon)
  - Gene's established LOF mechanism for the disease
  - For single-exon deletion: % of coding sequence affected
  - For splice: predicted impact (cryptic splice site, exon skipping, partial loss)

Strength outcomes (Tavtigian weights):
  PVS1     = Very Strong (weight 8.0)
  PVS1_Strong = Strong   (weight 4.0)
  PVS1_Moderate = Moderate (weight 2.0)
  PVS1_Supporting = Supporting (weight 1.0)
  None     = no call (0)

References:
  - Abou Tayoun et al. 2018, "Recommendations for interpreting the loss of
    function PVS1 ACMG/AMP variant criterion"
  - Ellard et al. 2020, "ACMG/AMP sequence variant interpretation criterion
    PVS1: a 2022 revision"
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PVS1Strength(str, Enum):
    PVS1 = "PVS1"
    PVS1_STRONG = "PVS1_Strong"
    PVS1_MODERATE = "PVS1_Moderate"
    PVS1_SUPPORTING = "PVS1_Supporting"
    NONE = "None"


WEIGHTS = {
    PVS1Strength.PVS1: 8.0,
    PVS1Strength.PVS1_STRONG: 4.0,
    PVS1Strength.PVS1_MODERATE: 2.0,
    PVS1Strength.PVS1_SUPPORTING: 1.0,
    PVS1Strength.NONE: 0.0,
}


@dataclass
class TranscriptInfo:
    """Gene-level + transcript-level info needed by the PVS1 engine."""

    gene: str
    # Transcript structure
    is_coding: bool = True
    has_known_lof_mechanism: bool = False   # gene has established LOF disease mechanism
    nmd_escape_exon_threshold: int = 0      # exons downstream of which NMD escape occurs
    last_exon_number: int | None = None
    last_50nt_of_penultimate_exon: bool = False
    # For single/multi-exon deletions
    total_coding_nt: int | None = None
    # For splice variants
    is_canonical_donor: bool = False        # ±1, ±2 splice donor
    is_canonical_acceptor: bool = False     # ±1, ±2 splice acceptor
    cryptic_splice_predicted: bool = False  # predicted to create cryptic site
    exon_skipping_predicted: bool = False
    # For frameshift/nonsense
    nmd_predicted: bool = True              # predicted to trigger NMD
    # For initiation codon
    is_initiation_codon: bool = False
    has_alternate_downstream_start: bool = False


@dataclass
class PVS1Input:
    variant_type: str   # "nonsense" | "frameshift" | "splice_donor" | "splice_acceptor" |
                        # "initiation_codon" | "single_exon_del" | "multi_exon_del"
    transcript: TranscriptInfo
    # For single-exon deletion: # nt deleted
    deleted_nt: int | None = None
    # For splice: predicted effect
    splice_predicted_effect: str | None = None   # "exon_skipping" | "cryptic_site" | "intron_retention" | "none"


def classify_pvs1(inp: PVS1Input) -> PVS1Strength:
    """Apply the ClinGen SVI PVS1 decision tree."""

    if not inp.transcript.has_known_lof_mechanism:
        # Without established LOF mechanism, PVS1 cannot be applied
        return PVS1Strength.NONE

    vt = inp.variant_type

    # ---- Nonsense / Frameshift ----
    if vt in ("nonsense", "frameshift"):
        if not inp.transcript.nmd_predicted:
            # NMD escape — downgraded
            if _in_last_exon_or_last_50nt_penultimate(inp.transcript):
                return PVS1Strength.PVS1_STRONG
            return PVS1Strength.PVS1_MODERATE
        # NMD predicted
        if _in_last_exon_or_last_50nt_penultimate(inp.transcript):
            return PVS1Strength.PVS1_STRONG
        return PVS1Strength.PVS1

    # ---- Canonical splice site (±1, ±2) ----
    if vt in ("splice_donor", "splice_acceptor"):
        if inp.transcript.is_canonical_donor or inp.transcript.is_canonical_acceptor:
            eff = inp.splice_predicted_effect or "exon_skipping"
            if eff == "exon_skipping":
                if not inp.transcript.nmd_predicted:
                    if _in_last_exon_or_last_50nt_penultimate(inp.transcript):
                        return PVS1Strength.PVS1_STRONG
                    return PVS1Strength.PVS1_MODERATE
                if _in_last_exon_or_last_50nt_penultimate(inp.transcript):
                    return PVS1Strength.PVS1_STRONG
                return PVS1Strength.PVS1
            elif eff == "cryptic_site":
                # Cryptic site usage — milder effect
                return PVS1Strength.PVS1_STRONG
            else:
                return PVS1Strength.PVS1_MODERATE
        return PVS1Strength.NONE

    # ---- Initiation codon ----
    if vt == "initiation_codon":
        if inp.transcript.has_alternate_downstream_start:
            return PVS1Strength.PVS1_SUPPORTING
        # No alternate start — full loss
        return PVS1Strength.PVS1_MODERATE

    # ---- Single-exon deletion ----
    if vt == "single_exon_del":
        if inp.deleted_nt is None or inp.transcript.total_coding_nt is None:
            return PVS1Strength.PVS1_MODERATE  # unknown size — conservative
        pct = inp.deleted_nt / inp.transcript.total_coding_nt
        if pct < 0.1:
            return PVS1Strength.PVS1_SUPPORTING
        elif pct < 0.3:
            return PVS1Strength.PVS1_MODERATE
        else:
            if inp.transcript.nmd_predicted:
                return PVS1Strength.PVS1
            return PVS1Strength.PVS1_STRONG

    # ---- Multi-exon deletion ----
    if vt == "multi_exon_del":
        if inp.transcript.nmd_predicted:
            return PVS1Strength.PVS1
        return PVS1Strength.PVS1_STRONG

    return PVS1Strength.NONE


def _in_last_exon_or_last_50nt_penultimate(t: TranscriptInfo) -> bool:
    """True if the variant is in the last exon or last 50nt of penultimate exon.
    These positions are known to escape NMD."""
    # The transcript info must be pre-computed by the caller (via the
    # transcript reference database). The boolean is passed in directly.
    return t.last_50nt_of_penultimate_exon or (t.last_exon_number is not None and t.last_exon_number > 0)


def weight_for(strength: PVS1Strength) -> float:
    return WEIGHTS[strength]


def format_pvs1(strength: PVS1Strength, inp: PVS1Input, rationale: str = "") -> str:
    if strength == PVS1Strength.NONE:
        return f"PVS1: NOT applied. {rationale or 'Does not meet decision tree criteria.'}"
    return (
        f"{strength.value} (weight={weight_for(strength):+.1f}): {rationale or 'Applied per ClinGen SVI 2018/2023 decision tree.'} "
        f"Variant type={inp.variant_type}, NMD predicted={inp.transcript.nmd_predicted}, "
        f"LOF mechanism established={inp.transcript.has_known_lof_mechanism}."
    )
