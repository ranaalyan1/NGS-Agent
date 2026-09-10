"""Tests for samplesheet parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ngs_agent.bioinformatics.samplesheet import SampleSheetError, parse_samplesheet


@pytest.fixture()
def samplesheet(tmp_path: Path) -> Path:
    (tmp_path / "S1_R1.fastq.gz").touch()
    (tmp_path / "S1_R2.fastq.gz").touch()
    (tmp_path / "S2_R1.fastq.gz").touch()
    path = tmp_path / "samplesheet.csv"
    path.write_text(
        "sample,fastq_1,fastq_2,strandedness,condition\n"
        "S1,S1_R1.fastq.gz,S1_R2.fastq.gz,forward,control\n"
        "S2,S2_R1.fastq.gz,,unstranded,treated\n",
        encoding="utf-8",
    )
    return path


class TestParseSamplesheet:
    def test_basic_parse(self, samplesheet: Path) -> None:
        records = parse_samplesheet(samplesheet)
        assert [record.sample for record in records] == ["S1", "S2"]
        assert records[0].paired_end is True
        assert records[0].fastq_r1 == samplesheet.parent / "S1_R1.fastq.gz"
        assert records[0].strandedness == "forward"
        assert records[0].condition == "control"
        assert records[1].paired_end is False

    def test_header_aliases(self, tmp_path: Path) -> None:
        path = tmp_path / "s.csv"
        path.write_text("sample_id,read1,read2\nWT,data_R1.fq,data_R2.fq\n", encoding="utf-8")
        (tmp_path / "data_R1.fq").touch()
        (tmp_path / "data_R2.fq").touch()
        record = parse_samplesheet(path)[0]
        assert record.fastq_r1.name == "data_R1.fq"
        assert record.paired_end

    def test_tsv_detection(self, tmp_path: Path) -> None:
        path = tmp_path / "s.tsv"
        path.write_text("sample\tfastq_1\nA\tA.fq.gz\n", encoding="utf-8")
        (tmp_path / "A.fq.gz").touch()
        record = parse_samplesheet(path)[0]
        assert record.sample == "A"

    def test_missing_sample_column_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        path.write_text("fastq_1\nx.fq\n", encoding="utf-8")
        with pytest.raises(SampleSheetError, match="sample column"):
            parse_samplesheet(path)

    def test_missing_fastq1_column_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        path.write_text("sample\nA\n", encoding="utf-8")
        with pytest.raises(SampleSheetError, match="fastq_1"):
            parse_samplesheet(path)

    def test_duplicate_samples_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "dup.csv"
        path.write_text("sample,fastq_1\nA,a.fq\nA,b.fq\n", encoding="utf-8")
        with pytest.raises(SampleSheetError, match="Duplicate sample"):
            parse_samplesheet(path)

    def test_invalid_strandedness_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.csv"
        path.write_text("sample,fastq_1,strandedness\nA,a.fq,sideways\n", encoding="utf-8")
        with pytest.raises(SampleSheetError, match="strandedness"):
            parse_samplesheet(path, validate_files=False)

    def test_missing_files_raise_when_validating(self, tmp_path: Path) -> None:
        path = tmp_path / "s.csv"
        path.write_text("sample,fastq_1\nA,missing.fq.gz\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="missing.fq.gz"):
            parse_samplesheet(path)

    def test_relative_path_resolution_fallback_chain(self, tmp_path: Path) -> None:
        # Samplesheet in data/ but paths relative to the experiment root.
        data = tmp_path / "data"
        data.mkdir()
        (tmp_path / "reads").mkdir()
        (tmp_path / "reads" / "A_R1.fastq.gz").touch()
        path = data / "samplesheet.csv"
        path.write_text("sample,fastq_1\nA,reads/A_R1.fastq.gz\n", encoding="utf-8")
        record = parse_samplesheet(path)[0]
        assert record.fastq_r1 == tmp_path / "reads" / "A_R1.fastq.gz"

    def test_absolute_paths_pass_through(self, tmp_path: Path) -> None:
        fastq = tmp_path / "abs.fastq.gz"
        fastq.touch()
        path = tmp_path / "s.csv"
        path.write_text(f"sample,fastq_1\nA,{fastq}\n", encoding="utf-8")
        record = parse_samplesheet(path)[0]
        assert record.fastq_r1 == fastq
