"""The optional LLM explanation layer — deliberately outside the signed path.

This module is the *only* place in ``ngs_agent.core`` that may touch a language
model, and it is built so that touching one cannot change a classification:

* It receives a completed :class:`VariantReviewResult`. It cannot receive a
  variant, an evidence record, or a criterion to "evaluate".
* Its prompt is assembled exclusively from ledger-derived facts. There is no
  parameter through which free text can enter the prompt.
* Its output is scanned. Any ACMG code the model mentions that the engine did
  not apply is recorded as a boundary violation; any evidence id it cites that
  is not in the ledger is recorded as an unsupported citation.
* The result is attached to a **copy** of the contract. The classification
  block, the criteria, and the evidence are structurally untouched — the models
  are frozen and ``model_copy`` cannot mutate them.

A model that hallucinates "PVS1 applied" therefore produces a contract that
says, in writing, that the model claimed PVS1 and the engine did not apply it.
That is the accountability layer: the fabrication is captured, not laundered.

If no backend is configured the layer is skipped entirely and
``explanation.present`` stays ``False``. Nothing downstream depends on it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from ngs_agent.core.acmg.criteria import CRITERIA
from ngs_agent.core.contract import ExplanationBlock, VariantReviewResult
from ngs_agent.core.evidence.models import utc_now
from ngs_agent.core.hashing import canonical_json_sha256

#: Any ACMG/AMP code, including strength-modified spellings such as PM2_supporting.
#: Matches anything shaped like an ACMG/AMP criterion code, *including codes
#: that do not exist*. The character classes are deliberately wider than the
#: published ranges (``PS[1-5]``, ``PM[1-7]``, ...): a model that invents "PS9"
#: or "BP12" is fabricating a criterion, and a pattern that only matches real
#: codes would let that fabrication through unremarked. Unknown codes are
#: reported as ``unknown_criterion:<code>`` by :func:`audit_explanation`.
_ACMG_CODE_PATTERN = re.compile(
    r"\b(PVS\d+|PS\d+|PM\d+|PP\d+|BA\d+|BS\d+|BP\d+)"
    r"(?:[_\s-]?(very\s*strong|strong|moderate|supporting))?\b",
    re.IGNORECASE,
)

_EVIDENCE_ID_PATTERN = re.compile(r"\bev\.v1\.[A-Za-z0-9_\-]{10,64}\b")


class CompletionBackend(Protocol):
    """The minimal LLM surface the explanation layer needs.

    Matches ``ngs_agent.backends.base.LLMBackend`` so the existing Anthropic,
    Ollama, Gemini, and OpenAI-compatible backends can be reused without the
    explanation layer depending on any of them.
    """

    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


SYSTEM_PROMPT = (
    "You are a scientific writing assistant inside NGS-Agent, a research-use-only variant review "
    "tool. You are NOT a variant classifier and you must not act as one.\n\n"
    "Hard rules:\n"
    "1. Describe ONLY the evidence and criteria listed in the structured input. Do not add "
    "evidence, criteria, frequencies, citations, or classifications of your own.\n"
    "2. Do not state or imply an ACMG/AMP criterion was applied unless it appears in "
    "'applied_criteria'. If you mention a criterion the engine did not apply, NGS-Agent records "
    "that as a boundary violation against you.\n"
    "3. Cite evidence by its evidence_id in square brackets, e.g. [ev.v1.xxxx]. Only cite ids "
    "present in 'evidence_ids'.\n"
    "4. State plainly what is missing, indeterminate, or conflicting.\n"
    "5. Never give a clinical recommendation, a management action, a probability, or a "
    "confidence score.\n"
    "6. End by restating that the result is research-use-only and requires human review.\n\n"
    "Write 150-250 words of plain prose."
)


class ExplanationInput(BaseModel):
    """The exact facts a model is allowed to see.

    Serialized and hashed; the hash is stored on the explanation so a reader can
    confirm which facts a narrative was generated from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1.0"
    variant: dict[str, Any]
    classification: dict[str, Any]
    applied_criteria: tuple[dict[str, Any], ...]
    indeterminate_criteria: tuple[dict[str, Any], ...]
    rejected_criteria: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    missing_evidence: tuple[str, ...]
    external_classifications: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    evidence_ids: tuple[str, ...]

    def to_prompt(self) -> str:
        return self.model_dump_json(indent=2)

    def prompt_hash(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


def build_explanation_input(result: VariantReviewResult) -> ExplanationInput:
    """Project a completed contract down to explainable facts.

    Deliberately excludes provenance internals, retrieval timings, and audit
    identifiers: they add nothing to a narrative and their absence keeps the
    prompt small enough to hash cheaply.
    """
    return ExplanationInput(
        variant=result.variant.model_dump(mode="json"),
        classification={
            key: value
            for key, value in result.classification.model_dump(mode="json").items()
            if key
            in {
                "label",
                "display_label",
                "abstained",
                "decision_state",
                "decision_basis",
                "requires_human_review",
                "rule_set",
                "concordance",
            }
        },
        applied_criteria=tuple(
            _criterion_facts(item) for item in result.applied_criteria
        ),
        indeterminate_criteria=tuple(
            _criterion_facts(item) for item in result.indeterminate_criteria
        ),
        rejected_criteria=tuple(_criterion_facts(item) for item in result.rejected_criteria),
        conflicts=tuple(item.model_dump(mode="json") for item in result.conflicts),
        missing_evidence=result.missing_evidence,
        external_classifications=tuple(
            item.model_dump(mode="json", exclude={"evidence_id"})
            for item in result.external_classifications
        ),
        evidence=tuple(
            {
                "evidence_id": record.evidence_id,
                "source": record.source.name,
                "source_version": record.source.version,
                "data_type": record.data_type.value,
                "status": record.status.value,
                "applicability": record.applicability.value,
                "verification": record.verification.value,
                "observed_value": record.observed_value,
                "limitations": list(record.limitations),
            }
            for record in result.evidence
        ),
        evidence_ids=tuple(record.evidence_id for record in result.evidence),
    )


def _criterion_facts(evaluation: Any) -> dict[str, Any]:
    return {
        "code": evaluation.code,
        "state": evaluation.state,
        "strength": evaluation.strength.value if evaluation.strength else None,
        "reason": evaluation.reason,
        "evidence_ids": list(evaluation.evidence_ids),
        "limitations": list(evaluation.limitations),
    }


def audit_explanation(
    text: str,
    *,
    applied_codes: set[str],
    known_evidence_ids: set[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Scan a model narrative for boundary violations and unsupported citations.

    Returns ``(boundary_violations, unsupported_citations)``.

    A boundary violation is a mention of an ACMG code that the engine did not
    apply. Mentions inside a negation ("PVS1 was not applied") are still
    flagged, because the check is deliberately blunt: a reviewer reading the
    narrative should see every code the model uttered and be told which ones
    the engine did not support. False positives here are cheap; a missed
    fabrication is not.
    """
    violations: list[str] = []
    for match in _ACMG_CODE_PATTERN.finditer(text or ""):
        code = match.group(1).upper()
        modifier = (match.group(2) or "").strip().lower().replace(" ", "_")
        rendered = f"{code}_{modifier}" if modifier else code
        if code not in CRITERIA:
            violations.append(f"unknown_criterion:{rendered}")
        elif code not in applied_codes:
            violations.append(f"criterion_not_applied:{rendered}")
    unsupported = sorted(
        {
            citation
            for citation in _EVIDENCE_ID_PATTERN.findall(text or "")
            if citation not in known_evidence_ids
        }
    )
    return tuple(dict.fromkeys(violations)), tuple(unsupported)


def explain_classification(
    result: VariantReviewResult,
    backend: CompletionBackend,
    *,
    provider: str | None = None,
    model: str | None = None,
    now: datetime | None = None,
) -> tuple[VariantReviewResult, ExplanationBlock]:
    """Attach a model-generated narrative to a copy of ``result``.

    Returns ``(new_result, explanation)``. The input contract is never
    modified; ``classification`` in the returned contract is the same frozen
    object as in the input.
    """
    facts = build_explanation_input(result)
    applied_codes = {item.code for item in result.applied_criteria}
    known_ids = set(facts.evidence_ids) | {
        evidence_id for item in result.applied_criteria for evidence_id in item.evidence_ids
    }

    metadata: dict[str, Any] = {
        "provider": provider or type(backend).__name__,
        "model": model,
        "prompt_hash": facts.prompt_hash(),
    }
    try:
        text = backend.complete(facts.to_prompt(), system=SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - a failed explanation must never fail a review
        block = ExplanationBlock(
            present=False,
            provider=metadata["provider"],
            model=model,
            prompt_hash=metadata["prompt_hash"],
            text=None,
            generated_at=now or utc_now(),
            boundary_violations=(f"explanation_unavailable:{type(exc).__name__}",),
            disclaimer=(
                "Explanation could not be generated. This does not affect the classification, "
                "which is produced entirely by the deterministic engine."
            ),
        )
        return result.model_copy(update={"explanation": block}), block

    violations, unsupported = audit_explanation(
        text, applied_codes=applied_codes, known_evidence_ids=known_ids
    )
    disclaimer = ExplanationBlock.model_fields["disclaimer"].default
    if violations or unsupported:
        disclaimer = (
            "Generated by a language model from the evidence ledger. This narrative is not "
            "evidence and did not contribute to the classification. NGS-Agent detected claims "
            "in it that the evidence ledger does not support; see boundary_violations and "
            "unsupported_citations. Do not rely on this text."
        )

    block = ExplanationBlock(
        present=True,
        provider=metadata["provider"],
        model=model,
        prompt_hash=metadata["prompt_hash"],
        text=text,
        cited_evidence_ids=tuple(
            citation for citation in _EVIDENCE_ID_PATTERN.findall(text or "") if citation
                in known_ids
        ),
        unsupported_citations=unsupported,
        boundary_violations=violations,
        generated_at=now or utc_now(),
        disclaimer=disclaimer,
    )
    annotated = result.model_copy(update={"explanation": block})
    return annotated, block


def explanation_model_metadata(block: ExplanationBlock) -> dict[str, Any] | None:
    """Provenance payload for ``provenance.model_metadata`` when a model was used."""
    if not block.present and not block.boundary_violations:
        return None
    return {
        "role": "explanation_only",
        "contributed_to_classification": False,
        "provider": block.provider,
        "model": block.model,
        "prompt_hash": block.prompt_hash,
        "boundary_violations": list(block.boundary_violations),
        "unsupported_citations": list(block.unsupported_citations),
        "generated_at": block.generated_at.isoformat() if block.generated_at else None,
    }
