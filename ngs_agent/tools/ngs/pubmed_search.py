"""PubMed search tool — uses NCBI E-utilities.

The LLM can query PubMed for `gene AND variant` to retrieve recent literature.
This is the differentiation layer commercial tools miss — most don't do live
literature retrieval.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


async def _esearch(term: str, retmax: int, client: httpx.AsyncClient,
                   date_from: str | None = None, date_to: str | None = None) -> list[str]:
    """Search PubMed. date_from/date_to are YYYY/MM/DD format."""
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": retmax,
        "sort": "relevance",
    }
    if date_from or date_to:
        daterange = f"({date_from or '1900/01/01'}[PDAT] : {date_to or '3000/01/01'}[PDAT])"
        params["term"] = f"{term} AND {daterange}"
    r = await client.get(f"{EUTILS}/esearch.fcgi", params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


async def _esummary(ids: list[str], client: httpx.AsyncClient) -> list[dict]:
    if not ids:
        return []
    r = await client.get(
        f"{EUTILS}/esummary.fcgi",
        params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
        timeout=20,
    )
    r.raise_for_status()
    return list(r.json().get("result", {}).values())


async def search_pubmed(
    term: str, retmax: int = 5, client: httpx.AsyncClient | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> list[dict]:
    async def _do(c: httpx.AsyncClient) -> list[dict]:
        ids = await _esearch(term, retmax, c, date_from, date_to)
        return await _esummary(ids, c)

    if client:
        return await _do(client)
    async with httpx.AsyncClient() as c:
        return await _do(c)


def _format(records: list[dict], term: str) -> str:
    if not records:
        return f"No PubMed results for: {term}"
    out = [f"# PubMed search: '{term}' — {len(records)} result(s)\n"]
    for r in records:
        if not isinstance(r, dict) or "uid" not in r:
            continue
        authors = r.get("authors", [])[:3]
        author_str = ", ".join(a.get("name", "") for a in authors)
        if len(r.get("authors", [])) > 3:
            author_str += " et al."
        out.append(f"## PMID:{r.get('uid')} — {r.get('title', 'no title')[:200]}")
        out.append(f"  Authors: {author_str}")
        out.append(f"  Source: {r.get('source', '?')} {r.get('pubdate', '?')}")
        out.append(f"  URL: https://pubmed.ncbi.nlm.nih.gov/{r.get('uid')}/")
        out.append("")
    return "\n".join(out)


class PubMedSearchTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="pubmed_search",
            description=(
                "Search PubMed for peer-reviewed literature on a gene, variant, or "
                "phenotype. Returns up to 5 most-relevant abstracts with PMIDs. "
                "Use this AFTER gnomad_query and clinvar_query to gather functional "
                "or clinical evidence from recent publications. For variant-specific "
                "searches, prefer litvar_search first. v0.4: supports date_from / "
                "date_to filters (YYYY/MM/DD) to focus on recent literature."
            ),
            parameters={
                "term": {"type": "string", "description": "PubMed search query"},
                "retmax": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                "date_from": {"type": "string", "description": "YYYY/MM/DD — filter by publication date"},
                "date_to": {"type": "string", "description": "YYYY/MM/DD — filter by publication date"},
            },
            required=["term"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        term = params["term"]
        retmax = int(params.get("retmax", 5))
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        try:
            records = await search_pubmed(term, retmax, date_from=date_from, date_to=date_to)
        except httpx.HTTPError as e:
            return ToolResponse(
                content=f"PubMed search failed: {e}", is_error=True,
            )

        return ToolResponse(
            content=_format(records, term),
            metadata={"pubmed": records},
        )
