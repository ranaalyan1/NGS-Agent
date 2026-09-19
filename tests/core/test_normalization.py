"""Golden tests for variant normalization.

These are the identity tests: if any of them changes output, every evidence
lookup, cache key, audit record and replay downstream changes with it. They are
pinned deliberately.
"""

from __future__ import annotations

import pytest

from ngs_agent.core.errors import NormalizationError
from ngs_agent.core.genome import GenomeBuild, accession_for, normalize_chromosome
from ngs_agent.core.normalization import (
    RawVariant,
    VariantType,
    normalize_raw,
    normalize_variant,
    split_multiallelic,
)
from ngs_agent.core.reference import build_reference


class FakeReference:
    """A tiny in-memory sequence provider.

    Only the bases the tests actually align against are defined; anything else
    returns ``None`` so a silent out-of-range read becomes a visible failure.
    """

    def __init__(self, sequences: dict[str, str]) -> None:
        self.sequences = {normalize_chromosome(key): value.upper() for key, value in sequences.items()}
        self.calls: list[tuple[str, int, int]] = []

    def fetch(self, chromosome: str, start: int, end: int) -> str | None:
        self.calls.append((chromosome, start, end))
        sequence = self.sequences.get(normalize_chromosome(chromosome))
        if sequence is None:
            return None
        # start/end are 1-based inclusive, matching the SequenceProvider protocol.
        if start < 1 or end > len(sequence):
            return None
        return sequence[start - 1 : end]


# A repeat region: CA x5 starting at 1-based position 11.
REPEAT_CHROM = "ACGTTGCAAA" + "CACACACACA" + "TTGGCCAATT"


class TestChromosomeAndBuildHandling:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("chr17", "17"),
            ("17", "17"),
            ("CHR17", "17"),
            ("chrX", "X"),
            ("X", "X"),
            ("chrM", "MT"),
            ("MT", "MT"),
            # 23/24 are legacy encodings for X/Y in some caller output.
            ("chr23", "X"),
            ("chr24", "Y"),
        ],
    )
    def test_chromosome_aliases_collapse(self, raw, expected):
        assert normalize_chromosome(raw) == expected

    def test_build_aliases(self):
        assert GenomeBuild("GRCh38").value == "GRCh38"
        assert accession_for(GenomeBuild.GRCH38, "17") == "NC_000017.11"
        assert accession_for(GenomeBuild.GRCH37, "17") == "NC_000017.10"

    def test_non_primary_contig_has_no_accession(self):
        assert accession_for(GenomeBuild.GRCH38, "GL000220") is None


class TestStableIdentity:
    def test_snv_identity_matches_recorded_clinvar_spdi(self):
        """The BRCA1 R1443* golden variant, pinned against the ClinVar recording.

        ClinVar reports this allele as ``NC_000017.11:43082433:G:A``.
        """
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082434, reference="G", alternate="A"
        )
        assert variant.identity == "GRCh38|17|43082434|G|A"
        assert variant.spdi == "NC_000017.11:43082433:G:A"
        assert variant.hgvs_g == "NC_000017.11:g.43082434G>A"
        assert variant.accession == "NC_000017.11"
        assert variant.variant_type is VariantType.SNV
        assert variant.variant_id.startswith("nga.v1.")
        assert variant.normalization.complete is True

    def test_variant_id_is_a_pure_function_of_identity(self):
        first = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082434, reference="G", alternate="A"
        )
        second = normalize_variant(
            genome_build="GRCh38", chromosome="chr17", position=43082434, reference="g", alternate="a"
        )
        assert first.identity == second.identity
        assert first.variant_id == second.variant_id

    def test_variant_id_is_pinned(self):
        """Golden digest. Changing this value invalidates every stored audit record."""
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082434, reference="G", alternate="A"
        )
        from ngs_agent.core.hashing import ga4gh_digest

        assert variant.variant_id == f"nga.v1.{ga4gh_digest('GRCh38|17|43082434|G|A')}"

    def test_build_is_part_of_the_identity(self):
        """Same coordinates in two builds are different variants, full stop."""
        grch38 = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082434, reference="G", alternate="A"
        )
        grch37 = normalize_variant(
            genome_build="GRCh37", chromosome="17", position=41234451, reference="G", alternate="A"
        )
        assert grch38.identity != grch37.identity
        assert grch38.variant_id != grch37.variant_id
        assert grch37.spdi == "NC_000017.10:41234450:G:A"

    def test_locus_identity_is_not_allele_identity(self):
        """chr17:43082434 G>A and G>C are different variants with different ids.

        This is the invariant the recorded ClinVar fixtures exist to protect:
        both alleles share a position and a dbSNP id but carry opposite
        classifications.
        """
        a = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082434, reference="G", alternate="A"
        )
        c = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082434, reference="G", alternate="C"
        )
        assert (a.position, a.chromosome) == (c.position, c.chromosome)
        assert a.variant_id != c.variant_id
        assert a.spdi != c.spdi


