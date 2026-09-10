"""Unit tests for swarm.store — the local JSON run-state store."""

from __future__ import annotations

from swarm.store import RunStore


def test_create_and_get(tmp_path):
    store = RunStore(base_dir=tmp_path)
    record = store.create("run-abc", {"experiment": "RNA-Seq"})

    assert record["status"] == "submitted"
    assert record["mode"] == "local"
    got = store.get("run-abc")
    assert got is not None
    assert got["meta"]["experiment"] == "RNA-Seq"


def test_update_merges_and_tracks_fields(tmp_path):
    store = RunStore(base_dir=tmp_path)
    store.create("run-1")
    store.update("run-1", status="running")
    updated = store.update("run-1", status="complete", report_html="s3://b/index.html")

    assert updated["status"] == "complete"
    assert updated["report_html"] == "s3://b/index.html"
    assert updated["updated_at"]


def test_get_missing_returns_none(tmp_path):
    store = RunStore(base_dir=tmp_path)
    assert store.get("does-not-exist") is None


def test_list_runs_returns_all(tmp_path):
    store = RunStore(base_dir=tmp_path)
    store.create("run-a")
    store.create("run-b")
    store.update("run-a", status="complete")

    runs = store.list_runs()
    ids = {r["run_id"] for r in runs}
    assert ids == {"run-a", "run-b"}
    # Most recently updated run sorts first.
    assert runs[0]["run_id"] == "run-a"


def test_update_missing_creates_record(tmp_path):
    store = RunStore(base_dir=tmp_path)
    record = store.update("run-new", status="running")
    assert record["run_id"] == "run-new"
    assert record["status"] == "running"
    assert store.get("run-new") is not None
