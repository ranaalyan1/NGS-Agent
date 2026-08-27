"""Three-persona VUS debate — LLM required."""

from __future__ import annotations

from dataclasses import dataclass

from ngs_agent.analyzer import Variant
from ngs_agent.backends.base import LLMBackend, NoBackend


@dataclass
class PersonaOpinion:
    persona: str
    stance: str
    reasoning: str


@dataclass
class DebateResult:
    variant: Variant
    opinions: list[PersonaOpinion]
    consensus: str
    recommendation: str


PERSONAS = {
    "population": {
        "name": "Population Geneticist",
        "system": (
            "You are a population geneticist. Evaluate variants using allele frequency, "
            "population stratification, and gnomAD context. Be concise (3-5 sentences)."
        ),
    },
    "clinical": {
        "name": "Clinical Geneticist",
        "system": (
            "You are a clinical geneticist. Evaluate variants using ClinVar, ACMG criteria, "
            "and phenotype fit. Be concise (3-5 sentences)."
        ),
    },
    "functional": {
        "name": "Functional Geneticist",
        "system": (
            "You are a functional geneticist. Evaluate variants using consequence, "
            "splice predictors, and protein impact. Be concise (3-5 sentences)."
        ),
    },
}


def _variant_prompt(variant: Variant) -> str:
    return (
        f"Variant: {variant.gene} {variant.chrom}:{variant.pos} {variant.ref}>{variant.alt}\n"
        f"Consequence: {variant.consequence}\n"
        f"ClinVar: {variant.clinvar}\n"
        f"Allele frequency: {variant.af if variant.af is not None else 'unknown'}\n"
        f"Depth/VAF: {variant.depth}/{variant.vaf}\n\n"
        "State your stance (Pathogenic / Likely pathogenic / VUS / Likely benign / Benign) "
        "and brief reasoning."
    )


def debate_variant(variant: Variant, backend: LLMBackend) -> DebateResult:
    if isinstance(backend, NoBackend):
        backend.complete("")  # raises with clear message

    opinions: list[PersonaOpinion] = []
    for key, persona in PERSONAS.items():
        try:
            text = backend.complete(_variant_prompt(variant), system=persona["system"])
        except Exception as exc:
            text = f"[LLM call failed: {exc}]"
        stance = _extract_stance(text)
        opinions.append(PersonaOpinion(persona=persona["name"], stance=stance, reasoning=text))

    consensus = _build_consensus(opinions)
    recommendation = _build_recommendation(consensus, variant)
    return DebateResult(
        variant=variant,
        opinions=opinions,
        consensus=consensus,
        recommendation=recommendation,
    )


def _extract_stance(text: str) -> str:
    """Extract clinical stance from LLM response with negation handling.
    
    Uses a priority-based approach to handle negations correctly:
    1. Check for explicit negation patterns first
    2. Then look for positive assertions
    3. Default to Uncertain if no clear stance found
    """
    import re
    
    lower = text.lower()
    
    # First, check for negation patterns - these should override positive matches
    negation_patterns = [
        (r"not\s+(?:likely\s+)?pathogenic", "not_pathogenic"),
        (r"not\s+(?:likely\s+)?benign", "not_benign"),
        (r"(?:unlikely|not)\s+pathogenic", "not_pathogenic"),
        (r"(?:unlikely|not)\s+benign", "not_benign"),
    ]
    
    for pattern, neg_type in negation_patterns:
        if re.search(pattern, lower):
            # If we find negation of pathogenic, lean toward benign/VUS
            # If we find negation of benign, lean toward pathogenic/VUS
            if neg_type == "not_pathogenic":
                # Check if it suggests benign instead
                if "benign" in lower and "not" not in lower.split("benign")[0].split()[-2:]:
                    return "Benign"
                return "Vus"
            else:  # not_benign
                # Check if it suggests pathogenic instead
                if "pathogenic" in lower and "not" not in lower.split("pathogenic")[0].split()[-2:]:
                    return "Pathogenic"
                return "Vus"
    
    # No negation found - use standard priority matching
    # Order matters: check more specific labels first
    for label in (
        "likely pathogenic",
        "pathogenic",
        "likely benign",
        "benign",
        "vus",
        "uncertain",
    ):
        if label in lower:
            return label.title()
    
    return "Uncertain"


def _build_consensus(opinions: list[PersonaOpinion]) -> str:
    stances = [o.stance.lower() for o in opinions]
    if all("pathogenic" in s for s in stances):
        return "All personas lean pathogenic."
    if all("benign" in s for s in stances):
        return "All personas lean benign."
    if all("vus" in s or "uncertain" in s for s in stances):
        return "All personas agree: remains VUS."
    return "Mixed opinions — no consensus."


def _build_recommendation(consensus: str, variant: Variant) -> str:
    if "pathogenic" in consensus.lower():
        return f"Prioritize {variant.gene} for clinical correlation and segregation testing."
    if "benign" in consensus.lower():
        return f"{variant.gene} variant likely benign; document and deprioritize."
    return (
        f"{variant.gene} remains VUS. Consider RNA studies, segregation, or reclassification "
        "when new evidence appears."
    )