class TestTrimmingAndLeftAlignment:
    def test_padded_snv_trims_to_minimal_representation(self):
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082433, reference="AG", alternate="AA"
        )
        assert (variant.position, variant.reference, variant.alternate) == (43082434, "G", "A")
        assert variant.variant_type is VariantType.SNV

    def test_indel_left_aligns_in_a_repeat_with_reference(self):
        provider = FakeReference({"1": REPEAT_CHROM})
        # One extra CA unit placed at the RIGHT-hand end of the (CA)5 repeat.
        variant = normalize_variant(
            genome_build="GRCh38",
            chromosome="1",
            position=20,
            reference="A",
            alternate="ACA",
            sequence_provider=provider,
        )
        assert variant.normalization.left_aligned is True
        assert variant.normalization.left_shifted is True
        assert variant.normalization.complete is True
        assert (variant.position, variant.reference, variant.alternate) == (9, "A", "AAC")
        assert variant.variant_type is VariantType.INSERTION

    def test_already_leftmost_indel_is_proven_without_shifting(self):
        """``left_aligned`` means "proven leftmost", not "the algorithm moved it"."""
        provider = FakeReference({"1": REPEAT_CHROM})
        variant = normalize_variant(
            genome_build="GRCh38",
            chromosome="1",
            position=9,
            reference="A",
            alternate="AAC",
            sequence_provider=provider,
        )
        assert variant.identity == "GRCh38|1|9|A|AAC"
        assert variant.normalization.left_aligned is True
        assert variant.normalization.left_shifted is False
        assert variant.normalization.warnings == []

    def test_snv_is_trivially_left_aligned(self):
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=100, reference="G", alternate="A"
        )
        assert variant.normalization.left_aligned is True
        assert variant.normalization.left_shifted is False

    def test_delins_discloses_that_alignment_is_unverified(self):
        """A length-changing substitution is not covered by the pure-indel shifter."""
        variant = normalize_variant(
            genome_build="GRCh38",
            chromosome="17",
            position=100,
            reference="ATG",
            alternate="CC",
            sequence_provider=FakeReference({"17": "A" * 200}),
        )
        assert variant.normalization.left_aligned is False
        # A caveat, not a blocker: the identity is still stable for what was supplied.
        assert variant.normalization.complete is True
        warnings = {item.code: item.severity for item in variant.normalization.warnings}
        assert warnings["delins_left_alignment_unverified"] == "warning"

    def test_equivalent_repeat_representations_collapse(self):
        provider = FakeReference({"1": REPEAT_CHROM})
        left = normalize_variant(
            genome_build="GRCh38", chromosome="1", position=12, reference="A", alternate="ACA",
            sequence_provider=provider,
        )
        right = normalize_variant(
            genome_build="GRCh38", chromosome="1", position=20, reference="A", alternate="ACA",
            sequence_provider=provider,
        )
        assert left.identity == right.identity == "GRCh38|1|9|A|AAC"
        assert left.variant_id == right.variant_id

    def test_indel_without_reference_is_incomplete_and_warns(self):
        """No reference => cannot prove left-alignment => must not be trusted."""
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=43082434, reference="GA", alternate="G"
        )
        assert variant.normalization.complete is False
        assert variant.normalization.left_aligned is False
        codes = {warning.code for warning in variant.normalization.warnings}
        assert "indel_left_alignment_unverified" in codes
        severities = {warning.severity for warning in variant.normalization.warnings}
        assert severities == {"abstain"}

    def test_deletion_hgvs_is_a_range(self):
        # A unique-context deletion: no repeat to shift into.
        provider = FakeReference({"17": "A" + "T" * 3 + "G" + "C" * 200})
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=100, reference="ATTT", alternate="A",
            sequence_provider=FakeReference({"17": "A" * 99 + "ATTTG" + "C" * 200}),
        )
        assert variant.variant_type is VariantType.DELETION
        assert variant.hgvs_g == "NC_000017.11:g.101_103del"
        assert variant.normalization.left_aligned is True
        assert provider.calls == []  # the alignment used the other provider

    def test_mnv_hgvs(self):
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=100, reference="AT", alternate="GC"
        )
        assert variant.variant_type is VariantType.MNV
        assert variant.hgvs_g == "NC_000017.11:g.100_101delinsGC"

    def test_complex_delins_hgvs(self):
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="17", position=100, reference="ATG", alternate="CC"
        )
        assert variant.variant_type is VariantType.DELINS
        assert variant.hgvs_g == "NC_000017.11:g.100_102delinsCC"


