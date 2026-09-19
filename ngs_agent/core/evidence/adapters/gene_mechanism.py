"""Gene/disease mechanism evidence — the gate in front of PVS1.

PVS1 is the single most consequential criterion in ACMG/AMP: it is the only
"very strong" pathogenic code, and it applies *only* when loss of function is
an established disease mechanism for the gene. Applying PVS1 to a gene whose
disease mechanism is gain of function or dominant negative is a well-known way
to produce a false pathogenic call.

This adapter supplies that gate as structured evidence rather than as a
hard-coded ``if`` in the engine.

.. warning::
   The bundled table is a **curated seed**, not an authoritative source. Every
   row is marked ``curation_status="seed_unverified"`` and every record it
   emits carries a limitation saying so. Before production use, replace it with
   ClinGen Gene-Disease Validity curations and the relevant ClinGen VCEP
   specifications, and bump ``GENE_MECHANISM_TABLE_VERSION``.

Design note: this adapter is *gene-level*, not variant-level. It answers "is
loss of function an established mechanism for this gene?", which is one of
several inputs PVS1 needs — the others (transcript/exon context, NMD escape)
come from separate evidence types so that each can be sourced, versioned, and
audited independently.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ngs_agent.core.errors import UnknownGeneError
from ngs_agent.core.evidence.base import AdapterDeclaration, BaseEvidenceAdapter
from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    RetrievalDetail,
    VerificationStatus,
)
from ngs_agent.core.normalization import NormalizedVariant

ADAPTER_VERSION = "gene-mechanism-adapter-1.0.0"

DEFAULT_TABLE_PATH = Path(__file__).resolve().parents[2] / "data" / "gene_disease_mechanisms.v1.json"

#: Mechanisms for which PVS1 ("null variant in a gene where LoF is a known
#: disease mechanism") is mechanism-applicable.
LOF_APPLICABLE_MECHANISMS = frozenset({"loss_of_function"})

#: Mechanisms that explicitly block PVS1. Recorded as a *rejection* with a
#: reason, not as a silent no-op, so the reader can see why PVS1 was withheld.
LOF_BLOCKING_MECHANISMS = frozenset({"gain_of_function", "dominant_negative", "mixed", "unknown"})


class GeneMechanismTable:
    """An immutable, versioned gene/mechanism lookup."""

    def __init__(self, payload: dict[str, Any], *, path: Path | None = None) -> None:
        self.path = path
        self.version = str(payload.get("table_version", "unversioned"))
        self.source = dict(payload.get("source", {}))
        self._genes: dict[str, dict[str, Any]] = {}
        for entry in payload.get("genes", []):
            symbol = str(entry.get("symbol", "")).upper()
            if not symbol:
                continue
            self._genes[symbol] = dict(entry)

    @classmethod
    def load(cls, path: Path | str | None = None) -> GeneMechanismTable:
        resolved = Path(path) if path else DEFAULT_TABLE_PATH
        if not resolved.is_file():
            raise UnknownGeneError(f"gene mechanism table not found: {resolved}")
        return cls(json.loads(resolved.read_text(encoding="utf-8")), path=resolved)

    def __len__(self) -> int:
        return len(self._genes)

    def symbols(self) -> list[str]:
        return sorted(self._genes)

    def get(self, symbol: str) -> dict[str, Any] | None:
        return self._genes.get(str(symbol).upper())

    def is_known_gene(self, symbol: str) -> bool:
        return str(symbol).upper() in self._genes


class GeneMechanismAdapter(BaseEvidenceAdapter):
    """Emit :data:`EvidenceDataType.GENE_DISEASE_MECHANISM` records from a table."""

    def __init__(self, table: GeneMechanismTable | None = None, *, clock: Any = None) -> None:
        super().__init__(clock=clock)
        self.table = table or GeneMechanismTable.load()

    @property
    def declaration(self) -> AdapterDeclaration:
        return AdapterDeclaration(
            name="gene_disease_mechanism_table",
            version=self.table.version,
            adapter_version=ADAPTER_VERSION,
            data_types=(EvidenceDataType.GENE_DISEASE_MECHANISM,),
            endpoint=str(self.table.path) if self.table.path else None,
            license="Apache-2.0 (bundled curation seed); replace with ClinGen G2P data in production",
            requires_network=False,
            hosted_by="local",
            notes=(
                "Curated seed table of gene disease mechanisms used to gate PVS1. "
                "NOT an authoritative source; see the module docstring."
            ),
        )

    def _retrieve(
        self, variant: NormalizedVariant, *, gene: str | None
    ) -> Sequence[EvidenceRecord]:
        from ngs_agent.core.evidence.models import utc_now

        retrieved_at = self._clock() if self._clock is not None else utc_now()
        retrieval = RetrievalDetail(
            transport="file",
            urls=(str(self.table.path),) if self.table.path else (),
        )

        if not gene:
            return [
                self._record_unavailable(
                    variant=variant,
                    gene=None,
                    data_type=EvidenceDataType.GENE_DISEASE_MECHANISM,
                    retrieved_at=retrieved_at,
                    retrieval=retrieval,
                    reason=(
                        "No gene symbol was supplied or resolved for this variant, so the "
                        "gene/disease mechanism could not be looked up. PVS1 cannot be evaluated."
                    ),
                )
            ]

        entry = self.table.get(gene)
        if entry is None:
            return [
                self._record_unavailable(
                    variant=variant,
                    gene=gene,
                    data_type=EvidenceDataType.GENE_DISEASE_MECHANISM,
                    retrieved_at=retrieved_at,
                    retrieval=retrieval,
                    reason=(
                        f"Gene {gene!r} is not present in the mechanism table "
                        f"({self.table.version}, {len(self.table)} genes). Absence from the table "
                        "is not evidence that loss of function is not the mechanism; PVS1 is "
                        "therefore not evaluated."
                    ),
                )
            ]

        mechanism = str(entry.get("mechanism", "unknown"))
        pvs1_applicable = mechanism in LOF_APPLICABLE_MECHANISMS and bool(
            entry.get("pvs1_applicable", False)
        )
        limitations = [
            f"Mechanism table {self.table.version} is a curated seed, not an authoritative "
            "source. Every row is marked seed_unverified and must be re-verified against "
            "ClinGen Gene-Disease Validity and the applicable ClinGen VCEP specification "
            "before production use.",
            "Mechanism is gene-level. It does not establish that *this* variant is a null "
            "allele, nor that the transcript context permits PVS1.",
        ]
        if entry.get("notes"):
            limitations.append(f"Curator note: {entry['notes']}")

        return [
            self._record_present(
                variant=variant,
                gene=gene,
                data_type=EvidenceDataType.GENE_DISEASE_MECHANISM,
                observed_value={
                    "gene_symbol": str(entry.get("symbol", gene)).upper(),
                    "entrez_id": entry.get("entrez_id"),
                    "mechanism": mechanism,
                    "inheritance": entry.get("inheritance"),
                    "disease_mane": entry.get("disease"),
                    "pvs1_mechanism_applicable": pvs1_applicable,
                    "curation_status": entry.get("curation_status", "seed_unverified"),
                    "reference": entry.get("reference"),
                },
                retrieved_at=retrieved_at,
                verification=VerificationStatus.VERIFIED,
                applicability=ApplicabilityStatus.APPLIES,
                limitations=limitations,
                retrieval=retrieval,
                provenance={
                    "source_database": "NGS-Agent curated gene mechanism seed table",
                    "table_version": self.table.version,
                    "table_path": str(self.table.path) if self.table.path else None,
                    "table_source": self.table.source,
                },
            )
        ]
