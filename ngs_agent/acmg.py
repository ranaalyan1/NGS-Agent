"""ACMG/AMP guidelines variant classification engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ACMG_CRITERIA: dict[str, dict[str, Any]] = {
    "PVS1": {
        "code": "PVS1",
        "strength": "Very Strong Pathogenic",
        "category": "pathogenic",
        "description": "Null variant in a gene where loss of function is a known disease mechanism.",
    },
    "PS1": {
        "code": "PS1",
        "strength": "Strong Pathogenic",
        "category": "pathogenic",
        "description": "Same amino acid change as a previously established pathogenic variant.",
    },
    "PS2": {
        "code": "PS2",
        "strength": "Strong Pathogenic",
        "category": "pathogenic",
        "description": "De novo variant (with confirmed maternity and paternity).",
    },
    "PS3": {
        "code": "PS3",
        "strength": "Strong Pathogenic",
        "category": "pathogenic",
        "description": "Functional studies show a validated damaging effect on protein function.",
    },
    "PS4": {
        "code": "PS4",
        "strength": "Strong Pathogenic",
        "category": "pathogenic",
        "description": "Prevalence in affected individuals is significantly increased over controls.",
    },
    "PM1": {
        "code": "PM1",
        "strength": "Moderate Pathogenic",
        "category": "pathogenic",
        "description": "Located in a known mutational hotspot or well-established functional domain.",
    },
    "PM2": {
        "code": "PM2",
        "strength": "Moderate Pathogenic",
        "category": "pathogenic",
        "description": "Absent or extremely rare in population databases (gnomAD).",
    },
    "PM4": {
        "code": "PM4",
        "strength": "Moderate Pathogenic",
        "category": "pathogenic",
        "description": "Protein length changes due to in-frame deletion/insertion or stop-loss.",
    },
    "PM5": {
        "code": "PM5",
        "strength": "Moderate Pathogenic",
        "category": "pathogenic",
        "description": "Novel missense at an amino acid residue where a different pathogenic missense is known.",
    },
    "PP1": {
        "code": "PP1",
        "strength": "Supporting Pathogenic",
        "category": "pathogenic",
        "description": "Cosegregation with disease in multiple affected family members.",
    },
    "PP2": {
        "code": "PP2",
        "strength": "Supporting Pathogenic",
        "category": "pathogenic",
        "description": "Missense variant in a gene where missense is a common disease mechanism.",
    },
    "PP3": {
        "code": "PP3",
        "strength": "Supporting Pathogenic",
        "category": "pathogenic",
        "description": "Multiple in silico computational tools predict a damaging effect.",
    },
    "PP5": {
        "code": "PP5",
        "strength": "Supporting Pathogenic",
        "category": "pathogenic",
        "description": "Reputable source reports pathogenic, but detailed evidence is not available.",
    },
    "BA1": {
        "code": "BA1",
        "strength": "Standalone Benign",
        "category": "benign",
        "description": "Allele frequency is > 5% in a major population database (gnomAD).",
    },
    "BS1": {
        "code": "BS1",
        "strength": "Strong Benign",
        "category": "benign",
        "description": "Allele frequency is greater than expected for disorder.",
    },
    "BS2": {
        "code": "BS2",
        "strength": "Strong Benign",
        "category": "benign",
        "description": "Observed in a healthy adult individual for a penetrant disorder.",
    },
    "BS3": {
        "code": "BS3",
        "strength": "Strong Benign",
        "category": "benign",
        "description": "Well-established functional studies show no damaging effect.",
    },
    "BP1": {
        "code": "BP1",
        "strength": "Supporting Benign",
        "category": "benign",
        "description": "Missense variant in a gene for which only truncating mutations cause disease.",
    },
    "BP4": {
        "code": "BP4",
        "strength": "Supporting Benign",
        "category": "benign",
        "description": "Multiple lines of computational evidence predict no damaging effect.",
    },
    "BP6": {
        "code": "BP6",
        "strength": "Supporting Benign",
        "category": "benign",
        "description": "Reputable source reports benign, but detailed evidence is not available.",
    },
    "BP7": {
        "code": "BP7",
        "strength": "Supporting Benign",
        "category": "benign",
        "description": "Synonymous variant with no predicted splice impact.",
    },
}


@dataclass
class ACMGEvaluation:
    codes: list[str] = field(default_factory=list)
    classification: str = "VUS"
    explanation: str = ""
    confidence: float = 0.0


def compute_acmg_classification(criteria_codes: list[str]) -> ACMGEvaluation:
    """Compute ACMG/AMP 5-tier classification from criteria codes."""
    codes = [c.upper().strip() for c in criteria_codes if c.upper().strip() in ACMG_CRITERIA]

    pvs1 = codes.count("PVS1")
    ps = sum(1 for c in codes if c.startswith("PS"))
    pm = sum(1 for c in codes if c.startswith("PM"))
    pp = sum(1 for c in codes if c.startswith("PP"))

    ba1 = codes.count("BA1")
    bs = sum(1 for c in codes if c.startswith("BS"))
    bp = sum(1 for c in codes if c.startswith("BP"))

    has_pathogenic = (pvs1 > 0 or ps > 0 or pm > 0 or pp > 0)
    has_benign = (ba1 > 0 or bs > 0 or bp > 0)

    # Standalone Benign
    if ba1 >= 1 and not has_pathogenic:
        return ACMGEvaluation(
            codes=codes,
            classification="Benign",
            explanation="Standalone Benign: Allele frequency > 5% in population databases (BA1).",
            confidence=0.99,
        )

    # Pathogenic combinations
    is_pathogenic = (
        (pvs1 >= 1 and (ps >= 1 or pm >= 2 or (pm >= 1 and pp >= 1) or pp >= 2))
        or (ps >= 2)
        or (ps >= 1 and (pm >= 3 or (pm >= 2 and pp >= 2) or (pm >= 1 and pp >= 4)))
    )
    if is_pathogenic and not has_benign:
        return ACMGEvaluation(
            codes=codes,
            classification="Pathogenic",
            explanation=f"Meets ACMG/AMP Pathogenic criteria rules ({len(codes)} criteria applied).",
            confidence=0.95,
        )

    # Likely Pathogenic combinations
    is_likely_pathogenic = (
        (pvs1 >= 1 and pm >= 1)
        or (ps >= 1 and (pm in (1, 2) or pp >= 2))
        or (pm >= 3)
        or (pm >= 2 and pp >= 2)
        or (pm >= 1 and pp >= 4)
    )
    if is_likely_pathogenic and not has_benign:
        return ACMGEvaluation(
            codes=codes,
            classification="Likely Pathogenic",
            explanation=f"Meets ACMG/AMP Likely Pathogenic criteria rules ({len(codes)} criteria applied).",
            confidence=0.90,
        )

    # Benign combinations
    if bs >= 2 and not has_pathogenic:
        return ACMGEvaluation(
            codes=codes,
            classification="Benign",
            explanation=f"Meets ACMG/AMP Benign criteria rules ({len(codes)} criteria applied).",
            confidence=0.95,
        )

    # Likely Benign combinations
    is_likely_benign = (
        (bs >= 1 and bp >= 1)
        or (bp >= 2)
    )
    if is_likely_benign and not has_pathogenic:
        return ACMGEvaluation(
            codes=codes,
            classification="Likely Benign",
            explanation=f"Meets ACMG/AMP Likely Benign criteria rules ({len(codes)} criteria applied).",
            confidence=0.90,
        )

    return ACMGEvaluation(
        codes=codes,
        classification="VUS",
        explanation="Criteria do not reach Pathogenic or Benign thresholds or exhibit conflicting evidence; remains Variant of Uncertain Significance.",
        confidence=0.75,
    )
