"""Station 2 (part 2) — six QC rules, each with a trigger and a non-trigger case.

The non-trigger cases matter as much as the trigger cases: a rule that fires on
healthy data is a false positive, and a PI will stop trusting the tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import (
    DECISION_HEALTHY,
    DECISION_RESEQUENCE,
    DECISION_TRIM_AND_PROCEED,
    SEVERITY_FAIL,
    SEVERITY_INFO,
    SEVERITY_WARN,
    FastQCFacts,
    Finding,
    Point,
    Receipt,
)
from core.parse.fastqc import parse_fastqc
from core.rules.qc_rules import (
    QC_ADAPT,
    QC_DUP,
    QC_GC,
    QC_LEN,
    QC_N,
    QC_QUAL,
    RULE_IDS,
    RULESET_VERSION,
    decide,
    evaluate,
    rule_adapter_content,
    rule_duplication,
    rule_gc_spike,
    rule_n_content,
    rule_quality_drop,
    rule_read_length,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def make_facts(**kwargs) -> FastQCFacts:
    """A bare FastQCFacts with just the fields a rule needs."""
    base = FastQCFacts(
        source_path="fixtures/fastqc/synthetic.zip",
        source_sha256="a" * 64,
        fastqc_version="0.12.1",
        data_member="s_fastqc/fastqc_data.txt",
    )
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


def curve(values: list[float], start_line: int = 10) -> list[Point]:
    return [
        Point(x=float(i + 1), y=v, label=str(i + 1), line=start_line + i)
        for i, v in enumerate(values)
    ]


def smooth_gc(peak_bin: int = 45, peak_height: float = 1000.0) -> list[Point]:
    import math

    return [
        Point(
            x=float(i),
            y=peak_height * math.exp(-((i - peak_bin) ** 2) / (2 * 6**2)),
            label=str(i),
            line=100 + i,
        )
        for i in range(101)
    ]


# --------------------------------------------------------------------------
# QC-QUAL-01
# --------------------------------------------------------------------------
def test_quality_drop_fires_and_names_the_trim_position():
    facts = make_facts(per_base_quality=curve([36] * 8 + [28, 24, 21, 18, 15, 12, 10]))
    finding = rule_quality_drop(facts)
    assert finding is not None
    assert finding.id == QC_QUAL
    assert finding.severity == SEVERITY_FAIL
    assert finding.details["trim_position"] == 12
    assert f"position {finding.details['trim_position']}" in finding.action
    assert finding.details["fixable_by_trimming"] is True


def test_quality_drop_warns_but_does_not_fail_above_q20():
    facts = make_facts(per_base_quality=curve([36] * 10 + [24, 23, 22]))
    finding = rule_quality_drop(facts)
    assert finding is not None
    assert finding.severity == SEVERITY_WARN
    assert finding.details["needs_resequencing"] is False


def test_quality_drop_does_not_fire_on_flat_quality():
    facts = make_facts(per_base_quality=curve([36.0] * 15))
    assert rule_quality_drop(facts) is None


def test_quality_collapse_recommends_resequencing_not_trimming():
    facts = make_facts(per_base_quality=curve([12.0] * 10 + [30.0] * 2))
    finding = rule_quality_drop(facts)
    assert finding is not None
    assert finding.details["needs_resequencing"] is True
    assert "Re-sequence" in finding.action


def test_quality_rule_is_silent_when_there_is_no_curve():
    assert rule_quality_drop(make_facts()) is None


# --------------------------------------------------------------------------
# QC-ADAPT-01
# --------------------------------------------------------------------------
def test_adapter_content_fires_above_five_percent():
    facts = make_facts(adapter_content=curve([0.1, 0.4, 1.2, 3.1, 6.4, 9.2]))
    finding = rule_adapter_content(facts)
    assert finding is not None
    assert finding.id == QC_ADAPT
    assert finding.severity == SEVERITY_WARN
    assert finding.details["adapter_percent"] == pytest.approx(9.2)
    assert "adapter trimming" in finding.action.lower()


def test_adapter_content_fails_above_ten_percent():
    facts = make_facts(adapter_content=curve([1.0, 5.5, 14.2]))
    finding = rule_adapter_content(facts)
    assert finding is not None
    assert finding.severity == SEVERITY_FAIL


def test_adapter_content_does_not_fire_at_five_percent():
    facts = make_facts(adapter_content=curve([0.1, 2.0, 5.0]))
    assert rule_adapter_content(facts) is None


def test_adapter_content_is_silent_without_a_curve():
    assert rule_adapter_content(make_facts()) is None


# --------------------------------------------------------------------------
# QC-DUP-01
# --------------------------------------------------------------------------
def test_duplication_informs_above_twenty_percent():
    facts = make_facts(duplication_percent=35.0, duplication_line=178)
    finding = rule_duplication(facts)
    assert finding is not None
    assert finding.id == QC_DUP
    assert finding.severity == SEVERITY_INFO
    assert "RNA-seq" in finding.meaning and "WGS" in finding.meaning


def test_duplication_warns_above_fifty_percent():
    finding = rule_duplication(make_facts(duplication_percent=58.0))
    assert finding is not None
    assert finding.severity == SEVERITY_WARN
    assert finding.details["needs_resequencing"] is False


def test_duplication_fails_above_seventy_percent():
    finding = rule_duplication(make_facts(duplication_percent=81.0))
    assert finding is not None
    assert finding.severity == SEVERITY_FAIL
    assert finding.details["needs_resequencing"] is True


def test_duplication_does_not_fire_on_a_clean_library():
    assert rule_duplication(make_facts(duplication_percent=7.0)) is None


def test_duplication_is_silent_when_fastqc_did_not_report_it():
    assert rule_duplication(make_facts(duplication_percent=None)) is None


# --------------------------------------------------------------------------
# QC-GC-01
# --------------------------------------------------------------------------
def test_gc_spike_fires_on_a_narrow_peak():
    curve_points = smooth_gc()
    curve_points[72] = Point(x=72.0, y=26000.0, label="72", line=172)
    facts = make_facts(gc_curve=curve_points)
    finding = rule_gc_spike(facts)
    assert finding is not None
    assert finding.id == QC_GC
    assert finding.severity == SEVERITY_FAIL
    assert "contamination" in finding.title.lower()
    assert finding.details["needs_resequencing"] is True


def test_gc_spike_does_not_fire_on_a_smooth_distribution():
    facts = make_facts(gc_curve=smooth_gc())
    assert rule_gc_spike(facts) is None


def test_gc_rule_is_silent_without_data():
    assert rule_gc_spike(make_facts()) is None
    assert rule_gc_spike(make_facts(gc_curve=[Point(x=1, y=1), Point(x=2, y=2)])) is None


# --------------------------------------------------------------------------
# QC-N-01
# --------------------------------------------------------------------------
def test_n_content_fires_and_names_the_cycle():
    facts = make_facts(n_content=curve([0.0, 0.1, 12.4, 11.9, 0.2]))
    finding = rule_n_content(facts)
    assert finding is not None
    assert finding.id == QC_N
    assert finding.severity == SEVERITY_WARN
    assert finding.details["cycle"] == "3"
    assert "cycle 3" in finding.action


def test_n_content_fails_above_twenty_percent():
    finding = rule_n_content(make_facts(n_content=curve([0.0, 24.0])))
    assert finding is not None
    assert finding.severity == SEVERITY_FAIL


def test_n_content_does_not_fire_at_five_percent():
    assert rule_n_content(make_facts(n_content=curve([0.0, 5.0, 1.0]))) is None


def test_n_content_is_silent_without_data():
    assert rule_n_content(make_facts()) is None


# --------------------------------------------------------------------------
# QC-LEN-01
# --------------------------------------------------------------------------
def test_read_length_fires_on_a_range():
    facts = make_facts(read_length=None, read_length_range=(35, 150))
    finding = rule_read_length(facts)
    assert finding is not None
    assert finding.id == QC_LEN
    assert finding.severity == SEVERITY_WARN


def test_read_length_does_not_fire_on_fixed_length():
    assert rule_read_length(make_facts(read_length=150, read_length_range=None)) is None
    # A range whose ends are equal is fixed-length too.
    assert rule_read_length(make_facts(read_length=None, read_length_range=(150, 150))) is None


# --------------------------------------------------------------------------
# Receipts — the Law of Receipts
# --------------------------------------------------------------------------
def test_every_finding_carries_a_rule_receipt_and_a_file_receipt():
    facts = make_facts(
        per_base_quality=curve([36] * 8 + [24, 18, 12]),
        adapter_content=curve([0.1, 6.2]),
        duplication_percent=58.0,
        n_content=curve([0.0, 9.0]),
        read_length_range=(35, 150),
    )
    findings = evaluate(facts)
    assert findings, "expected the synthetic facts to trigger rules"
    for finding in findings:
        assert finding.has_valid_receipts(), f"{finding.id} has invalid receipts"
        sources = [r.source for r in finding.receipts]
        assert any(s.startswith("rule:") for s in sources), finding.id
        assert any(s.startswith("file:") for s in sources), finding.id
        rule_receipt = next(r for r in finding.receipts if r.source.startswith("rule:"))
        assert rule_receipt.version == RULESET_VERSION
        # The locator names the FastQC module the number came from.
        assert rule_receipt.locator
        file_receipt = next(r for r in finding.receipts if r.source.startswith("file:"))
        assert "fastqc_data.txt" in file_receipt.locator


def test_receipt_locators_point_at_real_lines_in_the_fixture():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    for finding in evaluate(facts):
        file_receipt = next(r for r in finding.receipts if r.source.startswith("file:"))
        locator = file_receipt.locator
        if "line=" in locator:
            line_no = int(locator.split("line=")[1])
            text = Path(fx("fastqc", "sample_fastqc.zip")).read_bytes()
            import zipfile

            with zipfile.ZipFile(fx("fastqc", "sample_fastqc.zip")) as zf:
                lines = zf.read(facts.data_member).decode().splitlines()
            assert 0 < line_no <= len(lines)
            assert text  # fixture is non-empty
            assert lines[line_no - 1].strip(), "receipt points at a blank line"


# --------------------------------------------------------------------------
# Bottom line
# --------------------------------------------------------------------------
def test_clean_library_is_healthy():
    facts = parse_fastqc(fx("fastqc", "clean_fastqc.zip"))
    findings = evaluate(facts)
    assert findings == []
    decision, reason = decide(facts, findings)
    assert decision == DECISION_HEALTHY
    assert reason


def test_fixable_problems_mean_trim_and_proceed():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    findings = evaluate(facts)
    assert findings
    decision, reason = decide(facts, findings)
    assert decision == DECISION_TRIM_AND_PROCEED
    assert "fixable" in reason.lower() or "trim" in reason.lower()


def test_unfixable_problems_mean_resequence():
    facts = parse_fastqc(fx("fastqc", "messy_fastqc.zip"))
    findings = evaluate(facts)
    fired = {f.id for f in findings}
    assert fired == {QC_QUAL, QC_ADAPT, QC_DUP, QC_N, QC_LEN}, fired
    decision, _ = decide(facts, findings)
    assert decision == DECISION_RESEQUENCE


def test_gc_spike_fixture_triggers_the_contamination_rule():
    findings = evaluate(parse_fastqc(fx("fastqc", "gc_spike_fastqc.zip")))
    assert QC_GC in {f.id for f in findings}


def test_decision_is_never_left_unexplained():
    for name in (
        "clean_fastqc.zip",
        "sample_fastqc.zip",
        "messy_fastqc.zip",
        "gc_spike_fastqc.zip",
    ):
        facts = parse_fastqc(fx("fastqc", name))
        findings = evaluate(facts)
        decision, reason = decide(facts, findings)
        assert decision in {DECISION_HEALTHY, DECISION_TRIM_AND_PROCEED, DECISION_RESEQUENCE}
        assert len(reason.split(".")) >= 1 and len(reason) > 20


# --------------------------------------------------------------------------
# Scope guards
# --------------------------------------------------------------------------
def test_no_llm_or_network_code_in_the_qc_station():
    """Station 2 must reach its verdict without asking a model anything."""
    banned = ("openai", "anthropic", "requests", "httpx", "socket", "urllib", "llm(")
    roots = [
        Path("core/rules/qc_rules.py"),
        Path("core/parse/fastqc.py"),
        Path("core/models.py"),
    ]
    for root in roots:
        text = root.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{root} mentions {word}"


def test_rule_ids_are_stable_and_exhaustive():
    assert RULE_IDS == [QC_QUAL, QC_ADAPT, QC_DUP, QC_GC, QC_N, QC_LEN]
    assert len(RULE_IDS) == 6


def test_finding_model_round_trips():
    finding = Finding(
        id=QC_QUAL,
        title="t",
        severity=SEVERITY_WARN,
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
        details={"k": 1},
    )
    clone = Finding.from_dict(finding.to_dict())
    assert clone == finding
    assert clone.has_valid_receipts()
