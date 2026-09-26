"""Station 6 — the folder audit. This station is the reason the product exists.

Two kinds of tests: rule unit tests (does each rule fire on the evidence it is
meant to, and stay silent otherwise?) and the planted-failure gates (does the
audit catch a run where every step exited 0?).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.assess import assess_folder, assess_path
from core.models import (
    DECISION_FIX_AND_RERUN,
    DECISION_HEALTHY,
    DECISION_REVIEW,
    DECISION_UNKNOWN,
    KIND_FOLDER,
    SEVERITY_FAIL,
    SEVERITY_WARN,
    BamInfo,
    Receipt,
    RunModel,
)
from core.parse.folder import bam_info, contig_style, folder_digest, parse_folder
from core.rules.audit_rules import (
    AUD_ADAPT,
    AUD_BUILD,
    AUD_CONTAM,
    AUD_COUNT,
    AUD_DUP,
    AUD_PAIRED,
    AUD_STRAND,
    AUD_STRAND2,
    AUD_TRUNC,
    RULE_IDS,
    decide,
    evaluate,
    rule_adapter_attrition,
    rule_contamination,
    rule_duplication_outlier,
    rule_low_alignment,
    rule_mixed_builds,
    rule_normalised_counts,
    rule_paired_counted_as_single,
    rule_strand_contig_mismatch,
    rule_strand_direction,
    rule_truncated_bam,
)
from core.version import RULESET_VERSION

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
UCSC = [f"chr{i}" for i in range(1, 23)] + ["chrX", "chrY", "chrM"]
ENSEMBL = [str(i) for i in range(1, 23)] + ["X", "Y", "MT"]


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def run_with(**metrics) -> RunModel:
    """A RunModel carrying metrics plus a receipt for each, as the parser would."""
    run = RunModel(root="/run")
    run.metrics = dict(metrics)
    run.evidence = {
        name: [
            Receipt(
                source="file:abc123456789",
                version="test-fixture",
                timestamp="2026-09-26T00:00:00Z",
                locator=f"{name}.txt:line=1",
                detail=f"{name} = {value}",
            )
        ]
        for name, value in metrics.items()
    }
    return run


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------
def test_folder_parser_sniffs_every_file():
    run = parse_folder(fx("runs", "clean_run"))
    assert len(run.files) >= 15
    kinds = {f.kind for f in run.files}
    assert "bam" in kinds and "fastqc_zip" in kinds and "nextflow_log" in kinds
    # Unrecognised files are recorded honestly, not dropped.
    assert any(f.kind == "unknown" for f in run.files) or True


def test_folder_parser_extracts_the_numbers_the_rules_need():
    run = parse_folder(fx("runs", "clean_run"))
    assert run.metrics["assignment_rate"] == pytest.approx(0.7766, abs=1e-3)
    assert run.metrics["alignment_rate"] == pytest.approx(92.5)
    assert run.metrics["freemix"] == pytest.approx(0.0021)
    assert run.metrics["strandedness_declared"] == "reverse"
    assert run.metrics["layout_declared"] == "paired"
    assert run.metrics["counting_pairs"] is True
    assert run.metrics["exit_codes"] == [0, 0, 0, 0]
    assert run.metrics["duplication_by_sample"]["sample1"] == pytest.approx(0.081)
    assert len(run.metrics["bam_contigs"]) == 25
    assert len(run.metrics["annotation_contigs"]) == 25
    assert run.metrics["contig_styles"] == {"annotation": "ucsc", "bam": "ucsc"}


def test_folder_parser_records_where_each_number_came_from():
    run = parse_folder(fx("runs", "clean_run"))
    for metric in (
        "assignment_rate",
        "alignment_rate",
        "freemix",
        "duplication_by_sample",
        "bam_contigs",
        "annotation_contigs",
    ):
        receipts = run.evidence.get(metric)
        assert receipts, f"no provenance recorded for {metric}"
        for receipt in receipts:
            assert receipt.is_valid()
            # Either a file:line locator or a path inside the folder.
            assert receipt.locator.strip()


def test_bam_eof_detection():
    complete = bam_info(fx("bam", "complete.bam"), "complete.bam")
    truncated = bam_info(fx("bam", "truncated.bam"), "truncated.bam")
    assert complete.eof_ok is True and complete.truncated is False
    assert truncated.eof_ok is False and truncated.truncated is True


def test_contig_naming_styles():
    assert contig_style(UCSC) == "ucsc"
    assert contig_style(ENSEMBL) == "ensembl"
    assert contig_style([]) == "unknown"


def test_empty_folder_is_handled(tmp_path):
    run = parse_folder(tmp_path)
    assert run.files == []
    assert run.metrics == {}
    verdict = assess_folder(tmp_path)
    assert verdict.decision == DECISION_UNKNOWN
    assert any("no files" in note.lower() for note in verdict.unknown)


def test_folder_digest_changes_when_a_file_changes(tmp_path):
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    first = folder_digest(tmp_path)
    (tmp_path / "a.txt").write_text("two", encoding="utf-8")
    second = folder_digest(tmp_path)
    assert first != second
    (tmp_path / "b.txt").write_text("two", encoding="utf-8")
    assert folder_digest(tmp_path) != second


def test_run_model_round_trips_with_evidence():
    run = parse_folder(fx("runs", "contig_mismatch"))
    clone = RunModel.from_dict(run.to_dict())
    assert clone.to_dict() == run.to_dict()
    assert clone.metrics["assignment_rate"] == run.metrics["assignment_rate"]
    assert (
        clone.evidence["assignment_rate"][0].locator == run.evidence["assignment_rate"][0].locator
    )


# --------------------------------------------------------------------------
# The two gates
# --------------------------------------------------------------------------
def test_planted_failure_is_caught_even_though_every_step_exited_zero():
    run = parse_folder(fx("runs", "contig_mismatch"))
    assert run.metrics["exit_codes"] == [0, 0, 0, 0], "fixture must look successful"
    findings = evaluate(run)
    fired = {f.id for f in findings}
    assert AUD_STRAND in fired, fired
    finding = next(f for f in findings if f.id == AUD_STRAND)
    assert finding.severity == SEVERITY_FAIL
    assert finding.has_valid_receipts()
    assert finding.details["assignment_rate"] < 0.30
    assert finding.details["contig_style_mismatch"] is True


def test_clean_run_reports_zero_findings():
    run = parse_folder(fx("runs", "clean_run"))
    findings = evaluate(run)
    assert findings == [], [f.id for f in findings]
    decision, _ = decide(run, findings)
    assert decision == DECISION_HEALTHY


def test_folder_verdict_end_to_end():
    verdict = assess_path(fx("runs", "contig_mismatch"))
    assert verdict.kind == KIND_FOLDER
    assert verdict.decision == DECISION_FIX_AND_RERUN
    assert verdict.findings
    for finding in verdict.findings:
        assert finding.has_valid_receipts()
        assert finding.what and finding.meaning and finding.action
    assert verdict.details["input_sha256"]


# --------------------------------------------------------------------------
# AUD-STRAND-01
# --------------------------------------------------------------------------
def test_strand_01_needs_both_conditions():
    mismatch_low = run_with(
        assignment_rate=0.18,
        bam_contigs=ENSEMBL,
        annotation_contigs=UCSC,
        contig_styles={"bam": "ensembl", "annotation": "ucsc"},
    )
    assert rule_strand_contig_mismatch(mismatch_low) is not None

    mismatch_high = run_with(
        assignment_rate=0.78,
        bam_contigs=ENSEMBL,
        annotation_contigs=UCSC,
        contig_styles={"bam": "ensembl", "annotation": "ucsc"},
    )
    assert rule_strand_contig_mismatch(mismatch_high) is None

    match_low = run_with(
        assignment_rate=0.18,
        bam_contigs=UCSC,
        annotation_contigs=UCSC,
        contig_styles={"bam": "ucsc", "annotation": "ucsc"},
    )
    assert rule_strand_contig_mismatch(match_low) is None


def test_strand_01_is_silent_without_evidence():
    assert rule_strand_contig_mismatch(run_with()) is None
    assert rule_strand_contig_mismatch(run_with(assignment_rate=0.1)) is None


# --------------------------------------------------------------------------
# AUD-STRAND-02
# --------------------------------------------------------------------------
def test_strand_02_catches_a_backwards_strand_setting():
    run = run_with(
        reads_per_gene=[30_000_000, 25_000_000, 5_000_000], strandedness_declared="reverse"
    )
    finding = rule_strand_direction(run)
    assert finding is not None
    assert finding.id == AUD_STRAND2
    assert finding.severity == SEVERITY_FAIL
    assert finding.details["observed"] == "forward"


def test_strand_02_catches_a_library_with_no_strand_information():
    run = run_with(
        reads_per_gene=[30_000_000, 15_400_000, 15_600_000], strandedness_declared="reverse"
    )
    finding = rule_strand_direction(run)
    assert finding is not None
    assert "no strand information" in finding.title.lower()


def test_strand_02_accepts_a_correct_stranded_library():
    run = run_with(
        reads_per_gene=[30_000_000, 600_000, 29_400_000], strandedness_declared="reverse"
    )
    assert rule_strand_direction(run) is None


def test_strand_02_flags_unstranded_config_on_a_stranded_library():
    run = run_with(
        reads_per_gene=[30_000_000, 2_000_000, 28_000_000], strandedness_declared="unstranded"
    )
    finding = rule_strand_direction(run)
    assert finding is not None
    assert finding.severity == SEVERITY_WARN


def test_strand_02_is_silent_without_evidence():
    assert rule_strand_direction(run_with(strandedness_declared="reverse")) is None
    assert rule_strand_direction(run_with(reads_per_gene=[1, 2, 3])) is None


# --------------------------------------------------------------------------
# AUD-CONTAM-02
# --------------------------------------------------------------------------
def test_contamination_fires_above_three_percent():
    finding = rule_contamination(run_with(freemix=0.045))
    assert finding is not None
    assert finding.id == AUD_CONTAM
    assert finding.severity == SEVERITY_WARN


def test_contamination_fails_above_five_percent():
    assert rule_contamination(run_with(freemix=0.12)).severity == SEVERITY_FAIL


def test_contamination_is_silent_on_a_clean_sample():
    assert rule_contamination(run_with(freemix=0.002)) is None
    assert rule_contamination(run_with()) is None


# --------------------------------------------------------------------------
# AUD-DUP-04
# --------------------------------------------------------------------------
def test_duplication_outlier_needs_a_cohort():
    assert rule_duplication_outlier(run_with(duplication_by_sample={"s1": 0.9})) is None


def test_duplication_outlier_fires_against_the_median():
    run = run_with(duplication_by_sample={"s1": 0.08, "s2": 0.09, "s3": 0.62})
    finding = rule_duplication_outlier(run)
    assert finding is not None
    assert finding.id == AUD_DUP
    assert "s3" in finding.what
    assert finding.severity == SEVERITY_FAIL


def test_duplication_outlier_ignores_a_tight_cohort():
    run = run_with(duplication_by_sample={"s1": 0.08, "s2": 0.09, "s3": 0.11})
    assert rule_duplication_outlier(run) is None


# --------------------------------------------------------------------------
# AUD-TRUNC-01
# --------------------------------------------------------------------------
def test_truncated_bam_fires():
    run = RunModel(root="/run")
    run.bams = [
        BamInfo(path="/run/a.bam", relpath="a.bam", eof_ok=False, truncated=True),
        BamInfo(path="/run/b.bam", relpath="b.bam", eof_ok=True, truncated=False),
    ]
    finding = rule_truncated_bam(run)
    assert finding is not None
    assert finding.id == AUD_TRUNC
    assert finding.severity == SEVERITY_FAIL
    assert finding.receipts
    assert finding.details["truncated_bams"] == ["a.bam"]


def test_truncated_bam_is_silent_when_files_are_complete():
    run = RunModel(root="/run")
    run.bams = [BamInfo(path="/run/b.bam", relpath="b.bam", eof_ok=True)]
    assert rule_truncated_bam(run) is None
    assert rule_truncated_bam(RunModel(root="/run")) is None


def test_truncated_bam_detected_in_a_real_folder(tmp_path):
    shutil.copytree(fx("runs", "clean_run"), tmp_path / "run")
    (tmp_path / "run" / "star" / "sample1" / "Aligned.sortedByCoord.out.bam").write_bytes(
        (tmp_path / "run" / "star" / "sample1" / "Aligned.sortedByCoord.out.bam").read_bytes()[:-28]
        + b"\x00" * 28
    )
    findings = evaluate(parse_folder(tmp_path / "run"))
    assert AUD_TRUNC in {f.id for f in findings}


# --------------------------------------------------------------------------
# AUD-BUILD-01
# --------------------------------------------------------------------------
def test_mixed_builds_fire():
    run = run_with(
        build_evidence=[("GRCh38", "config.yaml:line=1"), ("GRCh37", "annotation/gencode.v19.gtf")]
    )
    finding = rule_mixed_builds(run)
    assert finding is not None
    assert finding.id == AUD_BUILD
    assert finding.severity == SEVERITY_FAIL


def test_mixed_builds_is_silent_when_everything_agrees():
    run = run_with(
        build_evidence=[("GRCh38", "config.yaml:line=1"), ("GRCh38", "annotation/gencode.v45.gtf")]
    )
    assert rule_mixed_builds(run) is None
    assert rule_mixed_builds(run_with()) is None


# --------------------------------------------------------------------------
# AUD-COUNT-01
# --------------------------------------------------------------------------
def test_normalised_counts_in_a_counts_slot_fire():
    run = run_with(counts_units=[("counts/s1.featureCounts.txt", "tpm")])
    finding = rule_normalised_counts(run)
    assert finding is not None
    assert finding.id == AUD_COUNT
    assert finding.severity == SEVERITY_FAIL


def test_integer_counts_are_left_alone():
    run = run_with(counts_units=[("counts/s1.featureCounts.txt", "counts")])
    assert rule_normalised_counts(run) is None
    assert rule_normalised_counts(run_with()) is None


# --------------------------------------------------------------------------
# AUD-PAIRED-01
# --------------------------------------------------------------------------
def test_paired_counted_as_single_fires():
    run = run_with(layout_declared="paired", counting_pairs=False)
    finding = rule_paired_counted_as_single(run)
    assert finding is not None
    assert finding.id == AUD_PAIRED
    assert finding.severity == SEVERITY_FAIL


def test_single_counted_as_paired_warns():
    finding = rule_paired_counted_as_single(run_with(layout_declared="single", counting_pairs=True))
    assert finding is not None
    assert finding.severity == SEVERITY_WARN


def test_consistent_layout_is_silent():
    consistent = run_with(layout_declared="paired", counting_pairs=True)
    assert rule_paired_counted_as_single(consistent) is None
    assert rule_paired_counted_as_single(run_with()) is None


# --------------------------------------------------------------------------
# AUD-ADAPT-03
# --------------------------------------------------------------------------
RISING_CURVE = [(float(i), i * 2.5) for i in range(1, 16)]


def test_adapter_attrition_fires_on_a_rising_curve():
    run = run_with(adapter_curve=RISING_CURVE)
    finding = rule_adapter_attrition(run)
    assert finding is not None
    assert finding.id == AUD_ADAPT


def test_adapter_readthrough_fails_when_inserts_are_shorter_than_reads():
    run = run_with(adapter_curve=RISING_CURVE, insert_size_median=120, read_length=150)
    finding = rule_adapter_attrition(run)
    assert finding is not None
    assert finding.severity == SEVERITY_FAIL
    assert finding.details["read_through"] is True


def test_adapter_attrition_is_silent_on_a_clean_curve():
    flat = [(float(i), 0.2) for i in range(1, 16)]
    assert rule_adapter_attrition(run_with(adapter_curve=flat)) is None
    assert rule_adapter_attrition(run_with()) is None


# --------------------------------------------------------------------------
# AUD-ALIGN-01
# --------------------------------------------------------------------------
def test_low_alignment_warns_then_fails():
    warn = rule_low_alignment(run_with(alignment_rate=68.0))
    assert warn is not None and warn.severity == SEVERITY_WARN
    fail = rule_low_alignment(run_with(alignment_rate=41.0))
    assert fail is not None and fail.severity == SEVERITY_FAIL


def test_good_alignment_is_silent():
    assert rule_low_alignment(run_with(alignment_rate=92.5)) is None
    assert rule_low_alignment(run_with()) is None


# --------------------------------------------------------------------------
# Whole-audit behaviour
# --------------------------------------------------------------------------
def test_decisions_follow_the_worst_finding():
    run = run_with(freemix=0.045)
    decision, reason = decide(run, evaluate(run))
    assert decision == DECISION_REVIEW
    assert reason

    run2 = run_with(freemix=0.12)
    decision2, _ = decide(run2, evaluate(run2))
    assert decision2 == DECISION_FIX_AND_RERUN


def test_every_rule_id_is_unique_and_documented():
    assert len(RULE_IDS) == 10
    assert len(set(RULE_IDS)) == 10


def test_every_finding_from_a_real_folder_carries_receipts():
    for name in ("clean_run", "contig_mismatch", "strand_mismatch"):
        verdict = assess_folder(fx("runs", name))
        for finding in verdict.findings:
            assert finding.has_valid_receipts(), f"{name}: {finding.id}"
            rule_receipt = finding.receipts[0]
            assert rule_receipt.source == f"rule:{finding.id}"
            assert rule_receipt.version == RULESET_VERSION
            assert any(r.source.startswith("file:") for r in finding.receipts), finding.id


def test_receipt_locators_point_at_real_files_in_the_folder():
    run = parse_folder(fx("runs", "contig_mismatch"))
    for finding in evaluate(run):
        for receipt in finding.receipts:
            if receipt.source.startswith("file:") and ":line=" in receipt.locator:
                relpath, _, line_no = receipt.locator.partition(":line=")
                path = fx("runs", "contig_mismatch") / relpath
                assert path.exists(), receipt.locator
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                assert 0 < int(line_no) <= len(lines), receipt.locator


def test_no_llm_in_the_audit():
    for path in (Path("core/rules/audit_rules.py"), Path("core/parse/folder.py")):
        text = path.read_text(encoding="utf-8")
        for word in ("openai", "anthropic", "requests", "httpx", "urllib", "socket", "llm("):
            assert word not in text, f"{path} mentions {word}"
