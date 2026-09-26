"""Station 2 (part 1) — the FastQC parser extracts facts and can cite them."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.models import FastQCFacts, ModuleStatus, Point
from core.parse.fastqc import (
    MOD_ADAPTER,
    MOD_DUP,
    MOD_QUALITY,
    FastQCParseError,
    _bin_start,
    parse_fastqc,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def test_parses_module_statuses_from_the_fixture():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    statuses = {m.name: m.status for m in facts.modules}
    assert statuses["Per base sequence quality"] == "fail"
    assert statuses["Adapter Content"] == "warn"
    assert statuses["Sequence Duplication Levels"] == "warn"
    assert statuses["Per base N content"] == "fail"
    # Every module we know about is present, with a status of its own.
    assert all(s in ("pass", "warn", "fail") for s in statuses.values())
    assert len(facts.modules) >= 8


def test_parses_basic_metrics():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    assert facts.total_sequences == 1_000_000
    assert facts.read_length == 150
    assert facts.read_length_range is None
    assert facts.fastqc_version == "0.12.1"
    assert facts.gc_percent == 45
    assert facts.source_sha256 and len(facts.source_sha256) == 64
    assert facts.data_member.endswith("fastqc_data.txt")


def test_parses_curves_with_source_line_numbers():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    assert len(facts.per_base_quality) == 15
    assert facts.per_base_quality[0].y == pytest.approx(35.8)
    assert facts.per_base_quality[-1].y == pytest.approx(11.6)
    # Every point must be traceable to a line in fastqc_data.txt.
    assert all(p.line > 0 for p in facts.per_base_quality)
    assert all(p.line > 0 for p in facts.adapter_content)
    assert all(p.line > 0 for p in facts.gc_curve)
    assert all(p.line > 0 for p in facts.n_content)


def test_adapter_curve_keeps_the_worst_adapter_at_each_position():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    peak = max(facts.adapter_content, key=lambda p: p.y)
    assert peak.y == pytest.approx(7.4)
    assert facts.module_status(MOD_ADAPTER) == "warn"


def test_duplication_is_derived_from_deduplicated_percentage():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    # fastqc_data.txt says 42% survives deduplication -> 58% duplicated.
    assert facts.duplication_percent == pytest.approx(58.0)
    assert facts.duplication_line > 0
    assert facts.module_status(MOD_DUP) == "warn"


def test_n_content_is_extracted():
    facts = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    assert max(p.y for p in facts.n_content) == pytest.approx(12.4)


def test_read_length_range_is_detected_when_reads_are_not_fixed_length():
    facts = parse_fastqc(fx("fastqc", "messy_fastqc.zip"))
    assert facts.read_length is None
    assert facts.read_length_range == (35, 150)


def test_clean_fixture_parses_with_no_failures():
    facts = parse_fastqc(fx("fastqc", "clean_fastqc.zip"))
    assert all(m.status == "pass" for m in facts.modules)
    assert facts.duplication_percent == pytest.approx(7.0)


def test_bin_labels_are_understood():
    assert _bin_start("1") == 1.0
    assert _bin_start("10-19") == 10.0
    assert _bin_start("100") == 100.0


def test_zip_without_fastqc_data_raises_a_parse_error(tmp_path):
    empty_zip = tmp_path / "not_fastqc.zip"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("readme.txt", "nothing to see\n")
    with pytest.raises(FastQCParseError):
        parse_fastqc(empty_zip)


def test_summary_txt_overrides_module_status(tmp_path):
    """FastQC's summary.txt is authoritative when present."""
    zpath = tmp_path / "override_fastqc.zip"
    data = (
        "##FastQC\t0.11.9\n>>Per base sequence quality\tpass\n#Base\tMean\n1\t36.0\n>>END_MODULE\n"
    )
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("s_fastqc/fastqc_data.txt", data)
        zf.writestr("s_fastqc/summary.txt", "FAIL\tPer base sequence quality\ts.fastq\n")
    facts = parse_fastqc(zpath)
    assert facts.module_status(MOD_QUALITY) == "fail"
    assert facts.fastqc_version == "0.11.9"


def test_facts_survive_a_dict_round_trip():
    original = parse_fastqc(fx("fastqc", "sample_fastqc.zip"))
    clone = FastQCFacts.from_dict(original.to_dict())
    assert clone.to_dict() == original.to_dict()
    assert clone.per_base_quality[0].line == original.per_base_quality[0].line


def test_empty_module_tables_do_not_crash(tmp_path):
    zpath = tmp_path / "sparse_fastqc.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("s_fastqc/fastqc_data.txt", "##FastQC\t0.12.1\n\n")
    facts = parse_fastqc(zpath)
    assert facts.modules == []
    assert facts.per_base_quality == []
    assert facts.duplication_percent is None


def test_garbage_rows_are_skipped_not_fatal(tmp_path):
    zpath = tmp_path / "dirty_fastqc.zip"
    data = (
        "##FastQC\t0.12.1\n"
        ">>Per base sequence quality\tpass\n"
        "#Base\tMean\n"
        "1\t36.0\n"
        "not-a-number\tnot-a-number\n"
        "2\tnan\n"
        ">>END_MODULE\n"
    )
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("s_fastqc/fastqc_data.txt", data)
    facts = parse_fastqc(zpath)
    # Only the one row with a real number survives; junk rows are dropped.
    assert len(facts.per_base_quality) == 1
    assert facts.per_base_quality[0].y == pytest.approx(36.0)


def test_module_and_point_models_are_serialisable():
    assert ModuleStatus("x", "pass").to_dict() == {"name": "x", "status": "pass"}
    assert Point(1.0, 2.0, "1", 7).to_dict() == {"x": 1.0, "y": 2.0, "label": "1", "line": 7}
