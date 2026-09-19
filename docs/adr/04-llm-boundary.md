# ADR-04 — The LLM boundary: explanation only

**Status.** Accepted and implemented. Two layers were rewritten to enforce it:
`ngs_agent/core/explanation.py` (new) and `ngs_agent/debate.py` (rewritten from a
classifier into a narrative-only consultation).

## Context

Language models are genuinely useful here: a reviewer facing a dense contract
benefits from prose that says *what was found, what was missing, and what to check
next*. They are also unreliable in ways that matter enormously in this domain —
they fabricate plausible citations, invert negations, and disagree with
themselves between calls.

The legacy code put a model on the classification path: `_extract_acmg_codes`
scraped criterion codes out of prose and `_extract_stance` scraped the tier
([audit A2](../audit/01-current-state-audit.md)). Three personas repeating PM2
produced Likely Pathogenic ([audit A3](../audit/01-current-state-audit.md)).

The requirement is therefore not "use models carefully" but a structural
separation: **models may summarize and explain ledger evidence; they may never
create evidence or write to the classification engine.**

## Decision

### Ordering: the model runs last, on a finished decision

`explain_classification` receives a complete `VariantReviewResult` and returns a
*copy* with a narrative attached:

```python
def explain_classification(
    result: VariantReviewResult,
    backend: CompletionBackend,
    *, provider: str | None = None, model: str | None = None, now: datetime | None = None,
) -> tuple[VariantReviewResult, ExplanationBlock]:
    """Attach a model-generated narrative to a copy of ``result``.

    The input contract is never modified; ``classification`` in the returned
    contract is the same frozen object as in the input.
    """
```

There is no ordering in which model output could influence the tier, because the
tier is already computed, frozen, and hashed before the model is called. The
narrative lives in its own `ExplanationBlock` (`test_explanation_lives_in_its_own_block`).

### The backend is a two-method protocol, so models stay replaceable

```python
class CompletionBackend(Protocol):
    def complete(self, prompt: str, *, system: str | None = None) -> str: ...
```

It matches `ngs_agent.backends.base.LLMBackend`, so Anthropic, OpenAI-compatible,
Gemini and Ollama backends are reusable without the explanation layer depending
on any of them. Swapping or removing the model changes prose, never
classification — which is what "models are an optional, replaceable explanation
layer" has to mean in code.

### The prompt forbids the behaviour; the audit does not trust the prompt

The system prompt states six hard rules: describe only the supplied evidence and
criteria; never claim a criterion was applied unless it appears in
`applied_criteria`; cite only listed `evidence_id`s; state plainly what is missing
or conflicting; never give a clinical recommendation, probability, or confidence
score; end by restating research-use-only status.

Prompts are requests, not guarantees. So every reply is audited:

```python
def audit_explanation(
    text: str, *, applied_codes: set[str], known_evidence_ids: set[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Returns ``(boundary_violations, unsupported_citations)``."""
```

Two independent checks:

* **Boundary violations** — an ACMG code the engine did not apply. The pattern is
  deliberately wider than the published code ranges
  (`PVS\d+|PS\d+|PM\d+|PP\d+|BA\d+|BS\d+|BP\d+`, with optional strength
  modifiers), so a hallucinated `PS9` is flagged as `unknown_criterion:` rather
  than slipping through as an unrecognized-but-plausible code. A real code the
  engine did not apply is flagged `criterion_not_applied:`. The legacy pattern
  used published ranges and therefore passed inventions.
* **Unsupported citations** — anything shaped like an evidence id
  (`\bev\.v1\.[A-Za-z0-9_\-]{10,64}\b`) that is not in the ledger.

