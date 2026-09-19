# 01 — Current-State Audit

**Scope.** What NGS-Agent did before the evidence-backed engine existed, why
those behaviours were unsafe for variant review, and — in the second half — the
defects found in the *new* code while building it. An audit that only lists the
old code's sins is marketing; this one includes our own.

Legacy line references are to commit `d95cd9d` (the branch point), so they can
be checked with `git show d95cd9d:<path>`.

---

## Part A — What the legacy classification path did

### A1. The classifier's entire input was a list of strings

```python
# ngs_agent/acmg.py @ d95cd9d:146
def compute_acmg_classification(criteria_codes: list[str]) -> ACMGEvaluation:
    codes = [c.upper().strip() for c in criteria_codes if c.upper().strip() in ACMG_CRITERIA]
```

No variant. No genome build. No transcript. No source, no source version, no
retrieval timestamp. The function could not distinguish a criterion backed by a
3-star ClinVar record from a criterion backed by a language model's prose, because
it never saw either — it saw `"PM2"`.

Its output carried the same poverty:

```python
# ngs_agent/acmg.py @ d95cd9d:139
@dataclass
class ACMGEvaluation:
    codes: list[str]
    classification: str = "VUS"
    explanation: str = ""
    confidence: float = 0.0
```

Four fields. Nothing downstream could ask *"which evidence supports this?"*
because the answer was not representable.

**Consequence.** The result was unfalsifiable and unreplayable. Given only
`["PVS1", "PM2"]` there is no way to re-derive the decision later, because the
inputs that produced those codes were never recorded.

### A2. Criteria were read out of LLM prose with a regular expression

```python
# ngs_agent/debate.py @ d95cd9d:178
def _extract_acmg_codes(text: str) -> list[str]:
    codes = re.findall(r"\b(PVS1|PS[1-4]|PM[1-6]|PP[1-5]|BA1|BS[1-4]|BP[1-7])\b", text, re.I)
```

and, separately, the tier itself:

```python
# ngs_agent/debate.py @ d95cd9d:114
def _extract_stance(text: str) -> str:
    explicit_match = re.search(
        r"(?:stance|verdict|classification|conclusion)\s*[:=-]\s*(likely\s+pathogenic|pathogenic|...)",
        text, re.I)
```

A model that *mentioned* PVS1 — including one saying "PVS1 does not apply
here" — contributed PVS1. The regex has no negation handling and no notion of
assertion versus discussion. The extracted stance then drove
`_build_consensus` → `"All personas lean pathogenic."` → a recommendation shown
to the user.

This is the direct violation of the project's central rule: a model was the
origin of ACMG criteria and of the tier.

### A3. Duplicate criteria inflated the classification

`_extract_acmg_codes` de-duplicated within one persona's reply
(`dict.fromkeys`), but the caller concatenated across personas without
de-duplicating:

```python
# ngs_agent/debate.py @ d95cd9d:89
codes = _extract_acmg_codes(text)
all_codes.extend(codes)          # three personas → three copies of PM2
```

and the engine counted occurrences, not distinct codes:

```python
pm = sum(1 for c in codes if c.startswith("PM"))     # d95cd9d:152
```

So `compute_acmg_classification(['PM2','PM2','PM2'])` returned **Likely
Pathogenic** from one criterion, mentioned three times. Agreement between
models was treated as accumulating evidence. It is not: three personas trained
on overlapping text repeating PM2 is one claim, stated thrice.

### A4. Hardcoded, uncalibrated confidence scores

Every branch returned a literal:

| Classification | `confidence` | Source |
|---|---|---|
| Benign (BA1) | `0.99` | `d95cd9d:168` |
| Pathogenic | `0.95` | `d95cd9d:182`, `:207` |
| Likely Pathogenic | `0.90` | `d95cd9d:198` |
| Likely Benign | `0.90` | `d95cd9d:220` |
| VUS | `0.75` | `d95cd9d:227` |

These numbers were constants attached to a tier, not measurements. They varied
with nothing: not evidence quality, not conflict, not review status. A VUS
supported by one weak record and a VUS supported by no evidence at all both
scored `0.75`. A reader would reasonably interpret `0.95` as a calibrated
probability. It was a label with a decimal point.

### A5. Failed model calls produced a confident, exit-0 report

With no backend reachable, the loop collected per-persona errors, then still
built a consensus, still wrote the HTML report, and still exited 0
(`BUGS_FOUND.md` B1). A total failure was indistinguishable from a successful
consultation that found nothing concerning — the most dangerous possible
confusion in this domain.

### A6. Contradiction was resolved by silent downgrade

```python
if is_pathogenic and not has_benign:      # d95cd9d:177
```

When pathogenic and benign criteria were both present, the branch was skipped
and execution fell through to `VUS` with the boilerplate *"exhibit conflicting
evidence"*. The right tier, but the conflict was never surfaced as a conflict:
no record of which sources disagreed, no flag for a reviewer, no distinction
between "insufficient evidence" and "evidence contradicts itself". Those are
different situations requiring different human responses.

### A7. Invented codes were dropped silently

The whitelist filter at `d95cd9d:148` discarded anything not in
`ACMG_CRITERIA`. A model hallucinating `PS9` produced no error and no record —
the fabrication was invisible. The new code does the opposite: an unrecognized
code is reported as a boundary violation
(`test_an_uncited_evidence_id_is_flagged`, `test_a_model_cannot_add_a_criterion`).

---

## Part B — Defects found in the new code while building it

These were introduced by this work and caught by it. They are recorded because
the patterns are the ones most likely to recur.

### B1. `--gene` relabelled variants instead of filtering them 🔴

