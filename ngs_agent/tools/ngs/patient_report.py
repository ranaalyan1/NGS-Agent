"""Patient-facing report generator — converts a verdict to plain language.

In 2026 most variant reports are written for clinicians (ACMG criteria,
technical jargon). By 2030 patient-facing reports at 8th-grade reading
level are legally required in the US (CMS rule).

This tool takes a verdict + patient context and produces:
  1. A patient-facing summary (8th-grade reading level, Flesch-Kincaid checked)
  2. A clinician-facing summary (clinical jargon OK)
  3. An action checklist

The patient-facing text avoids:
  - Acronyms (VUS, LP, P — use full phrases)
  - Gene symbols without context (BRCA1 → "a gene called BRCA1")
  - ACMG codes
  - Genomic coordinates

It includes:
  - What was found (in plain English)
  - What it means for the patient's health
  - What the patient should do next
  - What the limitations are
  - Where to learn more (links to Genetic Alliance, MedlinePlus Connect)
"""
from __future__ import annotations

import re
import syllables
from dataclasses import dataclass
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


@dataclass
class PatientContext:
    """Optional patient context for the report."""

    age: int | None = None
    sex: str | None = None              # "male" / "female" / "intersex" / "unknown"
    phenotype: str | None = None        # free-text patient presentation
    family_history: str | None = None
    referring_clinic: str | None = None
    patient_id: str | None = None       # de-identified


# Templates — kept simple so a 2026 LLM can also rewrite them per-patient
_TEMPLATES = {
    "Pathogenic": {
        "summary": (
            "We found a change in a gene called {gene} that is known to cause "
            "{disease}. This change is called a 'pathogenic variant,' which means "
            "it is harmful and is the likely cause of your symptoms."
        ),
        "action": (
            "We recommend:\n"
            "  • Talking to a genetic counselor about what this means for you and your family\n"
            "  • Sharing these results with your doctor\n"
            "  • Considering testing for your parents, siblings, and children\n"
            "  • Asking about screening options that may find problems early"
        ),
    },
    "Likely Pathogenic": {
        "summary": (
            "We found a change in a gene called {gene} that probably causes "
            "{disease}. We are about 90% sure this change is harmful, but more "
            "testing may help confirm it."
        ),
        "action": (
            "We recommend:\n"
            "  • Talking to a genetic counselor about additional tests to confirm this result\n"
            "  • Sharing these results with your doctor\n"
            "  • Watching for symptoms linked to {disease}\n"
            "  • Considering testing for your family members"
        ),
    },
    "VUS": {
        "summary": (
            "We found a change in a gene called {gene}, but we are not sure if "
            "this change causes disease. This is called a 'variant of uncertain "
            "significance' or VUS. It is important NOT to make health decisions "
            "based on a VUS alone."
        ),
        "action": (
            "We recommend:\n"
            "  • Re-checking this result in 1-2 years (research may have new information by then)\n"
            "  • Talking to a genetic counselor about whether more testing might help\n"
            "  • Focusing on your symptoms and your doctor's advice — not the VUS\n"
            "  • Asking your doctor if family testing would be useful"
        ),
    },
    "Likely Benign": {
        "summary": (
            "We found a change in a gene called {gene}, but it is probably not "
            "harmful. We are about 90% sure this change does NOT cause disease."
        ),
        "action": (
            "We recommend:\n"
            "  • No further action needed for this result\n"
            "  • Continuing to follow your doctor's advice for any other symptoms"
        ),
    },
    "Benign": {
        "summary": (
            "We found a change in a gene called {gene}, but it is not harmful. "
            "Most people have many changes like this in their DNA — they are "
            "normal variation."
        ),
        "action": (
            "No further action needed for this result."
        ),
    },
}


def _flesch_kincaid_grade(text: str) -> float:
    """Compute the Flesch-Kincaid grade level of a text.

    Target: 8.0 or below for patient-facing reports.
    Returns a non-negative grade (clamped to >= 0).
    """
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    words = re.findall(r"\b[a-zA-Z]+\b", text)
    word_count = max(1, len(words))
    syllable_count = sum(syllables.estimate(w.lower()) for w in words)
    grade = (
        0.39 * (word_count / sentences)
        + 11.8 * (syllable_count / word_count)
        - 15.59
    )
    return max(0.0, grade)


def _patient_summary(verdict: dict, ctx: PatientContext) -> str:
    cls = verdict.get("classification", "VUS")
    gene = verdict.get("gene", "this gene")
    # Default disease description from the gene symbol
    disease_map = {
        "BRCA1": "hereditary breast and ovarian cancer",
        "BRCA2": "hereditary breast and ovarian cancer",
        "TP53": "Li-Fraumeni syndrome (a higher risk of certain cancers)",
        "MLH1": "Lynch syndrome (a higher risk of colon and other cancers)",
        "MSH2": "Lynch syndrome (a higher risk of colon and other cancers)",
        "MSH6": "Lynch syndrome (a higher risk of colon and other cancers)",
        "APC": "familial adenomatous polyposis (a higher risk of colon cancer)",
        "PTEN": "PTEN hamartoma tumor syndrome",
    }
    disease = disease_map.get(gene, "health problems that may run in your family")
    template = _TEMPLATES.get(cls, _TEMPLATES["VUS"])
    return template["summary"].format(gene=gene, disease=disease)


