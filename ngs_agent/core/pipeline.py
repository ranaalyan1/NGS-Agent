"""The review pipeline — the vertical slice, end to end.

    VCF
      -> normalized variant
      -> structured evidence (validated, ledgered)
      -> deterministic ACMG/AMP classification
      -> versioned JSON contract
      -> audit record
      -> replayable

Everything here is orchestration. The pipeline makes no clinical decision of its
own: it normalizes, retrieves, validates, hands the ledger to the engine, and
records what happened.

Two properties are load-bearing and worth stating where they are implemented:

* **A failure anywhere is a result, not a crash.** An unreachable source, a
  malformed line, an unresolvable gene — each produces a contract that says so,
  with ``abstained`` and ``missing_evidence`` populated. The alternative (an
  exception that aborts the run) loses the record that the attempt happened.
* **The explanation layer runs last and cannot reach back.** :func:`explain` is
  applied to a completed contract and produces a new one; the audit record for
  the classification is written before any model is called.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.acmg.derivation import DerivationConfig
from ngs_agent.core.acmg.engine import AbstentionPolicy, AcmgEngine, ClassificationOutcome
from ngs_agent.core.audit import (
    AuditLog,
    AuditRecord,
    build_review_audit_record,
    configuration_hash,
    new_audit_id,
)
from ngs_agent.core.contract import (
    GeneBlock,
    ProvenanceBlock,
    VariantReviewResult,
)
from ngs_agent.core.errors import CoreError, GenomeBuildError
from ngs_agent.core.evidence.base import EvidenceAdapter
from ngs_agent.core.evidence.models import EvidenceRecord, EvidenceStatus, utc_now
from ngs_agent.core.evidence.registry import EvidenceConfiguration, EvidenceRegistry
from ngs_agent.core.evidence.validation import ValidationReport, validate_evidence
from ngs_agent.core.explanation import (
    CompletionBackend,
    explain_classification,
    explanation_model_metadata,
)
from ngs_agent.core.genome import GenomeBuild, parse_genome_build
from ngs_agent.core.hashing import ga4gh_digest
from ngs_agent.core.ledger import EvidenceLedger, MemoryLedger
from ngs_agent.core.log import get_logger, structured_event
from ngs_agent.core.normalization import (
    NormalizedVariant,
    RawVariant,
    SequenceProvider,
    normalize_raw,
)
from ngs_agent.core.vcf import VcfDocument, VcfParseError, read_vcf
from ngs_agent.core.version import CONTRACT_SCHEMA_VERSION, ENGINE_VERSION, NORMALIZATION_VERSION

#: INFO keys consulted when resolving a gene symbol, in precedence order.
_GENE_INFO_KEYS = ("GENE", "SYMBOL")

#: Consequence-annotation field layouts, in precedence order.
_CSQ_KEYS = ("CSQ", "ANN")


class ReviewError(CoreError):
    """A review could not be performed at all (as opposed to abstaining)."""


class GeneResolution(BaseModel):
    """Where a gene symbol came from and whether it could be validated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str | None = None
    resolved_from: str = "not_resolved"
    info_key: str | None = None
    validated_against: str | None = None
    validation_status: str = "not_checked"
    note: str = ""

    def as_block(self) -> GeneBlock:
        return GeneBlock(
            symbol=self.symbol,
            resolved_from=self.resolved_from,  # type: ignore[arg-type]
            info_key=self.info_key,
            validated_against=self.validated_against,
            validation_status=self.validation_status,  # type: ignore[arg-type]
            note=self.note,
        )


class VariantReview(BaseModel):
    """One variant's complete outcome."""

    model_config = ConfigDict(extra="forbid")

    variant: NormalizedVariant
    gene: GeneResolution
    result: VariantReviewResult
    audit_record: AuditRecord | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    errors: tuple[str, ...] = ()