The most serious defect found in the new code. `_resolve_gene` returned the
CLI-supplied symbol as the *gene of record*, and the filter then compared that
value to itself — so it always matched. Result: `--gene TP53` against a BRCA1
VCF returned every variant, each now labelled TP53.

Gene identity is not cosmetic. It selects the disease mechanism used for the
PVS1 loss-of-function applicability check. Relabelling BRCA1 as TP53 would
evaluate a breast-cancer gene against TP53's mechanism — a wrong classification
produced by a *filter* flag, with no error anywhere.

Fix: filtering and assertion are now separate parameters. `--gene` filters
against the resolved gene and never writes to it; `--gene-of-record` asserts a
symbol only when the VCF carries no annotation, and is recorded as
`resolved_from="cli"` with an operator-assertion note. An assertion never
overrides an annotated symbol.

Proven by `test_gene_filter_never_relabels_a_variant`,
`test_gene_of_record_does_not_override_an_annotated_gene`,
`test_gene_filter_excludes_other_genes`.

### B2. `--max-variants` capped VCF records, not alleles

A multiallelic record expands to several alleles, so a cap of 1 returned 2
results. A limit that silently does not limit is worse than no limit, because an
operator believes they bounded the run. Fixed by moving the check inside the
per-allele loop; `test_max_variants_caps_the_run` and
`test_max_variants_is_respected_for_a_multiallelic_first_record` cover it.

### B3. The CLI guessed a genome build 🔴

The pipeline correctly refuses to proceed when no build is declared, but the CLI
carried a duplicate resolver that defaulted to GRCh37. Coordinates interpreted
in the wrong build point at entirely different variants — a silent,
catastrophic misidentification. The duplicate was deleted; both paths now call
one `resolve_build`, which raises.

### B4. Machine-readable JSON was word-wrapped by the pretty-printer

JSON contracts were emitted through a rich `Console`, which wraps at terminal
width by inserting newlines *inside string values*. The output was valid to a
human and unparsable by a machine, and the corruption depended on terminal
width — so it appeared and disappeared between environments. Anything a program
must parse now goes through `click.echo`; rich is confined to stderr.
`test_stdout_is_byte_exact_json` guards it.

### B5. Limitations from unresolved criteria never reached the contract

`_outcome_limitations` iterated only `applied` criteria, so a criterion that was
*indeterminate* or *rejected* — precisely the cases where a reviewer most needs
to know why — contributed no limitation text. The contract looked cleaner than
reality. Fixed by passing `unresolved=[*indeterminate, *rejected]`.

This is the subtlest defect in the list: every test passed, the output was well
formed, and it was still dishonest by omission.

### B6. An adopted classification was reported as "discordant"

When the engine adopts an authoritative external classification — a ≥3-star
ClinVar record — it forms no independent opinion, and the code says so:

```python
# Discordance is only a *conflict* when NGS-Agent actually formed an
# independent opinion. With no applied criteria there is nothing to
# contradict an authoritative source; that case adopts it instead.
```

The conflict record was correctly suppressed, but `concordance` was computed
*before* that guard, comparing the fallback `uncertain_significance` against the
external tier. So the contract emitted `label="pathogenic"`,
`decision_basis="authoritative_external_classification"` and
`concordance="discordant"` — the engine reporting that it disagreed with the very
source it had just adopted.

A reviewer reading that contract would see a self-contradiction where none exists.
Fixed by returning `not_comparable` when no criteria were applied: adoption is
neither corroboration nor contradiction.

Proven by `test_an_adopted_classification_is_not_reported_as_discordant`,
alongside the genuine case in
`test_contradicting_external_and_criteria_is_a_blocking_conflict`.

Like B5, this passed every test and produced well-formed output. Both are the same
species of defect: a field whose value is technically computed but semantically
wrong, which no consumer would think to question.

### B7. Two smaller correctness faults

* `import html` in `reports.py` was shadowed by a local variable named `html` in
  `generate_html_report`, so escaping calls raised `UnboundLocalError`. Now
  `from html import escape as html_escape`.
* A rich markup closing tag `[/bold]` did not match its opening
  `[bold red]`, raising `MarkupError` on an error path — meaning the error
  handler crashed instead of reporting. Error paths deserve tests precisely
  because they are rarely executed.

---

## What the audit changed

| Legacy behaviour | Replacement | Item |
|---|---|---|
| Criteria from LLM prose | Criteria derived only from structured evidence records | [03](../adr/03-evidence-first-acmg.md) |
| `_extract_stance` tier from prose | Models produce narrative only; tier vocabulary is redacted and reported | [04](../adr/04-llm-boundary.md) |
| Duplicate codes counted | `count_strengths()` counts a *set* of codes | [07](../adr/07-deterministic-engine.md) |
| Hardcoded `confidence` | No confidence or probability field exists anywhere in the contract | [08](../adr/08-result-contract.md) |
| `["PM2"]` as input | Variant identity, build, transcript, source, version, retrieval time | [05](../adr/05-variant-identity.md), [06](../adr/06-evidence-ledger.md) |
| Silent VUS on contradiction | Explicit conflict records + abstention, distinct from insufficient evidence | [07](../adr/07-deterministic-engine.md), [08](../adr/08-result-contract.md) |
| Exit 0 on total failure | Exit 1, no report written | `BUGS_FOUND.md` B1 |
| No history | Append-only audit chain, sign-off, replay | [09](../adr/09-audit-replay-signoff.md) |

`ngs_agent/acmg.py` and `tests/test_acmg.py` were deleted rather than adapted.
Keeping a second, weaker classifier reachable from any import path would have
made the safety guarantees in item [02](02-safety-invariants.md) unenforceable.
