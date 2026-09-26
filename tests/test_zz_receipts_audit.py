"""Station 8 — the receipts audit.

The Law of Receipts is not a style guide: a claim without a receipt is a guess
with a confident face. This file checks the law against every Finding created
anywhere in the test session (see the registry in conftest.py), not just the
ones built by hand here.

This file is named ``test_zz_*`` so that it runs LAST: the registry has to have
seen everything the other test files created before it can audit them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import FINDINGS_SEEN
from core.assess import assess_path
from core.models import Finding, Receipt
from core.version import RULESET_VERSION, TOOL_VERSION

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA_RE = re.compile(r"^[0-9a-f]{12}$")
KNOWN_IDS = (
    re.compile(r"^QC-(QUAL|ADAPT|DUP|GC|N|LEN)-\d+$"),
    re.compile(r"^AUD-(STRAND|CONTAM|DUP|TRUNC|BUILD|COUNT|PAIRED|ADAPT|ALIGN)-\d+$"),
    re.compile(r"^NF-[A-Z]+-\d+$"),
)


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def test_the_registry_records_every_finding_that_is_created():
    """Guard against a silent refactor that stops the registry seeing findings."""
    before = len(FINDINGS_SEEN)
    Finding(
        id="AUD-ALIGN-01",
        title="registry probe",
        severity="info",
        what="w",
        meaning="m",
        action="a",
        receipts=[
            Receipt(
                source="rule:AUD-ALIGN-01",
                version=RULESET_VERSION,
                timestamp="2026-09-26T00:00:00Z",
                locator="alignment summary",
            )
        ],
    )
    assert len(FINDINGS_SEEN) == before + 1


def test_the_audit_covered_the_whole_suite(request):
    """When the full suite runs, the audit must have seen findings from all of it.

    Running this file on its own cannot see the other files' findings, so the
    coverage check stands down rather than fail for the wrong reason.
    """
    if request.session.testscollected < 300:
        pytest.skip("partial run: findings from other test files are not in scope")
    assert len(FINDINGS_SEEN) > 80, f"only {len(FINDINGS_SEEN)} findings seen"


def test_every_finding_created_this_session_carries_a_valid_receipt():
    offenders = [
        f"{f.id}: {[r.source for r in f.receipts]}"
        for f in FINDINGS_SEEN
        if not f.has_valid_receipts()
    ]
    assert not offenders, "findings without receipts:\n" + "\n".join(offenders[:20])


def test_every_receipt_has_all_four_fields_and_a_real_timestamp():
    for finding in FINDINGS_SEEN:
        for receipt in finding.receipts:
            assert receipt.source.strip(), finding.id
            assert receipt.version.strip(), finding.id
            assert receipt.locator.strip(), finding.id
            assert TIMESTAMP_RE.match(receipt.timestamp), f"{finding.id}: {receipt.timestamp}"


def test_rule_and_signature_receipts_are_stamped_with_the_ruleset_version():
    for finding in FINDINGS_SEEN:
        for receipt in finding.receipts:
            if receipt.source.startswith(("rule:", "signature:")):
                assert receipt.version == RULESET_VERSION, f"{finding.id}: {receipt.version}"


def test_file_receipts_identify_the_file_they_came_from():
    for finding in FINDINGS_SEEN:
        for receipt in finding.receipts:
            if receipt.source.startswith("file:"):
                assert SHA_RE.match(receipt.source.removeprefix("file:")), receipt.source


def test_no_finding_is_missing_its_three_sentences():
    """What is it, what does it mean, what is the ONE action."""
    for finding in FINDINGS_SEEN:
        assert finding.what.strip(), finding.id
        assert finding.meaning.strip(), finding.id
        assert finding.action.strip(), finding.id


def test_every_finding_id_comes_from_a_known_rule_set():
    for finding in FINDINGS_SEEN:
        assert any(pattern.match(finding.id) for pattern in KNOWN_IDS), finding.id


def test_findings_survive_serialisation_with_their_receipts():
    for finding in FINDINGS_SEEN[:40]:
        clone = Finding.from_dict(finding.to_dict())
        assert clone.receipts == finding.receipts
        assert clone.details == finding.details


# --------------------------------------------------------------------------
# End-to-end: every fixture, through the front door
# --------------------------------------------------------------------------
ALL_INPUTS = [
    fx("fastqc", "sample_fastqc.zip"),
    fx("fastqc", "clean_fastqc.zip"),
    fx("fastqc", "messy_fastqc.zip"),
    fx("fastqc", "gc_spike_fastqc.zip"),
    fx("runs", "clean_run"),
    fx("runs", "contig_mismatch"),
    fx("runs", "strand_mismatch"),
    fx("logs", "nextflow_star_index.log"),
    fx("logs", "nextflow_warnings.log"),
    fx("logs", "nextflow_nomatch.log"),
    fx("logs", "nextflow.log"),
    fx("folder"),
]


@pytest.mark.parametrize("path", ALL_INPUTS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_every_fixture_produces_a_verdict_whose_findings_all_have_receipts(path):
    verdict = assess_path(path)
    assert verdict.tool_version == TOOL_VERSION
    assert verdict.ruleset_version == RULESET_VERSION
    for finding in verdict.findings:
        assert finding.has_valid_receipts(), f"{path}: {finding.id}"
        assert finding.receipts[0].source.startswith(("rule:", "signature:"))
        assert any(r.source.startswith("file:") for r in finding.receipts)


@pytest.mark.parametrize("path", ALL_INPUTS, ids=lambda p: str(p.relative_to(FIXTURES)))
def test_verdicts_round_trip_without_losing_a_receipt(path):
    verdict = assess_path(path)
    before = [r.to_dict() for f in verdict.findings for r in f.receipts]
    clone = type(verdict).from_json(verdict.to_json())
    after = [r.to_dict() for f in clone.findings for r in f.receipts]
    assert before == after
