"""Evidence adapters, the registry's air-gap guarantee, and the validation gate.

The headline test in this file is
:func:`TestRecordedClinvar.test_same_locus_opposite_classifications_are_never_conflated`.
ClinVar holds two expert-panel records at GRCh38 chr17:43082434 with the same
dbSNP id and opposite classifications. An adapter that matches on coordinates
rather than on the canonical SPDI turns a benign missense allele into a
pathogenic nonsense call. Everything else here supports that guarantee.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ngs_agent.core.errors import CoreError, EvidenceValidationError
from ngs_agent.core.evidence.adapters.clinvar import (
    AUTHORITATIVE_REVIEW_STATUSES,
    ClinVarAdapter,
    _identity_match_strength,
)
from ngs_agent.core.evidence.adapters.gene_mechanism import GeneMechanismTable
from ngs_agent.core.evidence.adapters.offline_pack import (
    EvidencePackError,
    OfflineEvidencePackAdapter,
)
from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    EvidenceStatus,
    VerificationStatus,
)
from ngs_agent.core.evidence.recordings import (
    DEFAULT_CLINVAR_RECORDINGS,
    load_manifest,
    load_recordings,
)
from ngs_agent.core.evidence.registry import (
    LOCAL_ADAPTERS,
    NETWORK_ADAPTERS,
    EvidenceConfiguration,
    EvidenceRegistry,
    RegistryError,
)
from ngs_agent.core.evidence.transport import EUTILS_BASE_URL, canonical_url
from ngs_agent.core.evidence.validation import summarize_gaps, validate_evidence
from tests.core.helpers import (
    FIXED_NOW,
    consequence_evidence,
    make_evidence,
    mechanism_evidence,
    missing_evidence,
)


@pytest.fixture
def recorded_registry() -> EvidenceRegistry:
    return EvidenceRegistry(EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar")))


def adapter_named(registry: EvidenceRegistry, name: str):
    """Fetch a built adapter by its declared source name."""
    for adapter in registry.adapters:
        if adapter.declaration.name == name:
            return adapter
    raise AssertionError(
        f"adapter {name!r} not built; got {[a.declaration.name for a in registry.adapters]}")


def recorded_clinvar(registry: EvidenceRegistry) -> ClinVarAdapter:
    """The ClinVar adapter, which must be the recorded (offline) one here."""
    adapter = adapter_named(registry, "clinvar")
    assert isinstance(adapter, ClinVarAdapter)
    return adapter


class TestRecordedClinvar:
    """The bundled ClinVar recordings: real responses, no network."""

    def test_bundled_recordings_exist_and_are_manifested(self):
        assert DEFAULT_CLINVAR_RECORDINGS.is_dir()
        manifest = load_manifest(DEFAULT_CLINVAR_RECORDINGS)
        assert manifest.recording_version
        assert manifest.clinvar_release
        assert manifest.recorded_at
        assert manifest.alleles_covered
        assert "clinvar" in str(manifest.source).lower()
        assert manifest.recordings, "the manifest must list the recorded request/response pairs"
        for entry in manifest.recordings:
            assert entry.file and entry.path and entry.status == 200

    def test_manifest_discloses_its_own_edits(self):
        """A recording that was reduced must say so, or it is not a recording."""
        manifest = load_manifest(DEFAULT_CLINVAR_RECORDINGS)
        assert manifest.integrity_note
        assert manifest.retrieval_note

    def test_recordings_load_verbatim(self):
        recordings = load_recordings(DEFAULT_CLINVAR_RECORDINGS)
        assert recordings, "no recorded responses found"

    def test_recorded_adapter_declares_itself_offline(self, recorded_registry):
        adapter = recorded_clinvar(recorded_registry)
        declaration = adapter.declaration
        assert declaration.requires_network is False
        assert declaration.hosted_by == "recorded_fixture"
        assert declaration.version.startswith("recorded-")

    def test_recorded_adapter_stamps_the_clinvar_release(self, recorded_registry):
        manifest = load_manifest(DEFAULT_CLINVAR_RECORDINGS)
        adapter = recorded_clinvar(recorded_registry)
        assert adapter.declaration.version == manifest.clinvar_release

    def test_pathogenic_allele_is_found(self, recorded_registry, brca1_nonsense):
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(brca1_nonsense, gene="BRCA1")
        significance = [
            record for record in records if record.data_type
                is EvidenceDataType.CLINICAL_SIGNIFICANCE
        ]
        assert significance
        record = significance[0]
        assert record.status is EvidenceStatus.PRESENT
        assert record.observed_value["classification_label"] == "pathogenic"
        assert record.observed_value["accession"] == "VCV000017675"
        assert record.observed_value["review_status"] == "reviewed by expert panel"
        assert record.verification is VerificationStatus.VERIFIED
        assert record.usable_as_evidence is True

    def test_benign_allele_at_the_same_locus_is_found(
        self, recorded_registry, brca1_missense_locus):
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(brca1_missense_locus, gene="BRCA1")
        significance = [
            record for record in records
            if record.data_type is EvidenceDataType.CLINICAL_SIGNIFICANCE and record.informative
        ]
        assert significance
        assert significance[0].observed_value["classification_label"] == "benign"
        assert significance[0].observed_value["accession"] == "VCV000017676"

    def test_same_locus_opposite_classifications_are_never_conflated(
        self, recorded_registry, brca1_nonsense, brca1_missense_locus
    ):
        """THE golden safety test for identity matching.

        Both alleles sit at chr17:43082434 and share dbSNP rs41293455. One is
        Pathogenic (p.Arg1443Ter), the other Benign (p.Arg1443Gly), both
        expert-panel reviewed. Coordinate matching alone cannot tell them apart.
        """
        adapter = recorded_clinvar(recorded_registry)

        def label(variant) -> str:
            records = adapter.fetch(variant, gene="BRCA1")
            found = [
                record for record in records
                if record.data_type is EvidenceDataType.CLINICAL_SIGNIFICANCE and record.informative
            ]
            assert len(found) == 1, [record.observed_value for record in found]
            return str(found[0].observed_value["classification_label"])

        assert brca1_nonsense.position == brca1_missense_locus.position
        assert label(brca1_nonsense) == "pathogenic"
        assert label(brca1_missense_locus) == "benign"

    def test_unrecorded_allele_is_unavailable_not_benign(self, recorded_registry, brca1_unrecorded):
        """G>T at a locus we hold records for is still 'no record for this allele'."""
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(brca1_unrecorded, gene="BRCA1")
        significance = [
            record for record in records if record.data_type
                is EvidenceDataType.CLINICAL_SIGNIFICANCE
        ]
        assert significance, "an adapter must always return a record, even for a gap"
        for record in significance:
            assert record.status is EvidenceStatus.UNAVAILABLE
            assert record.observed_value == {}
            assert record.usable_as_evidence is False
            assert "no record" in record.describe_gap()

    def test_unrecorded_locus_is_also_unavailable(self, recorded_registry):
        """A variant nowhere near the recordings must not raise."""
        from ngs_agent.core.normalization import normalize_variant

        elsewhere = normalize_variant(
            genome_build="GRCh38", chromosome="12", position=25200000, reference="C", alternate="T"
        )
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(elsewhere, gene="KRAS")
        assert records
        assert all(record.status is not EvidenceStatus.PRESENT for record in records)

    def test_molecular_consequence_comes_from_the_recording(
        self, recorded_registry, brca1_nonsense):
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(brca1_nonsense, gene="BRCA1")
        consequences = [
            record for record in records if record.data_type
                is EvidenceDataType.MOLECULAR_CONSEQUENCE
        ]
        assert consequences
        terms = consequences[0].observed_value["consequences"]
        assert any("stop_gained" in str(term) or "nonsense" in str(term).lower() for term in terms)
        assert consequences[0].observed_value["canonical_spdi"] == "NC_000017.11:43082433:G:A"

    def test_allele_frequency_is_an_exact_ratio(self, recorded_registry, brca1_nonsense):
        """ClinVar reports TOPMed AF 0.00002; it must arrive as 2/100000, not a float."""
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(brca1_nonsense, gene="BRCA1")
        frequencies = [
            record for record in records if record.data_type is EvidenceDataType.ALLELE_FREQUENCY
        ]
        assert frequencies
        value = frequencies[0].observed_value
        ratio = value["allele_frequency_ratio"]
        assert isinstance(ratio["numerator"], int) and isinstance(ratio["denominator"], int)
        assert ratio["numerator"] / ratio["denominator"] == pytest.approx(2e-05)
        assert "." in value["allele_frequency"]

    def test_authoritative_review_status_is_recognised(self, recorded_registry, brca1_nonsense):
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(brca1_nonsense, gene="BRCA1")
        significance = [
            record for record in records
            if record.data_type is EvidenceDataType.CLINICAL_SIGNIFICANCE and record.informative
        ]
        assert significance[0].observed_value["authoritative_review"] is True
        assert significance[0].observed_value["review_status"] in AUTHORITATIVE_REVIEW_STATUSES

    def test_provenance_names_the_public_record_url(self, recorded_registry, brca1_nonsense):
        adapter = recorded_clinvar(recorded_registry)
        records = adapter.fetch(brca1_nonsense, gene="BRCA1")
        significance = [
            record for record in records
            if record.data_type is EvidenceDataType.CLINICAL_SIGNIFICANCE and record.informative
        ][0]
        assert "clinvar/variation/17675" in str(significance.provenance)
        assert significance.retrieval.transport == "recorded_fixture"

    def test_recorded_fetch_is_repeatable(self, recorded_registry, brca1_nonsense):
        adapter = recorded_clinvar(recorded_registry)
        first = adapter.fetch(brca1_nonsense, gene="BRCA1")
        second = adapter.fetch(brca1_nonsense, gene="BRCA1")
        assert [record.evidence_id for record in first] == [record.evidence_id for record in second]


def clinvar_document(spdi: str | None, *, start: int = 43082434, stop: int = 43082434) -> dict:
    """The minimal esummary document shape the identity matcher inspects."""
    variation: dict = {
        "variation_loc": [
            {
                "assembly_name": "GRCh38",
                "chr": "17",
                "start": str(start),
                "stop": str(stop),
                "status": "current",
            }
        ]
    }
    if spdi is not None:
        variation["canonical_spdi"] = spdi
    return {"variation_set": [variation]}


class TestIdentityMatchStrength:
    def test_spdi_equality_is_the_only_exact_match(self, brca1_nonsense):
        assert brca1_nonsense.spdi == "NC_000017.11:43082433:G:A"
        assert (
            _identity_match_strength(brca1_nonsense, clinvar_document("NC_000017.11:43082433:G:A"))
            == "exact"
        )

    def test_a_different_alt_at_the_same_position_is_not_a_match(self, brca1_nonsense):
        """The chr17:43082434 G>C record must not be attached to the G>A query."""
        assert (
            _identity_match_strength(brca1_nonsense, clinvar_document("NC_000017.11:43082433:G:C"))
            == "none"
        )

    def test_coordinates_without_spdi_are_locus_only(self, brca1_nonsense):
        """A locus match is a hint, never proof of allele identity."""
        assert _identity_match_strength(brca1_nonsense, clinvar_document(None)) == "locus_only"

    def test_a_different_position_is_never_a_match(self, brca1_nonsense):
        assert (
            _identity_match_strength(brca1_nonsense, clinvar_document(None, start=1, stop=1))
                == "none"
        )

    def test_locus_only_is_not_usable_as_evidence(self, brca1_nonsense):
        record = make_evidence(
            variant=brca1_nonsense,
            data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
            observed_value={"classification_label": "pathogenic"},
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.IDENTITY_UNVERIFIED,
        )
        assert record.usable_as_evidence is False


class TestClinVarAdapterRequiresAVersion:
    def test_live_adapter_without_a_source_version_is_refused(self):
        """A record that does not say which release it came from cannot be replayed."""
        with pytest.raises(ValueError, match="explicit source_version"):
            ClinVarAdapter(source_version="")

    def test_live_adapter_declares_network_and_the_eutils_endpoint(self):
        adapter = ClinVarAdapter(source_version="eutils-live:2026-09-18")
        declaration = adapter.declaration
        assert declaration.requires_network is True
        assert declaration.endpoint.startswith(EUTILS_BASE_URL)
        assert declaration.hosted_by == "vendor"

    def test_recorded_adapter_does_not_declare_a_live_endpoint(self, recorded_registry):
        adapter = recorded_clinvar(recorded_registry)
        declaration = adapter.declaration
        assert "recorded_fixture" in declaration.hosted_by or declaration.requires_network is False


class TestTransportAndCacheKeys:
    def test_canonical_url_excludes_non_semantic_parameters(self):
        """A fixture recorded without an API key must still match a keyed call."""
        with_key = canonical_url(
            f"{EUTILS_BASE_URL}/esearch.fcgi",
            {
                "db": "clinvar",
                "term": "17[chr]",
                "api_key": "SECRET",
                "tool": "ngsagent",
                "email": "someone@example.org",
            },
        )
        without_key = (
            canonical_url(f"{EUTILS_BASE_URL}/esearch.fcgi", {"db": "clinvar", "term": "17[chr]"})
        )
        assert with_key == without_key
        assert "SECRET" not in with_key
        assert "someone@example.org" not in with_key

    def test_canonical_url_sorts_parameters(self):
        first = canonical_url(
            f"{EUTILS_BASE_URL}/esummary.fcgi", {"db": "clinvar", "id": "17675", "retmode": "json"}
        )
        second = canonical_url(
            f"{EUTILS_BASE_URL}/esummary.fcgi", {"retmode": "json", "id": "17675", "db": "clinvar"}
        )
        assert first == second

    def test_canonical_url_keeps_semantic_parameters(self):
        first = (
            canonical_url(f"{EUTILS_BASE_URL}/esearch.fcgi", {"db": "clinvar", "term": "17[chr]"})
        )
        second = (
            canonical_url(f"{EUTILS_BASE_URL}/esearch.fcgi", {"db": "clinvar", "term": "18[chr]"})
        )
        assert first != second

    def test_api_key_is_never_part_of_a_cache_key(self):
        """Two deployments, one with a key and one without, must share a cache entry."""
        keyed = (
            canonical_url(f"{EUTILS_BASE_URL}/esummary.fcgi", {"db": "clinvar", "id": "17675",
                "api_key": "K"})
        )
        plain = canonical_url(f"{EUTILS_BASE_URL}/esummary.fcgi", {"db": "clinvar", "id": "17675"})
        assert keyed == plain

    def test_cache_is_opt_in(self):
        assert EvidenceRegistry(EvidenceConfiguration(adapters=("gene_mechanism",))).cache is None
        registry = EvidenceRegistry(
            EvidenceConfiguration(adapters=("gene_mechanism",),
                cache_dir=Path("/tmp/ngs-cache-test"))
        )
        assert registry.cache is not None


class TestGeneMechanismAdapter:
    def test_bundled_table_covers_brca1_as_loss_of_function(self):
        table = GeneMechanismTable.load()
        entry = table.get("BRCA1")
        assert entry is not None
        assert entry["mechanism"] == "loss_of_function"
        assert entry["pvs1_applicable"] is True
        assert table.is_known_gene("BRCA1") is True

    def test_every_seeded_row_is_marked_unverified(self):
        """A curated seed is not a ClinGen assertion; it must say so."""
        table = GeneMechanismTable.load()
        assert len(table) > 10
        for symbol in table.symbols():
            entry = table.get(symbol)
            assert entry is not None
            assert entry["curation_status"] == "seed_unverified", symbol

    def test_unknown_gene_is_not_in_the_table(self):
        table = GeneMechanismTable.load()
        assert table.is_known_gene("NOTAGENE") is False
        assert table.get("NOTAGENE") is None

    def test_unknown_gene_is_reported_not_guessed(self, brca1_nonsense):
        registry = EvidenceRegistry(EvidenceConfiguration(adapters=("gene_mechanism",)))
        adapter = registry.adapters[0]
        records = adapter.fetch(brca1_nonsense, gene="NOTAGENE")
        assert records
        assert all(record.status is not EvidenceStatus.PRESENT for record in records)

    def test_mechanism_record_is_usable_evidence(self, brca1_nonsense):
        registry = EvidenceRegistry(EvidenceConfiguration(adapters=("gene_mechanism",)))
        adapter = registry.adapters[0]
        records = adapter.fetch(brca1_nonsense, gene="BRCA1")
        usable_records = [record for record in records if record.usable_as_evidence]
        assert usable_records
        assert usable_records[0].data_type is EvidenceDataType.GENE_DISEASE_MECHANISM
        assert usable_records[0].observed_value["pvs1_mechanism_applicable"] is True

    def test_mechanism_record_carries_its_curated_limitation(self, brca1_nonsense):
        registry = EvidenceRegistry(EvidenceConfiguration(adapters=("gene_mechanism",)))
        adapter = registry.adapters[0]
        record = [r for r in adapter.fetch(brca1_nonsense, gene="BRCA1") if r.informative][0]
        assert record.limitations, "a seeded mechanism table must disclose that it is seeded"
        assert any("seed" in item.lower() or "curat" in item.lower() for item in record.limitations)


class TestRegistryAirGap:
    def test_default_configuration_is_offline(self):
        config = EvidenceConfiguration()
        assert config.allow_network is False
        assert set(config.adapters) <= set(LOCAL_ADAPTERS)

    def test_network_adapter_is_refused_when_network_is_disallowed(self):
        with pytest.raises(RegistryError):
            EvidenceRegistry(EvidenceConfiguration(adapters=("clinvar",), allow_network=False))

    def test_air_gapped_deployment_can_still_run(self):
        """Local-only configuration builds cleanly and needs no egress."""
        registry = EvidenceRegistry(
            EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar"),
                allow_network=False)
        )
        assert len(registry.adapters) == 2
        declarations = registry.declarations()
        assert all(declaration.requires_network is False for declaration in declarations)
        assert NETWORK_ADAPTERS == ("clinvar",)
        # The ClinVar adapter that *was* built is the recorded one, so an
        # air-gapped deployment never has a network-capable adapter in hand.
        assert not any(
            declaration.name in NETWORK_ADAPTERS and declaration.requires_network
            for declaration in declarations
        )

    def test_network_adapter_builds_when_explicitly_allowed(self):
        registry = EvidenceRegistry(
            EvidenceConfiguration(
                adapters=("clinvar",), allow_network=True,
                    clinvar_source_version="eutils-live:2026-09-18"
            )
        )
        declaration = registry.declarations()[0]
        assert declaration.requires_network is True
        assert declaration.hosted_by == "vendor"
        assert declaration.version == "eutils-live:2026-09-18"

    def test_unknown_adapter_is_refused(self):
        with pytest.raises(RegistryError):
            EvidenceRegistry(EvidenceConfiguration(adapters=("alphagenome",)))

    def test_configuration_hash_excludes_secrets(self):
        plain = EvidenceConfiguration(adapters=("clinvar",), allow_network=True)
        keyed = EvidenceConfiguration(
            adapters=("clinvar",), allow_network=True, ncbi_api_key="SUPERSECRET"
        )
        assert json.dumps(keyed.to_hashable()).find("SUPERSECRET") == -1
        assert keyed.to_hashable()["ncbi_api_key_configured"] is True
        assert plain.to_hashable()["ncbi_api_key_configured"] is False

    def test_declarations_are_serializable_into_provenance(self):
        registry = EvidenceRegistry(EvidenceConfiguration(adapters=("gene_mechanism",)))
        payload = registry.declarations_as_dicts()
        json.dumps(payload)  # must not raise
        assert payload[0]["name"]

    def test_recording_manifest_is_exposed_for_provenance(self, recorded_registry):
        manifest = recorded_registry.recording_manifest
        assert manifest is not None
        assert manifest.clinvar_release


class TestOfflineEvidencePack:
    def _write_pack(self, tmp_path: Path, records: list[EvidenceRecord], **extra) -> Path:
        payload = {
            "pack_name": "test-pack",
            "pack_version": "1.0.0",
            "created_at": FIXED_NOW.isoformat(),
            "records": [json.loads(record.model_dump_json()) for record in records],
            **extra,
        }
        path = tmp_path / "pack.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def test_pack_preserves_the_original_source_and_digest(self, tmp_path, brca1_nonsense):
        original = mechanism_evidence(brca1_nonsense)
        path = self._write_pack(tmp_path, [original])
        adapter = OfflineEvidencePackAdapter(path)
        records = adapter.fetch(brca1_nonsense, gene="BRCA1")
        restored = [record for record in records if record.informative]
        assert restored
        assert restored[0].evidence_id == original.evidence_id
        assert restored[0].source.name == original.source.name
        assert restored[0].retrieved_at == original.retrieved_at

    def test_a_tampered_pack_is_refused(self, tmp_path, brca1_nonsense):
        original = mechanism_evidence(brca1_nonsense)
        path = self._write_pack(tmp_path, [original])
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload["records"][0]["observed_value"]
        value["pvs1_mechanism_applicable"] = not value["pvs1_mechanism_applicable"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        adapter = OfflineEvidencePackAdapter(path)
        with pytest.raises(EvidencePackError, match="modified"):
            adapter.fetch(brca1_nonsense, gene="BRCA1")

    def test_a_pack_that_encodes_missing_data_as_a_value_is_refused(self, tmp_path, brca1_nonsense):
        path = tmp_path / "pack.json"
        record = json.loads(mechanism_evidence(brca1_nonsense).model_dump_json())
        record["status"] = "unavailable"  # but keeps its observed_value
        path.write_text(
            json.dumps(
                {
                    "pack_name": "bad-pack",
                    "pack_version": "1.0.0",
                    "created_at": FIXED_NOW.isoformat(),
                    "records": [record],
                }
            ),
            encoding="utf-8",
        )
        adapter = OfflineEvidencePackAdapter(path)
        with pytest.raises((EvidencePackError, EvidenceValidationError, CoreError)):
            adapter.fetch(brca1_nonsense, gene="BRCA1")

    def test_a_missing_pack_file_is_an_error_not_an_empty_result(self, tmp_path, brca1_nonsense):
        adapter = OfflineEvidencePackAdapter(tmp_path / "absent.json")
        with pytest.raises((EvidencePackError, CoreError)):
            adapter.fetch(brca1_nonsense, gene="BRCA1")


class TestValidationGate:
    def test_clean_records_pass(self, brca1_nonsense):
        records = [
            consequence_evidence(brca1_nonsense, "stop_gained"),
            mechanism_evidence(brca1_nonsense),
        ]
        report = validate_evidence(records, brca1_nonsense)
        assert report.ok is True
        assert report.errors == []
        assert len(report.accepted) == 2
        assert all(record.usable_as_evidence for record in report.accepted)

    def test_evidence_about_another_allele_is_downgraded_not_deleted(
        self, brca1_nonsense, brca1_missense_locus
    ):
        """Cross-record check: the stated identity must match the query."""
        foreign = make_evidence(
            variant=brca1_missense_locus,
            data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
            observed_value={"classification_label": "benign"},
        )
        relabelled = (
            foreign.model_copy(update={"queried_variant_identity": brca1_nonsense.identity})
        )
        report = validate_evidence([relabelled], brca1_nonsense)
        assert report.accepted
        assert report.accepted[0].usable_as_evidence is False
        assert report.accepted[0].verification is VerificationStatus.IDENTITY_UNVERIFIED
        assert any("downgraded" in item.lower() for item in report.accepted[0].limitations)
        assert report.findings

    def test_build_mismatch_is_downgraded(self, brca1_nonsense):
        from ngs_agent.core.genome import GenomeBuild

        wrong_build = make_evidence(
            variant=brca1_nonsense, data_type=EvidenceDataType.MOLECULAR_CONSEQUENCE,
            observed_value={"consequences": ["stop_gained"]},
        ).model_copy(update={"genome_build": GenomeBuild.GRCH37})
        report = validate_evidence([wrong_build], brca1_nonsense)
        survived = report.accepted + [record for record, _ in report.rejected]
        assert survived
        # A GRCh37 record cannot support a GRCh38 query, whether it is
        # downgraded in place or rejected outright.
        assert all(not record.usable_as_evidence for record in report.accepted)
        assert not report.ok or report.accepted == []

    def test_a_gap_is_never_deleted_by_validation(self, brca1_nonsense):
        """Deleting a gap would silently turn 'not retrieved' into 'not mentioned'."""
        gap = missing_evidence(brca1_nonsense, EvidenceDataType.ALLELE_FREQUENCY)
        report = validate_evidence([gap], brca1_nonsense)
        assert len(report.accepted) == 1
        assert report.accepted[0].status is EvidenceStatus.UNAVAILABLE
        assert summarize_gaps(report.accepted)

    def test_duplicate_retrievals_collapse_with_a_warning(self, brca1_nonsense):
        record = consequence_evidence(brca1_nonsense, "stop_gained")
        report = validate_evidence([record, record], brca1_nonsense)
        assert len(report.accepted) == 1
        assert report.duplicate_retrievals
        assert any(finding.code == "duplicate" for finding in report.findings)
        # A duplicate is a warning, not an error: the observation is still sound.
        assert report.ok is True

    def test_summarize_gaps_explains_every_non_usable_record(self, brca1_nonsense):
        records = [
            missing_evidence(brca1_nonsense, EvidenceDataType.ALLELE_FREQUENCY,
                source_name="gnomAD"),
            consequence_evidence(brca1_nonsense, "stop_gained"),
        ]
        gaps = summarize_gaps(records)
        assert len(gaps) == 1
        assert "gnomAD" in gaps[0]
