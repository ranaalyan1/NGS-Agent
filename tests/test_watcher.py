"""Unit tests for NGS-Agent log watcher module."""

from pathlib import Path
import pytest

from ngs_agent.watcher import (
    Signature,
    Match,
    load_signatures,
    _extract_value,
    match_line,
    scan_file,
    signatures_dir,
)

DATA_DIR = Path(__file__).parent / "data"

class TestSignatureLoading:
    """Test signature RAML loading and compilation."""

    def test_load_default_signatures(self):
        sigs = load_signatures()
        assert len(sigs) >= 5
        sig_ids = [s.id for s in sigs]
        assert "adapter_contamination" in sig_ids
        assert "low_alignment_rate" in sig_ids
        assert "low_coverage" in sig_ids
        assert "high_duplication" in sig_ids
        assert "poor_insert_size" in sig_ids

    def test_signatures_compiled(self):
        sigs = load_signatures()
        for sig in sigs:
            assert len(sig._compiled) > 0

class TestExtractValue:
    """Test numeric and scientific notation extraction from log lines."""

    def test_simple_integer(self):
        line = "Mean coverage: 18x"
        assert _extract_value(line, "Mean coverage") == 18.0

    def test_percentage_float(self):
        line = "Overall alignment rate: 74.2%"
        assert _extract_value(line, "Overall alignment rate") == 74.2

    def test_scientific_notation(self):
        line = "Error rate: 1.2e-4"
        assert _extract_value(line, "Error rate") == 0.00012

    def test_negative_number(self):
        line = "Score offset: -5.5"
        assert _extract_value(line, "Score offset") == -5.5

    def test_unlabeled_line_takes_last_number(self):
        line = "Read 10000 reads,mapped 7200"
        assert _extract_value(line, None) == 7200.0

    def test_empty_line_returns_none(self):
        assert _extract_value("", None) is None

class TestMatchLine:
    """Test matching single lines against signatures."""

    def test_low_alignment_rate_triggers(self):
        sigs = load_signatures()
        line = "Overall alignment rate: 68.5%"
        matches = match_line(line, 42, sigs)
        assert len(matches) > 0
        matched_sig_ids = [m.signature.id for m in matches]
        assert "low_alignment_rate" in matched_sig_ids
        assert matches[0].value == 68.5

    def test_good_alignment_rate_does_not_trigger(self):
        sigs = load_signatures()
        line = "Overall alignment rate: 98.2%"
        matches = match_line(line, 42, sigs)
        matched_sig_ids = [m.signature.id for m in matches]
        assert "low_alignment_rate" not in matched_sig_ids

    def test_comment_or_empty_line_skipped(self):
        sigs = load_signatures()
        assert match_line("# Comment line with 5% alignment", 1, sigs) == []
        assert match_line("   ", 2, sigs) == []

class TestScanFile:
    """Test scanning files."""

    def test_scan_file_returns_matches(self, tmp_path):
        log_file = tmp_path / "test.log"
        log_file.write_text(
            "Starting alignment...\n"
            "Overall alignment rate: 65.0%\n"
            "Duplicate rate: 45.0%\n"
            "Done.\n",
            encoding="utf-8",
        )
        sigs = load_signatures()
        matches = scan_file(log_file, sigs)
        assert len(matches) >= 2
        sig_ids = [m.signature.id for m in matches]
        assert "low_alignment_rate" in sig_ids
        assert "high_duplication" in sig_ids
