"""The Verdict produced for one input, end to end through core."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.assess import assess_fastqc, assess_path
from core.models import (
    DECISION_FIX_AND_RERUN,
    DECISION_HEALTHY,
    DECISION_RESEQUENCE,
    DECISION_REVIEW,
    DECISION_TRIM_AND_PROCEED,
    DECISION_UNKNOWN,
    KIND_FASTQC_ZIP,
    KIND_FOLDER,
    KIND_VCF,
    Verdict,
)
from core.rules.qc_rules import QC_QUAL
from core.version import RULESET_VERSION, TOOL_VERSION

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def test_fastqc_fixture_gives_a_complete_verdict():
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    assert isinstance(verdict, Verdict)
    assert verdict.kind == KIND_FASTQC_ZIP
    assert verdict.decision == DECISION_TRIM_AND_PROCEED
    assert verdict.headline
    assert verdict.findings, "the sample fixture must produce findings"
    assert QC_QUAL in {f.id for f in verdict.findings}
    # Every finding carries at least one valid receipt.
    for finding in verdict.findings:
        assert finding.has_valid_receipts(), finding.id
    assert verdict.ruleset_version == RULESET_VERSION
    assert verdict.tool_version == TOOL_VERSION


def test_verdict_json_round_trips_without_loss():
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    clone = Verdict.from_json(verdict.to_json())
    assert clone.to_dict() == verdict.to_dict()
    assert [f.id for f in clone.findings] == [f.id for f in verdict.findings]
    assert clone.findings[0].receipts[0].locator == verdict.findings[0].receipts[0].locator


def test_clean_fastqc_verdict_is_healthy_with_no_findings():
    verdict = assess_path(fx("fastqc", "clean_fastqc.zip"))
    assert verdict.decision == DECISION_HEALTHY
    assert verdict.findings == []


def test_messy_fastqc_verdict_is_resequence():
    verdict = assess_path(fx("fastqc", "messy_fastqc.zip"))
    assert verdict.decision == DECISION_RESEQUENCE


def test_vcf_is_declared_out_of_scope_not_guessed_at():
    verdict = assess_path(fx("vcf", "sample.vcf"))
    assert verdict.kind == KIND_VCF
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []
    assert any("out of scope" in reason for reason in verdict.unknown)


def test_vcf_trap_files_are_also_declared_out_of_scope():
    for name in ("mystery.txt", "vcf_no_extension", "sample.vcf.gz"):
        verdict = assess_path(fx("vcf", name))
        assert verdict.decision == DECISION_UNKNOWN, name
        assert verdict.findings == []


def test_unknown_file_gives_an_honest_unknown_verdict():
    verdict = assess_path(fx("misc", "plain.txt"))
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []
    assert verdict.unknown


def test_empty_file_gives_an_honest_unknown_verdict():
    assert assess_path(fx("misc", "empty.txt")).decision == DECISION_UNKNOWN


def test_missing_path_gives_an_honest_unknown_verdict(tmp_path):
    verdict = assess_path(tmp_path / "nope.zip")
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []


def test_folder_route_runs_the_audit():
    verdict = assess_path(fx("folder"))
    assert verdict.kind == KIND_FOLDER
    assert verdict.decision in {DECISION_HEALTHY, DECISION_REVIEW, DECISION_FIX_AND_RERUN}
    for finding in verdict.findings:
        assert finding.has_valid_receipts()


def test_assess_fastqc_carries_a_top_level_input_receipt():
    verdict = assess_fastqc(fx("fastqc", "sample_fastqc.zip"))
    sources = [r.source for r in verdict.receipts]
    assert any(s.startswith("file:") for s in sources)
    assert any(s.startswith("tool:") for s in sources)
    assert all(r.is_valid() for r in verdict.receipts)


@pytest.mark.parametrize(
    "name",
    ["sample_fastqc.zip", "clean_fastqc.zip", "messy_fastqc.zip", "gc_spike_fastqc.zip"],
)
def test_every_fastqc_fixture_produces_a_decision_and_receipts(name):
    verdict = assess_fastqc(fx("fastqc", name))
    assert verdict.decision in {
        DECISION_HEALTHY,
        DECISION_TRIM_AND_PROCEED,
        DECISION_RESEQUENCE,
    }
    for finding in verdict.findings:
        assert finding.has_valid_receipts()
        assert finding.what and finding.meaning and finding.action
