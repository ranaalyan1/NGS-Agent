"""Variant normalization and stable identity.

The job of this module is to guarantee one property above all others:

    Equivalent biological variants written in different VCF representations
    resolve to the same stable identity.

What it does, in order:

1. Validate the alleles (ACGTN, non-empty, position >= 1).
2. Split multiallelic records into one biallelic record each.
3. Minimally trim each allele pair (right-trim then left-trim, as ``bcftools
   norm`` does).
4. Left-align pure indels against a reference sequence, *if one is supplied*.
5. Derive the variant type, SPDI expression, genomic HGVS, and a stable
   digest-based identifier.

What it deliberately does **not** do:

* It never guesses a reference base. Without a reference provider it cannot
  left-align an indel, and it says so: ``normalization.complete`` is ``False``
  and a warning is attached. A silent, possibly wrong alignment is worse than
  an honest "I could not align this".
* It never invents a transcript-level HGVS (``c.``/``p.``) description. That
  requires a transcript model; when one is not available the field is ``None``
  rather than a plausible-looking string.
* It never emits a ``ga4gh:SQ.``-based VRS ``_id`` without a sequence digest
  provider, for the same reason. The ``ga4gh_digest`` we do emit uses the VRS
  ``sha512t24`` algorithm over our own canonical identity string, which is
  documented as an NGS-Agent namespace identifier, not a VRS absolute id.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field

from ngs_agent.core.errors import NormalizationError
from ngs_agent.core.genome import (
    GenomeBuild,
    accession_for,
    is_primary_contig,
    normalize_chromosome,
)
from ngs_agent.core.hashing import ga4gh_digest
from ngs_agent.core.version import NORMALIZATION_VERSION

_VALID_BASES = frozenset("ACGTN")

#: Namespace prefix for NGS-Agent computed variant identifiers.
IDENTIFIER_NAMESPACE = "nga"


class VariantType(str, Enum):
    SNV = "snv"
    MNV = "mnv"
    INSERTION = "insertion"
    DELETION = "deletion"
    DELINS = "delins"


class SequenceProvider(Protocol):
    """Minimal reference-sequence access needed for left-alignment.

    Implementations may be a FASTA + ``.fai`` reader, an in-memory test double,
    or a customer-hosted refget/HTSGET endpoint. Returning ``None`` means
    "unavailable", which the normalizer converts into an explicit warning —
    never into a fabricated base.
    """

    def fetch(self, chromosome: str, start: int, end: int) -> str | None:
        """Return the 1-based inclusive reference substring, or ``None``."""
        ...


class NormalizationWarning(BaseModel):
    """A non-fatal normalization problem that must reach the user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    severity: str = "warning"  # "warning" | "abstain"


class NormalizationReport(BaseModel):
    """Machine-readable account of how a variant was normalized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm_version: str = NORMALIZATION_VERSION
    reference_used: bool = False
    reference_source: str | None = None
    #: True only when the representation is *proven* to sit at its leftmost
    #: equivalent position. False means "not proven" -- never "not leftmost".
    #: An unproven left-alignment for a pure indel clears :attr:`complete`.
    left_aligned: bool = False
    #: True when the alignment algorithm actually moved the position. An indel
    #: already at its leftmost position has ``left_aligned=True`` and
    #: ``left_shifted=False``; the two fields answer different questions.
    left_shifted: bool = False
    multiallelic_split: bool = False
    original_allele_index: int | None = None
    complete: bool = True
    warnings: list[NormalizationWarning] = Field(default_factory=list)

    def with_warning(
        self, code: str, message: str, *, severity: str = "warning") -> NormalizationReport:
        """Return a copy with an added warning (and ``complete`` cleared if fatal)."""
        warnings = [*self.warnings, NormalizationWarning(
            code=code, message=message, severity=severity)]
        return self.model_copy(
            update={"warnings": warnings, "complete": self.complete and severity != "abstain"}
        )


class NormalizedVariant(BaseModel):
    """A variant reduced to a stable, build-explicit identity.

    Immutable and hashable: this object is the join key between the VCF input,
    the evidence ledger, and the audit record.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    genome_build: GenomeBuild
    chromosome: str
    position: int = Field(ge=1)
    reference: str
    alternate: str
    variant_type: VariantType

    #: RefSeq accession for the chromosome in this build, if it is a primary contig.
    accession: str | None = None

    #: NCBI SPDI expression (``accession:interbase_start:deleted:inserted``).
    #: ``None`` when the accession is unknown (non-primary contig).
    spdi: str | None = None

    #: Genomic HGVS (``NC_000017.11:g.43082434G>A``). ``None`` without accession.
    hgvs_g: str | None = None

    #: Stable identity string: ``GRCh38|17|43082434|G|A``.
    identity: str = ""

    #: ``nga.v1.<sha512t24(identity)>`` — the primary stable variant identifier.
    variant_id: str = ""

    normalization: NormalizationReport = Field(default_factory=NormalizationReport)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def on_primary_contig(self) -> bool:
        return is_primary_contig(self.chromosome)

    def to_vcf_row(self) -> tuple[str, int, str, str]:
        """Return the normalized ``(chrom, pos, ref, alt)`` tuple."""
        return self.chromosome, self.position, self.reference, self.alternate


