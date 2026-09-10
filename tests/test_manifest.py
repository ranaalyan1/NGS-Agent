"""Tests for the provenance manifest: streaming hashes and JSONL output."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ngs_agent.provenance.manifest import RunManifest, sha256_file


class TestSha256File:
    def test_matches_hashlib_for_small_files(self, tmp_path: Path) -> None:
        target = tmp_path / "small.bin"
        target.write_bytes(b"ngs-agent" * 1000)
        assert sha256_file(target) == hashlib.sha256(target.read_bytes()).hexdigest()

    def test_chunk_size_independent(self, tmp_path: Path) -> None:
        target = tmp_path / "chunked.bin"
        target.write_bytes(bytes(range(256)) * 5000)
        assert sha256_file(target, chunk_size=1024) == sha256_file(target, chunk_size=65536)

    def test_large_file_never_loses_its_checksum(self, tmp_path: Path) -> None:
        """The old behaviour dropped checksums for files over 512MB; hashing
        is streaming, so a >512MB file still gets a full SHA-256."""
        target = tmp_path / "large.bam"
        # Sparse file: 600MB on disk accounting, instant to create.
        with target.open("wb") as handle:
            handle.truncate(600 * 1024 * 1024)
            handle.seek(512 * 1024 * 1024)
            handle.write(b"payload-beyond-the-old-cutoff")
        digest = sha256_file(target)
        assert isinstance(digest, str) and len(digest) == 64
        payload = b"payload-beyond-the-old-cutoff"
        expected_content = (
            b"\x00" * (512 * 1024 * 1024)
            + payload
            + b"\x00" * (600 * 1024 * 1024 - 512 * 1024 * 1024 - len(payload))
        )
        assert digest == hashlib.sha256(expected_content).hexdigest()


class TestRunManifest:
    def test_record_hashes_existing_file(self, tmp_path: Path) -> None:
        artifact = tmp_path / "counts.txt"
        artifact.write_text("gene,count\nG1,10\n", encoding="utf-8")
        manifest = RunManifest(run_id="run-1")
        record = manifest.record(artifact, role="step:quantify", tool="featureCounts")
        assert record is not None
        assert record.size_bytes == artifact.stat().st_size
        assert record.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert record.tool == "featureCounts"

    def test_record_missing_file_returns_none(self, tmp_path: Path) -> None:
        manifest = RunManifest(run_id="run-1")
        assert manifest.record(tmp_path / "ghost.bam") is None
        assert manifest.records == []

    def test_duplicate_paths_recorded_once(self, tmp_path: Path) -> None:
        artifact = tmp_path / "x.txt"
        artifact.write_text("x", encoding="utf-8")
        manifest = RunManifest(run_id="run-1")
        manifest.record(artifact)
        manifest.record(artifact)
        assert len(manifest.records) == 1

    def test_record_outcome_collects_paths_from_nested_payload(self, tmp_path: Path) -> None:
        bam = tmp_path / "aligned.bam"
        bai = tmp_path / "aligned.bam.bai"
        bam.write_bytes(b"bam")
        bai.write_bytes(b"bai")
        manifest = RunManifest(run_id="run-1")
        recorded = manifest.record_outcome(
            "align-S1",
            {"bam_path": str(bam), "extra": {"nested": [str(bai), "not-a-path"]}},
            tool="hisat2",
        )
        recorded_paths = {record.path for record in recorded}
        assert recorded_paths == {str(bam.resolve()), str(bai.resolve())}

    def test_jsonl_format_one_record_per_line(self, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt"):
            (tmp_path / name).write_text(name, encoding="utf-8")
        manifest = RunManifest(run_id="run-1")
        manifest.record(tmp_path / "a.txt")
        manifest.record(tmp_path / "b.txt")
        destination = manifest.write_jsonl(tmp_path / "manifest.jsonl")
        lines = destination.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            payload = json.loads(line)
            assert {"path", "size_bytes", "sha256", "role", "tool"} <= set(payload)

    def test_summary_json_contains_totals(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("abc", encoding="utf-8")
        manifest = RunManifest(run_id="run-42")
        manifest.record(tmp_path / "a.txt")
        summary = manifest.write_summary(tmp_path / "manifest.json")
        payload = json.loads(summary.read_text(encoding="utf-8"))
        assert payload["run_id"] == "run-42"
        assert payload["artifact_count"] == 1
        assert payload["total_bytes"] == 3
