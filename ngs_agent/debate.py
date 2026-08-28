"""Three-persona VUS debate with ACMG/AMP criteria integration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ngs_agent.acmg import compute_acmg_classification, ACMGEvaluation
from ngs_agent.analyzer import Variant
from ngs_agent.backends.base import LLMBackend, NoBackend
from ngs_agent.common import extract_json


@dataclass
class PersonaOpinion:
    persona: str
    stance: str
    reasoning: str
    acmg_criteria: list[str] = field(default_factory=list)


@dataclass
class DebateResult:
    variant: Variant
    opinions: list[PersonaOpinion]
    consensus: str
    recommendation: str
    acmg_evaluation: ACMGEvaluation = field(default_factory=ACMGEvaluation)


PERSONAS = {
    "population": {
        "name": "Population Geneticist",
        "system": (
            "You are a population geneticist evaluating variant pathogenicity using allele frequency, "
            "gnomAD population databases, subpopulation stratification, and ACMG criteria (e.g. BA1, BS1, BS2, PM2). "
            "State your stance explicitly (Pathogenic / Likely Pathogenic / VUS / Likely Benign / Benign), "
            "list relevant ACMG codes (e.g. [PM2]), and provide concise reasoning (3-5 sentences)."
        ),
    },
    "clinical": {
        "name": "Clinical Geneticist",
        "system": (
            "You are a clinical geneticist evaluating variant pathogenicity using ClinVar classification, "
            "patient phenotype concordance, de novo status, segregation, and ACMG clinical criteria (e.g. PS2, PM1, PP1, PP4, BP5). "
            "State your stance explicitly (Pathogenic / Likely Pathogenic / VUS / Likely Benign / Benign), "
            "list relevant ACMG codes, and provide concise reasoning (3-5 sentences)."
        ),
    },
    "functional": {
        "name": "Functional Geneticist",
        "system": (
            "You are a functional geneticist evaluating variant impact using protein domain disruption, "
            "splice site predictors, evolutionary conservation, in vitro assays, and ACMG functional criteria (e.g. PVS1, PS3, PM4, PP3, BS3, BP4, BP7). "
            "State your stance explicitly (Pathogenic / Likely Pathogenic / VUS / Likely Benign / Benign), "
            "list relevant ACMG codes, and provide concise reasoning (3-5 sentences)."
        ),
    },
}


def _variant_prompt(variant: Variant) -> str:
    return (
        f"Variant: {variant.gene} {variant.chrom}:{variant.pos} {variant.ref}>{variant.alt}\n"
        f"Consequence: {variant.consequence}\n"
        f"ClinVar Annotation: {variant.clinvar}\n"
        f"Allele Frequency (AF): {variant.af if variant.af is not None else 'unknown'}\n"
        f"Read Depth / VAF: {variant.depth or 'unknown'} / {f'{variant.vaf:.1%}' if variant.vaf is not None else 'unknown'}\n\n"
        "Please provide your assessment in the following format:\n"
        "STANCE: [Pathogenic / Likely Pathogenic / VUS / Likely Benign / Benign]\n"
        "ACMG_CODES: [comma-separated codes e.g. PM2, PP3]\n"
        "REASONING: [3-5 sentences of evidence and rationale]"
    )


def debate_variant(variant: Variant, backend: LLMBackend) -> DebateResult:
    if isinstance(backend, NoBackend):
        backend.complete("")

    opinions: list[PersonaOpinion] = []
    all_codes: list[str] = []

    for key, persona in PERSONAS.items():
        try:
            text = backend.complete(_variant_prompt(variant), system=persona["system"])
        except Exception as exc:
            text = f"[LLM call failed: {exc}]"

        stance = _extract_stance(text)
        codes = _extract_acmg_codes(text)
        all_codes.extend(codes)

        opinions.append(
            PersonaOpinion(
                persona=persona["name"],
                stance=stance,
                reasoning=text,
                acmg_criteria=codes,
            )
        )

    acmg_eval = compute_acmg_classification(all_codes)
    consensus = _build_consensus(opinions, acmg_eval)
    recommendation = _build_recommendation(consensus, variant, acmg_eval)

    return DebateResult(
        variant=variant,
        opinions=opinions,
        consensus=consensus,
        recommendation=recommendation,
        acmg_evaluation=acmg_eval,
    )


def _extract_stance(text: str) -> str:
    if not text or not text.strip():
        return "Uncertain"

    # 1. Check for explicit labeled stance (e.g. "STANCE: Pathogenic", "Verdict: Likely Benign")
    explicit_match = re.search(
        r"(?:^|\n|\b)(?:stance|verdict|classification|conclusion)\s*[:=-]\s*(likely\s+pathogenic|pathogenic|likely\s+benign|benign|vus|uncertain(?:\s+significance)?)",
        text,
        re.I,
    )
    if explicit_match:
        val = explicit_match.group(1).lower()
        if "likely pathogenic" in val:
            return "Likely Pathogenic"
        if "pathogenic" in val:
            return "Pathogenic"
        if "likely benign" in val:
            return "Likely Benign"
        if "benign" in val:
            return "Benign"
        if "vus" in val or "uncertain" in val:
            return "Vus"

    # 2. Mask negated phrases so they don't trigger positive classifications
    cleaned = re.sub(
        r"\b(?:not|never|unlikely|non[-\s]?|hardly|cannot\s+be(?:\s+considered)?)\s+(?:likely\s+)?pathogenic\b",
        "__NEGATED_PATHOGENIC__",
        text,
        flags=re.I,
    )
    cleaned = re.sub(
        r"\b(?:not|never|unlikely|non[-\s]?|hardly|cannot\s+be(?:\s+considered)?)\s+(?:likely\s+)?benign\b",
        "__NEGATED_BENIGN__",
        cleaned,
        flags=re.I,
    )

    lower = cleaned.lower()

    has_likely_pathogenic = bool(re.search(r"\blikely\s+pathogenic\b", lower))
    has_pathogenic = bool(re.search(r"\bpathogenic\b", lower))
    has_likely_benign = bool(re.search(r"\blikely\s+benign\b", lower))
    has_benign = bool(re.search(r"\bbenign\b", lower))
    has_vus = bool(re.search(r"\b(vus|uncertain(?:\s+significance)?|unknown\s+significance)\b", lower))

    if has_likely_pathogenic and not has_likely_benign and not has_benign:
        return "Likely Pathogenic"
    if has_pathogenic and not has_benign:
        return "Pathogenic"
    if has_likely_benign and not has_pathogenic:
        return "Likely Benign"
    if has_benign and not has_pathogenic:
        return "Benign"
    if has_vus:
        return "Vus"

    if "__NEGATED_PATHOGENIC__" in cleaned and has_benign:
        return "Likely Benign" if has_likely_benign else "Benign"
    if "__NEGATED_BENIGN__" in cleaned and has_pathogenic:
        return "Likely Pathogenic" if has_likely_pathogenic else "Pathogenic"

    return "Uncertain"


def _extract_acmg_codes(text: str) -> list[str]:
    codes = re.findall(r"\b(PVS1|PS[1-4]|PM[1-6]|PP[1-5]|BA1|BS[1-4]|BP[1-7])\b", text, re.I)
    return list(dict.fromkeys(c.upper() for c in codes))


def _build_consensus(opinions: list[PersonaOpinion], acmg_eval: Optional[ACMGEvaluation] = None) -> str:
    stances = [o.stance.lower() for o in opinions]
    if all("pathogenic" in s for s in stances):
        return "All personas lean pathogenic."
    if all("benign" in s for s in stances):
        return "All personas lean benign."
    if all("vus" in s or "uncertain" in s for s in stances):
        return "All personas agree: remains VUS."

    if acmg_eval and acmg_eval.classification != "VUS":
        return f"Debate resolved via ACMG criteria: {acmg_eval.classification}."

    return "Mixed opinions — no consensus."


def _build_recommendation(consensus: str, variant: Variant, acmg_eval: Optional[ACMGEvaluation] = None) -> str:
    if "pathogenic" in consensus.lower():
        return f"Prioritize {variant.gene} for clinical correlation and segregation testing."
    if "benign" in consensus.lower():
        return f"{variant.gene} variant likely benign; document and deprioritize."
    return (
        f"{variant.gene} remains VUS. Consider RNA studies, segregation, or reclassification "
        "when new evidence appears."
    )
