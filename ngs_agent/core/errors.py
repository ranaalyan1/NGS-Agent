"""Exception hierarchy for the core engine.

Every failure mode that must not be silently swallowed has its own type, so
callers can distinguish "the source has no record for this variant" (which is
a *result*, not an error) from "we could not reach the source" (which is an
error that must never be reported as negative evidence).
"""

from __future__ import annotations


class CoreError(Exception):
    """Base class for all core-engine failures."""


class NonCanonicalValueError(CoreError, ValueError):
    """A value that cannot be hashed deterministically was supplied."""


class GenomeBuildError(CoreError, ValueError):
    """An unknown, missing, or ambiguous genome build was supplied."""


class NormalizationError(CoreError, ValueError):
    """A variant record could not be normalized (malformed allele, bad position)."""


class UnknownGeneError(CoreError, ValueError):
    """A gene symbol could not be validated against the curated gene table."""


class EvidenceValidationError(CoreError):
    """An adapter returned a payload that failed structural validation.

    This is *not* the same as "no evidence". A validation failure means the
    source responded with something we cannot trust, and the result must be
    recorded as ``invalid`` — never converted into a benign or absent call.

    Deliberately **not** a :class:`ValueError`. Pydantic catches ``ValueError``
    and ``AssertionError`` raised inside a validator and re-raises them as an
    opaque ``pydantic.ValidationError``. The evidence-record invariants below
    are the safety floor of this system ("missing data may never be encoded as
    a value"), so callers must be able to catch them by name. Any other
    exception type propagates out of a pydantic validator unchanged.
    """


class AdapterError(CoreError):
    """An evidence adapter failed (transport, decoding, rate limiting)."""


class RuleSetError(CoreError, ValueError):
    """An unknown or internally inconsistent ACMG rule set was requested."""


class ContractError(CoreError):
    """A result contract violated its own invariants.

    Not a :class:`ValueError`, for the same reason as
    :class:`EvidenceValidationError`: these are raised from pydantic validators
    and must survive the trip to the caller as themselves.
    """


class AuditError(CoreError):
    """An audit record could not be written, read, or replayed."""


class ReplayDivergenceError(AuditError):
    """Replaying an audit record produced a different classification.

    This is a hard failure of the reproducibility guarantee. It should never
    happen; if it does, the divergence is the bug and the audit record is the
    evidence.
    """


class BoundaryViolationError(CoreError):
    """An attempt was made to write into the signed classification path
    from outside it (for example, from an LLM explanation layer).
    """


class ReviewRequiredError(CoreError):
    """A clinical-facing action was attempted without human sign-off."""
