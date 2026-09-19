"""The vertical slice, end to end, as a golden test.

    VCF -> normalized variant -> one authoritative evidence source ->
    deterministic classification -> versioned JSON contract -> audit record ->
    replay reproduces it

This is the acceptance test for the whole architecture in miniature. It runs
entirely offline against recorded ClinVar responses at GRCh38 chr17:43082434,
a locus where ClinVar holds two expert-panel records with the same dbSNP id and
opposite classifications. Every number in here is pinned on purpose: if a change
moves one of them, that change altered a signed classification and must be
reviewed, not quietly accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ngs_agent.core.audit import AuditLog, replay_decision
from ngs_agent.core.contract import CONTRACT_SCHEMA_VERSION, VariantReviewResult
from ngs_agent.core.evidence.models import EvidenceDataType
from ngs_agent.core.evidence.registry import EvidenceConfiguration
from ngs_agent.core.pipeline import ReviewPipeline, summarize_run
from tests.core.helpers import FIXED_NOW, fixed_clock

DEMO_VCF = Path("demo_data/review_demo.vcf")

#: The three distinct alleles the demo VCF contains. The fourth record is a
#: multiallelic ``G>A,C`` whose alleles duplicate the first two and must be
#: collapsed rather than reviewed twice.
EXPECTED = {
    "GRCh38|17|43082434|G|A": {
        "label": "pathogenic",
        "abstained": False,
        "decision_state": "classified",
        "decision_basis": "authoritative_external_classification",
        "accession": "VCV000017675.110",
        "external_label": "pathogenic",
    },
    "GRCh38|17|43082434|G|C": {
        "label": "benign",
        "abstained": False,
        "decision_state": "classified",
        "decision_basis": "authoritative_external_classification",
        "accession": "VCV000017676.77",
        "external_label": "benign",
    },
    "GRCh38|17|43082434|G|T": {
        "label": "uncertain_significance",
        "abstained": True,
        "decision_state": "insufficient_evidence",
        "decision_basis": "no_criteria_met",
        "accession": None,
        "external_label": None,
    },
}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One reviewed run over the demo VCF, on a frozen clock, with an audit log."""
    directory = tmp_path_factory.mktemp("vertical-slice")
    return run_once(directory), AuditLog(directory / "audit")


def run_once(directory: Path):
    """One offline review run over the demo VCF, on the frozen clock."""
    pipeline = ReviewPipeline(
        configuration=EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar")),
        audit_log=AuditLog(directory / "audit"),
        clock=fixed_clock,
    )
    return pipeline.review_vcf(DEMO_VCF, genome_build="GRCh38")


def by_identity(reviewed) -> dict[str, VariantReviewResult]:
    """Index a run's contracts by normalized variant identity."""
    return {review.variant.identity: review.result for review in reviewed.reviews}


class TestInputHandling:
    def test_demo_vcf_exists_and_is_documented(self):
        assert DEMO_VCF.is_file(), "the golden test's input must be checked in"
        text = DEMO_VCF.read_text(encoding="utf-8")
        assert text.startswith("##fileformat=VCFv4")

    def test_four_records_collapse_to_three_alleles(self, run):
        reviewed, _ = run
        assert len(reviewed.reviews) == 3
        identities = [review.variant.identity for review in reviewed.reviews]
        assert sorted(identities) == sorted(EXPECTED)
        assert len(set(identities)) == 3

    def test_duplicate_alleles_are_reported_not_silently_dropped(self, run):
        reviewed, _ = run
        assert any("duplicat" in warning.lower() for warning in reviewed.run_warnings)

    def test_the_run_is_offline(self, run):
        """No adapter in this configuration may require the network."""
        reviewed, _ = run
        for declaration in reviewed.adapters:
            assert declaration["requires_network"] is False

    def test_input_hash_is_recorded(self, run):
        reviewed, _ = run
        assert reviewed.input_sha256
        assert len(reviewed.input_sha256) == 64
        for review in reviewed.reviews:
            assert review.result.provenance.input_hashes