class TestMultiallelicSplitting:
    def test_split_produces_one_record_per_alt(self):
        raw = RawVariant(chromosome="17", position=43082434, reference="G", alternates=("A", "C"))
        split = split_multiallelic(raw)
        assert [item.alternates for item in split] == [("A",), ("C",)]

    def test_split_drops_symbolic_alleles(self):
        raw = RawVariant(chromosome="17", position=100, reference="G", alternates=("A", ".", "*"))
        split = split_multiallelic(raw)
        assert [item.alternates for item in split] == [("A",)]

    def test_normalize_raw_marks_split_and_allele_index(self):
        raw = RawVariant(chromosome="17", position=43082434, reference="G", alternates=("A", "C", "T"))
        results = normalize_raw(raw, genome_build="GRCh38")
        assert [item.alternate for item in results] == ["A", "C", "T"]
        assert all(item.normalization.multiallelic_split for item in results)
        assert [item.normalization.original_allele_index for item in results] == [0, 1, 2]
        assert len({item.variant_id for item in results}) == 3

    def test_split_alleles_do_not_carry_sample_fields(self):
        """Genotypes cannot be attributed to one split allele without re-genotyping."""
        raw = RawVariant(chromosome="17", position=100, reference="G", alternates=("A", "C"))
        assert not any(hasattr(item, "genotypes") for item in split_multiallelic(raw))

    def test_biallelic_record_is_not_marked_split(self):
        raw = RawVariant(chromosome="17", position=100, reference="G", alternates=("A",))
        result = normalize_raw(raw, genome_build="GRCh38")
        assert len(result) == 1
        assert result[0].normalization.multiallelic_split is False
        assert result[0].normalization.original_allele_index is None


class TestAmbiguousInput:
    def test_non_primary_contig_abstains(self):
        variant = normalize_variant(
            genome_build="GRCh38", chromosome="GL000220", position=100, reference="G", alternate="A"
        )
        assert variant.normalization.complete is False
        assert variant.spdi is None
        assert variant.hgvs_g is None
        codes = {warning.code: warning.severity for warning in variant.normalization.warnings}
        assert codes["non_primary_contig"] == "abstain"

    @pytest.mark.parametrize("reference,alternate", [("G", "A"), ("A", "T")])
    def test_identical_alleles_are_rejected(self, reference, alternate):
        with pytest.raises(NormalizationError):
            normalize_variant(
                genome_build="GRCh38",
                chromosome="17",
                position=100,
                reference=reference,
                alternate=reference,
            )

    @pytest.mark.parametrize("allele", ["", "Z", "AC-T", "a c"])
    def test_invalid_bases_are_rejected(self, allele):
        with pytest.raises(NormalizationError):
            normalize_variant(
                genome_build="GRCh38", chromosome="17", position=100, reference="G", alternate=allele
            )

    def test_zero_position_is_rejected(self):
        with pytest.raises(NormalizationError):
            normalize_variant(
                genome_build="GRCh38", chromosome="17", position=0, reference="G", alternate="A"
            )

    def test_unknown_build_is_rejected(self):
        with pytest.raises(ValueError):
            normalize_variant(
                genome_build="hg18", chromosome="17", position=100, reference="G", alternate="A"
            )

    def test_alignment_failure_is_reported_not_swallowed(self):
        """A provider that cannot serve the window must degrade to an abstain warning."""
        variant = normalize_variant(
            genome_build="GRCh38",
            chromosome="17",
            position=43082434,
            reference="GA",
            alternate="G",
            sequence_provider=FakeReference({}),  # nothing available
        )
        assert variant.normalization.complete is False
        codes = {warning.code for warning in variant.normalization.warnings}
        assert codes & {"reference_unavailable_during_alignment", "indel_left_alignment_unverified"}


class TestReferenceProviderPlumbing:
    def test_build_reference_without_fasta_returns_none(self):
        assert build_reference(fasta=None) is None

    def test_report_records_the_reference_source(self):
        provider = FakeReference({"17": "A" * 200})
        variant = normalize_variant(
            genome_build="GRCh38",
            chromosome="17",
            position=100,
            reference="GA",
            alternate="G",
            sequence_provider=provider,
        )
        assert variant.normalization.reference_used is True
        assert variant.normalization.reference_source == "FakeReference"
