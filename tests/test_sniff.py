"""Station 1 — The Sniffer.

The sniffer's whole job is to be right about CONTENT and indifferent to names.
Most tests here are traps: files whose names lie.
"""

from __future__ import annotations

import gzip
import shutil
import zipfile
from pathlib import Path

import pytest

from core.models import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    KIND_BAM,
    KIND_FASTQC_ZIP,
    KIND_FOLDER,
    KIND_NEXTFLOW_LOG,
    KIND_UNKNOWN,
    KIND_VCF,
    SniffResult,
)
from core.sniff import (
    ACTION_AUDIT_FOLDER,
    ACTION_DIAGNOSE_LOG,
    ACTION_PARSE_FASTQC,
    ACTION_UNKNOWN,
    sniff,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    path = FIXTURES.joinpath(*parts)
    assert path.exists(), f"missing fixture: {path} (run scripts/make_fixtures.py)"
    return path


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------
def test_fastqc_zip_is_detected():
    result = sniff(fx("fastqc", "sample_fastqc.zip"))
    assert result.kind == KIND_FASTQC_ZIP
    assert result.confidence == CONFIDENCE_HIGH
    assert result.suggested_action == ACTION_PARSE_FASTQC


def test_vcf_is_detected():
    result = sniff(fx("vcf", "sample.vcf"))
    assert result.kind == KIND_VCF
    assert result.confidence == CONFIDENCE_HIGH


def test_bam_is_detected_from_magic_bytes_even_when_truncated():
    """A truncated BAM is still a BAM — but we must admit it is incomplete."""
    result = sniff(fx("bam", "truncated.bam"))
    assert result.kind == KIND_BAM
    # Not CONFIDENCE_HIGH: the gzip stream ends early, so the file is partial.
    assert result.confidence == CONFIDENCE_MEDIUM
    assert any("truncated" in note.lower() for note in result.notes)


def test_complete_bam_is_detected():
    assert sniff(fx("bam", "complete.bam")).kind == KIND_BAM


def test_nextflow_log_is_detected():
    result = sniff(fx("logs", "nextflow.log"))
    assert result.kind == KIND_NEXTFLOW_LOG
    assert result.suggested_action == ACTION_DIAGNOSE_LOG


def test_directory_is_a_folder():
    result = sniff(fx("folder"))
    assert result.kind == KIND_FOLDER
    assert result.suggested_action == ACTION_AUDIT_FOLDER
    assert result.confidence == CONFIDENCE_HIGH


# --------------------------------------------------------------------------
# TRAPS — the file name lies; content must win
# --------------------------------------------------------------------------
def test_vcf_renamed_to_txt_is_still_vcf():
    """mystery.txt is a VCF. The extension must not matter."""
    result = sniff(fx("vcf", "mystery.txt"))
    assert result.kind == KIND_VCF
    assert result.confidence == CONFIDENCE_HIGH


def test_vcf_with_no_extension_is_still_vcf():
    result = sniff(fx("vcf", "vcf_no_extension"))
    assert result.kind == KIND_VCF


def test_gzipped_vcf_is_still_vcf():
    """Gzip is transparent: we look at the decompressed content."""
    result = sniff(fx("vcf", "sample.vcf.gz"))
    assert result.kind == KIND_VCF
    assert any("gzip" in note.lower() for note in result.notes)


def test_fastqc_zip_renamed_to_vcf_is_still_a_fastqc_zip(tmp_path):
    renamed = tmp_path / "evil.vcf"
    shutil.copyfile(fx("fastqc", "sample_fastqc.zip"), renamed)
    assert sniff(renamed).kind == KIND_FASTQC_ZIP


def test_bam_renamed_to_fastq_is_still_bam(tmp_path):
    renamed = tmp_path / "reads.fastq"
    shutil.copyfile(fx("bam", "complete.bam"), renamed)
    assert sniff(renamed).kind == KIND_BAM


def test_plain_text_renamed_to_vcf_is_still_unknown(tmp_path):
    renamed = tmp_path / "variants.vcf"
    shutil.copyfile(fx("misc", "plain.txt"), renamed)
    result = sniff(renamed)
    assert result.kind == KIND_UNKNOWN
    assert result.confidence == CONFIDENCE_NONE
    assert result.suggested_action == ACTION_UNKNOWN


# --------------------------------------------------------------------------
# Unknown inputs: never crash, never guess
# --------------------------------------------------------------------------
def test_plain_text_is_unknown():
    result = sniff(fx("misc", "plain.txt"))
    assert result.kind == KIND_UNKNOWN
    assert result.confidence == CONFIDENCE_NONE


def test_empty_file_is_unknown():
    result = sniff(fx("misc", "empty.txt"))
    assert result.kind == KIND_UNKNOWN
    assert result.confidence == CONFIDENCE_NONE


def test_random_bytes_are_unknown():
    result = sniff(fx("misc", "garbage.bin"))
    assert result.kind == KIND_UNKNOWN


def test_missing_path_is_unknown_not_a_crash(tmp_path):
    result = sniff(tmp_path / "does_not_exist.fastq")
    assert result.kind == KIND_UNKNOWN
    assert result.confidence == CONFIDENCE_NONE
    assert "does not exist" in " ".join(result.notes).lower()


def test_truncated_gzip_is_still_identified_but_admitted_incomplete(tmp_path):
    """A cut-off .vcf.gz is still a VCF — but we must say we only read part."""
    broken = tmp_path / "broken.vcf.gz"
    raw = gzip.compress(b"##fileformat=VCFv4.2\nchr1\t1\t.\tA\tG\t.\tPASS\t.\n" * 2000)
    broken.write_bytes(raw[: int(len(raw) * 0.9)])
    result = sniff(broken)
    assert result.kind == KIND_VCF
    assert result.confidence == CONFIDENCE_MEDIUM
    assert any("truncated" in note.lower() for note in result.notes)


def test_badly_truncated_gzip_is_unknown_not_a_crash(tmp_path):
    """Too little survives to identify anything: say unknown, do not guess."""
    broken = tmp_path / "shredded.vcf.gz"
    raw = gzip.compress(b"##fileformat=VCFv4.2\nchr1\t1\t.\tA\tG\t.\tPASS\t.\n" * 200)
    broken.write_bytes(raw[: len(raw) // 4])
    result = sniff(broken)
    assert result.kind == KIND_UNKNOWN
    assert result.confidence == CONFIDENCE_NONE


def test_unreadable_gzip_is_unknown_not_a_crash(tmp_path):
    """1f 8b followed by noise: nothing real to identify, so: unknown."""
    broken = tmp_path / "noise.gz"
    broken.write_bytes(b"\x1f\x8b" + bytes(range(200, 256)) * 4)
    result = sniff(broken)
    assert result.kind == KIND_UNKNOWN
    assert result.confidence == CONFIDENCE_NONE


def test_zip_without_fastqc_data_is_not_a_fastqc_zip(tmp_path):
    plain_zip = tmp_path / "archive.zip"
    with zipfile.ZipFile(plain_zip, "w") as zf:
        zf.writestr("hello.txt", "just an archive\n")
    result = sniff(plain_zip)
    assert result.kind == KIND_UNKNOWN
    assert result.suggested_action == ACTION_UNKNOWN


# --------------------------------------------------------------------------
# Detection ORDER is part of the contract
# --------------------------------------------------------------------------
def test_vcf_wins_over_bam_magic_appearing_later(tmp_path):
    """Rule 2 (VCF first line) is checked before rule 3 (BAM magic)."""
    tricky = tmp_path / "tricky.dat"
    tricky.write_bytes(b"##fileformat=VCFv4.2\n" + b"BAM\x01" + b"\x00" * 32)
    assert sniff(tricky).kind == KIND_VCF


def test_bam_wins_over_nextflow_fingerprint_appearing_later(tmp_path):
    """Rule 3 (BAM magic) is checked before rule 4 (Nextflow text)."""
    tricky = tmp_path / "tricky.dat"
    tricky.write_bytes(b"BAM\x01" + b" N E X T F L O W \n" + b"Process `X` completed\n")
    assert sniff(tricky).kind == KIND_BAM


def test_fastqc_zip_wins_over_everything(tmp_path):
    """Rule 1: a ZIP containing fastqc_data.txt is a FastQC report, full stop."""
    zpath = tmp_path / "mixed.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("run/fastqc_data.txt", "##fileformat=VCFv4.2\n")
    assert sniff(zpath).kind == KIND_FASTQC_ZIP


# --------------------------------------------------------------------------
# Mixed folder: one call per file
# --------------------------------------------------------------------------
def test_every_file_in_the_mixed_folder_is_identified_by_content():
    expected = {
        "sample_fastqc.zip": KIND_FASTQC_ZIP,
        "mystery.txt": KIND_VCF,
        "sample.vcf": KIND_VCF,
        "truncated.bam": KIND_BAM,
        "nextflow.log": KIND_NEXTFLOW_LOG,
        "notes.txt": KIND_UNKNOWN,
        "empty.txt": KIND_UNKNOWN,
    }
    for name, kind in expected.items():
        assert sniff(fx("folder", name)).kind == kind, f"{name} mis-identified"


# --------------------------------------------------------------------------
# Shape of the result object
# --------------------------------------------------------------------------
def test_sniff_result_has_the_three_required_fields():
    result = sniff(fx("vcf", "sample.vcf"))
    assert isinstance(result, SniffResult)
    assert result.kind and result.confidence and result.suggested_action
    assert set(SniffResult("x", "y", "z").to_dict()) == {
        "kind",
        "confidence",
        "suggested_action",
        "path",
        "notes",
    }


def test_sniff_result_survives_a_dict_round_trip():
    original = sniff(fx("vcf", "mystery.txt"))
    clone = SniffResult.from_dict(original.to_dict())
    assert clone == original


@pytest.mark.parametrize(
    "name",
    [
        "fastqc/sample_fastqc.zip",
        "vcf/sample.vcf",
        "vcf/mystery.txt",
        "vcf/vcf_no_extension",
        "vcf/sample.vcf.gz",
        "bam/truncated.bam",
        "logs/nextflow.log",
        "misc/plain.txt",
        "misc/empty.txt",
        "misc/garbage.bin",
    ],
)
def test_sniff_never_raises_on_any_fixture(name):
    """Fuzz-lite: whatever we are handed, we answer, we do not explode."""
    result = sniff(fx(*name.split("/")))
    assert result.kind in {
        KIND_FASTQC_ZIP,
        KIND_VCF,
        KIND_BAM,
        KIND_NEXTFLOW_LOG,
        KIND_FOLDER,
        KIND_UNKNOWN,
    }
    assert result.confidence in {CONFIDENCE_HIGH, "medium", "low", CONFIDENCE_NONE}
