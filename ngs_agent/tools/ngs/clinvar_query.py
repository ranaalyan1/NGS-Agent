"""ClinVar query tool.

Uses the NCBI E-utilities API (esearch + esummary) to retrieve ClinVar
classifications and review status for a variant by RSID or by gene+HGVS.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


async def _esearch(term: str, client: httpx.AsyncClient) -> list[str]:
    r = await client.get(
        f"{EUTILS}/esearch.fcgi",
        params={
            "db": "clinvar",
            "term": term,
            "retmode": "json",
            "retmax": 5,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", [])


async def _esummary(ids: list[str], client: httpx.AsyncClient) -> list[dict]:
    if not ids:
        return []
    r = await client.get(
        f"{EUTILS}/esummary.fcgi",
        params={"db": "clinvar", "id": ",".join(ids), "retmode": "json"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    return list(data.get("result", {}).values())


async def query_clinvar(
    *, rsid: str | None = None, gene: str | None = None, hgvs: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Query ClinVar by rsID or by gene+HGVS. Returns matching records."""

    if rsid:
        term = rsid if rsid.startswith("rs") else f"rs{rsid}"
    elif gene and hgvs:
        term = f"{gene}[gene] AND {hgvs}[variant]"
    else:
        raise ValueError("Provide rsid, or gene+hgvs")

    async def _do(c: httpx.AsyncClient) -> list[dict]:
        ids = await _esearch(term, c)
        return await _esummary(ids, c)

    if client:
        return await _do(client)
    async with httpx.AsyncClient() as c:
        return await _do(c)


def _format(records: list[dict]) -> str:
    if not records:
        return "No ClinVar records found."
    out = ["# ClinVar results\n"]
    for rec in records:
        if not isinstance(rec, dict) or "uid" not in rec:
            continue
        out.append(f"## {rec.get('title', 'unknown')}")
        out.append(f"  UID: {rec.get('uid')}")
        out.append(f"  Clinical significance: {rec.get('clinical_significance', 'unknown')}")
        out.append(f"  Review status: {rec.get('clinical_significance_description', 'unknown')}")
        out.append(f"  Variation set: {rec.get('variation_set', [])}")
        out.append(f"  Phenotypes: {rec.get('phenotype_names', 'none listed')}")
        out.append(f"  Gene: {rec.get('genes', [])}")
        out.append("")
    return "\n".join(out)


class ClinvarQueryTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="clinvar_query",
            description=(
                "Query ClinVar via NCBI E-utilities for clinical classifications of a "
                "variant. Pass an rsID (e.g. rs121908917) OR a gene symbol + HGVS "
                "string. Returns current ClinVar classification, review status, "
                "and associated phenotypes. Always query this BEFORE classifying — "
                "existing ClinVar concordance is strong supporting evidence (PS1/BS1)."
            ),
            parameters={
                "rsid": {"type": "string", "description": "dbSNP rsID (with or without 'rs' prefix)"},
                "gene": {"type": "string", "description": "Gene symbol (e.g. BRCA1)"},
                "hgvs": {"type": "string", "description": "HGVS notation (e.g. c.5266dupC)"},
            },
            required=[],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        try:
            records = await query_clinvar(
                rsid=params.get("rsid"),
                gene=params.get("gene"),
                hgvs=params.get("hgvs"),
            )
        except httpx.HTTPError as e:
            return ToolResponse(
                content=f"ClinVar query failed: {e}", is_error=True,
            )
        except ValueError as e:
            return ToolResponse(content=str(e), is_error=True)

        return ToolResponse(
            content=_format(records),
            metadata={"clinvar": records},
        )