class ReviewRun(BaseModel):
    """A whole VCF's worth of reviews plus run-level provenance."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    input_path: str
    input_sha256: str
    genome_build: str
    genome_build_source: str
    configuration_hash: str
    engine_version: str = ENGINE_VERSION
    contract_schema_version: str = CONTRACT_SCHEMA_VERSION
    normalization_version: str = NORMALIZATION_VERSION
    started_at: datetime
    reviews: list[VariantReview] = Field(default_factory=list)
    run_warnings: tuple[str, ...] = ()
    database_versions: dict[str, str] = Field(default_factory=dict)
    adapters: tuple[dict[str, Any], ...] = ()

    @property
    def results(self) -> list[VariantReviewResult]:
        return [review.result for review in self.reviews]

    def abstention_rate(self) -> float:
        if not self.reviews:
            return 0.0
        abstained = sum(1 for review in self.reviews if review.result.classification.abstained)
        return abstained / len(self.reviews)


@dataclass
class ReviewPipeline:
    """Orchestrates the signed classification path."""

    configuration: EvidenceConfiguration
    rule_set: str | None = None
    derivation_config: DerivationConfig | None = None
    abstention_policy: AbstentionPolicy | None = None
    sequence_provider: SequenceProvider | None = None
    registry: EvidenceRegistry | None = None
    audit_log: AuditLog | None = None
    ledger: EvidenceLedger | None = None
    extra_adapters: Sequence[EvidenceAdapter] = field(default_factory=tuple)
    transport: Any = None
    clock: Any = None
    #: Built in ``__post_init__``; not a constructor argument.
    engine: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.derivation_config is None:
            self.derivation_config = DerivationConfig()
        if self.abstention_policy is None:
            self.abstention_policy = AbstentionPolicy()
        if self.registry is None:
            self.registry = EvidenceRegistry(
                self.configuration, transport=self.transport, clock=self.clock,
                extra_adapters=self.extra_adapters,
            )
        if self.ledger is None:
            self.ledger = MemoryLedger()
        self.engine = AcmgEngine(
            rule_set=self.rule_set,
            config=self.derivation_config,
            policy=self.abstention_policy,
        )

    # -- entry points -------------------------------------------------------

    def review_vcf(
        self,
        path: Path | str,
        *,
        genome_build: str | GenomeBuild | None = None,
        gene_filter: str | None = None,
        gene_of_record: str | None = None,
        max_variants: int | None = None,
        run_id: str | None = None,
        explain_backend: CompletionBackend | None = None,
        explain_provider: str | None = None,
        explain_model: str | None = None,
    ) -> ReviewRun:
        """Review every variant in a VCF.

        ``gene_filter`` selects which variants are reviewed; it never changes
        what a variant is. ``gene_of_record`` asserts a gene symbol for a VCF
        that does not carry one, and *does* change the gene used for gene-level
        criteria, so it is recorded in provenance as ``resolved_from="cli"``.
        Conflating the two would let a filter flag alter a classification.
        """
        started_at = self._now()
        document = read_vcf(path)
        build, build_source = self._resolve_build(document, genome_build)

        config_hash = configuration_hash(
            {
                "evidence": self.configuration.to_hashable(),
                "rule_set": self.rule_set or "default",
                "derivation_config": self.derivation_config.model_dump(mode="json"),
                "abstention_policy": self.abstention_policy.model_dump(mode="json"),
                "genome_build": build.value,
                "engine_version": ENGINE_VERSION,
                "normalization_version": NORMALIZATION_VERSION,
            }
        )
        resolved_run_id = run_id or _stable_run_id(document, config_hash)

        warnings: list[str] = []
        if build_source == "vcf_header":
            warnings.append(
                f"Genome build {build.value} was taken from the VCF header "
                f"(declared_reference={document.facts.declared_reference!r}, "
                f"declared_assembly={document.facts.declared_assembly!r}). A header declaration "
                "is a claim by the file's producer, not a guarantee; pass --genome-build "
                "explicitly for an auditable run."
            )
        if self.sequence_provider is None:
            warnings.append(
                "No reference sequence provider was configured, so indels cannot be proven "
                "left-aligned. Any indel in this run will be reported with an incomplete "
                "normalization and the engine will abstain on it."
            )

        run = ReviewRun(
            run_id=resolved_run_id,
            input_path=document.facts.path,
            input_sha256=document.facts.sha256,
            genome_build=build.value,
            genome_build_source=build_source,
            configuration_hash=config_hash,
            started_at=started_at,
            run_warnings=tuple(warnings),
            adapters=tuple(self.registry.declarations_as_dicts()),
        )

        reviewed_identities: set[str] = set()
        duplicates: list[str] = []
        filtered_out: list[str] = []
        capped = False
        for raw in document.records:
            if capped:
                break
            for variant in self._normalize(raw, build):
                # Checked per allele, not per VCF record: a multiallelic site
                # expands to several alleles and would otherwise overshoot the
                # cap the caller asked for.
                if max_variants is not None and len(run.reviews) >= max_variants:
                    capped = True
                    break
                # A multiallelic site and a separately written biallelic record
                # can normalize to the same allele. Reviewing it twice would
                # produce two audit records for one decision, which is worse
                # than useless: it makes a single observation look like two.
                if variant.identity in reviewed_identities:
                    duplicates.append(
                        f"{variant.identity} (line {raw.line_number}) duplicates an already "
                        "reviewed allele and was skipped"
                    )
                    continue
                reviewed_identities.add(variant.identity)
                gene = self._resolve_gene(raw, variant, gene_of_record=gene_of_record)
                if gene_filter:
                    resolved = (gene.symbol or "").upper()
                    if resolved != gene_filter.upper():
                        # An unresolved gene does not match a filter either: we
                        # will not review a variant whose gene we cannot name
                        # when the caller asked for one specific gene.
                        filtered_out.append(
                            f"{variant.identity} (line {raw.line_number}): gene "
                            f"{resolved or 'unresolved'} is not {gene_filter.upper()}"
                        )
                        continue
                review = self._review_one(
                    variant=variant,
                    raw=raw,
                    gene=gene,
                    run_id=resolved_run_id,
                    document=document,
                    config_hash=config_hash,
                )
                run.reviews.append(review)

        if explain_backend is not None:
            self._apply_explanations(
                run,
                explain_backend,
                provider=explain_provider,
                model=explain_model,
            )

        if duplicates:
            run.run_warnings = (*run.run_warnings, *_dedupe(duplicates))
        if filtered_out:
            run.run_warnings = (
                *run.run_warnings,
                f"{len(filtered_out)} variant(s) skipped by --gene {gene_filter}: "
                + "; ".join(_dedupe(filtered_out)[:5])
                + ("; ..." if len(filtered_out) > 5 else ""),
            )
        if capped:
            run.run_warnings = (
                *run.run_warnings,
                f"Run stopped at --max-variants {max_variants}; the remaining records in "
                f"{document.facts.path} were not reviewed.",
            )
        run.database_versions = dict(self.ledger.source_versions())
        structured_event(
            get_logger(),
            20,
            "review_run_complete",
            run_id=run.run_id,
            variants=len(run.reviews),
            abstention_rate=round(run.abstention_rate(), 4),
            input_sha256=run.input_sha256,
        )
        return run

    def review_variant(
        self,
        variant: NormalizedVariant,
        *,
        gene: str | None = None,
        run_id: str | None = None,
        input_hashes: dict[str, str] | None = None,
    ) -> VariantReview:
        """Review a single already-normalized variant (used by MCP and tests)."""
        resolution = GeneResolution(
            symbol=gene,
            resolved_from="cli" if gene else "not_resolved",
        )
        resolution = self._validate_gene(resolution)
        config_hash = configuration_hash(
            {
                "evidence": self.configuration.to_hashable(),
                "rule_set": self.rule_set or "default",
                "derivation_config": self.derivation_config.model_dump(mode="json"),
                "abstention_policy": self.abstention_policy.model_dump(mode="json"),
                "engine_version": ENGINE_VERSION,
            }
        )
        return self._review_one(
            variant=variant,
            raw=None,
            gene=resolution,
            run_id=run_id or _stable_run_id_from_hashes(input_hashes or {}, config_hash),
            document=None,
            config_hash=config_hash,
            input_hashes=input_hashes,
        )

    # -- internals ----------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock() if callable(self.clock) else utc_now()

    def _resolve_build(
        self, document: VcfDocument, explicit: str | GenomeBuild | None
    ) -> tuple[GenomeBuild, str]:
        """Delegate to :func:`resolve_build`; kept as a method for call sites."""
        return resolve_build(document, explicit)

    def _normalize(self, raw: RawVariant, build: GenomeBuild) -> list[NormalizedVariant]:
        return normalize_raw(raw, genome_build=build, sequence_provider=self.sequence_provider)

    def _resolve_gene(
        self, raw: RawVariant, variant: NormalizedVariant, *, gene_of_record: str | None = None
    ) -> GeneResolution:
        """Resolve the gene symbol for one variant.

        ``gene_of_record`` is an *assertion* by the operator, used only when the
        VCF itself carries no gene annotation. It deliberately does not override
        a symbol the VCF does state: silently replacing an annotated gene would
        change gene-level criteria (PVS1 mechanism, PP2, BP1) and therefore the
        classification, which no input-selection flag should be able to do.
        """
        for key in _GENE_INFO_KEYS:
            value = raw.info.get(key)
            if value and value != ".":
                return self._validate_gene(
                    GeneResolution(
                        symbol=value.split(",")[0].strip().upper(),
                        resolved_from="vcf_info",
                        info_key=key,
                    )
                )
        for key in _CSQ_KEYS:
            value = raw.info.get(key)
            symbol = _symbol_from_consequence(value)
            if symbol:
                return self._validate_gene(
                    GeneResolution(symbol=symbol, resolved_from="vcf_info", info_key=key)
                )
        if gene_of_record:
            return self._validate_gene(
                GeneResolution(
                    symbol=gene_of_record.upper(),
                    resolved_from="cli",
                    note=(
                        f"The VCF carried no gene annotation for this record; "
                        f"{gene_of_record.upper()} was asserted by the operator. Gene-level "
                        "criteria are evaluated against that assertion."
                    ),
                )
            )
        return self._validate_gene(
            GeneResolution(
                resolved_from="not_resolved",
                note=(
                    "No gene symbol could be resolved from the VCF INFO field or the "
                    "consequence annotation, and none was asserted by the operator. "
                    "Gene-level criteria (PVS1 mechanism, PP2, BP1) cannot be evaluated."
                ),
            )
        )

    def _validate_gene(self, resolution: GeneResolution) -> GeneResolution:
        """Check the symbol against the configured gene mechanism table."""
        if not resolution.symbol:
            return resolution
        table = None
        for adapter in self.registry.adapters:
            table = getattr(adapter, "table", None)
            if table is not None:
                break
        if table is None:
            return resolution.model_copy(
                update={
                    "validation_status": "not_checked",
                    "note": (
                        resolution.note
                        or "No gene reference table is configured, so the symbol "
                        "could not be validated."
                    ),
                }
            )
        if table.is_known_gene(resolution.symbol):
            return resolution.model_copy(
                update={
                    "validated_against": f"{table.version}",
                    "validation_status": "validated",
                    "note": resolution.note,
                }
            )
        return resolution.model_copy(
            update={
                "validated_against": f"{table.version}",
                "validation_status": "not_found",
                "note": (
                    f"Gene symbol {resolution.symbol!r} was not found in the configured gene "
                    f"reference ({table.version}). This is not evidence about the gene; it means "
                    "NGS-Agent cannot validate the symbol or look up its disease mechanism."
                ),
            }
        )

    def _retrieve(self, variant: NormalizedVariant, gene: str | None) -> ValidationReport:
        collected: list[EvidenceRecord] = []
        for adapter in self.registry.adapters:
            declaration = adapter.declaration
            cache = self.registry.cache
            cached: list[EvidenceRecord] = []
            if cache is not None:
                for data_type in declaration.data_types:
                    hit = cache.get(
                        adapter_name=declaration.name,
                        adapter_version=declaration.adapter_version,
                        variant_identity=variant.identity,
                        data_type=data_type.value,
                        now=self._now(),
                    )
                    if hit is not None:
                        cached.append(hit)
            if cached:
                collected.extend(cached)
                continue
            records = list(adapter.fetch(variant, gene=gene))
            collected.extend(records)
            if cache is not None:
                for record in records:
                    cache.put(record, adapter_name=declaration.name,
                              adapter_version=declaration.adapter_version)
        return validate_evidence(collected, variant)

    def _review_one(
        self,
        *,
        variant: NormalizedVariant,
        raw: RawVariant | None,
        gene: GeneResolution,
        run_id: str,
        document: VcfDocument | None,
        config_hash: str,
        input_hashes: dict[str, str] | None = None,
    ) -> VariantReview:
        errors: list[str] = []
        hashes = dict(input_hashes or {})
        if document is not None:
            hashes[Path(document.facts.path).name] = document.facts.sha256

        try:
            report = self._retrieve(variant, gene.symbol)
        except Exception as exc:  # noqa: BLE001 - a retrieval blow-up must still yield a contract
            errors.append(f"evidence retrieval failed: {type(exc).__name__}: {exc}")
            report = ValidationReport()

        self.ledger.append(report.accepted)
        usable = self.ledger.usable_for_variant(variant)
        all_records = self.ledger.for_variant(variant)

        outcome: ClassificationOutcome = self.engine.evaluate(
            variant=variant,
            gene=gene.symbol,
            usable_evidence=usable,
            all_evidence=all_records,
            normalization_complete=variant.normalization.complete and variant.on_primary_contig,
        )

        result_id = f"res.v1.{ga4gh_digest(f'{run_id}|{variant.variant_id}')}"
        created_at = self._now()
        audit_id = new_audit_id(
            action="review", variant_id=variant.variant_id, created_at=created_at
        )
        provenance = ProvenanceBlock(
            engine_version=ENGINE_VERSION,
            normalization_version=NORMALIZATION_VERSION,
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
            rule_set=outcome.rule_set,
            rule_set_version=outcome.rule_set_version,
            generated_at=created_at,
            input_hashes=hashes,
            database_versions=dict(self.ledger.source_versions()),
            adapters=tuple(self.registry.declarations_as_dicts()),
            configuration_hash=config_hash,
            run_id=run_id,
            audit_id=audit_id if self.audit_log is not None else None,
            audit_path=str(self.audit_log.path) if self.audit_log is not None else None,
            environment={
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "implementation": platform.python_implementation(),
            },
        )

        limitations = list(outcome.limitations)
        if not variant.on_primary_contig:
            limitations.append(
                f"Contig {variant.chromosome!r} is not part of the primary assembly. NGS-Agent "
                "does not classify variants on non-primary contigs, patches, or decoys."
            )
        for error in errors:
            limitations.append(f"Pipeline error: {error}")
        for finding in report.findings:
            limitations.append(
                f"evidence_validation[{finding.severity.value}:{finding.code}]: {finding.message}")
        if gene.note:
            limitations.append(f"gene[{gene.validation_status}]: {gene.note}")

        result = VariantReviewResult.build(
            result_id=result_id,
            variant=variant,
            gene=gene.symbol,
            gene_block=gene.as_block(),
            outcome=_with_limitations(outcome, limitations),
            evidence=tuple(self.ledger.informative_for_variant(variant)),
            evidence_gaps=tuple(self.ledger.gaps_for_variant(variant)),
            provenance=provenance,
        )

        audit_record: AuditRecord | None = None
        if self.audit_log is not None:
            audit_record = build_review_audit_record(
                audit_id=audit_id,
                result=result,
                variant=variant,
                gene=gene.symbol,
                gene_resolution=json.loads(gene.as_block().model_dump_json()),
                ledger=self.ledger,
                input_hashes=hashes,
                configuration_hash=config_hash,
                adapter_declarations=self.registry.declarations_as_dicts(),
                normalization_version=NORMALIZATION_VERSION,
                run_id=run_id,
                created_at=created_at,
            )
            self.audit_log.append(audit_record)

        structured_event(
            get_logger(),
            20,
            "variant_reviewed",
            run_id=run_id,
            variant_id=variant.variant_id,
            genome_build=variant.genome_build.value,
            label=outcome.label,
            abstained=outcome.abstained,
            decision_state=outcome.decision_state,
            applied=[item.code for item in outcome.applied_criteria],
            conflicts=len(outcome.conflicts),
            evidence_records=len(all_records),
            usable_records=len(usable),
            audit_id=audit_id,
        )

        return VariantReview(
            variant=variant,
            gene=gene,
            result=result,
            audit_record=audit_record,
            validation={
                "accepted": len(report.accepted),
                "rejected": len(report.rejected),
                "findings": [finding.model_dump(mode="json") for finding in report.findings],
                "duplicate_retrievals": report.duplicate_retrievals,
            },
            errors=tuple(errors),
        )

    def _apply_explanations(
        self,
        run: ReviewRun,
        backend: CompletionBackend,
        *,
        provider: str | None,
        model: str | None,
    ) -> None:
        """Attach model narratives *after* every audit record has been written.

        Ordering matters: the classification is signed and recorded before any
        model is invoked, so a model failure, a hallucination, or a provider
        outage cannot alter the audit trail of the decision itself.
        """
        for review in run.reviews:
            annotated, block = explain_classification(
                review.result,
                backend,
                provider=provider,
                model=model,
                now=self._now(),
            )
            metadata = explanation_model_metadata(block)
            if metadata is not None:
                annotated = annotated.model_copy(
                    update={
                        "provenance": annotated.provenance.model_copy(
                            update={"model_metadata": metadata}
                        )
                    }
                )
            review.result = annotated


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _with_limitations(
    outcome: ClassificationOutcome, limitations: Sequence[str]) -> ClassificationOutcome:
    """Return a copy of ``outcome`` with additional run-level limitations."""
    merged: list[str] = list(outcome.limitations)
    for item in limitations:
        if item not in merged:
            merged.append(item)
    return outcome.model_copy(update={"limitations": tuple(merged)})


def _symbol_from_consequence(value: str | None) -> str | None:
    """Extract a gene symbol from a VEP ``CSQ`` or snpEff ``ANN`` string.

    VEP default: ``Allele|Consequence|IMPACT|SYMBOL|Gene|...`` (symbol at 3).
    snpEff:      ``ALT|effect|impact|gene_name|gene_id|...`` (symbol at 3).
    Both put the symbol at index 3, which is why one parse serves both.
    """
    if not value or value == ".":
        return None
    first = value.split(",")[0]
    if "|" not in first:
        return None
    parts = first.split("|")
    if len(parts) > 3 and parts[3] and parts[3] != ".":
        return parts[3].strip().upper()
    if len(parts) > 4 and parts[4] and parts[4] != ".":
        return parts[4].strip().upper()
    return None


def _stable_run_id(document: VcfDocument, config_hash: str) -> str:
    return _stable_run_id_from_hashes(
        {Path(document.facts.path).name: document.facts.sha256}, config_hash
    )


def _stable_run_id_from_hashes(hashes: dict[str, str], config_hash: str) -> str:
    """A run id that is identical for identical inputs and configuration.

    This is deliberate: re-running the same VCF with the same configuration
    produces the same ``run_id``, which makes it obvious in an audit log that
    two runs are repetitions rather than independent observations.
    """
    payload = {
        "inputs": {key: hashes[key] for key in sorted(hashes)},
        "configuration_hash": config_hash,
        "engine_version": ENGINE_VERSION,
    }
    from ngs_agent.core.hashing import canonical_json_sha256

    return f"run.{canonical_json_sha256(payload)[:16]}"


def resolve_build(
    document: VcfDocument, requested: str | GenomeBuild | None = None
) -> tuple[GenomeBuild, str]:
    """Resolve the genome build for a VCF document.

    The single implementation of this decision, shared by
    :meth:`ReviewPipeline.review_vcf` and the CLI's ``normalize`` command.

    Priority: an explicit request, then the assembly the VCF header declares.
    If neither is available this **raises** rather than defaulting. The same
    coordinate denotes a different allele in GRCh37 and GRCh38, so assuming a
    build would silently make every downstream coordinate, SPDI, and ClinVar
    match wrong -- and would do it in a way that looks like a successful run.

    Returns ``(build, source)`` where ``source`` is recorded in provenance so a
    reviewer can tell whether the build was stated or assumed. It is never
    assumed.
    """
    if requested is not None:
        try:
            return parse_genome_build(requested), "explicit"
        except GenomeBuildError:
            raise ReviewError(
                f"Unsupported genome build {requested!r}. Pass GRCh37 or GRCh38."
            ) from None
    declared = document.facts.declared_assembly or document.facts.declared_reference
    if declared:
        try:
            return parse_genome_build(declared), "vcf_header"
        except GenomeBuildError:
            raise ReviewError(
                f"The VCF declares reference {declared!r}, which NGS-Agent cannot map to a "
                "supported genome build. Pass --genome-build GRCh37 or GRCh38 explicitly."
            ) from None
    raise ReviewError(
        "The VCF does not declare a reference assembly and none was supplied. NGS-Agent "
        "never assumes a genome build; the same coordinate denotes a different allele in "
        "GRCh37 and GRCh38. Pass --genome-build explicitly."
    )


def summarize_run(run: ReviewRun) -> dict[str, Any]:
    """A compact, machine-readable summary of a run (for CI and reporting)."""
    labels: dict[str, int] = {}
    states: dict[str, int] = {}
    for review in run.reviews:
        labels[review.result.classification.display_label] = (
            labels.get(review.result.classification.display_label, 0) + 1
        )
        states[review.result.classification.decision_state] = (
            states.get(review.result.classification.decision_state, 0) + 1
        )
    return {
        "run_id": run.run_id,
        "variants": len(run.reviews),
        "labels": labels,
        "decision_states": states,
        "abstention_rate": round(run.abstention_rate(), 4),
        "conflicts": sum(len(review.result.conflicts) for review in run.reviews),
        "requires_human_review": sum(
            1 for review in run.reviews if review.result.classification.requires_human_review
        ),
        "database_versions": run.database_versions,
        "run_warnings": list(run.run_warnings),
    }


__all__ = [
    "EvidenceStatus",
    "GeneResolution",
    "ReviewError",
    "ReviewPipeline",
    "ReviewRun",
    "VcfParseError",
    "VariantReview",
    "summarize_run",
]
