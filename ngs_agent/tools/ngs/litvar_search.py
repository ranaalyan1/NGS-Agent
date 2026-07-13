"""LitVar API tool — variant-specific PubMed search.

LitVar (NCBI) indexes publications by variant, so a search for a specific
rsID returns papers that *mention that variant*, not just papers that
mention the gene. Much more precise than free-text PubMed search.

Endpoint:
  https://www.ncbi.nlm.nih.gov/research/litvar2-api/variant/search/?query=rs121908917
"""
from __future__ import annotations

from typing import Any

import httpx

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse

LITVAR_URL = "https://www.ncbi.nlm.nih.gov/research/litvar2-api"


async def search_litvar(rsid: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Search LitVar by rsID. Returns list of related publications."""

    rsid_norm = rsid if rsid.startswith("rs") else f"rs{rsid}"

    async def _do(c: httpx.AsyncClient) -> list[dict]:
        r = await c.get(
            f"{LITVAR_URL}/variant/search",
            params={"query": rsid_norm, "size": 10},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    if client:
        return await _do(client)
    async with httpx.AsyncClient() as c:
        return await _do(c)


def _format(publications: list, rsid: str) -> str:
    if not publications:
        return f"No variant-specific publications found in LitVar for {rsid}."
    out = [f"# LitVar — variant-specific publications for {rsid}\n"]
    out.append(f"Found {len(publications)} publication(s):\n")
    for pub in publications[:10]:
        if isinstance(pub, dict):
            pmid = pub.get("pmid", "?")
            title = pub.get("title", "?")[:200]
            year = pub.get("year", "?")
            out.append(f"## PMID:{pmid} ({year})")
            out.append(f"  {title}")
            out.append(f"  https://pubmed.ncbi.nlm.nih.gov/{pmid}/\n")
    return "\n".join(out)


class LitVarTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="litvar_search",
            description=(
                "Search LitVar (NCBI) for variant-specific publications by rsID. "
                "LitVar indexes publications by the variants they mention — "
                "much more precise than free-text PubMed search. Use this "
                "AFTER clinvar_query and BEFORE pubmed_search for the variant-"
                "specific literature pass. Returns up to 10 PMIDs."
            ),
            parameters={
                "rsid": {"type": "string", "description": "dbSNP rsID"},
            },
            required=["rsid"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        try:
            pubs = await search_litvar(params["rsid"])
        except httpx.HTTPError as e:
            return ToolResponse(
                content=f"LitVar search failed: {e}", is_error=True,
            )
        return ToolResponse(
            content=_format(pubs, params["rsid"]),
            metadata={"litvar_publications": pubs},
        )
