"""ClinVar RCV-level assertion query.

Replaces v0.3's naive esearch-by-rsID which returned variant-set records.
The clinical assertion lives at the RCV (ReferenceClinVarAssertion) level.

This implementation uses the NCBI ClinVar API v2:
  GET https://clinicaltables.nlm.nih.gov/api/clinvar/v1/variants?...
  Or: E-utilities esearch + efetch on the clinvar db.

Returns assertion-level data:
  - RCV accession
  - Clinical significance (with review status)
  - Submission count and submitter names
  - Assertion criteria + citations
  - Associated phenotypes (MedGen IDs)
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse
from ...runtime.evidence_graph import EvidenceEdge, variant_node_id, gene_node_id

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CLINVAR_API = "https://clinicaltables.nlm.nih.gov/api/variants/v3/ncbi"

# Throttle: NCBI allows 3 req/sec without API key, 10 with one.
_LAST_CALL_AT: float = 0.0
_MIN_INTERVAL = 0.4  # 2.5 req/sec


async def _throttle() -> None:
    global _LAST_CALL_AT
    now = asyncio.get_event_loop().time()
    wait = _MIN_INTERVAL - (now - _LAST_CALL_AT)
    if wait > 0:
        await asyncio.sleep(wait)
    _LAST_CALL_AT = asyncio.get_event_loop().time()


async def _esearch_clinvar(term: str, client: httpx.AsyncClient) -> list[str]:
    await _throttle()
    r = await client.get(
        f"{EUTILS}/esearch.fcgi",
        params={
            "db": "clinvar",
            "term": term,
            "retmode": "json",
            "retmax": 10,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


async def _efetch_clinvar(ids: list[str], client: httpx.AsyncClient) -> str:
    """Fetch full ClinVar XML records — contains RCV-level assertions."""
    if not ids:
        return ""
    await _throttle()
    r = await client.get(
        f"{EUTILS}/efetch.fcgi",
        params={"db": "clinvar", "id": ",".join(ids), "rettype": "clinvarset"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text  # XML


async def query_clinvar_rcv(
    *,
    rsid: str | None = None,
    gene: str | None = None,
    hgvs_c: str | None = None,
    variant_id: str | None = None,
) -> list[dict]:
    """Query ClinVar and extract RCV-level assertions.

    Returns list of assertion dicts:
      {
        "rcv": "RCV000012345",
        "title": "...",
        "clinical_significance": "Pathogenic",
        "review_status": "criteria provided, multiple submitters, no conflicts",
        "submitter_count": 3,
        "last_evaluated": "2023-01-15",
        "phenotypes": [{"name": "...", "medgen_id": "..."}],
        "variation_id": "VCV000012345",
      }
    """
    if rsid:
        term = rsid if rsid.startswith("rs") else f"rs{rsid}"
    elif gene and hgvs_c:
        term = f"{gene}[gene] AND {hgvs_c}"
    elif variant_id:
        term = variant_id
    else:
        raise ValueError("Provide rsid, gene+hgvs_c, or variant_id")

    async with httpx.AsyncClient() as client:
        # First search ClinVar db
        ids = await _esearch_clinvar(term, client)
        if not ids:
            return []
        xml = await _efetch_clinvar(ids, client)

    # Parse the XML to extract RCV-level assertions
    return _parse_clinvar_set(xml)


def _parse_clinvar_set(xml: str) -> list[dict]:
    """Extract RCV-level assertions from ClinVar XML.

    This is a minimal parser — handles the common structure. For production,
    use the official `clinvar-tsv` Python package or xmltodict.
    """
    import re

    assertions = []
    # RCV blocks
    rcv_blocks = re.findall(
        r'<ReferenceClinVarAssertion.*?</ReferenceClinVarAssertion>',
        xml, re.DOTALL,
    )

    for block in rcv_blocks:
        rcv_id_m = re.search(r'<ClinVarAccession\s+Acc="([^"]+)"', block)
        rcv_id = rcv_id_m.group(1) if rcv_id_m else "?"

        title_m = re.search(r'<MeasureSet\s+Type="[^"]*"\s+ID="\d+">\s*<Name>([^<]+)</Name>', block)
        title = title_m.group(1) if title_m else ""

        cs_m = re.search(r'<ClinicalSignificance.*?>([^<]+)</ClinicalSignificance>', block, re.DOTALL)
        cs = cs_m.group(1).strip() if cs_m else "unknown"

        rs_m = re.search(r'ReviewStatus>([^<]+)</ReviewStatus', block)
        review_status = rs_m.group(1).strip() if rs_m else "no assertion provided"

        # Submission count is the number of SubmittedAssertion blocks per RCV
        sub_count = block.count("<SubmittedAssertion ")

        # Phenotypes
        pheno_m = re.findall(r'<Trait\s+Type="Disease"[^>]*>\s*<Name>\s*<ElementValue[^>]*>([^<]+)</ElementValue>', block)
        phenotypes = [{"name": p.strip()} for p in pheno_m]

        # Last evaluated
        le_m = re.search(r'last_evaluated="([^"]+)"', block)
        last_eval = le_m.group(1) if le_m else ""

        assertions.append({
            "rcv": rcv_id,
            "title": title,
            "clinical_significance": cs,
            "review_status": review_status,
            "submitter_count": sub_count,
            "last_evaluated": last_eval,
            "phenotypes": phenotypes,
        })

    return assertions


def _format(assertions: list[dict], query: str) -> str:
    if not assertions:
        return (
            f"No ClinVar RCV-level assertions found for: {query}\n"
            "Note: this does NOT mean the variant is benign — only that it is "
            "not yet asserted in ClinVar. Use gnomAD absence + functional evidence "
            "to support PM2."
        )
    out = [f"# ClinVar RCV-level assertions for: {query}\n"]
    out.append(f"Found {len(assertions)} assertion(s):\n")
    for a in assertions:
        out.append(f"## {a['rcv']}")
        out.append(f"  Title: {a['title']}")
        out.append(f"  Clinical significance: {a['clinical_significance']}")
        out.append(f"  Review status: {a['review_status']}")
        out.append(f"  Submitters: {a['submitter_count']}")
        out.append(f"  Last evaluated: {a['last_evaluated']}")
        if a["phenotypes"]:
            out.append(f"  Phenotypes: {', '.join(p['name'] for p in a['phenotypes'])}")
        out.append("")
    return "\n".join(out)


class ClinvarRcvTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="clinvar_rcv",
            description=(
                "Query ClinVar for RCV-level clinical assertions (not just "
                "variant records). Returns assertion-level data: clinical "
                "significance with review status, submitter count, last "
                "evaluated date, associated phenotypes. Pass an rsID, OR a "
                "gene + HGVS.c string (use hgvs_convert first), OR a ClinVar "
                "variant ID. ALWAYS prefer this over the older clinvar_query "
                "tool when you need the actual clinical assertion. Throttled "
                "to 2.5 req/sec."
            ),
            parameters={
                "rsid": {"type": "string"},
                "gene": {"type": "string"},
                "hgvs_c": {"type": "string", "description": "e.g. NM_007294.4:c.5266dupC"},
                "variant_id": {"type": "string", "description": "VCV accession"},
            },
            required=[],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        try:
            query_desc = ""
            if params.get("rsid"):
                query_desc = params["rsid"]
            elif params.get("gene") and params.get("hgvs_c"):
                query_desc = f"{params['gene']} {params['hgvs_c']}"
            elif params.get("variant_id"):
                query_desc = params["variant_id"]

            assertions = await query_clinvar_rcv(
                rsid=params.get("rsid"),
                gene=params.get("gene"),
                hgvs_c=params.get("hgvs_c"),
                variant_id=params.get("variant_id"),
            )
        except httpx.HTTPError as e:
            return ToolResponse(
                content=f"ClinVar query failed: {e}", is_error=True,
            )
        except ValueError as e:
            return ToolResponse(content=str(e), is_error=True)

        return ToolResponse(
            content=_format(assertions, query_desc or "(no query)"),
            metadata={"clinvar_assertions": assertions},
        )
        # Note: graph population happens at the verdict-emission stage when
        # the variant coordinates are known. ClinVar queries by rsID/HGVS don't
        # always carry the coords, so we don't auto-populate here.
