"""Human review and sign-off.

NGS-Agent is research-use-only. No result this package produces is a clinical
decision, and the sign-off workflow exists to make that structurally true
rather than merely stated:

* A result starts ``pending``. Interfaces must render it as unreviewed.
* Signing produces a **new** contract. The unsigned contract is never mutated,
  so the artefact a reviewer saw and the artefact that was signed are both
  recoverable.
* An override — a reviewer's classification that differs from the engine's
  label — requires a written reason. There is no code path that records an
  override without one.
* Rejecting a result requires a reason too. "Rejected" with no explanation is
  not an auditable event.

Two orthogonal things are recorded, and keeping them apart is what makes the
audit trail readable:

``action``
    What the reviewer did with *this result*: approve it for release, reject it
    back for rework, or request another reviewer.

``decision``
    What the reviewer concludes the *variant* is, in the ACMG/AMP five-tier
    space. On approval it defaults to the engine label; anything else is an
    override.

This module performs no I/O. Persistence is the audit layer's job
(:func:`ngs_agent.core.audit.record_sign_off`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ngs_agent.core.contract import ReviewBlock, VariantReviewResult
from ngs_agent.core.errors import ReviewRequiredError
from ngs_agent.core.evidence.models import utc_now

#: The five ACMG/AMP tiers a reviewer may record as their decision.
TIERS: tuple[str, ...] = (
    "pathogenic",
    "likely_pathogenic",
    "uncertain_significance",
    "likely_benign",
    "benign",
)

ReviewerAction = Literal["approve", "reject", "request_review"]

_STATUS_BY_ACTION: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
    "request_review": "review_requested",
}

#: Roles permitted to sign when role enforcement is enabled. The default
#: deliberately excludes roles that cannot take clinical responsibility for a
#: variant classification.
DEFAULT_PERMITTED_ROLES: tuple[str, ...] = (
    "clinical_laboratory_scientist",
    "clinical_geneticist",
    "certified_variant_scientist",
    "laboratory_director",
    "pathologist",
)


class ReviewDecision(BaseModel):
    """A human decision about one result contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer: str = Field(min_length=1)
    action: ReviewerAction
    #: The reviewer's ACMG/AMP tier. ``None`` on approval means "I agree with
    #: the engine"; on rejection it may still be supplied.
    decision: str | None = None
    reviewer_role: str | None = None
    #: Mandatory whenever ``decision`` differs from the engine label.
    override_reason: str | None = None
    #: Mandatory on rejection.
    notes: str | None = None
    signed_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> ReviewDecision:
        if not self.reviewer.strip():
            raise ReviewRequiredError("A sign-off requires a reviewer identity.")
        if self.decision is not None and self.decision not in TIERS:
            raise ReviewRequiredError(
                f"Reviewer decision {self.decision!r} is not one of the five ACMG/AMP tiers "
                f"({', '.join(TIERS)})."
            )
        if self.action == "reject" and not (self.notes or "").strip():
            raise ReviewRequiredError(
                "Rejecting a result requires a written reason in 'notes'; an unexplained "
                "rejection is not an auditable event."
            )
        if self.override_reason is not None and not self.override_reason.strip():
            raise ReviewRequiredError("An override reason must not be blank.")
        return self


def sign_off(
    result: VariantReviewResult,
    decision: ReviewDecision,
    *,
    permitted_roles: tuple[str, ...] = DEFAULT_PERMITTED_ROLES,
    enforce_role: bool = False,
    now: datetime | None = None,
) -> VariantReviewResult:
    """Return a new contract carrying the reviewer's decision.

    ``enforce_role`` is off by default so a deployment can adopt the workflow
    before wiring up its directory of qualified reviewers. Either way the role
    is *recorded*, so an unqualified sign-off is visible in the audit trail
    rather than silently accepted.
    """
    if enforce_role and (
        decision.reviewer_role is None or decision.reviewer_role not in permitted_roles
    ):
        raise ReviewRequiredError(
            f"Reviewer role {decision.reviewer_role!r} is not permitted to sign off. "
            f"Permitted roles: {', '.join(permitted_roles)}."
        )

    engine_label = result.classification.label
    reviewer_tier = decision.decision or engine_label
    is_override = reviewer_tier != engine_label
    override_reason = decision.override_reason

    if is_override and not override_reason:
        raise ReviewRequiredError(
            f"Reviewer decision {reviewer_tier!r} differs from the engine classification "
            f"{engine_label!r}. An override requires an explicit written reason; NGS-Agent does "
            "not record silent overrides."
        )
    if decision.action == "request_review" and is_override:
        raise ReviewRequiredError(
            "A request-for-review action cannot carry a classification override; only a reviewer "
            "who signs the result may override it."
        )

    review = ReviewBlock(
        status=_STATUS_BY_ACTION[decision.action],  # type: ignore[arg-type]
        reviewer=decision.reviewer,
        reviewer_role=decision.reviewer_role,
        signed_at=decision.signed_at or now or utc_now(),
        decision=reviewer_tier,
        override_reason=override_reason if is_override else None,
        notes=decision.notes,
    )

    signed = result.model_copy(update={"review": review})
    if is_override:
        # An override does not rewrite the engine's classification. The engine
        # label stays exactly where it was; the reviewer's conclusion sits
        # alongside it in ``review`` and in the limitations list.
        signed = signed.model_copy(
            update={
                "limitations": (
                    *result.limitations,
                    f"HUMAN OVERRIDE: {decision.reviewer} recorded {reviewer_tier!r} where the "
                    f"engine derived {engine_label!r}. Reason: {override_reason}",
                )
            }
        )
    return signed


def require_signed(result: VariantReviewResult) -> VariantReviewResult:
    """Gate for any clinical-facing consumer.

    Raises unless a human has signed. Interfaces that render a result for
    clinical consumption must call this; interfaces that render for research
    review must not, and must show the ``pending`` state prominently instead.
    """
    if not result.review.is_signed:
        raise ReviewRequiredError(
            "This result has not been signed off by a human reviewer "
            f"(review.status={result.review.status!r}). NGS-Agent is research-use-only and does "
            "not release unsigned classifications to clinical-facing consumers."
        )
    return result
