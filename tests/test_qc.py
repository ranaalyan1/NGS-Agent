"""Unit tests for multi-format QC parser."""

from pathlib import Path
import pytest
from ngs_agent.qc import QCParser, QCMetric

DATA_DIR = Path(__file__).parent / "data"


class TestQCParser:
    def test_fastqc_data_parsing(self):
        """FastQC data files should extract Total Sequences, %GC, and modules."""
        fastqc_file = DATA_DIR / "fastqc_data.txt"
        metrics = QCParser.parse(fastqc_file)
        assert len(metrics) > 0
        
        names = [m.name for m in metrics]
        assert "Total Sequences" in names
        assert "GC Content" in names
        
        gc_metric = next(m for m in metrics if m.name == "GC Content")
        assert "45" in gc_metric.value
        assert gc_metric.status == "pass"

    def test_samtools_flagstat_parsing(self, tmp_path):
        """Samtools flagstat text should extract mapping rate and duplicate percentage."""
        stat_file = tmp_path / "align.flagstat"
        stat_file.write_text(
            "1000000 + 0 in total (QC-passed reads + QC-failed reads)\n"
            "50000 + 0 duplicates\n"
            "950000 + 0 mapped (95.00% : N/A)\n",
            encoding="utf-8",
        )
        metrics = QCParser.parse(stat_file)
        assert len(metrics) >= 2
        
        map_metric = next(m for m in metrics if m.name == "Mapping Rate")
        assert map_metric.value == "95.0%"
        assert map_metric.status == "pass"

    def test_generic_summary_parsing(self, tmp_path):
        """Plaintext summary with Q30 and coverage should be extracted."""
        summary_file = tmp_path / "qc_summary.txt"
        summary_file.write_text(
            "Mapping rate: 88.5%\n"
            "Mean coverage: 42.1\n"
            "Q30: 91.2%\n"
            "Duplication rate: 12.4%\n",
            encoding="utf-8",
        )
        metrics = QCParser.parse(summary_file)
        assert len(metrics) == 4
        statuses = {m.name: m.status for m in metrics}
        assert statuses["Mapping Rate"] == "warn"  # 88.5% < 90%
        assert statuses["Mean Coverage"] == "pass"
        assert statuses["Q30 Fraction"] == "pass"
        assert statuses["Duplication Rate"] == "pass"
