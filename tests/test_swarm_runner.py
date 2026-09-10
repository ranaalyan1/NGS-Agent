"""Unit tests for swarm.runner — the Docker agent runner and local cache."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from swarm.runner import (
    AgentRunner,
    LocalCache,
    compute_input_hash,
    replace_local_file_paths,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# LocalCache
# ---------------------------------------------------------------------------

def test_local_cache_roundtrip(tmp_path):
    cache = LocalCache(base_dir=tmp_path)
    key = "abc123"
    data = {"status": "ok", "payload": {"x": 1}}

    assert run(cache.get(key)) is None
    run(cache.set(key, data))
    assert run(cache.get(key)) == data


def test_local_cache_ttl_expiry(tmp_path):
    cache = LocalCache(base_dir=tmp_path)
    key = "expired"
    run(cache.set(key, {"status": "ok"}))

    # Force the file to look very old.
    path = tmp_path / f"{key}.json"
    os.utime(path, (0, 0))

    assert run(cache.get(key)) is None


def test_local_cache_ignores_corrupt_files(tmp_path):
    cache = LocalCache(base_dir=tmp_path)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert run(cache.get("bad")) is None


# ---------------------------------------------------------------------------
# Content-aware hashing
# ---------------------------------------------------------------------------

def test_compute_input_hash_stable():
    ctx = {"run_id": "run-1", "paired_end": True}
    h1 = compute_input_hash("qc", {"fastq": "/data/x.fastq"}, ctx)
    h2 = compute_input_hash("qc", {"fastq": "/data/x.fastq"}, ctx)
    assert h1 == h2
    assert len(h1) == 16


def test_compute_input_hash_changes_with_file_content(tmp_path):
    f = tmp_path / "reads.fastq"
    f.write_text("ACGT\n", encoding="utf-8")
    ctx = {"run_id": "run-1"}
    h1 = compute_input_hash("ingest", {"fastq": str(f)}, ctx)

    f.write_text("ACGTACGTACGTACGT\n", encoding="utf-8")
    h2 = compute_input_hash("ingest", {"fastq": str(f)}, ctx)
    assert h1 != h2


def test_compute_input_hash_differs_by_agent():
    ctx = {"run_id": "run-1"}
    assert compute_input_hash("qc", {"a": 1}, ctx) != compute_input_hash("align", {"a": 1}, ctx)


# ---------------------------------------------------------------------------
# Mount rewriting
# ---------------------------------------------------------------------------

def test_replace_local_file_paths(tmp_path):
    f = tmp_path / "reads.fastq"
    f.write_text("ACGT", encoding="utf-8")

    mounts: list[tuple[str, str]] = []
    index = [0]
    out = replace_local_file_paths(
        {"fastq": str(f), "label": "not-a-file", "nested": [str(f), 42]},
        mounts,
        index,
    )

    assert out["fastq"] == "/mnt/inputs/0_reads.fastq"
    assert out["label"] == "not-a-file"
    assert out["nested"] == ["/mnt/inputs/1_reads.fastq", 42]
    assert len(mounts) == 2
    assert mounts[0] == (str(f.resolve()), "/mnt/inputs/0_reads.fastq")


# ---------------------------------------------------------------------------
# AgentRunner docker command construction (no Docker execution)
# ---------------------------------------------------------------------------

def test_docker_command_builds_mounts_and_env(tmp_path):
    f = tmp_path / "r1.fastq.gz"
    f.write_text("ACGT", encoding="utf-8")

    runner = AgentRunner()
    cmd, mounts = runner._docker_command(
        "align",
        {"fastq_r1": str(f), "ref_genome": "/ref/genome"},
        {"run_id": "run-1", "paired_end": True},
    )

    assert cmd[0:4] == ["docker", "run", "--rm", "--cpus=2"]
    assert f"ngs/align-agent:latest" in cmd

    joined = " ".join(cmd)
    assert f"{str(f.resolve())}:/mnt/inputs/0_r1.fastq.gz:ro" in joined
    assert "AGENT_INPUTS=" in joined
    assert '"run_id": "run-1"' in joined or "run-1" in joined
    # The original host path must not leak into AGENT_INPUTS.
    assert str(f) not in [c for c in cmd if c.startswith("AGENT_INPUTS=")]


def test_docker_command_high_resource_profile(monkeypatch):
    monkeypatch.setenv("HIGH_AGENT_CPUS", "8")
    monkeypatch.setenv("HIGH_AGENT_MEMORY", "12g")
    runner = AgentRunner()
    cmd, _ = runner._docker_command("gatk_agent", {}, {"run_id": "run-1"})
    assert "--cpus=8" in cmd
    assert "--memory=12g" in cmd


def test_runner_no_cache_skips_cache(monkeypatch):
    monkeypatch.setenv("NGS_NO_CACHE", "1")
    runner = AgentRunner()
    assert runner.cache is None
