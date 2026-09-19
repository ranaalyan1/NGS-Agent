"""Exact numeric quantities for the evidence ledger.

The ledger forbids floats (see :mod:`ngs_agent.core.hashing`) because a float's
serialized form is not guaranteed stable across builds, and an
``evidence_id`` that changes between runs destroys reproducibility.

Allele frequencies are the awkward case: every upstream source publishes them
as decimals. Rather than lose precision or smuggle floats in, frequencies are
carried as an :class:`ExactRatio` — the allele count over the allele number the
source actually reported. That is both exact and *more* informative than the
decimal, because a frequency of 1/20 with 20 alleles observed is not the same
evidence as 1/20000 with 20000 alleles observed, and ACMG/AMP frequency
criteria depend on that distinction.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.errors import EvidenceValidationError


class ExactRatio(BaseModel):
    """A non-negative rational number stored as an exact integer pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    numerator: int = Field(ge=0)
    denominator: int = Field(gt=0)

    @property
    def value(self) -> Decimal:
        """Exact decimal value, quantized to 12 significant places.

        Returned as :class:`decimal.Decimal`, never as ``float``, so callers
        cannot accidentally reintroduce binary rounding into the ledger.
        """
        return (Decimal(self.numerator) / Decimal(self.denominator)).quantize(Decimal("1E-12"))

    def as_decimal_string(self) -> str:
        return str(self.value.normalize())

    def __lt__(self, other: ExactRatio) -> bool:
        return (
            Fraction(self.numerator, self.denominator) < Fraction(other.numerator,
                other.denominator)
        )

    def __le__(self, other: ExactRatio) -> bool:
        return (
            Fraction(self.numerator, self.denominator) <= Fraction(other.numerator,
                other.denominator)
        )

    def __gt__(self, other: ExactRatio) -> bool:
        return (
            Fraction(self.numerator, self.denominator) > Fraction(other.numerator,
                other.denominator)
        )

    def __ge__(self, other: ExactRatio) -> bool:
        return (
            Fraction(self.numerator, self.denominator) >= Fraction(other.numerator,
                other.denominator)
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ExactRatio):
            return NotImplemented
        return (
            Fraction(self.numerator, self.denominator) == Fraction(other.numerator,
                other.denominator)
        )

    def __hash__(self) -> int:
        return hash(Fraction(self.numerator, self.denominator))


def ratio_from_decimal_string(text: str) -> ExactRatio:
    """Convert ``"0.00002"`` into an exact ratio (``2/100000``).

    Accepts the decimal string forms upstream APIs actually return. Rejects
    anything that is not a finite, non-negative decimal — including ``nan``,
    ``inf``, and scientific-notation strings that would silently lose meaning.
    """
    try:
        decimal = Decimal(str(text).strip())
    except (InvalidOperation, ValueError) as exc:
        raise EvidenceValidationError(f"{text!r} is not a decimal number") from exc
    if not decimal.is_finite():
        raise EvidenceValidationError(f"{text!r} is not a finite decimal")
    if decimal < 0:
        raise EvidenceValidationError(f"{text!r} is negative; frequencies cannot be negative")
    fraction = Fraction(decimal)
    return ExactRatio(numerator=fraction.numerator, denominator=fraction.denominator)


def ratio_from_counts(allele_count: int, allele_number: int) -> ExactRatio:
    """Build a ratio from the AC/AN pair a frequency source reports."""
    if allele_number <= 0:
        raise EvidenceValidationError(
            f"allele number must be positive to compute a frequency, got {allele_number}"
        )
    if allele_count < 0:
        raise EvidenceValidationError(f"allele count cannot be negative, got {allele_count}")
    if allele_count > allele_number:
        raise EvidenceValidationError(
            f"allele count {allele_count} exceeds allele number {allele_number}"
        )
    return ExactRatio(numerator=allele_count, denominator=allele_number)


#: Threshold used for ACMG BA1 ("allele frequency > 5%").
BA1_THRESHOLD = ratio_from_decimal_string("0.05")

#: Default rarity threshold used for PM2_supporting.
#:
#: This is an *implementation* default, not a ClinGen universal rule: ClinGen
#: SVI recommends PM2 be applied at supporting strength but does not publish a
#: single disease-independent frequency cutoff. Gene- and disease-specific
#: thresholds must come from a ClinGen VCEP specification, and until one is
#: configured this default is reported as a limitation on every criterion that
#: uses it.
DEFAULT_PM2_RARITY_THRESHOLD = ratio_from_decimal_string("0.0001")
