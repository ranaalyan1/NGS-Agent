"""Combined quality summaries (MultiQC): sniffed by content, judged per sample.

The four fixtures exercise the four decisions a summary can produce:
general-stats table -> TRIM_AND_PROCEED, clean table -> HEALTHY,
data JSON -> RESEQUENCE, report HTML -> REVIEW.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.answer import (
    SECTION_MATTERS,
    SECTION_RECEIPTS,
    SECTION_TODO,
    SECTION_WHAT,
    answer_verdict,
    has_jargon,
    primary_text,
)
from core.assess import assess_multiqc, assess_path
from core.models import (
    CONFIDENCE_HIGH,
    CONFIDENCE_NONE,
    DECISION_HEALTHY,
    DECISION_RESEQUENCE,
    DECISION_REVIEW,
    DECISION_TRIM_AND_PROCEED,
    DECISION_UNKNOWN,
    KIND_MULTIQC,
    KIND_UNKNOWN,
    MultiQCFacts,
    Verdict,
)
from core.parse.folder import parse_folder
from core.parse.multiqc import MultiQCParseError, parse_multiqc
from core.report import render_html
from core.rules.multiqc_rules import decide, evaluate
from core.sniff import ACTION_PARSE_MULTIQC, sniff
from core.version import RULESET_VERSION, TOOL_VERSION
from doors.cli import EXIT_FAILED, EXIT_OK, main
from doors.gui.app import app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA_RE = re.compile(r"^[0-9a-f]{12}$")


def fx(*parts: str) -> Path:
    path = FIXTURES.joinpath(*parts)
    assert path.exists(), f"missing fixture: {path} (run scripts/make_fixtures.py)"
    return path


# --------------------------------------------------------------------------
# Sniffing: content wins over names
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "multiqc_general_stats.txt",
        "clean_general_stats.txt",
        "multiqc_data.json",
        "multiqc_report.html",
    ],
)
def test_each_shape_is_detected(name):
    result = sniff(fx("multiqc", name))
    assert result.kind == KIND_MULTIQC
    assert result.confidence == CONFIDENCE_HIGH
    assert result.suggested_action == ACTION_PARSE_MULTIQC


@pytest.mark.parametrize("disguise", ["renamed.txt", "renamed.log", "renamed.vcf", "no_extension"])
def test_disguised_summaries_are_still_detected(tmp_path, disguise):
    for name in ("multiqc_general_stats.txt", "multiqc_data.json", "multiqc_report.html"):
        renamed = tmp_path / disguise
        shutil.copyfile(fx("multiqc", name), renamed)
        assert sniff(renamed).kind == KIND_MULTIQC, f"{name} as {disguise}"


def test_a_samplesheet_is_not_a_summary(tmp_path):
    sheet = tmp_path / "samplesheet.csv"
    sheet.write_text(
        "sample,fastq_1,fastq_2,strandedness\n"
        "sample1,/data/s1_R1.fq.gz,/data/s1_R2.fq.gz,reverse\n",
        encoding="utf-8",
    )
    assert sniff(sheet).kind == KIND_UNKNOWN


def test_a_plain_table_starting_with_sample_is_not_a_summary(tmp_path):
    table = tmp_path / "counts.tsv"
    table.write_text("Sample\tgeneA\tgeneB\nsample1\t10\t20\n", encoding="utf-8")
    assert sniff(table).kind == KIND_UNKNOWN


def test_json_without_multiqc_keys_is_not_a_summary(tmp_path):
    plain = tmp_path / "config.json"
    plain.write_text('{"sample": "s1", "threshold": 0.5}\n', encoding="utf-8")
    assert sniff(plain).kind == KIND_UNKNOWN


def test_html_without_multiqc_is_not_a_summary(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(
        "<html><body><h1>General Statistics</h1><p>no tables here</p></body></html>",
        encoding="utf-8",
    )
    assert sniff(page).kind == KIND_UNKNOWN


def test_vcf_first_line_wins_over_multiqc_text_appearing_later(tmp_path):
    tricky = tmp_path / "tricky.dat"
    tricky.write_bytes(b"##fileformat=VCFv4.2\n# report_general_stats_data in a comment\n")
    from core.models import KIND_VCF

    assert sniff(tricky).kind == KIND_VCF


# --------------------------------------------------------------------------
# Parsing: numbers out, with their provenance
# --------------------------------------------------------------------------
def test_general_stats_table_parses_four_samples_with_row_numbers():
    facts = parse_multiqc(fx("multiqc", "multiqc_general_stats.txt"))
    assert facts.format == "general_stats"
    assert [s.name for s in facts.samples] == ["sample1", "sample2", "sample3", "sample4"]
    by_name = {s.name: s for s in facts.samples}
    assert by_name["sample2"].duplication_percent == pytest.approx(58.3)
    assert by_name["sample3"].gc_percent == pytest.approx(62.0)
    assert by_name["sample4"].read_length == 100
    assert by_name["sample1"].total_sequences == 20_000_000
    assert by_name["sample1"].row == 2
    assert by_name["sample4"].row == 5
    # Tables carry no per-position curves: the curve rules must stay silent.
    assert all(not s.per_base_quality for s in facts.samples)


def test_json_parses_samples_curves_and_version():
    facts = parse_multiqc(fx("multiqc", "multiqc_data.json"))
    assert facts.format == "json"
    assert facts.multiqc_version == "1.21"
    assert [s.name for s in facts.samples] == ["mqc_sample1", "mqc_sample2"]
    bad = next(s for s in facts.samples if s.name == "mqc_sample1")
    assert bad.duplication_percent == pytest.approx(74.2)
    assert len(bad.per_base_quality) == 15
    assert min(p.y for p in bad.per_base_quality) < 20.0
    good_adapter = next(s for s in facts.samples if s.name == "mqc_sample2")
    assert max(p.y for p in good_adapter.adapter_content) > 10.0


def test_html_table_is_recovered_from_markup():
    facts = parse_multiqc(fx("multiqc", "multiqc_report.html"))
    assert facts.format == "html"
    assert [s.name for s in facts.samples] == ["html_sample1", "html_sample2"]
    bad = facts.samples[0]
    assert bad.duplication_percent == pytest.approx(66.4)
    assert bad.read_length == 150
    assert bad.total_sequences == 18_500_000  # "18.5" under an "M Seqs" header


def test_empty_and_garbage_inputs_raise_instead_of_guessing(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    with pytest.raises(MultiQCParseError):
        parse_multiqc(empty)
    garbage = tmp_path / "garbage.txt"
    garbage.write_text("Sample\njust one column\n", encoding="utf-8")
    with pytest.raises(MultiQCParseError):
        parse_multiqc(garbage)


def test_facts_survive_a_dict_round_trip():
    facts = parse_multiqc(fx("multiqc", "multiqc_data.json"))
    assert MultiQCFacts.from_dict(facts.to_dict()) == facts


# --------------------------------------------------------------------------
# Rules: the same QC judgments, per sample
# --------------------------------------------------------------------------
def test_table_summary_fires_dup_length_and_gc_cohort_findings():
    facts = parse_multiqc(fx("multiqc", "multiqc_general_stats.txt"))
    findings = evaluate(facts)
    assert {f.id for f in findings} == {"QC-DUP-01", "QC-LEN-01", "QC-GC-01"}
    dup = next(f for f in findings if f.id == "QC-DUP-01")
    assert "sample2" in dup.title
    assert dup.details["sample"] == "sample2"
    outlier = next(f for f in findings if f.id == "QC-GC-01")
    assert outlier.details["outlier_samples"] == ["sample3"]
    lengths = next(f for f in findings if f.id == "QC-LEN-01")
    assert lengths.details["read_length_range"] == [100, 150]


def test_json_summary_fires_dup_quality_and_adapter_findings():
    facts = parse_multiqc(fx("multiqc", "multiqc_data.json"))
    findings = evaluate(facts)
    assert {f.id for f in findings} == {"QC-DUP-01", "QC-QUAL-01", "QC-ADAPT-01"}
    dup = next(f for f in findings if f.id == "QC-DUP-01")
    assert dup.severity == "fail"
    assert dup.details["needs_resequencing"] is True
    qual = next(f for f in findings if f.id == "QC-QUAL-01")
    assert qual.details["sample"] == "mqc_sample1"
    assert qual.details["trim_position"] == 9
    adapt = next(f for f in findings if f.id == "QC-ADAPT-01")
    assert adapt.details["sample"] == "mqc_sample2"


def test_clean_summary_produces_no_findings():
    facts = parse_multiqc(fx("multiqc", "clean_general_stats.txt"))
    assert evaluate(facts) == []


def test_decisions_cover_all_four_outcomes():
    table = parse_multiqc(fx("multiqc", "multiqc_general_stats.txt"))
    assert decide(table, evaluate(table))[0] == DECISION_TRIM_AND_PROCEED
    clean = parse_multiqc(fx("multiqc", "clean_general_stats.txt"))
    assert decide(clean, evaluate(clean))[0] == DECISION_HEALTHY
    blob = parse_multiqc(fx("multiqc", "multiqc_data.json"))
    assert decide(blob, evaluate(blob))[0] == DECISION_RESEQUENCE
    html = parse_multiqc(fx("multiqc", "multiqc_report.html"))
    decision, reason = decide(html, evaluate(html))
    assert decision == DECISION_REVIEW
    assert "html_sample1" in reason


def test_every_finding_carries_valid_receipts_with_sample_provenance():
    for name in ("multiqc_general_stats.txt", "multiqc_data.json", "multiqc_report.html"):
        for finding in evaluate(parse_multiqc(fx("multiqc", name))):
            assert finding.has_valid_receipts(), f"{name}: {finding.id}"
            assert finding.receipts[0].source == f"rule:{finding.id}"
            assert finding.receipts[0].version == RULESET_VERSION
            file_receipts = [r for r in finding.receipts if r.source.startswith("file:")]
            assert file_receipts, f"{name}: {finding.id} has no file receipt"
            for receipt in file_receipts:
                assert SHA_RE.match(receipt.source.removeprefix("file:"))
                assert TIMESTAMP_RE.match(receipt.timestamp)
                assert "sample" in receipt.locator or "line=" in receipt.locator
            assert finding.what and finding.meaning and finding.action


def test_table_receipts_cite_real_line_numbers():
    findings = evaluate(parse_multiqc(fx("multiqc", "multiqc_general_stats.txt")))
    dup = next(f for f in findings if f.id == "QC-DUP-01")
    locator = next(r.locator for r in dup.receipts if r.source.startswith("file:"))
    assert locator.endswith(":line=3")
    lines = fx("multiqc", "multiqc_general_stats.txt").read_text(encoding="utf-8").splitlines()
    assert lines[2].startswith("sample2\t")


# --------------------------------------------------------------------------
# End to end through the front door
# --------------------------------------------------------------------------
def test_assess_routes_summaries_to_multiqc_verdicts():
    verdict = assess_path(fx("multiqc", "multiqc_general_stats.txt"))
    assert verdict.kind == KIND_MULTIQC
    assert verdict.decision == DECISION_TRIM_AND_PROCEED
    assert verdict.details["n_samples"] == 4
    assert verdict.tool_version == TOOL_VERSION
    assert verdict.ruleset_version == RULESET_VERSION


def test_assess_says_what_was_not_judged():
    verdict = assess_multiqc(fx("multiqc", "multiqc_general_stats.txt"))
    joined = " ".join(verdict.unknown)
    assert "quality was not judged" in joined
    assert "adapter content was not judged" in joined


def test_broken_summary_is_unknown_not_a_crash(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text('{"report_general_stats_data": [}', encoding="utf-8")
    # Still recognised as MultiQC output from its markers...
    assert sniff(broken).kind == KIND_MULTIQC
    # ...but answered honestly when the data will not parse.
    verdict = assess_path(broken)
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []


def test_verdict_json_round_trips_without_loss():
    verdict = assess_path(fx("multiqc", "multiqc_data.json"))
    clone = Verdict.from_json(verdict.to_json())
    assert clone.to_dict() == verdict.to_dict()


# --------------------------------------------------------------------------
# Language, report, doors
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "multiqc_general_stats.txt",
        "clean_general_stats.txt",
        "multiqc_data.json",
        "multiqc_report.html",
    ],
)
def test_primary_output_has_no_jargon(name):
    answer = answer_verdict(assess_path(fx("multiqc", name)))
    assert [b.heading for b in answer.blocks] == [
        SECTION_WHAT,
        SECTION_MATTERS,
        SECTION_TODO,
        SECTION_RECEIPTS,
    ]
    assert has_jargon(primary_text(answer)) == []


def test_what_this_is_names_the_sample_count():
    answer = answer_verdict(assess_path(fx("multiqc", "multiqc_general_stats.txt")))
    line = answer.section(SECTION_WHAT).lines[0]
    assert "4 samples" in line


def test_html_report_has_no_external_assets_and_shows_receipts():
    verdict = assess_path(fx("multiqc", "multiqc_data.json"))
    html = render_html(verdict)
    for marker in ("http://", "https://", "<script", "<link", "<img", "src=", "cdn."):
        assert marker not in html
    for finding in verdict.findings:
        assert finding.title in html
        for receipt in finding.receipts:
            assert receipt.source in html


def test_cli_exit_codes_follow_severity(capsys):
    assert main([str(fx("multiqc", "multiqc_data.json"))]) == EXIT_FAILED
    capsys.readouterr()
    assert main([str(fx("multiqc", "clean_general_stats.txt"))]) == EXIT_OK
    capsys.readouterr()
    # Warnings only: exit 0, like the FastQC door.
    assert main([str(fx("multiqc", "multiqc_general_stats.txt"))]) == EXIT_OK
    out = capsys.readouterr().out
    assert "combined quality-control summary" in out


def test_box_answers_a_summary_upload():
    client = TestClient(app)
    data = {"file": ("multiqc_data.json", fx("multiqc", "multiqc_data.json").read_bytes())}
    response = client.post("/analyze", files=data)
    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == KIND_MULTIQC
    assert payload["decision"] == DECISION_RESEQUENCE
    assert payload["findings"]


# --------------------------------------------------------------------------
# Folders: summaries count as QC evidence
# --------------------------------------------------------------------------
def test_folder_audit_uses_summary_duplication_for_the_cohort_check(tmp_path):
    shutil.copyfile(fx("multiqc", "multiqc_general_stats.txt"), tmp_path / "summary.txt")
    run = parse_folder(tmp_path)
    assert run.qc_files, "the summary was not recognised as a QC file"
    by_sample = run.metrics.get("duplication_by_sample", {})
    assert by_sample["sample2"] == pytest.approx(0.583)
    verdict = assess_path(tmp_path)
    assert any(f.id == "AUD-DUP-04" for f in verdict.findings)


def test_folder_prefers_picard_metrics_over_summary_numbers(tmp_path):
    shutil.copyfile(fx("multiqc", "multiqc_general_stats.txt"), tmp_path / "summary.txt")
    (tmp_path / "sample2.MarkDuplicates.metrics.txt").write_text(
        "## METRICS CLASS\tpicard.sam.DuplicationMetrics\n"
        "LIBRARY\tPERCENT_DUPLICATION\n"
        "sample2\t0.123456\n",
        encoding="utf-8",
    )
    run = parse_folder(tmp_path)
    assert run.metrics["duplication_by_sample"]["sample2"] == pytest.approx(0.123456)


def test_sniff_never_raises_on_summaries():
    for name in ("multiqc_general_stats.txt", "multiqc_data.json", "multiqc_report.html"):
        result = sniff(fx("multiqc", name))
        assert result.kind == KIND_MULTIQC
        assert result.confidence == CONFIDENCE_HIGH
    assert sniff(fx("misc", "plain.txt")).confidence == CONFIDENCE_NONE
