"""gnomAD allele frequency query tool.

Calls the gnomAD v4 GraphQL API. Returns total AF, popmax AF, FAF, and
per-population AFs. This is the kind of evidence retrieval the v0.2
'debate' command couldn't do — the LLM now calls this directly.

v0.4: throttled to 1 req/sec (gnomAD public API limit) + retry on 429.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse
from ...runtime.evidence_graph import EvidenceEdge, variant_node_id


GNOMAD_GRAPHQL_URL = "https://gnomad.broadinstitute.org/api"

# gnomAD public API: ~1 req/sec anonymous, ~10 req/sec with API key.
_MIN_INTERVAL = 1.0  # seconds between calls
_LAST_CALL_AT: float = 0.0

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0

_QUERY = """
query($variantId: String!, $dataset: Dataset!) {
  variant(dataset: $dataset, variantId: $variantId) {
    variantId
    chrom
    pos
    ref
    alt
    genome {
      ac
      an
      af
      populations { id ac an af }
    }
    exome {
      ac
      an
      af
      populations { id ac an af }
    }
    faf95 { population faf95 }
    flags
    lof
  }
}
"""


def _variant_id(chrom: str, pos: int, ref: str, alt: str) -> str:
    # gnomAD variantId format: 1-55516888-G-A
    chrom_clean = chrom.replace("chr", "")
    return f"{chrom_clean}-{pos}-{ref}-{alt}"


async def _throttle() -> None:
    global _LAST_CALL_AT
    now = asyncio.get_event_loop().time()
    wait = _MIN_INTERVAL - (now - _LAST_CALL_AT)
    if wait > 0:
        await asyncio.sleep(wait)
    _LAST_CALL_AT = asyncio.get_event_loop().time()


async def query_gnomad(
    chrom: str, pos: int, ref: str, alt: str,
    dataset: str = "gnomad_r4",
    client: httpx.AsyncClient | None = None,
) -> dict | None:
    """Query gnomAD for a single variant. Returns None if not found.

    Throttled to 1 req/sec and retries on 429 with exponential backoff.
    """
    variant_id = _variant_id(chrom, pos, ref, alt)
    payload = {
        "query": _QUERY,
        "variables": {"variantId": variant_id, "dataset": dataset},
    }

    async def _do(c: httpx.AsyncClient) -> dict | None:
        for attempt in range(MAX_RETRIES + 1):
            await _throttle()
            try:
                r = await c.post(GNOMAD_GRAPHQL_URL, json=payload, timeout=30)
                if r.status_code == 429 and attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF ** (attempt + 1))
                    continue
                r.raise_for_status()
                data = r.json()
                if "errors" in data:
                    return None
                return data.get("data", {}).get("variant")
            except httpx.HTTPError:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF ** (attempt + 1))
                    continue
                raise
        return None

    if client:
        return await _do(client)
    async with httpx.AsyncClient() as c:
        return await _do(c)


def _format(variant: dict | None, chrom: str, pos: int, ref: str, alt: str) -> str:
    if not variant:
        return (
            f"Variant {chrom}:{pos}{ref}>{alt} NOT FOUND in gnomAD. "
            "Absence is supporting evidence for pathogenicity (PM2) but is NOT "
            "sufficient alone — combine with functional and segregation evidence."
        )

    out = [f"# gnomAD — {chrom}:{pos}{ref}>{alt}"]
    out.append(f"variantId: {variant.get('variantId')}")
    out.append(f"flags: {variant.get('flags', [])}")
    out.append("")

    for source in ("genome", "exome"):
        s = variant.get(source)
        if not s:
            continue
        ac, an, af = s.get("ac", 0), s.get("an", 0), s.get("af")
        af_str = f"{af:.6e}" if af is not None else "n/a"
        out.append(f"## {source}")
        out.append(f"  AC={ac}  AN={an}  AF={af_str}")
        pops = s.get("populations") or []
        if pops:
            popmax = max(pops, key=lambda p: p.get("af") or 0)
            out.append(
                f"  popmax: {popmax['id']} AF={popmax.get('af', 0):.6e} "
                f"(AC={popmax.get('ac')}, AN={popmax.get('an')})"
            )
        out.append("")

    faf = variant.get("faf95") or []
    if faf:
        out.append("## FAF95 (filtering allele frequency)")
        for f in faf:
            out.append(f"  {f['population']}: {f['faf95']:.6e}")

    return "\n".join(out)


class GnomadQueryTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="gnomad_query",
            description=(
                "Query gnomAD v4 for allele frequency of a variant. Returns total AF, "
                "popmax AF, filtering allele frequency (FAF95), and per-population AFs. "
                "Use BEFORE classifying a variant as pathogenic — absence from gnomAD "
                "is supporting evidence (PM2) but not sufficient alone."
            ),
            parameters={
                "chrom": {"type": "string", "description": "Chromosome (e.g. '17' or 'chr17')"},
                "pos": {"type": "integer", "description": "1-based position"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
                "dataset": {
                    "type": "string",
                    "enum": ["gnomad_r4", "gnomad_v3"],
                    "default": "gnomad_r4",
                },
            },
            required=["chrom", "pos", "ref", "alt"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        chrom = params["chrom"]
        pos = int(params["pos"])
        ref = params["ref"]
        alt = params["alt"]
        try:
            v = await query_gnomad(
                chrom, pos, ref, alt,
                params.get("dataset", "gnomad_r4"),
            )
        except httpx.HTTPError as e:
            return ToolResponse(
                content=f"gnomAD query failed: {e}", is_error=True,
                metadata={"error": str(e)},
            )

        # v0.5: populate evidence graph
        if ctx.evidence_graph is not None:
            vid = variant_node_id(chrom, pos, ref, alt)
            popmax_af = None
            total_af = None
            if v:
                for source in ("genome", "exome"):
                    s = v.get(source)
                    if s and s.get("af") is not None:
                        total_af = s["af"]
                        pops = s.get("populations") or []
                        if pops:
                            popmax_af = max(p.get("af") or 0 for p in pops)
                        break
            ctx.evidence_graph.add_edge(
                vid,
                f"population_frequency:{vid}",
                EvidenceEdge(
                    source="gnomad",
                    weight=1.0 if v is not None else 0.5,
                    citation=f"gnomAD:{vid.split(':', 1)[1]}",
                    properties={
                        "af": total_af,
                        "popmax_af": popmax_af,
                        "found": v is not None,
                    },
                ),
            )

        return ToolResponse(
            content=_format(v, chrom, pos, ref, alt),
            metadata={"gnomad": v},
        )
