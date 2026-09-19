# ADR-05 — Variant identity and normalization

**Status.** Accepted and implemented in `ngs_agent/core/normalization.py`,
`genome.py`, `vcf.py`. Algorithm version `norm-1.0.0`.

## Context

Every guarantee downstream depends on knowing *which allele* is being discussed.
Two failure modes dominate real pipelines:

1. **The same allele written differently.** `chr17` vs `17`, right- vs
   left-aligned indels, multiallelic records, GRCh37 vs GRCh38 coordinates. Two
   representations of one allele that do not match produce duplicate work and, at
   worst, evidence attached to the wrong variant.
2. **Different alleles that look the same.** Position and dbSNP id are *locus*
   identifiers, not allele identifiers.

The second is not hypothetical. At GRCh38 `chr17:43082434` — BRCA1 c.4327, both
under dbSNP `rs41293455` — ClinVar holds:

| Allele | SPDI | HGVS | ClinVar | Review |
|---|---|---|---|---|
| `G>A` | `NC_000017.11:43082433:G:A` | c.4327C>T p.Arg1443Ter | **Pathogenic** | expert panel, 2016-04-22 |
| `G>C` | `NC_000017.11:43082433:G:C` | c.4327C>G p.Arg1443Gly | **Benign** | expert panel, 2019-06-18 |

Same position, same rsid, opposite classification, both expert-panel reviewed.
Any lookup keyed on locus conflates a nonsense variant with a benign missense one.

## Decision

### Identity is a five-field string including the build

```
identity   = "GRCh38|17|43082434|G|A"
variant_id = "nga.v1." + ga4gh_digest(identity)     # e.g. nga.v1.WRtckd2ug5wbAjpBQub2-Hq62qLybM-X
```

The build is part of the identity, so `43082434` in GRCh38 and the corresponding
GRCh37 position can never be confused. `variant_id` is a stable, namespace-prefixed
digest suitable for use as a key; the human-readable identity is kept alongside it
because an opaque digest cannot be reviewed.

### Chromosome and build normalization is total and explicit

`normalize_chromosome` maps `chr17`/`CHR17`/`17` to `17`, and the legacy numeric
sex-chromosome conventions `chr23`→`X`, `chr24`→`Y`. `parse_genome_build` accepts
`GRCh37`/`hg19`/`b37` and `GRCh38`/`b38`.

**No build is ever inferred.** `parse_genome_build(None)` raises
`GenomeBuildError`:

> Genome build is required and must be explicit. Supply GRCh37 or GRCh38 …

The CLI once carried a duplicate resolver that defaulted to GRCh37
([audit B3](../audit/01-current-state-audit.md)). It was deleted; both paths now
call the single `resolve_build`. Guessing a build silently reinterprets every
coordinate in the file.

Non-primary contigs are recognized (`is_primary_contig`) and abstain rather than
being force-fitted to an accession
(`test_non_primary_contig_abstains`).

### Multiallelic records are split before anything else

`RawVariant.alternates` is a tuple; normalization emits one `NormalizedVariant`
per allele, recording `multiallelic_split` and `original_allele_index` so the
provenance of each allele is traceable back to its source line. Splitting first is
what makes the `chr17:43082434` case above work at all — the two alleles arrive as
two identities and are matched independently.

The shipped demo VCF lists the multiallelic record *first*, on purpose: a
duplicate allele appearing later in the file then exercises the split→dedupe path.

### `left_aligned` means "provably leftmost", not "we moved it"

This distinction is the subtlest part of the design, and conflating them would
make the flag worthless:

| Situation | `left_aligned` | `left_shifted` |
|---|---|---|
| SNV (trivially leftmost) | `True` | `False` |
| Indel already at the leftmost position of a repeat | `True` | `False` |
| Indel moved left by the algorithm | `True` | `True` |
| Indel, no reference sequence available | `False` | `False` |
| `delins` | `False` | — |

`left_aligned` is a claim that the position is correct; `left_shifted` is a record
that the algorithm changed it. An already-leftmost indel is `left_aligned=True,
left_shifted=False` — and a consumer that read `left_shifted` as "is aligned"
would wrongly discard it.