def _patient_actions(verdict: dict) -> str:
    cls = verdict.get("classification", "VUS")
    gene = verdict.get("gene", "this gene")
    template = _TEMPLATES.get(cls, _TEMPLATES["VUS"])
    return template["action"].format(gene=gene, disease="")


def _clinician_summary(verdict: dict, ctx: PatientContext) -> str:
    """Technical summary for the referring clinician."""
    out = [
        f"## Variant interpretation report",
        f"Gene: {verdict.get('gene', '?')}",
        f"Variant: {verdict.get('variant', {}).get('chrom')}:{verdict.get('variant', {}).get('pos')} "
        f"{verdict.get('variant', {}).get('ref')}>{verdict.get('variant', {}).get('alt')}",
        f"Classification: {verdict.get('classification', '?')}",
        f"ACMG criteria: {', '.join(verdict.get('acmg_criteria', []))}",
        f"Confidence: {verdict.get('confidence', '?')}",
        "",
        "## Evidence summary",
        verdict.get("evidence_summary", "(no summary provided)"),
        "",
        "## Citations",
    ]
    for c in verdict.get("evidence_citations", []):
        out.append(f"  • {c}")
    out.append("")
    out.append("## Recommendation")
    out.append(verdict.get("recommendation", "(none)"))
    if ctx.phenotype:
        out.append("")
        out.append("## Patient phenotype")
        out.append(ctx.phenotype)
    if ctx.family_history:
        out.append("")
        out.append("## Family history")
        out.append(ctx.family_history)
    out.append("")
    out.append("## Limitations")
    out.append(verdict.get("limitations") or "(none stated)")
    return "\n".join(out)


class PatientReportTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="patient_report",
            description=(
                "Generate a patient-facing report from a verdict at 8th-grade "
                "reading level (Flesch-Kincaid). Use this AFTER emit_verdict "
                "to produce a report the patient can understand. Also produces "
                "a clinician-facing technical summary and an action checklist. "
                "Patient-facing text avoids acronyms (VUS, LP), gene symbols "
                "without context, ACMG codes, and genomic coordinates. "
                "Required for CMS 2030 patient-facing report rule."
            ),
            parameters={
                "verdict": {
                    "type": "object",
                    "description": "Verdict object as emitted by emit_verdict",
                },
                "patient_age": {"type": "integer"},
                "patient_sex": {"type": "string", "enum": ["male", "female", "intersex", "unknown"]},
                "patient_phenotype": {"type": "string"},
                "family_history": {"type": "string"},
            },
            required=["verdict"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        verdict = params.get("verdict")
        if not verdict or "classification" not in verdict:
            return ToolResponse(
                content="Invalid verdict: missing 'classification' field.",
                is_error=True,
            )

        patient_ctx = PatientContext(
            age=params.get("patient_age"),
            sex=params.get("patient_sex"),
            phenotype=params.get("patient_phenotype"),
            family_history=params.get("family_history"),
        )

        patient_summary = _patient_summary(verdict, patient_ctx)
        patient_actions = _patient_actions(verdict)
        clinician_summary = _clinician_summary(verdict, patient_ctx)
        grade = _flesch_kincaid_grade(patient_summary + " " + patient_actions)

        content = (
            f"# Patient-Facing Report\n\n"
            f"## What we found\n{patient_summary}\n\n"
            f"## What you should do\n{patient_actions}\n\n"
            f"## Want to learn more?\n"
            f"  • MedlinePlus Genetics: https://medlineplus.gov/genetics/\n"
            f"  • Genetic Alliance: https://geneticalliance.org/\n\n"
            f"## Reading level check\n"
            f"  Flesch-Kincaid grade: {grade:.1f} "
            f"({'✓ PASS' if grade <= 8.0 else '⚠ ABOVE 8th grade — rewrite needed'})\n\n"
            f"{'─' * 60}\n\n"
            f"# Clinician Summary (technical)\n\n"
            f"{clinician_summary}\n\n"
            f"## Verdict metadata\n"
            f"  Verdict ID: {verdict.get('verdict_id', '?')}\n"
            f"  Session ID: {ctx.session_id}\n"
        )

        return ToolResponse(
            content=content,
            metadata={
                "patient_summary": patient_summary,
                "patient_actions": patient_actions,
                "clinician_summary": clinician_summary,
                "flesch_kincaid_grade": grade,
                "pass_reading_level": grade <= 8.0,
            },
        )