class TestVariantNormalization:
    def test_identities_are_pinned(self, run):
        reviewed, _ = run
        found = by_identity(reviewed)
        assert found["GRCh38|17|43082434|G|A"].variant.spdi == "NC_000017.11:43082433:G:A"
        assert found["GRCh38|17|43082434|G|C"].variant.spdi == "NC_000017.11:43082433:G:C"
        assert found["GRCh38|17|43082434|G|T"].variant.spdi == "NC_000017.11:43082433:G:T"

    def test_variant_ids_are_pinned(self, run):
        from ngs_agent.core.hashing import ga4gh_digest

        reviewed, _ = run
        found = by_identity(reviewed)
        for identity, result in found.items():
            assert result.variant.id == f"nga.v1.{ga4gh_digest(identity)}"

    def test_multiallelic_records_are_marked_as_split(self, run):
        """The demo lists the multiallelic site first, so the split is visible."""
        reviewed, _ = run
        found = by_identity(reviewed)
        for identity in ("GRCh38|17|43082434|G|A", "GRCh38|17|43082434|G|C"):
            block = found[identity].normalization
            assert block.multiallelic_split is True, identity
        assert found["GRCh38|17|43082434|G|A"].normalization.original_allele_index == 0
        assert found["GRCh38|17|43082434|G|C"].normalization.original_allele_index == 1
        # The G>T record was biallelic in the input and stays that way.
        assert found["GRCh38|17|43082434|G|T"].normalization.multiallelic_split is False

    def test_normalization_completed_for_every_allele(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            assert review.result.normalization.complete is True
            assert review.result.normalization.warnings == ()

    def test_gene_is_resolved_and_its_resolution_recorded(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            assert review.result.gene.symbol == "BRCA1"
            assert review.result.gene.resolved_from != "not_resolved"


class TestEvidenceRetrieval:
    def test_every_review_carries_evidence_records_and_gaps(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            assert review.result.evidence, "a review with no evidence block is unauditable"

    def test_gap_records_are_present_for_the_unrecorded_allele(self, run):
        reviewed, _ = run
        abstained = by_identity(reviewed)["GRCh38|17|43082434|G|T"]
        gap_types = {record.data_type for record in abstained.evidence_gaps}
        assert EvidenceDataType.CLINICAL_SIGNIFICANCE in gap_types
        for record in abstained.evidence_gaps:
            assert record.observed_value == {}, "a gap must never carry a value"
            assert record.usable_as_evidence is False

    def test_missing_evidence_is_explained_in_prose(self, run):
        reviewed, _ = run
        abstained = by_identity(reviewed)["GRCh38|17|43082434|G|T"]
        assert abstained.missing_evidence
        assert any("no record" in item.lower() for item in abstained.missing_evidence)

    def test_evidence_names_its_source_version_and_retrieval_time(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            for record in review.result.evidence:
                assert record.source.name
                assert record.source.version
                assert record.source.adapter_version
                assert record.retrieved_at == FIXED_NOW

    def test_the_recorded_transport_is_declared(self, run):
        reviewed, _ = run
        transports = {record.retrieval.transport for review in reviewed.reviews
                      for record in review.result.evidence}
        assert transports <= {"recorded_fixture", "file", "none", "snapshot"}
        assert "http" not in transports, "a recording must not be reported as an HTTP retrieval"

    def test_database_versions_are_recorded_for_the_run(self, run):
        reviewed, _ = run
        versions = reviewed.database_versions
        assert versions.get("clinvar"), versions
        assert versions["clinvar"].startswith("recorded-")


class TestDeterministicClassification:
    @pytest.mark.parametrize("identity", sorted(EXPECTED))
    def test_golden_classification(self, run, identity):
        reviewed, _ = run
        result = by_identity(reviewed)[identity]
        expected = EXPECTED[identity]
        assert result.classification.label == expected["label"]
        assert result.classification.abstained is expected["abstained"]
        assert result.classification.decision_state == expected["decision_state"]
        assert result.classification.decision_basis == expected["decision_basis"]

    @pytest.mark.parametrize("identity", sorted(EXPECTED))
    def test_golden_external_classification(self, run, identity):
        reviewed, _ = run
        result = by_identity(reviewed)[identity]
        expected = EXPECTED[identity]
        if expected["accession"] is None:
            assert result.external_classifications == ()
            return
        assert len(result.external_classifications) == 1
        external = result.external_classifications[0]
        assert external.accession == expected["accession"]
        assert external.classification_label == expected["external_label"]
        assert external.review_status == "reviewed by expert panel"
        assert external.authority
        assert external.evidence_id
        assert external.tier == expected["external_label"]

    def test_opposite_classifications_at_one_locus_are_both_correct(self, run):
        """The headline safety property of the whole slice."""
        reviewed, _ = run
        found = by_identity(reviewed)
        assert found["GRCh38|17|43082434|G|A"].classification.label == "pathogenic"
        assert found["GRCh38|17|43082434|G|C"].classification.label == "benign"

    def test_no_criterion_is_applied_without_evidence(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            known = {record.evidence_id for record in review.result.evidence}
            for criterion in review.result.applied_criteria:
                assert criterion.evidence_ids
                assert set(criterion.evidence_ids) <= known

    def test_pvs1_is_indeterminate_without_transcript_context(self, run):
        """A null allele in a LoF gene, but no exon/NMD source: not applied, not rejected."""
        reviewed, _ = run
        result = by_identity(reviewed)["GRCh38|17|43082434|G|A"]
        indeterminate = {item.code for item in result.indeterminate_criteria}
        assert "PVS1" in indeterminate
        assert not any(item.code == "PVS1" for item in result.applied_criteria)
        assert not any(item.code == "PVS1" for item in result.rejected_criteria)

    def test_pm2_is_indeterminate_when_observation_quality_is_unknown(self, run):
        reviewed, _ = run
        result = by_identity(reviewed)["GRCh38|17|43082434|G|A"]
        assert "PM2" in {item.code for item in result.indeterminate_criteria}

    def test_all_28_criteria_are_accounted_for(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            result = review.result
            total = (
                len(result.applied_criteria)
                + len(result.rejected_criteria)
                + len(result.indeterminate_criteria)
                + len(result.not_evaluated_criteria)
            )
            assert total == 28, "every criterion must be in exactly one state"

    def test_every_result_requires_human_review(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            assert review.result.classification.requires_human_review is True
            assert review.result.review.status == "pending"
            assert review.result.review.is_signed is False

    def test_no_confidence_score_anywhere_in_the_contract(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            payload = json.loads(review.result.model_dump_json())
            assert "confidence" not in json.dumps(payload).lower()

    def test_ruo_banner_is_on_every_result(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            assert review.result.research_use_only is True
            assert "RESEARCH USE ONLY" in review.result.disclaimer
            assert any("RESEARCH USE ONLY" in item for item in review.result.limitations)


class TestVersionedContract:
    def test_schema_version_is_pinned(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            assert review.result.schema_version == CONTRACT_SCHEMA_VERSION

    def test_contract_survives_a_json_round_trip(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            restored = VariantReviewResult.model_validate_json(review.result.model_dump_json())
            assert restored == review.result

    def test_two_runs_over_the_same_input_are_identical(self, tmp_path):
        """Determinism is the product. Same input, same config, same clock.

        ``provenance.audit_path`` is excluded: it names where *this* deployment
        wrote its log, which is legitimately deployment-specific and is already
        excluded from replay comparison for the same reason.
        """
        def strip(result: VariantReviewResult) -> dict:
            payload = json.loads(result.model_dump_json())
            payload["provenance"].pop("audit_path", None)
            return payload

        first = run_once(tmp_path / "one")
        second = run_once(tmp_path / "two")
        assert first.run_id == second.run_id
        assert first.input_sha256 == second.input_sha256
        assert first.configuration_hash == second.configuration_hash
        assert [strip(r.result) for r in first.reviews] == [strip(r.result) for r in second.reviews]

    def test_run_summary_reports_the_abstention_rate(self, run):
        reviewed, _ = run
        summary = summarize_run(reviewed)
        assert summary["variants"] == 3
        # summarize_run rounds to 4 decimal places for stable reporting.
        assert summary["abstention_rate"] == pytest.approx(1 / 3, abs=1e-4)
        # summarize_run reports display labels, since that is what a human reads.
        assert summary["labels"] == {"Pathogenic": 1, "Benign": 1, "VUS": 1}
        assert summary["decision_states"] == {"classified": 2, "insufficient_evidence": 1}
        assert summary["requires_human_review"] == 3

    def test_a_different_genome_build_produces_different_identities(self, tmp_path):
        """GRCh37 coordinates for the same locus must not match GRCh38 evidence."""
        pipeline = ReviewPipeline(
            configuration=EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar")),
            audit_log=AuditLog(tmp_path / "audit"),
            clock=fixed_clock,
        )
        from ngs_agent.core.normalization import normalize_variant

        grch37 = normalize_variant(
            genome_build="GRCh37", chromosome="17", position=41234451, reference="G", alternate="A"
        )
        review = pipeline.review_variant(grch37, gene="BRCA1")
        grch38 = by_identity(run_once(tmp_path / "grch38"))["GRCh38|17|43082434|G|A"]
        assert review.result.variant.id != grch38.variant.id
        assert review.result.variant.genome_build == "GRCh37"
        # The recordings are GRCh38; a GRCh37 query must not silently match them.
        assert review.result.classification.abstained is True



class TestAuditAndReplay:
    def test_one_audit_record_per_reviewed_variant(self, run):
        reviewed, log = run
        records = log.read_all()
        assert len(records) == len(reviewed.reviews) == 3
        assert all(record.action == "review" for record in records)

    def test_audit_record_carries_the_evidence_snapshot(self, run):
        _, log = run
        for record in log.read_all():
            assert record.evidence_snapshot
            assert record.result_snapshot["schema_version"] == CONTRACT_SCHEMA_VERSION

    def test_every_recorded_decision_replays(self, run):
        _, log = run
        for record in log.read_all():
            result = replay_decision(record, audit_log=log)
            assert result.reproduced is True, (record.variant_identity, result.divergence)

    def test_replay_reproduces_the_golden_labels(self, run):
        _, log = run
        replayed = {
            record.variant_identity: replay_decision(record, audit_log=log)
            for record in log.read_all()
            if record.action == "review"
        }
        for identity, expected in EXPECTED.items():
            assert replayed[identity].replayed_label == expected["label"]
            assert replayed[identity].replayed_decision_state == expected["decision_state"]

    def test_replay_evidence_count_matches_the_snapshot(self, run):
        _, log = run
        for record in log.read_all():
            if record.action != "review":
                continue
            result = replay_decision(record, audit_log=log)
            assert result.evidence_record_count == len(record.evidence_snapshot)

    def test_replay_is_repeatable(self, run):
        _, log = run
        record = log.read_all()[0]
        first = replay_decision(record)
        second = replay_decision(record)
        assert first.reproduced is second.reproduced is True
        assert first.replayed_label == second.replayed_label

    def test_the_log_grows_by_exactly_one_line_per_replay(self, run):
        _, log = run
        before = len(log.read_all())
        replay_decision(log.read_all()[0], audit_log=log)
        assert len(log.read_all()) == before + 1


class TestHonestyAboutLimitations:
    def test_adopted_external_classification_says_it_was_adopted(self, run):
        reviewed, _ = run
        result = by_identity(reviewed)["GRCh38|17|43082434|G|A"]
        assert any("adopted from an authoritative external classification" in item
                   for item in result.limitations)

    def test_the_abstained_variant_explains_why(self, run):
        reviewed, _ = run
        result = by_identity(reviewed)["GRCh38|17|43082434|G|T"]
        assert result.limitations
        assert result.missing_evidence
        assert result.classification.decision_basis == "no_criteria_met"

    def test_coverage_gaps_are_enumerated(self, run):
        reviewed, _ = run
        for review in reviewed.reviews:
            assert review.result.evidence_coverage
            covered = {item.data_type for item in review.result.evidence_coverage}
            assert EvidenceDataType.CLINICAL_SIGNIFICANCE.value in covered

    def test_seed_gene_table_discloses_that_it_is_seeded(self, run):
        reviewed, _ = run
        result = by_identity(reviewed)["GRCh38|17|43082434|G|A"]
        assert any("seed" in item.lower() for item in result.limitations)
