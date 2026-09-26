"""Station 3 — the answer writer and the forwardable HTML report.

The tests here stand in for two human gates:
  * the "forward test" — can this HTML be emailed to a PI and still make sense?
  * the "no loss" test — does the JSON sidecar hold the whole verdict?
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from core.answer import (
    SECTION_MATTERS,
    SECTION_RECEIPTS,
    SECTION_TODO,
    SECTION_WHAT,
    Answer,
    answer_text,
    answer_verdict,
    has_jargon,
    primary_text,
)
from core.assess import assess_path
from core.models import (
    DECISION_UNKNOWN,
    Finding,
    Receipt,
    Verdict,
    unknown_verdict,
)
from core.report import render_html, render_json, write_report
from core.version import RULESET_VERSION, TOOL_VERSION

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


SECTIONS = [SECTION_WHAT, SECTION_MATTERS, SECTION_TODO, SECTION_RECEIPTS]


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------
def test_answer_has_the_four_sections_in_fixed_order():
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    answer = answer_verdict(verdict)
    assert [b.heading for b in answer.blocks] == SECTIONS
    assert answer.to_text()


def test_clean_verdict_still_has_all_four_sections():
    verdict = assess_path(fx("fastqc", "clean_fastqc.zip"))
    answer = answer_verdict(verdict)
    assert [b.heading for b in answer.blocks] == SECTIONS
    assert answer.section(SECTION_MATTERS).lines
    assert "Nothing stood out" in answer.section(SECTION_MATTERS).lines[0]
    assert answer.section(SECTION_TODO).lines == ["Nothing to fix. Carry on with the next step."]


def test_unknown_verdict_names_the_file_type_without_mangling_it():
    """'This is a VCF variant file', never 'This is vcf variant file'."""
    for name in ("sample.vcf", "mystery.txt", "vcf_no_extension", "sample.vcf.gz"):
        answer = answer_verdict(assess_path(fx("vcf", name)))
        line = answer.section(SECTION_WHAT).lines[0]
        assert line.startswith(("This is a ", "This is an ", "I could not")), line
        assert " vcf " not in line, line  # never lower-cased mid-sentence


def test_unknown_verdict_is_honest_and_still_structured():
    verdict = assess_path(fx("misc", "plain.txt"))
    answer = answer_verdict(verdict)
    assert [b.heading for b in answer.blocks] == SECTIONS
    text = answer.to_text()
    assert "could not" in text.lower() or "cannot" in text.lower()
    # No fabricated findings: nothing to do but hand it to a human.
    assert answer.section(SECTION_MATTERS).lines == verdict.unknown


def test_vcf_verdict_says_out_of_scope_plainly():
    answer = answer_verdict(assess_path(fx("vcf", "sample.vcf")))
    text = answer.to_text().lower()
    assert "out of scope" in text or "could not" in text
    assert "receipts" in text


# --------------------------------------------------------------------------
# Language
# --------------------------------------------------------------------------
def test_primary_output_has_no_jargon():
    """Jargon lives behind details, never in the sentences a PI reads."""
    targets = [
        fx("fastqc", name)
        for name in ("sample_fastqc.zip", "messy_fastqc.zip", "gc_spike_fastqc.zip")
    ]
    targets += [fx("runs", name) for name in ("clean_run", "contig_mismatch", "strand_mismatch")]
    targets += [
        fx("logs", name)
        for name in ("nextflow_star_index.log", "nextflow_warnings.log", "nextflow_nomatch.log")
    ]
    for path in targets:
        answer = answer_verdict(assess_path(path))
        primary = primary_text(answer)
        assert has_jargon(primary) == [], f"{path}: {has_jargon(primary)}"


def test_every_finding_answers_the_three_questions():
    verdict = assess_path(fx("fastqc", "messy_fastqc.zip"))
    for finding in verdict.findings:
        assert finding.what and finding.meaning and finding.action
        assert finding.action.count(".") >= 0
    text = answer_text(verdict)
    assert "What it is:" in text and "What it means:" in text


def test_what_to_do_is_numbered_and_deduplicated():
    verdict = assess_path(fx("fastqc", "messy_fastqc.zip"))
    lines = answer_verdict(verdict).section(SECTION_TODO).lines
    assert lines
    assert all(line.startswith(f"{i}.") for i, line in enumerate(lines, start=1))
    actions = [line.split(". ", 1)[1] for line in lines]
    assert len(actions) == len(set(actions))


# --------------------------------------------------------------------------
# The forward test
# --------------------------------------------------------------------------
EXTERNAL_MARKERS = [
    "http://",
    "https://",
    "<script",
    "<link",
    "<img",
    "@import",
    "src=",
    "url(",
    "cdn.",
]


def test_html_has_no_external_assets():
    html = render_html(assess_path(fx("fastqc", "sample_fastqc.zip")))
    for marker in EXTERNAL_MARKERS:
        assert marker not in html, f"report depends on external asset: {marker}"


def test_html_contains_inline_css_and_is_a_single_document():
    html = render_html(assess_path(fx("fastqc", "sample_fastqc.zip")))
    assert "<style>" in html
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")


def test_html_shows_every_finding_and_its_receipts():
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    html = render_html(verdict)
    for finding in verdict.findings:
        assert finding.title in html, f"missing finding {finding.id}"
        assert finding.action in html
        for receipt in finding.receipts:
            assert receipt.source in html, f"missing receipt {receipt.source}"
            assert receipt.locator.split(":")[-1] in html


def test_html_footer_carries_versions_timestamp_and_input_hash():
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    html = render_html(verdict)
    assert TOOL_VERSION in html
    assert RULESET_VERSION in html
    assert verdict.timestamp in html
    assert verdict.details["input_sha256"] in html


def test_html_escapes_hostile_text():
    """A filename must never be able to inject markup into a forwarded report."""
    verdict = unknown_verdict("<script>alert(1)</script>.zip", "unknown", "nasty name")
    verdict.receipts = [
        Receipt(source="file:<b>x</b>", version="1", timestamp="2026-09-01T00:00:00Z", locator="y")
    ]
    html = render_html(verdict)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_html_is_well_formed():
    """Cheap structural check: every opened tag is closed in order."""

    class Checker(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):  # noqa: D102
            if tag not in ("meta", "br", "line", "rect", "polyline", "text", "path"):
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            elif tag in self.stack:
                self.errors.append(f"unclosed tags before </{tag}>: {self.stack}")

    checker = Checker()
    checker.feed(render_html(assess_path(fx("fastqc", "messy_fastqc.zip"))))
    assert checker.errors == []
    assert checker.stack == []


def test_report_files_are_written_side_by_side(tmp_path):
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    html_path, json_path = write_report(verdict, tmp_path)
    assert html_path.exists() and json_path.exists()
    assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["verdict"]["subject"] == verdict.subject


# --------------------------------------------------------------------------
# JSON sidecar
# --------------------------------------------------------------------------
def test_json_round_trips_through_the_models():
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    payload = json.loads(render_json(verdict))
    clone = Verdict.from_dict(payload["verdict"])
    assert clone.to_dict() == verdict.to_dict()
    assert clone.findings[0].receipts == verdict.findings[0].receipts
    assert clone.details == verdict.details


def test_json_carries_the_answer_blocks_and_versions():
    payload = json.loads(render_json(assess_path(fx("fastqc", "clean_fastqc.zip"))))
    assert set(payload["answer"]) == set(SECTIONS)
    assert payload["tool_version"] == TOOL_VERSION
    assert payload["ruleset_version"] == RULESET_VERSION
    assert payload["schema"]


def test_json_round_trip_survives_an_unknown_verdict(tmp_path):
    verdict = assess_path(fx("misc", "plain.txt"))
    payload = json.loads(render_json(verdict))
    clone = Verdict.from_dict(payload["verdict"])
    assert clone.decision == DECISION_UNKNOWN
    assert clone.to_dict() == verdict.to_dict()


def test_receipts_in_json_are_complete():
    verdict = assess_path(fx("fastqc", "messy_fastqc.zip"))
    payload = json.loads(render_json(verdict))
    for finding in payload["verdict"]["findings"]:
        assert finding["receipts"], finding["id"]
        for receipt in finding["receipts"]:
            for key in ("source", "version", "timestamp", "locator"):
                assert receipt[key], f"{finding['id']} receipt missing {key}"


# --------------------------------------------------------------------------
# No LLM
# --------------------------------------------------------------------------
def test_answer_and_report_contain_no_llm_calls():
    banned = ("openai", "anthropic", "requests", "httpx", "urllib", "socket", "llm(")
    for path in (Path("core/answer.py"), Path("core/report.py")):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{path} mentions {word}"


def test_answer_block_model_is_simple_and_serialisable():
    block = Answer(blocks=[]).blocks
    assert block == []
    finding = Finding(
        id="QC-QUAL-01",
        title="t",
        severity="warn",
        what="w",
        meaning="m",
        action="a",
        receipts=[
            Receipt(
                source="rule:QC-QUAL-01",
                version=RULESET_VERSION,
                timestamp="2026-09-26T00:00:00Z",
                locator="Per base sequence quality",
            )
        ],
    )
    assert Finding.from_dict(finding.to_dict()).details == {}
    assert finding.has_valid_receipts()