The check is intentionally blunt: a code inside a negation ("PVS1 was not
applied") is still flagged. False positives cost a reviewer a glance; a missed
fabrication costs them a wrong conclusion. That asymmetry is the design.

Both lists are fields on the emitted `ExplanationBlock`
(`boundary_violations`, `unsupported_citations`), so a violation is visible to
every consumer of the contract, not only in a terminal.

### The consultation command was defanged, not deleted

`ngsagent debate` became `ngsagent consult` (with a deprecated alias that
explains the rename and re-invokes). It is narrative-only:

* `PersonaOpinion` has `persona`, `reasoning` and `redactions` — **no `stance`,
  no `acmg_criteria`**. `DebateResult` has **no `classification`, no
  `consensus`, no `recommendation`**. There is no field in which a tier could be
  carried, so no consumer can read one by accident. This is stronger than
  validation: the state is unrepresentable.
* `redact_classification_language(text)` strips tier vocabulary, ACMG codes and
  strength modifiers, replacing them with an explicit marker
  (`[classification language removed: this command does not produce a
  classification]`), and returns the list of removals. Redaction is reported on
  the opinion and in the report — never silent.
* A missing backend raises rather than returning an empty consultation, because
  "the model produced nothing" must not read as "the model found nothing
  concerning".

## Rejected alternatives

**Keep the tier extraction and add a confidence threshold.** Rejected: any
threshold on scraped prose is uncalibrated, and the output still originates from
a model. There is no threshold at which this becomes evidence.

**Let models propose criteria that the engine then validates against the ledger.**
Rejected: the engine would still be accepting criterion *identity* from a model.
Validation can reject a bad proposal but cannot supply the missing observation,
and the proposal channel is exactly the entry point
[ADR-03](03-evidence-first-acmg.md) removes.

**Delete the consultation entirely.** Rejected: reviewers do find multi-persona
narrative useful for thinking through a VUS, and the guardrails make it safe. The
value is real; the classification was never legitimate.

**Fine-tune or prompt-engineer the model not to classify.** Rejected as the
*primary* control: it depends on a model version we do not control and cannot
test exhaustively. Prompting is kept as a first line, with redaction and audit as
the enforcement.

## Consequences

**Gains.** The classification is model-independent: with no backend configured at
all, `review`, `normalize`, `audit`, `replay` and `sign-off` work unchanged. A
model can be swapped, upgraded, or removed without re-validating the engine.
Fabrication is detected and reported rather than absorbed. Model output is
escaped in HTML reports, so a narrative cannot inject markup.

**Costs, accepted deliberately.**

* **Blunt detection produces false positives.** Explanations that correctly say
  "PVS1 was not applied" are flagged. We accept the noise; the flag list is
  advisory and the classification is unaffected.
* **Narrative quality is bounded by the ledger.** A model cannot fill gaps with
  general knowledge, so explanations of sparse evidence read sparse. That is
  accurate.
* **Two layers to maintain** (`explanation.py` for the signed path, `debate.py`
  for consultation), because their guarantees differ: one annotates a finished
  contract, the other must never be able to reach one.

## Verification

`test_a_model_cannot_add_a_criterion`, `test_a_model_cannot_change_the_label`,
`test_a_model_cannot_change_an_abstention`,
`test_explanation_cannot_change_the_classification`,
`test_explanation_lives_in_its_own_block`, `test_explanation_defaults_to_absent`,
`test_an_uncited_evidence_id_is_flagged`, `test_a_real_evidence_id_is_not_flagged`,
`test_a_classifying_model_is_redacted_and_reported`,
`test_the_redaction_is_visible_not_silent`,
`test_duplicate_redactions_are_collapsed`,
`test_a_well_behaved_model_produces_no_violations`,
`test_boundary_violations_are_recorded_on_the_block`,
`test_a_flagged_narrative_carries_a_stronger_disclaimer`,
`test_the_report_escapes_model_output`,
`test_a_missing_backend_is_an_error_not_an_empty_result`,
`test_no_command_produces_a_confidence_score`.

`tests/test_debate.py` opens by asserting the *absence* of the old behaviour —
no field that could carry a stance or criterion — before testing the replacement.

Invariant [I3](../audit/02-safety-invariants.md#i3-a-model-may-explain-it-may-never-create-evidence-or-classify).
