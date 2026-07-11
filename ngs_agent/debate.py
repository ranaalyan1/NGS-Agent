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
    lower = text.lower()
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
