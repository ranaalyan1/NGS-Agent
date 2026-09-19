"""Audit trail, replay, and human sign-off.

These are the accountability tests. The property under test is not "we wrote a
log" but "a reviewer can reconstruct why this result was produced, from the
evidence recorded at the time, without re-fetching anything".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ngs_agent.core.audit import (
    AuditLog,
    AuditRecord,
    build_review_audit_record,
    configuration_hash,
    iter_audit_records,
    new_audit_id,
    replay_decision,
    variant_from_audit,
)
from ngs_agent.core.errors import AuditError, ReplayDivergenceError
from ngs_agent.core.evidence.registry import EvidenceConfiguration, EvidenceRegistry
from ngs_agent.core.ledger import JsonlLedger, MemoryLedger
from ngs_agent.core.pipeline import ReviewPipeline
from ngs_agent.core.review import ReviewDecision, ReviewRequiredError, require_signed, sign_off
from tests.core.helpers import FIXED_NOW, fixed_clock


@pytest.fixture
def pipeline(tmp_path: Path) -> ReviewPipeline:
    """A fully offline pipeline with an audit log, on a frozen clock."""
    return ReviewPipeline(
        configuration=EvidenceConfiguration(adapters=("gene_mechanism", "recorded_clinvar")),
        audit_log=AuditLog(tmp_path / "audit"),
        clock=fixed_clock,
    )


@pytest.fixture
def demo_vcf() -> Path:
    return Path("demo_data/review_demo.vcf")


@pytest.fixture
def reviewed(pipeline, demo_vcf):
    """One completed review run over the bundled demo VCF."""
    return pipeline.review_vcf(demo_vcf, genome_build="GRCh38")


class TestAuditLogIsAppendOnly:
    def test_review_writes_one_audit_record_per_variant(self, pipeline, reviewed, tmp_path):
        log = AuditLog(tmp_path / "audit")
        records = log.read_all()
        assert len(records) == len(reviewed.reviews)
        assert all(record.action == "review" for record in records)

    def test_audit_file_is_jsonl(self, tmp_path, reviewed):
        path = tmp_path / "audit" / "audit.jsonl"
        assert path.is_file()
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 3
        for line in lines:
            json.loads(line)  # each line is a complete, self-contained record

    def test_appending_never_rewrites_prior_lines(self, tmp_path, reviewed):
        path = tmp_path / "audit" / "audit.jsonl"
        before = path.read_text(encoding="utf-8")
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        log.append(record.model_copy(update={"action": "replay"}))
        after = path.read_text(encoding="utf-8")
        assert after.startswith(before), "an append-only log must not modify what it already wrote"
        assert len(after) > len(before)

    def test_audit_id_is_deterministic_for_the_same_event(self):
        first = new_audit_id(action="review", variant_id="nga.v1.abc", created_at=FIXED_NOW)
        second = new_audit_id(action="review", variant_id="nga.v1.abc", created_at=FIXED_NOW)
        assert first == second

    def test_audit_id_differs_by_action_and_variant(self):
        base = dict(created_at=FIXED_NOW)
        assert new_audit_id(action="review", variant_id="nga.v1.abc", **base) != new_audit_id(
            action="replay", variant_id="nga.v1.abc", **base
        )
        assert new_audit_id(action="review", variant_id="nga.v1.abc", **base) != new_audit_id(
            action="review", variant_id="nga.v1.def", **base
        )

    def test_log_hash_changes_when_a_record_is_added(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        first = log.hash_file()
        log.append(log.read_all()[0].model_copy(update={"action": "replay"}))
        assert log.hash_file() != first

    def test_a_corrupt_line_is_an_error_not_a_silent_skip(self, tmp_path, reviewed):
        path = tmp_path / "audit" / "audit.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"audit_id": "broken"\n')
        with pytest.raises(AuditError):
            AuditLog(tmp_path / "audit").read_all()

    def test_find_raises_for_an_unknown_id(self, tmp_path, reviewed):
        with pytest.raises(AuditError):
            AuditLog(tmp_path / "audit").find("aud.v1.doesnotexist")


class TestAuditRecordContents:
    def test_record_embeds_the_evidence_snapshot(self, tmp_path, reviewed):
        record = AuditLog(tmp_path / "audit").read_all()[0]
        assert record.evidence_snapshot, "a replay must not need the network"
        for entry in record.evidence_snapshot:
            assert entry["evidence_id"].startswith("ev.v1.")
            assert entry["source"]["version"]
            assert entry["retrieved_at"]

    def test_record_embeds_the_full_result_contract(self, tmp_path, reviewed):
        record = AuditLog(tmp_path / "audit").read_all()[0]
        snapshot = record.result_snapshot
        assert snapshot["schema_version"]
        assert snapshot["classification"]["label"]
        assert snapshot["variant"]["id"] == record.variant_id

    def test_record_names_the_engine_rule_set_and_databases(self, tmp_path, reviewed):
        record = AuditLog(tmp_path / "audit").read_all()[0]
        assert record.engine_version
        assert record.normalization_version
        assert record.rule_set
        assert record.rule_set_version
        assert record.database_versions, "which release of which database produced this?"

    def test_record_carries_the_input_and_configuration_hashes(self, tmp_path, reviewed):
        record = AuditLog(tmp_path / "audit").read_all()[0]
        assert record.input_hashes
        assert record.configuration_hash

    def test_configuration_hash_excludes_secrets(self):
        plain = EvidenceConfiguration(adapters=("clinvar",), allow_network=True).to_hashable()
        keyed = EvidenceConfiguration(
            adapters=("clinvar",), allow_network=True, ncbi_api_key="SECRET"
        ).to_hashable()
        assert configuration_hash(plain) != configuration_hash(keyed)
        assert "SECRET" not in configuration_hash(keyed)

    def test_record_stores_gene_resolution(self, tmp_path, reviewed):
        record = AuditLog(tmp_path / "audit").read_all()[0]
        assert record.gene_resolution is not None

    def test_record_round_trips_through_json(self, tmp_path, reviewed):
        record = AuditLog(tmp_path / "audit").read_all()[0]
        restored = AuditRecord.model_validate_json(record.to_json_line())
        assert restored == record

    def test_iter_audit_records_rejects_a_bad_payload(self):
        with pytest.raises(AuditError):
            iter_audit_records([{"audit_id": "x"}])


class TestReplay:
    def test_replay_reproduces_every_recorded_decision(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        for record in log.read_all():
            result = replay_decision(record, audit_log=log)
            assert result.reproduced is True, (record.variant_identity, result.divergence)
            assert result.replayed_label == result.original_label
            assert result.replayed_decision_state == result.original_decision_state
            assert result.evidence_record_count == len(record.evidence_snapshot)

    def test_replay_uses_the_recorded_rule_set_not_todays(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        result = replay_decision(record, audit_log=log)
        assert result.rule_set == record.rule_set

    def test_replay_does_not_reach_the_network(self, tmp_path, reviewed, monkeypatch):
        """The evidence snapshot is the whole point: replay works air-gapped."""
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]

        def explode(*args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("replay attempted a network or registry call")

        monkeypatch.setattr(EvidenceRegistry, "__init__", explode)
        assert replay_decision(record, audit_log=log).reproduced is True

    def test_replay_reconstructs_the_variant_from_the_contract(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        variant = variant_from_audit(record)
        assert variant.identity == record.variant_identity
        assert variant.variant_id == record.variant_id
        assert variant.genome_build.value == record.genome_build

    def test_replay_appends_its_own_audit_record(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        before = len(log.read_all())
        record = log.read_all()[0]
        result = replay_decision(record, audit_log=log)
        after = log.read_all()
        assert len(after) == before + 1
        assert after[-1].action == "replay"
        assert result.replay_audit_id == after[-1].audit_id

    def test_replay_without_a_log_still_reports(self, tmp_path, reviewed):
        record = AuditLog(tmp_path / "audit").read_all()[0]
        result = replay_decision(record)
        assert result.reproduced is True
        assert result.replay_audit_id is None

    def test_a_tampered_evidence_snapshot_diverges(self, tmp_path, reviewed):
        """Changing recorded evidence must break the replay, loudly."""
        log = AuditLog(tmp_path / "audit")
        record = next(
            item for item in log.read_all()
            if item.result_snapshot["classification"]["decision_basis"]
            == "authoritative_external_classification"
        )
        snapshot = [dict(entry) for entry in record.evidence_snapshot]
        for entry in snapshot:
            if entry["data_type"] == "clinical_significance" and entry.get("observed_value"):
                entry["observed_value"] = dict(entry["observed_value"])
                entry["observed_value"]["classification_label"] = "benign"
                entry["observed_value"]["description_raw"] = "benign"
        tampered = record.model_copy(update={"evidence_snapshot": tuple(snapshot)})
        with pytest.raises(ReplayDivergenceError):
            replay_decision(tampered, audit_log=log)

    def test_a_tampered_result_snapshot_diverges(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        snapshot = dict(record.result_snapshot)
        snapshot["classification"] = dict(snapshot["classification"])
        original = snapshot["classification"]["label"]
        snapshot["classification"]["label"] = "benign" if original != "benign" else "pathogenic"
        snapshot["classification"]["display_label"] = "Tampered"
        tampered = record.model_copy(update={"result_snapshot": snapshot})
        with pytest.raises(ReplayDivergenceError):
            replay_decision(tampered, audit_log=log)

    def test_divergence_is_reported_field_by_field(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        snapshot = dict(record.result_snapshot)
        snapshot["classification"] = dict(snapshot["classification"])
        snapshot["classification"]["decision_basis"] = "fabricated"
        tampered = record.model_copy(update={"result_snapshot": snapshot})
        with pytest.raises(ReplayDivergenceError) as excinfo:
            replay_decision(tampered, audit_log=log)
        assert "$.classification" in str(excinfo.value)

    def test_non_deterministic_fields_are_not_treated_as_divergence(self, tmp_path, reviewed):
        """result_id, timestamps and run ids differ by construction."""
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        snapshot = dict(record.result_snapshot)
        snapshot["result_id"] = "res.v1.different"
        snapshot["provenance"] = dict(snapshot["provenance"])
        snapshot["provenance"]["generated_at"] = "2030-01-01T00:00:00+00:00"
        snapshot["provenance"]["run_id"] = "run.v1.different"
        snapshot["provenance"]["audit_id"] = "aud.v1.different"
        relocated = record.model_copy(update={"result_snapshot": snapshot})
        assert replay_decision(relocated, audit_log=log).reproduced is True

    def test_chain_is_the_ancestry_of_a_record(self, tmp_path, reviewed):
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        replay_decision(record, audit_log=log)
        assert [item.action for item in log.chain(record.audit_id)] == ["review"]
        replay_record = log.descendants(record.audit_id)[0]
        assert [item.action for item in log.chain(replay_record.audit_id)] == ["review", "replay"]

    def test_lineage_shows_what_happened_to_a_decision(self, tmp_path, reviewed):
        """A reviewer needs the replays and sign-offs, not just the ancestry."""
        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        replay_decision(record, audit_log=log)
        replay_decision(record, audit_log=log)
        lineage = log.lineage(record.audit_id)
        assert [item.action for item in lineage] == ["review", "replay", "replay"]

    def test_descendants_of_an_unknown_id_is_empty(self, tmp_path, reviewed):
        assert AuditLog(tmp_path / "audit").descendants("aud.v1.nope") == []


class TestLedger:
    def test_memory_ledger_returns_records_for_the_right_variant(self, reviewed, pipeline):
        ledger = MemoryLedger()
        variant = reviewed.reviews[0].variant
        for record in reviewed.reviews[0].result.evidence:
            ledger.append([record])
        assert ledger.for_variant(variant)
        assert all(
            record.queried_variant_identity == variant.identity for record
                in ledger.for_variant(variant))

    def test_jsonl_ledger_persists_and_reloads(self, tmp_path, reviewed):
        path = tmp_path / "ledger" / "evidence.jsonl"
        ledger = JsonlLedger(path)
        records = list(reviewed.reviews[0].result.evidence)
        ledger.append(records)
        reloaded = JsonlLedger(path)
        assert len(reloaded.all_records()) == len(records)
        assert {record.evidence_id for record in reloaded.all_records()} == {
            record.evidence_id for record in records
        }

    def test_ledger_is_append_only(self, tmp_path, reviewed):
        path = tmp_path / "ledger" / "evidence.jsonl"
        ledger = JsonlLedger(path)
        records = list(reviewed.reviews[0].result.evidence)
        ledger.append(records)
        before = path.read_text(encoding="utf-8")
        ledger.append(records)  # a duplicate observation is still an append
        after = path.read_text(encoding="utf-8")
        assert after.startswith(before)

    def test_a_corrupt_ledger_line_is_an_error(self, tmp_path, reviewed):
        path = tmp_path / "ledger" / "evidence.jsonl"
        JsonlLedger(path).append(list(reviewed.reviews[0].result.evidence))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        with pytest.raises(AuditError):
            JsonlLedger(path).all_records()


class TestSignOff:
    def test_a_fresh_result_is_unsigned(self, reviewed):
        result = reviewed.reviews[0].result
        assert result.review.status == "pending"
        with pytest.raises(ReviewRequiredError):
            require_signed(result)

    def test_approval_produces_a_signed_contract(self, reviewed):
        result = reviewed.reviews[0].result
        signed = (
            sign_off(result, ReviewDecision(reviewer="dr.who", action="approve"), now=FIXED_NOW)
        )
        assert signed.review.status == "approved"
        assert signed.review.is_signed is True
        assert signed.review.signed_at == FIXED_NOW
        assert require_signed(signed) is signed

    def test_sign_off_returns_a_new_contract_and_leaves_the_original_alone(self, reviewed):
        result = reviewed.reviews[0].result
        signed = (
            sign_off(result, ReviewDecision(reviewer="dr.who", action="approve"), now=FIXED_NOW)
        )
        assert signed is not result
        assert result.review.status == "pending"
        # The classification is never rewritten by a human action.
        assert signed.classification == result.classification

    def test_rejection_requires_a_written_reason(self, reviewed):
        with pytest.raises(ReviewRequiredError, match="written reason"):
            ReviewDecision(reviewer="dr.who", action="reject")

    def test_rejection_with_notes_is_recorded(self, reviewed):
        result = reviewed.reviews[0].result
        signed = sign_off(
            result,
            ReviewDecision(reviewer="dr.who", action="reject", notes="Wrong transcript."),
            now=FIXED_NOW,
        )
        assert signed.review.status == "rejected"
        assert signed.review.notes == "Wrong transcript."

    def test_override_requires_a_reason(self, reviewed):
        result = reviewed.reviews[0].result
        target = "benign" if result.classification.label != "benign" else "pathogenic"
        with pytest.raises(ReviewRequiredError, match="explicit written reason"):
            sign_off(result, ReviewDecision(reviewer="dr.who", action="approve", decision=target))

    def test_override_is_recorded_but_never_rewrites_the_engine(self, reviewed):
        result = reviewed.reviews[0].result
        target = "benign" if result.classification.label != "benign" else "pathogenic"
        signed = sign_off(
            result,
            ReviewDecision(
                reviewer="dr.who",
                action="approve",
                decision=target,
                override_reason="Laboratory-held segregation data not in the ledger.",
            ),
            now=FIXED_NOW,
        )
        assert signed.review.decision == target
        assert signed.classification.label == result.classification.label
        assert any("OVERRIDE" in item.upper() for item in signed.limitations)

    def test_request_review_cannot_carry_an_override(self, reviewed):
        result = reviewed.reviews[0].result
        target = "benign" if result.classification.label != "benign" else "pathogenic"
        with pytest.raises(ReviewRequiredError):
            sign_off(
                result,
                ReviewDecision(
                    reviewer="dr.who",
                    action="request_review",
                    decision=target,
                    override_reason="Escalating.",
                ),
            )

    def test_request_review_is_not_a_sign_off(self, reviewed):
        result = reviewed.reviews[0].result
        signed = (
            sign_off(result, ReviewDecision(reviewer="dr.who", action="request_review"),
                now=FIXED_NOW)
        )
        assert signed.review.status == "review_requested"
        assert signed.review.is_signed is False
        with pytest.raises(ReviewRequiredError):
            require_signed(signed)

    def test_reviewer_identity_is_mandatory(self):
        # A blank reviewer must fail model validation, not merely be tolerated.
        with pytest.raises(ValidationError):
            ReviewDecision(reviewer="", action="approve")

    def test_reviewer_decision_must_be_one_of_the_five_tiers(self):
        with pytest.raises(ReviewRequiredError, match="five ACMG/AMP tiers"):
            ReviewDecision(reviewer="dr.who", action="approve", decision="probably_fine")

    def test_role_is_recorded_even_when_not_enforced(self, reviewed):
        result = reviewed.reviews[0].result
        signed = sign_off(
            result,
            ReviewDecision(reviewer="dr.who", action="approve", reviewer_role="bioinformatician"),
            now=FIXED_NOW,
        )
        assert signed.review.reviewer_role == "bioinformatician"

    def test_role_enforcement_refuses_an_unqualified_reviewer(self, reviewed):
        result = reviewed.reviews[0].result
        with pytest.raises(ReviewRequiredError, match="not permitted"):
            sign_off(
                result,
                ReviewDecision(reviewer="dr.who", action="approve",
                    reviewer_role="bioinformatician"),
                enforce_role=True,
            )

    def test_role_enforcement_accepts_a_qualified_reviewer(self, reviewed):
        result = reviewed.reviews[0].result
        signed = sign_off(
            result,
            ReviewDecision(
                reviewer="dr.who", action="approve", reviewer_role="clinical_laboratory_scientist"
            ),
            enforce_role=True,
            now=FIXED_NOW,
        )
        assert signed.review.is_signed is True

    def test_record_sign_off_requires_a_prior_review(self, tmp_path, reviewed):
        """No free-standing approvals: a sign-off must attach to a review."""
        log = AuditLog(tmp_path / "audit")
        result = reviewed.reviews[0].result
        orphan = tmp_path / "empty-audit"
        empty = AuditLog(orphan)
        empty.path.parent.mkdir(parents=True, exist_ok=True)
        empty.path.touch()
        from ngs_agent.core.audit import record_sign_off

        with pytest.raises(AuditError, match="must attach to a review"):
            record_sign_off(
                audit_log=empty,
                result=result,
                decision=ReviewDecision(reviewer="dr.who", action="approve"),
            )
        assert log.read_all(), "the real log is untouched"

    def test_record_sign_off_appends_to_the_chain(self, tmp_path, reviewed):
        from ngs_agent.core.audit import record_sign_off

        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        from ngs_agent.core.contract import VariantReviewResult

        result = VariantReviewResult.model_validate(record.result_snapshot)
        signed, signoff_record = record_sign_off(
            audit_log=log,
            result=result,
            decision=ReviewDecision(reviewer="dr.who", action="approve"),
            created_at=FIXED_NOW,
        )
        assert signoff_record.action == "sign_off"
        assert signoff_record.reviewer == "dr.who"
        assert signed.review.is_signed is True
        # ``chain`` is ancestry only; the sign-off is a descendant of the review.
        assert [item.action for item in log.chain(record.audit_id)] == ["review"]
        assert [item.action for item in log.lineage(record.audit_id)] == ["review", "sign_off"]

    def test_signoff_audit_record_names_the_before_and_after(self, tmp_path, reviewed):
        from ngs_agent.core.audit import record_sign_off
        from ngs_agent.core.contract import VariantReviewResult

        log = AuditLog(tmp_path / "audit")
        record = log.read_all()[0]
        result = VariantReviewResult.model_validate(record.result_snapshot)
        target = "benign" if result.classification.label != "benign" else "pathogenic"
        _, signoff_record = record_sign_off(
            audit_log=log,
            result=result,
            decision=ReviewDecision(
                reviewer="dr.who",
                action="approve",
                decision=target,
                override_reason="Laboratory-held data.",
            ),
            created_at=FIXED_NOW,
        )
        assert signoff_record.classification_before == result.classification.label
        assert signoff_record.classification_after == target
        assert signoff_record.override_reason == "Laboratory-held data."


class TestAuditRecordIsSelfContained:
    def test_an_audit_record_can_be_replayed_from_its_json_alone(self, tmp_path, reviewed):
        """The record is the deliverable: no database, no cache, no network."""
        path = tmp_path / "audit" / "audit.jsonl"
        line = path.read_text(encoding="utf-8").splitlines()[0]
        relocated = tmp_path / "elsewhere" / "audit.jsonl"
        relocated.parent.mkdir(parents=True)
        relocated.write_text(line + "\n", encoding="utf-8")
        log = AuditLog(relocated.parent)
        record = log.read_all()[0]
        assert replay_decision(record, audit_log=log).reproduced is True

    def test_the_build_review_helper_matches_the_pipeline_output(
        self, pipeline, reviewed, tmp_path):
        """``build_review_audit_record`` is the documented construction path."""
        review = reviewed.reviews[0]
        result = review.result
        ledger = MemoryLedger()
        ledger.append(list(result.evidence) + list(result.evidence_gaps))
        registry = EvidenceRegistry(pipeline.configuration)
        record = build_review_audit_record(
            result=result,
            variant=review.variant,
            gene=result.gene.symbol,
            ledger=ledger,
            gene_resolution=json.loads(result.gene.model_dump_json()),
            input_hashes=result.provenance.input_hashes,
            configuration_hash=result.provenance.configuration_hash or "0" * 64,
            adapter_declarations=registry.declarations_as_dicts(),
            normalization_version=result.normalization.algorithm_version,
            created_at=FIXED_NOW,
        )
        assert record.variant_id == result.variant.id
        assert record.evidence_snapshot
        assert record.result_snapshot["classification"]["label"] == result.classification.label
        assert replay_decision(record).reproduced is True