When alignment cannot be proven, that is disclosed rather than assumed:
`indel_left_alignment_unverified` and `delins_left_alignment_unverified` warnings
are attached, with `reference_unavailable_during_alignment` when no
`SequenceProvider` was supplied. Reference sequence is optional
(`reference_used`, `reference_source` on the report) because an air-gapped
deployment may not have one; the consequence is a disclosed limitation, not a
silent guess.

Golden test: `A>ACA` at positions 12, 18 and 20 in a `CACACA…` repeat all
normalize to `GRCh38|1|9|A|AAC` when a reference provider is supplied
(`test_indel_left_aligns_in_a_repeat_with_reference`, and the convergence test at
`test_normalization.py:221`).

### Evidence is matched by allele, and a locus-only match is a non-match

Matching order is canonical SPDI first, then coordinate span. A record found at
the right locus but the wrong allele is reported as `unavailable` — never adopted,
never used to satisfy a criterion. SPDI uses interbase (0-based) coordinates, so
`43082434` becomes `43082433` in SPDI while `hgvs_g` keeps the 1-based position;
both are emitted, and the difference is a known trap the goldens pin down.

### Incomplete normalization abstains

An indel with no reference sequence cannot be verified, so
`NormalizationReport.complete` is `False` and the variant abstains
(`test_incomplete_normalization_abstains`). Ambiguity produces a warning or an
abstention; it never produces a confident identity.

## Rejected alternatives

**Key on dbSNP rsid.** Rejected — `rs41293455` covers both alleles above, with
opposite classifications.

**Key on position plus gene.** Rejected — same objection, and gene symbols are
themselves unstable across releases.

**Infer the build from chromosome naming or coordinate ranges.** Rejected: the
inference is right often enough to be trusted and wrong often enough to be
catastrophic. `resolve_build` reads a declared build from the VCF header
(`assembly`/`reference` contig facts) or from an explicit flag, and raises
otherwise. The source of the resolved build is recorded (`explicit`,
`vcf_header`) so a reviewer can see whether an operator or the file asserted it.

**Always require a reference genome.** Rejected: it would make the engine
unusable in air-gapped deployments, which are the primary target. Instead the
absence is disclosed and the affected variants abstain.

**VRS as the only identity.** Not adopted yet. The five-field identity and SPDI
are VRS-compatible in substance, and `variant_id` is a namespaced digest, but the
contract does not yet emit full VRS descriptors. Tracked in
[item 10](../design/10-benchmark-and-roadmap.md).

## Consequences

**Gains.** One allele, one identity, regardless of how the VCF wrote it. Locus-vs-
allele confusion is structurally excluded. Identities are stable keys for caching,
audit and replay. Every normalization decision is disclosed on the record.

**Costs.**

* **Abstention on unresolvable indels** reduces throughput where no reference is
  configured. Deliberate.
* **The two alignment flags invite misuse.** Mitigated by documenting the table
  above in the model docstrings and testing all five combinations.
* **`--gene` interacts with identity in a dangerous way.** Gene symbols drive the
  PVS1 mechanism lookup, so a filter that *relabelled* variants would silently
  change classifications. That bug existed and was fixed
  ([audit B1](../audit/01-current-state-audit.md)): `--gene` filters only;
  `--gene-of-record` asserts, only when the VCF is unannotated, and never
  overrides an annotated symbol.

## Verification

`test_indel_left_aligns_in_a_repeat_with_reference`,
`test_snv_is_trivially_left_aligned`,
`test_delins_discloses_that_alignment_is_unverified`,
`test_incomplete_normalization_abstains`, `test_non_primary_contig_abstains`,
`test_a_different_position_is_never_a_match`,
`test_evidence_about_a_different_variant_is_a_different_record`,
`test_gene_filter_never_relabels_a_variant`,
`test_gene_of_record_does_not_override_an_annotated_gene`,
`test_gene_resolution_provenance_is_kept`,
`test_a_gene_we_could_not_validate_says_so`.

43 tests in `tests/core/test_normalization.py`, including the multi-allele golden
loci drawn from public-domain ClinVar records.

Invariant [I9](../audit/02-safety-invariants.md#i9-identity-is-explicit-and-a-locus-is-not-an-allele).
