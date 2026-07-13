"""FHIR Genomics export — converts an emit_verdict payload to a FHIR R4
Observation resource conforming to the Genomics Reporting Implementation Guide.

Reference: https://build.fhir.org/ig/HL7/genomics-reporting/

The output is a JSON FHIR Observation with:
  - status: final
  - category: laboratory
  - code: LOINC 57979-7 (Variant interpretation)
  - subject: placeholder
  - valueCodeableConcept: the classification (Pathogenic / VUS / etc.)
  - component: gene, HGVS, ACMG criteria, evidence
  - derivedFrom: evidence citations as DocumentReference references
"""
from __future__ import annotations

import json
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse

LOINC_GENE = "48018-6"        # Gene studied [Identifier] in Blood or Tissue
LOINC_HGVS = "81290-9"        # Simple variant name
LOINC_ACMG = "93044-6"        # Level of significance
LOINC_INTERP = "57979-7"      # Variant interpretation

CLASSIFICATION_TO_SNOMED = {
    "Pathogenic": "10828004",
    "Likely Pathogenic": "10828004",   # same SNOMED, distinguished by display
    "VUS": "41868009",
    "Likely Benign": "48708007",
    "Benign": "48708007",
}


def verdict_to_fhir(verdict: dict) -> dict:
    """Convert an emit_verdict metadata['verdict'] dict to a FHIR R4 Observation."""

    classification = verdict.get("classification", "VUS")
    variant = verdict.get("variant", {})
    gene = verdict.get("gene", "")
    criteria = verdict.get("acmg_criteria", [])
    citations = verdict.get("evidence_citations", [])

    fhir_obs = {
        "resourceType": "Observation",
        "id": verdict.get("verdict_id", ""),
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "laboratory",
                "display": "Laboratory",
            }],
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": LOINC_INTERP,
                "display": "Variant interpretation",
            }],
        },
        "subject": {
            "reference": "Patient/placeholder",
        },
        "effectiveDateTime": "1970-01-01T00:00:00Z",  # caller should fill
        "issued": "1970-01-01T00:00:00.000Z",
        "valueCodeableConcept": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": CLASSIFICATION_TO_SNOMED.get(classification, "41868009"),
                "display": classification,
            }],
            "text": classification,
        },
        "component": [],
    }

    # Gene
    if gene:
        fhir_obs["component"].append({
            "code": {
                "coding": [{
                    "system": "http://loinc.org",
                    "code": LOINC_GENE,
                    "display": "Gene studied [Identifier]",
                }],
            },
            "valueCodeableConcept": {
                "text": gene,
            },
        })

    # Variant (HGVS short)
    if variant:
        hgvs_short = f"{variant.get('chrom')}:{variant.get('pos')}{variant.get('ref')}>{variant.get('alt')}"
        fhir_obs["component"].append({
            "code": {
                "coding": [{
                    "system": "http://loinc.org",
                    "code": LOINC_HGVS,
                    "display": "Simple variant name",
                }],
            },
            "valueCodeableConcept": {
                "text": hgvs_short,
            },
        })

    # ACMG criteria
    if criteria:
        fhir_obs["component"].append({
            "code": {
                "coding": [{
                    "system": "http://loinc.org",
                    "code": LOINC_ACMG,
                    "display": "Level of significance",
                }],
            },
            "valueCodeableConcept": {
                "text": ", ".join(criteria),
            },
        })

    # Evidence citations as derivedFrom (DocumentReferences)
    if citations:
        fhir_obs["derivedFrom"] = [
            {
                "reference": f"DocumentReference/{c}",
                "display": c,
            }
            for c in citations
        ]

    return fhir_obs


class FhirExportTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="fhir_export",
            description=(
                "Export a verdict to a FHIR R4 Observation resource per the "
                "HL7 Genomics Reporting Implementation Guide. Use this AFTER "
                "emit_verdict to produce a structured record for LIMS / EHR "
                "integration. Output includes LOINC codes for variant "
                "interpretation (57979-7), gene (48018-6), HGVS (81290-9), "
                "and ACMG criteria (93044-6). Evidence citations become "
                "DocumentReference derivedFrom references."
            ),
            parameters={
                "verdict": {
                    "type": "object",
                    "description": "Verdict object as emitted by emit_verdict",
                },
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

        try:
            fhir_obs = verdict_to_fhir(verdict)
        except Exception as e:
            return ToolResponse(
                content=f"FHIR export failed: {e}", is_error=True,
            )

        content = (
            "# FHIR R4 Observation (Genomics Reporting IG)\n\n"
            "```json\n" +
            json.dumps(fhir_obs, indent=2) +
            "\n```\n"
        )
        return ToolResponse(
            content=content,
            metadata={"fhir_observation": fhir_obs},
        )
