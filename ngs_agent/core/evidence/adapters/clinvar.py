"""ClinVar evidence adapter (NCBI E-utilities).

Retrieves ClinVar's germline classification, molecular consequence, and
reported allele frequencies for a normalized variant, and emits them as typed
:class:`~ngs_agent.core.evidence.models.EvidenceRecord` objects.

What this adapter will and will not do
--------------------------------------

It reports **what ClinVar says**. It does not decide what that means. Mapping a
ClinVar classification onto ACMG criteria is the deterministic engine's job
(:mod:`ngs_agent.core.acmg.derivation`), and this adapter never emits a
criterion code.

Identity is checked, not assumed. ClinVar is queried by chromosomal position,
which can return records for *other* alleles at the same locus. Every returned
record is compared against the queried variant by canonical SPDI and by
coordinate span before it is marked ``verified``. A record that cannot be tied
back to the query is emitted with ``verification=identity_unverified`` and
``applicability=indeterminate``, which the engine refuses to use.

Data provenance and licensing
-----------------------------

ClinVar is a public archive of the National Library of Medicine; its data are
distributed without restriction on use, with the request that NCBI be cited.
Records are versioned (``VCV000017675.110``), and the version is stored, so a
classification can be tied to the exact ClinVar record that produced it.

API reference: https://www.ncbi.nlm.nih.gov/clinvar/docs/programmatic_access/
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from ngs_agent.core.errors import EvidenceValidationError
from ngs_agent.core.evidence.base import AdapterDeclaration, BaseEvidenceAdapter
from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    RetrievalDetail,
    VerificationStatus,
    utc_now,
)
from ngs_agent.core.evidence.transport import (
    EUTILS_BASE_URL,
    HttpTransport,
    HttpxTransport,
    TransportError,
)
from ngs_agent.core.genome import GenomeBuild
from ngs_agent.core.normalization import NormalizedVariant, VariantType
from ngs_agent.core.quantities import ratio_from_decimal_string

ADAPTER_VERSION = "clinvar-adapter-1.0.0"
EUTILS_BASE = EUTILS_BASE_URL

#: ClinVar review status -> assertion ("star") level.
#: Source: https://www.ncbi.nlm.nih.gov/clinvar/docs/details/#review
REVIEW_STATUS_STARS: dict[str, int] = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, conflicting classifications": 1,
    "criteria provided, conflicting interpretations": 1,
    "criteria provided, single submitter": 1,
    "no assertion criteria provided": 0,
    "no assertion provided": 0,
    "no classification provided": 0,
    "no classifications from unflagged records": 0,
    "flagged submission": 0,
}

#: Review levels NGS-Agent treats as an *authoritative external classification*
#: (ClinGen VCEP or an approved practice guideline) rather than as PP5/BP6
#: fodder. ClinGen SVI recommends against PP5/BP6 entirely; see ADR-0003.
AUTHORITATIVE_REVIEW_STATUSES = frozenset({"practice guideline", "reviewed by expert panel"})

#: ClinVar germline classification description -> stable internal label.
CLASSIFICATION_LABELS: dict[str, str] = {
    "pathogenic": "pathogenic",
    "likely pathogenic": "likely_pathogenic",
    "pathogenic/likely pathogenic": "pathogenic_likely_pathogenic",
    "pathogenic, low penetrance": "pathogenic_low_penetrance",
    "likely pathogenic, low penetrance": "likely_pathogenic_low_penetrance",
    "uncertain significance": "uncertain_significance",
    "likely benign": "likely_benign",
    "benign": "benign",
    "benign/likely benign": "benign_likely_benign",
    "conflicting classifications of pathogenicity": "conflicting",
    "conflicting interpretations of this variant": "conflicting",
    "drug response": "drug_response",
    "association": "association",
    "risk factor": "risk_factor",
    "protective": "protective",
    "affects": "affects",
    "other": "other",
    "not provided": "not_provided",
    "no classification provided": "not_classified",
    "no classification for the single variant": "not_classified",
}


def review_status_stars(review_status: str | None) -> int | None:
    """Map a ClinVar review status string to its assertion level.

    Returns ``None`` for an unrecognized string rather than defaulting to 0:
    an unknown review status is a signal that ClinVar has changed its
    vocabulary, and silently treating that as "no assertion criteria" would
    understate the evidence.
    """
    if not review_status:
        return None
    return REVIEW_STATUS_STARS.get(review_status.strip().lower())


def normalize_classification(description: str | None) -> str:
    """Map a ClinVar classification description to a stable label."""
    if not description or not description.strip():
        return "not_classified"
    return CLASSIFICATION_LABELS.get(description.strip().lower(), "unmapped")


class ClinVarAdapter(BaseEvidenceAdapter):
    """Retrieve ClinVar evidence for a normalized variant."""

    def __init__(
        self,
        *,
        source_version: str,
        transport: HttpTransport | None = None,
        clock: Any = None,
        api_key: str | None = None,
        tool: str = "ngs-agent",
        email: str | None = None,
        min_request_interval: timedelta = timedelta(milliseconds=340),
        last_request_at: list[datetime] | None = None,
        requires_network: bool = True,
    ) -> None:
        super().__init__(clock=clock)
        if not source_version or source_version.strip().lower() in {"unknown", ""}:
            # Evidence validation rejects a record whose source version is
            # missing, so refusing here turns a late, confusing rejection into
            # an early, legible one.
            raise ValueError(
                "ClinVarAdapter requires an explicit source_version. ClinVar is released weekly; "
                "a record that does not say which release it came from cannot be reproduced. "
                "Pass the release stamp, or for recorded fixtures the manifest's clinvar_release."
            )
        self._transport = transport or HttpxTransport()
        self._source_version = source_version
        self._api_key = api_key
        self._tool = tool
        self._email = email
        # NCBI asks for <= 3 requests/second without an API key and <= 10 with.
        self._min_interval = (
            min_request_interval if api_key is None else timedelta(milliseconds=110)
        )
        self._last_request_at = last_request_at if last_request_at is not None else []
        self._requires_network = requires_network

    @property
    def _transport_label(self) -> str:
        """What the audit trail should call this retrieval.

        ``http`` for a live call, ``recorded_fixture`` when the transport is
        replaying bundled recordings. Reporting a recording as an HTTP
        retrieval would misstate the provenance of every criterion derived
        from it.
        """
        return str(getattr(self._transport, "transport_label", "http"))

    @property
    def declaration(self) -> AdapterDeclaration:
        return AdapterDeclaration(
            name="clinvar",
            version=self._source_version,
            adapter_version=ADAPTER_VERSION,
            data_types=(
                EvidenceDataType.CLINICAL_SIGNIFICANCE,
                EvidenceDataType.MOLECULAR_CONSEQUENCE,
                EvidenceDataType.ALLELE_FREQUENCY,
            ),
            endpoint=EUTILS_BASE,
            license=(
                "ClinVar is a public archive of NLM/NCBI; data are distributed without "
                "restriction on use. NCBI requests citation of ClinVar."
            ),
            requires_network=self._requires_network,
            hosted_by="vendor" if self._requires_network else "recorded_fixture",
            notes=(
                "Germline classification, molecular consequence, and submitter-reported allele "
                "frequencies. Somatic (oncogenicity) and clinical-impact classifications are not "
                "retrieved by this adapter."
            ),
        )

    # -- transport ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> Any:
        """Throttled GET returning parsed JSON.

        NCBI's rate limit is a hard constraint on a shared public resource;
        respecting it is part of being a good citizen of the ecosystem we are
        building the accountability layer around.
        """
        now = utc_now()
        if self._last_request_at:
            elapsed = now - self._last_request_at[-1]
            if elapsed < self._min_interval:
                import time

                time.sleep((self._min_interval - elapsed).total_seconds())
        self._last_request_at.append(now)
        url = f"{EUTILS_BASE}/{path}"
        response = self._transport.get(url, params=params)
        if not response.ok:
            raise TransportError(f"{url} returned HTTP {response.status_code}")
        payload = response.json()
        return payload, response

    def _base_params(self) -> dict[str, str]:
        params = {"db": "clinvar", "retmode": "json", "tool": self._tool}
        if self._api_key:
            params["api_key"] = self._api_key
        if self._email:
            params["email"] = self._email
        return params

    # -- retrieval ----------------------------------------------------------

    def _retrieve(
        self, variant: NormalizedVariant, *, gene: str | None
    ) -> Sequence[EvidenceRecord]:
        retrieved_at = self._clock() if self._clock is not None else utc_now()
        pos_field = "chrpos38" if variant.genome_build is GenomeBuild.GRCH38 else "chrpos37"
        term = f"{variant.chromosome}[chr] AND {variant.position}:{variant.position}[{pos_field}]"

        search_params = {**self._base_params(), "term": term, "retmax": "50"}
        try:
            search_payload, search_response = self._get("esearch.fcgi", search_params)
        except TransportError as exc:
            return [
                self._failed(data_type, variant, gene, retrieved_at, str(exc), search_params)
                for data_type in self.declaration.data_types
            ]

        uids = _extract_uids(search_payload)
        search_detail = RetrievalDetail(
            transport=self._transport_label,
            urls=(search_response.url,),
            response_sha256=search_response.body_sha256,
            latency_ms=search_response.elapsed_ms,
            http_status=search_response.status_code,
            request_hash=_hash_params(search_params),
        )

        if not uids:
            # ClinVar answered authoritatively: no record at this locus.
            return [
                self._record_unavailable(
                    variant=variant,
                    gene=gene,
                    data_type=data_type,
                    retrieved_at=retrieved_at,
                    retrieval=search_detail,
                    reason=(
                        "ClinVar esearch returned no records at "
                        f"{variant.chromosome}:{variant.position} ({variant.genome_build.value}). "
                        "Absence from ClinVar is NOT evidence of benignity."
                    ),
                )
                for data_type in self.declaration.data_types
            ]

        summary_params = {**self._base_params(), "id": ",".join(uids)}
        try:
            summary_payload, summary_response = self._get("esummary.fcgi", summary_params)
        except TransportError as exc:
            return [
                self._failed(data_type, variant, gene, retrieved_at, str(exc), summary_params)
                for data_type in self.declaration.data_types
            ]

        summary_detail = RetrievalDetail(
            transport=self._transport_label,
            urls=(search_response.url, summary_response.url),
            response_sha256=summary_response.body_sha256,
            latency_ms=search_response.elapsed_ms + summary_response.elapsed_ms,
            http_status=summary_response.status_code,
            request_hash=_hash_params(summary_params),
        )

        documents = _extract_documents(summary_payload)
        if not documents:
            return [
                self._record_failed(
                    variant=variant,
                    gene=gene,
                    data_type=data_type,
                    retrieved_at=retrieved_at,
                    error="esummary returned no document bodies for the requested variation ids",
                    retrieval=summary_detail,
                )
                for data_type in self.declaration.data_types
            ]

        strengths = {
            index: _identity_match_strength(variant, doc) for index, doc in enumerate(documents)
        }
        exact = [doc for index, doc in enumerate(documents) if strengths[index] == "exact"]
        locus_only = [doc for index, doc in enumerate(
            documents) if strengths[index] == "locus_only"]
        # An exact SPDI match outranks any number of locus-only matches: the
        # locus-only records are, by construction, records we cannot prove are
        # about this allele.
        matches = exact or locus_only
        allele_identity_confirmed = bool(exact)
        if not matches:
            return [
                self._record_unavailable(
                    variant=variant,
                    gene=gene,
                    data_type=data_type,
                    retrieved_at=retrieved_at,
                    retrieval=summary_detail,
                    reason=(
                        f"ClinVar has {len(documents)} record(s) at "
                        f"{variant.chromosome}:{variant.position} but none match this allele "
                        f"({variant.reference}>{variant.alternate}). Locus-level presence is not "
                        "allele-level evidence, and ClinVar commonly holds several alleles at one "
                        "position with different or opposite classifications."
                    ),
                )
                for data_type in self.declaration.data_types
            ]

        # Deterministic selection: lowest variation id. If several VCV records
        # describe the same allele with different classifications, that is a
        # source-level conflict and must be surfaced, not resolved silently.
        matches.sort(key=lambda doc: int(doc.get("uid", 0) or 0))
        chosen = matches[0]
        conflicting = _classification_conflicts(matches)

        identity_limitation = (
            ""
            if allele_identity_confirmed
            else (
                "This ClinVar record matched the query on genomic locus only; allele identity "
                "could not be confirmed by canonical SPDI. It is reported as identity_unverified "
                "and cannot support an ACMG criterion."
            )
        )
        records: list[EvidenceRecord] = []
        records.append(
            self._clinical_significance_record(
                variant,
                gene,
                chosen,
                matches,
                conflicting,
                retrieved_at,
                summary_detail,
                identity_confirmed=allele_identity_confirmed,
                identity_limitation=identity_limitation,
            )
        )
        consequence = self._consequence_record(
            variant,
            gene,
            chosen,
            retrieved_at,
            summary_detail,
            identity_confirmed=allele_identity_confirmed,
            identity_limitation=identity_limitation,
        )
        if consequence is not None:
            records.append(consequence)
        records.extend(
            self._frequency_records(
                variant,
                gene,
                chosen,
                retrieved_at,
                summary_detail,
                identity_confirmed=allele_identity_confirmed,
                identity_limitation=identity_limitation,
            )
        )
        return records

    def _failed(
        self,
        data_type: EvidenceDataType,
        variant: NormalizedVariant,
        gene: str | None,
        retrieved_at: datetime,
        error: str,
        params: dict[str, str],
    ) -> EvidenceRecord:
        return self._record_failed(
            variant=variant,
            gene=gene,
            data_type=data_type,
            retrieved_at=retrieved_at,
            error=error,
            retrieval=RetrievalDetail(
                transport=self._transport_label,
                urls=(f"{EUTILS_BASE}/esearch.fcgi?{_query_string(params)}",),
                error=error,
                request_hash=_hash_params(params),
            ),
        )

    # -- record builders ----------------------------------------------------

    def _clinical_significance_record(
        self,
        variant: NormalizedVariant,
        gene: str | None,
        document: dict[str, Any],
        matches: list[dict[str, Any]],
        conflicting: bool,
        retrieved_at: datetime,
        retrieval: RetrievalDetail,
        identity_confirmed: bool = True,
        identity_limitation: str = "",
    ) -> EvidenceRecord:
        classification = document.get("germline_classification") or {}
        description = classification.get("description") or ""
        review_status = classification.get("review_status") or ""
        stars = review_status_stars(review_status)
        label = normalize_classification(description)

        limitations: list[str] = []
        if stars is None:
            limitations.append(
                f"ClinVar review status {review_status!r} is not in NGS-Agent's mapping table; "
                "assertion level could not be determined and the record is treated as unverified."
            )
        if label == "unmapped":
            limitations.append(
                f"ClinVar classification {description!r} has no NGS-Agent mapping; the raw string "
                "is preserved but the engine cannot use it."
            )
        if label == "conflicting":
            limitations.append(
                "ClinVar reports conflicting classifications from its submitters. This is "
                "genuine disagreement in the source, not a data error."
            )
        if conflicting:
            limitations.append(
                f"{len(matches)} ClinVar records match this allele and disagree on classification."
            )
        if gene and document.get("gene_sort") and document["gene_sort"].upper() != gene.upper():
            limitations.append(
                f"ClinVar attributes this record to {document['gene_sort']!r}, not the queried "
                f"gene {gene!r}."
            )
        limitations.append(
            "A ClinVar classification is a submitted interpretation, not primary evidence. "
            "NGS-Agent uses it only through the criteria the configured rule set permits."
        )

        if conflicting or label == "conflicting":
            verification = VerificationStatus.SOURCE_REPORTED_CONFLICT
        elif identity_confirmed:
            verification = VerificationStatus.VERIFIED
        else:
            verification = VerificationStatus.IDENTITY_UNVERIFIED
        applicability = (
            ApplicabilityStatus.APPLIES
            if verification is VerificationStatus.VERIFIED and stars is not None
            else ApplicabilityStatus.INDETERMINATE
        )
        if identity_limitation:
            limitations.append(identity_limitation)

        observed_variant_identity = _observed_identity(variant, document)
        observed = {
            "description_raw": description,
            "classification_label": label,
            "review_status": review_status,
            "review_status_stars": stars if stars is not None else "unmapped",
            "authoritative_review": review_status.strip().lower() in AUTHORITATIVE_REVIEW_STATUSES,
            "last_evaluated": classification.get("last_evaluated") or None,
            "accession": document.get("accession"),
            "accession_version": document.get("accession_version"),
            "variation_id": document.get("uid"),
            "title": document.get("title"),
            "traits": _trait_names(classification),
            "matching_record_count": len(matches),
        }
        return self._record_present(
            variant=variant,
            gene=gene or document.get("gene_sort"),
            data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
            observed_value=observed,
            retrieved_at=retrieved_at,
            verification=verification,
            applicability=applicability,
            transcript=_transcript_from_title(document.get("title")),
            accession=document.get("accession_version") or document.get("accession"),
            limitations=limitations,
            retrieval=retrieval,
            observed_variant_identity=observed_variant_identity,
            provenance={
                "source_database": "ClinVar",
                "publisher": "National Library of Medicine, NCBI",
                "citation": (
                    "Landrum MJ, et al. ClinVar: improving access to variant interpretations and "
                    "supporting evidence. Nucleic Acids Res. 2018;46(D1):D1062-D1067."
                ),
                "record_url": f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{document.get('uid')}/",
            },
        )

    def _consequence_record(
        self,
        variant: NormalizedVariant,
        gene: str | None,
        document: dict[str, Any],
        retrieved_at: datetime,
        retrieval: RetrievalDetail,
        identity_confirmed: bool = True,
        identity_limitation: str = "",
    ) -> EvidenceRecord | None:
        consequences = document.get("molecular_consequence_list") or []
        variation = (document.get("variation_set") or [{}])[0]
        cdna = variation.get("cdna_change") or None
        protein_change = document.get("protein_change") or None
        if not consequences and not cdna and not protein_change:
            return None

        limitations = [
            "Molecular consequence is reported by ClinVar across all transcripts of the record; "
            "NGS-Agent has not resolved it to a single MANE Select transcript. Consequence-based "
            "criteria must not be applied from this record alone without transcript confirmation.",
        ]
        if protein_change and "," in str(protein_change):
            limitations.append(
                "ClinVar lists multiple protein changes because the record spans several "
                "transcripts; none is asserted to be the MANE Select consequence."
            )
        if identity_limitation:
            limitations.append(identity_limitation)

        observed = {
            "consequences": list(consequences),
            "cdna_change": cdna,
            "protein_change": protein_change,
            "variant_type": variation.get("variant_type") or document.get("obj_type"),
            "canonical_spdi": variation.get("canonical_spdi"),
            "gene_symbols": [entry.get("symbol") for entry in (document.get("genes") or [])],
        }
        return self._record_present(
            variant=variant,
            gene=gene or document.get("gene_sort"),
            data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value=observed,
            retrieved_at=retrieved_at,
            # Consequence is a property of the record we identity-matched, so it
            # inherits that match's strength — a locus-only match does not earn
            # a verified consequence either.
            verification=(
                VerificationStatus.VERIFIED
                if identity_confirmed
                else VerificationStatus.IDENTITY_UNVERIFIED
            ),
            applicability=(
                ApplicabilityStatus.APPLIES
                if identity_confirmed
                else ApplicabilityStatus.INDETERMINATE
            ),
            transcript=_transcript_from_title(document.get("title")),
            accession=document.get("accession_version") or document.get("accession"),
            limitations=limitations,
            retrieval=retrieval,
            observed_variant_identity=_observed_identity(variant, document),
            provenance={
                "source_database": "ClinVar",
                "derived_from": "molecular_consequence_list / variation_set[0]",
            },
        )

    def _frequency_records(
        self,
        variant: NormalizedVariant,
        gene: str | None,
        document: dict[str, Any],
        retrieved_at: datetime,
        retrieval: RetrievalDetail,
        identity_confirmed: bool = True,
        identity_limitation: str = "",
    ) -> list[EvidenceRecord]:
        variation = (document.get("variation_set") or [{}])[0]
        entries = variation.get("allele_freq_set") or []
        records: list[EvidenceRecord] = []
        for entry in entries:
            source_name = str(entry.get("source") or "unknown")
            raw_value = entry.get("value")
            if raw_value in (None, ""):
                continue
            try:
                ratio = ratio_from_decimal_string(str(raw_value))
            except EvidenceValidationError as exc:
                records.append(
                    self._record_unavailable(
                        variant=variant,
                        gene=gene,
                        data_type=EvidenceDataType.ALLELE_FREQUENCY,
                        retrieved_at=retrieved_at,
                        retrieval=retrieval,
                        reason=(
                            f"allele frequency {raw_value!r} from {source_name} is not a usable "
                            f"decimal: {exc}"
                        ),
                    )
                )
                continue
            records.append(
                self._record_present(
                    variant=variant,
                    gene=gene or document.get("gene_sort"),
                    data_type=EvidenceDataType.ALLELE_FREQUENCY,
                    observed_value={
                        "frequency_source": source_name,
                        "allele_frequency": ratio.as_decimal_string(),
                        "allele_frequency_ratio": {
                            "numerator": ratio.numerator,
                            "denominator": ratio.denominator,
                        },
                        "minor_allele": entry.get("minor_allele") or None,
                        "ancestry_specific": False,
                        "allele_count": None,
                        "allele_number": None,
                    },
                    retrieved_at=retrieved_at,
                    verification=(
                        VerificationStatus.VERIFIED
                        if identity_confirmed
                        else VerificationStatus.IDENTITY_UNVERIFIED
                    ),
                    applicability=(
                        ApplicabilityStatus.APPLIES
                        if identity_confirmed
                        else ApplicabilityStatus.INDETERMINATE
                    ),
                    transcript=_transcript_from_title(document.get("title")),
                    accession=document.get("accession_version") or document.get("accession"),
                    limitations=(
                        f"Frequency is reported by {source_name} via ClinVar, not retrieved from "
                        "the frequency database directly. No allele count, allele number, "
                        "ancestry breakdown, or filtering allele frequency is available, so "
                        "observation quality cannot be assessed. ACMG/AMP frequency criteria "
                        "(BA1/BS1/PM2) depend on that context.",
                        "A single aggregate frequency without ancestry stratification can miss a "
                        "population-specific enrichment that would change BS1/BA1 applicability.",
                        *( (identity_limitation,) if identity_limitation else () ),
                    ),
                    retrieval=retrieval,
                    observed_variant_identity=_observed_identity(variant, document),
                    provenance={
                        "source_database": "ClinVar allele_freq_set",
                        "frequency_database": source_name,
                    },
                )
            )
        return records


# ---------------------------------------------------------------------------
# Response parsing helpers (pure functions, unit-tested against recordings)
# ---------------------------------------------------------------------------


def _extract_uids(payload: Any) -> list[str]:
    result = (payload or {}).get("esearchresult") or {}
    ids = result.get("idlist") or []
    return [str(uid) for uid in ids if str(uid).strip()]


def _extract_documents(payload: Any) -> list[dict[str, Any]]:
    result = (payload or {}).get("result") or {}
    documents = []
    for uid in result.get("uids") or []:
        document = result.get(str(uid))
        if isinstance(document, dict):
            documents.append(document)
    return documents


def _hash_params(params: dict[str, str]) -> str:
    from ngs_agent.core.hashing import canonical_json_sha256

    return canonical_json_sha256(params)


def _query_string(params: dict[str, str]) -> str:
    return "&".join(f"{key}={params[key]}" for key in sorted(params))


def _transcript_from_title(title: Any) -> str | None:
    """Extract ``NM_007294.4`` from ``NM_007294.4(BRCA1):c.4327C>T (p.Arg1443Ter)``."""
    if not title:
        return None
    text = str(title)
    head = text.split("(", 1)[0].strip()
    return head or None


def _trait_names(classification: dict[str, Any]) -> list[str]:
    return [
        str(trait.get("trait_name"))
        for trait in (classification.get("trait_set") or [])
        if trait.get("trait_name")
    ]


def _locus_for(document: dict[str, Any], build: GenomeBuild) -> dict[str, Any] | None:
    variation = (document.get("variation_set") or [{}])[0]
    assembly = build.value
    fallback: dict[str, Any] | None = None
    for locus in variation.get("variation_loc") or []:
        if locus.get("assembly_name") == assembly:
            if locus.get("status") == "current":
                return locus
            fallback = fallback or locus
    return fallback


def _expected_span(variant: NormalizedVariant) -> tuple[int, int]:
    """The (start, stop) span ClinVar uses for this variant type.

    ClinVar reports the *changed* bases, not the VCF anchor base, so an indel's
    span is offset from the VCF position.
    """
    if variant.variant_type in (VariantType.SNV, VariantType.MNV):
        return variant.position, variant.position + len(variant.reference) - 1
    if variant.variant_type is VariantType.DELETION:
        start = variant.position + len(variant.alternate)
        return start, variant.position + len(variant.reference) - 1
    if variant.variant_type is VariantType.INSERTION:
        # ClinVar brackets an insertion between the flanking bases.
        start = variant.position + len(variant.reference)
        return start, start
    return variant.position, variant.position + len(variant.reference) - 1


def _identity_match_strength(
    variant: NormalizedVariant, document: dict[str, Any]
) -> Literal["exact", "locus_only", "none"]:
    """How confidently a ClinVar record can be tied to the queried allele.

    ``exact``
        The record's canonical SPDI equals ours. SPDI encodes the accession,
        the inter-base start, the deleted sequence and the inserted sequence,
        so equality is proof of allele identity.

    ``locus_only``
        SPDI was unavailable on one side and the coordinate span matches. This
        is **not** proof: ClinVar routinely holds several alleles at the same
        position with opposite classifications (see the bundled recording for
        chr17:43082434, where C>T is Pathogenic and C>G is Benign). A
        locus-only match is therefore emitted as ``identity_unverified`` and
        the engine refuses to derive a criterion from it.

    ``none``
        Different allele, different contig, or no comparable location.

    Coordinates alone can never produce ``exact``. That rule is the whole
    reason this function returns three values instead of a boolean.
    """
    variation = (document.get("variation_set") or [{}])[0]
    spdi = variation.get("canonical_spdi")
    if spdi and variant.spdi:
        return "exact" if str(spdi) == variant.spdi else "none"

    locus = _locus_for(document, variant.genome_build)
    if locus is None:
        return "none"
    if str(locus.get("chr")) != variant.chromosome:
        return "none"
    try:
        start = int(locus.get("start"))
        stop = int(locus.get("stop"))
    except (TypeError, ValueError):
        return "none"
    expected_start, expected_stop = _expected_span(variant)
    return "locus_only" if (start, stop) == (expected_start, expected_stop) else "none"


def _identity_matches(variant: NormalizedVariant, document: dict[str, Any]) -> bool:
    """Kept for readability at call sites; see :func:`_identity_match_strength`."""
    return _identity_match_strength(variant, document) != "none"


def _observed_identity(variant: NormalizedVariant, document: dict[str, Any]) -> str:
    """Best identity string for what ClinVar says it is describing."""
    locus = _locus_for(document, variant.genome_build)
    if locus is None:
        return variant.identity
    try:
        start = int(locus.get("start"))
    except (TypeError, ValueError):
        return variant.identity
    return (
        f"{variant.genome_build.value}|{locus.get('chr')}|{start}|"
        f"{variant.reference}|{variant.alternate}"
    )


def _classification_conflicts(matches: list[dict[str, Any]]) -> bool:
    labels = {
        normalize_classification((doc.get("germline_classification") or {}).get("description"))
        for doc in matches
    }
    return len(labels) > 1


__all__ = [
    "ADAPTER_VERSION",
    "AUTHORITATIVE_REVIEW_STATUSES",
    "CLASSIFICATION_LABELS",
    "ClinVarAdapter",
    "EUTILS_BASE",
    "REVIEW_STATUS_STARS",
    "normalize_classification",
    "review_status_stars",
]