class RawVariant(BaseModel):
    """One VCF data line, before normalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chromosome: str
    position: int
    reference: str
    alternates: tuple[str, ...]
    record_id: str | None = None
    info: dict[str, str] = Field(default_factory=dict)
    line_number: int | None = None


# ---------------------------------------------------------------------------
# Allele validation and trimming
# ---------------------------------------------------------------------------


def _validate_allele(allele: str, *, what: str) -> str:
    if not allele:
        raise NormalizationError(f"{what} allele must not be empty.")
    upper = allele.upper()
    if upper in {".", "*"}:
        raise NormalizationError(
            f"{what} allele {allele!r} is a symbolic/missing allele. NGS-Agent normalizes "
            "explicit sequence only; symbolic alleles (SVs, spanning deletions) must be "
            "handled by a dedicated adapter."
        )
    invalid = sorted({base for base in upper if base not in _VALID_BASES})
    if invalid:
        raise NormalizationError(
            f"{what} allele {allele!r} contains non-IUPAC-ACGTN bases: {invalid}")
    return upper


def _minimal_trim(position: int, reference: str, alternate: str) -> tuple[int, str, str]:
    """Right-trim then left-trim common bases, as ``bcftools norm`` does."""
    ref, alt = reference, alternate
    while len(ref) > 1 and len(alt) > 1 and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
    while len(ref) > 1 and len(alt) > 1 and ref[0] == alt[0]:
        position += 1
        ref, alt = ref[1:], alt[1:]
    return position, ref, alt


def _classify(position: int, reference: str, alternate: str) -> VariantType:
    if len(reference) == 1 and len(alternate) == 1:
        return VariantType.SNV
    if len(reference) == len(alternate):
        return VariantType.MNV
    if len(reference) > len(alternate):
        # Pure deletion iff the alternate is a prefix of the reference.
        return VariantType.DELETION if reference.startswith(alternate) else VariantType.DELINS
    return VariantType.INSERTION if alternate.startswith(reference) else VariantType.DELINS


def _left_align_pure_indel(
    chromosome: str,
    position: int,
    reference: str,
    alternate: str,
    provider: SequenceProvider,
) -> tuple[int, str, str, bool, str | None]:
    """Left-align a pure insertion or deletion against ``provider``.

    Returns ``(position, reference, alternate, shifted, failure_reason)``.

    On any failure the **original** coordinates are returned unchanged. A
    partially shifted indel is neither the caller's input nor a proven
    left-aligned form, and emitting one would make two equivalent
    representations disagree — the exact property this module exists to
    guarantee.
    """
    if len(reference) == len(alternate):
        return position, reference, alternate, False, None

    deletion = len(reference) > len(alternate)
    # The VCF anchor base is reference[0] for a deletion and alternate[0] for
    # an insertion; the changed sequence follows it.
    changed = reference[1:] if deletion else alternate[1:]
    # 1-based coordinate of the first base of the changed sequence.
    start = position + 1
    shifted = False

    while start > 1:
        preceding = provider.fetch(chromosome, start - 1, start - 1)
        if preceding is None or not preceding:
            return position, reference, alternate, shifted, (
                f"reference base at {chromosome}:{start - 1} is unavailable, so left-alignment "
                "could not be completed"
            )
        base = preceding.upper()
        if base != changed[-1]:
            break
        changed = base + changed[:-1]
        start -= 1
        shifted = True

    if not shifted:
        return position, reference, alternate, False, None

    # Re-anchor at the new position: the anchor base is whatever the reference
    # holds immediately left of the shifted indel.
    new_position = start - 1
    anchor = provider.fetch(chromosome, new_position, new_position)
    if anchor is None or not anchor:
        return position, reference, alternate, shifted, (
            f"reference base at {chromosome}:{new_position} is unavailable, so the left-aligned "
            "anchor base could not be determined"
        )
    anchor = anchor.upper()
    if deletion:
        return new_position, anchor + changed, anchor, True, None
    return new_position, anchor, anchor + changed, True, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_variant(
    *,
    genome_build: GenomeBuild | str,
    chromosome: str,
    position: int,
    reference: str,
    alternate: str,
    sequence_provider: SequenceProvider | None = None,
    multiallelic_split: bool = False,
    allele_index: int | None = None,
) -> NormalizedVariant:
    """Normalize one biallelic variant into a :class:`NormalizedVariant`.

    ``sequence_provider`` is optional. Without it, SNVs and MNVs normalize
    completely, but a pure indel that is not already left-aligned cannot be
    proven minimal, so ``normalization.complete`` is ``False`` and an
    ``indel_left_alignment_unverified`` warning is attached. Callers must treat
    an incomplete normalization as a reason to abstain, not to proceed.
    """
    build = genome_build if isinstance(genome_build, GenomeBuild) else GenomeBuild(genome_build)
    chrom = normalize_chromosome(chromosome)
    if not isinstance(position, int) or position < 1:
        raise NormalizationError(f"Position must be a positive integer, got {position!r}.")

    ref = _validate_allele(reference, what="reference")
    alt = _validate_allele(alternate, what="alternate")
    if ref == alt:
        raise NormalizationError(
            f"Reference and alternate alleles are identical ({ref}); this is not a variant."
        )

    report = NormalizationReport(
        reference_used=sequence_provider is not None,
        reference_source=type(sequence_provider).__name__ if sequence_provider else None,
        multiallelic_split=multiallelic_split,
        original_allele_index=allele_index,
    )

    if not is_primary_contig(chrom):
        report = report.with_warning(
            "non_primary_contig",
            f"Contig {chrom!r} is not part of the primary assembly; identity is computed but "
            "no RefSeq accession, SPDI, or HGVS can be derived and classification will abstain.",
            severity="abstain",
        )

    pos, ref, alt = _minimal_trim(position, ref, alt)

    variant_type = _classify(pos, ref, alt)
    left_aligned = False
    left_shifted = False
    if variant_type in (VariantType.INSERTION, VariantType.DELETION):
        if sequence_provider is None:
            report = report.with_warning(
                "indel_left_alignment_unverified",
                "Indel left-alignment requires a reference sequence provider; none was "
                "supplied, so the normalized representation is minimal but not proven "
                "left-aligned. Equivalent representations in a repeat region may not "
                "collapse to the same identity.",
                severity="abstain",
            )
        else:
            pos, ref, alt, shifted, failure = _left_align_pure_indel(
                chrom, pos, ref, alt, sequence_provider
            )
            if failure is not None:
                report = report.with_warning(
                    "reference_unavailable_during_alignment",
                    failure,
                    severity="abstain",
                )
            else:
                variant_type = _classify(pos, ref, alt)
                left_aligned = True
                left_shifted = shifted
    elif variant_type is VariantType.DELINS:
        # Minimal trimming is deterministic, but a length-changing substitution
        # can still have an equivalent leftmost placement inside a repeat, and
        # the pure-indel shifter does not cover it. Say so instead of claiming a
        # proof we did not perform. This is a caveat, not a blocker.
        report = report.with_warning(
            "delins_left_alignment_unverified",
            "This allele is a length-changing substitution (delins). It was minimally "
            "trimmed, but left-alignment for delins is not implemented, so equivalent "
            "representations inside a repeat region may not collapse to the same identity.",
        )
    elif variant_type is VariantType.SNV:
        # A single-base substitution at a fixed position has no equivalent
        # leftward placement once minimally trimmed.
        left_aligned = True

    report = report.model_copy(update={"left_aligned": left_aligned, "left_shifted": left_shifted})

    identity = f"{build.value}|{chrom}|{pos}|{ref}|{alt}"
    accession = accession_for(build, chrom)
    spdi = f"{accession}:{pos - 1}:{ref}:{alt}" if accession else None
    hgvs_g = _genomic_hgvs(accession, pos, ref, alt, variant_type) if accession else None

    return NormalizedVariant(
        genome_build=build,
        chromosome=chrom,
        position=pos,
        reference=ref,
        alternate=alt,
        variant_type=variant_type,
        accession=accession,
        spdi=spdi,
        hgvs_g=hgvs_g,
        identity=identity,
        variant_id=f"{IDENTIFIER_NAMESPACE}.v1.{ga4gh_digest(identity)}",
        normalization=report,
    )


def _genomic_hgvs(
    accession: str, position: int, reference: str, alternate: str, variant_type: VariantType
) -> str:
    if variant_type is VariantType.SNV:
        return f"{accession}:g.{position}{reference}>{alternate}"
    if variant_type is VariantType.MNV:
        end = position + len(reference) - 1
        return f"{accession}:g.{position}_{end}delins{alternate}"
    if variant_type is VariantType.DELETION:
        deleted = reference[len(alternate) :]
        start = position + len(alternate)
        end = start + len(deleted) - 1
        return f"{accession}:g.{start}del" if start == end else f"{accession}:g.{start}_{end}del"
    if variant_type is VariantType.INSERTION:
        inserted = alternate[len(reference) :]
        before = position + len(reference) - 1
        return f"{accession}:g.{before}_{before + 1}ins{inserted}"
    end = position + len(reference) - 1
    locus = f"g.{position}" if position == end else f"g.{position}_{end}"
    return f"{accession}:{locus}delins{alternate}"


def split_multiallelic(raw: RawVariant) -> list[RawVariant]:
    """Expand a multiallelic record into one biallelic record per ALT allele.

    FORMAT/sample columns are *not* propagated: a multiallelic site's genotype
    and allelic depths cannot be attributed to a single split allele without
    re-genotyping, and pretending otherwise would put fabricated numbers into
    the evidence ledger.
    """
    if len(raw.alternates) <= 1:
        return [raw]
    return [
        raw.model_copy(update={"alternates": (alt,)})
        for alt in raw.alternates
        if alt not in {".", "*", ""}
    ]


def normalize_raw(
    raw: RawVariant,
    *,
    genome_build: GenomeBuild | str,
    sequence_provider: SequenceProvider | None = None,
) -> list[NormalizedVariant]:
    """Normalize every ALT allele of a raw VCF record."""
    alleles = split_multiallelic(raw)
    split = len(alleles) > 1
    results: list[NormalizedVariant] = []
    for index, allele_record in enumerate(alleles):
        results.append(
            normalize_variant(
                genome_build=genome_build,
                chromosome=allele_record.chromosome,
                position=allele_record.position,
                reference=allele_record.reference,
                alternate=allele_record.alternates[0],
                sequence_provider=sequence_provider,
                multiallelic_split=split,
                allele_index=index if split else None,
            )
        )
    return results
