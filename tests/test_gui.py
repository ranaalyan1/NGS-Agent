"""Station 4 — The Box.

The GUI is only allowed to call core and render. These tests check both halves:
that the answers come back right, and that the door stayed dumb.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.version import RULESET_VERSION, TOOL_VERSION
from doors.gui.app import PAGE_PATH, app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def upload(client: TestClient, path: Path, name: str | None = None):
    data = {"file": (name or path.name, path.read_bytes(), "application/octet-stream")}
    return client.post("/analyze", files=data)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def test_index_page_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Drop your file here" in response.text


def test_page_has_three_states_only(client):
    page = client.get("/").text
    assert 'id="drop"' in page
    assert 'id="checking"' in page
    assert 'id="verdict"' in page
    assert "spinner" in page


def test_page_has_no_menus_settings_or_file_type_pickers(client):
    """No menus. No settings. No file-type pickers — the sniffer decides."""
    page = client.get("/").text
    assert "<select" not in page
    assert "accept=" not in page
    for word in ("Settings", "Preferences", "Options", "Command", "Choose file type"):
        assert word not in page, f"page exposes a {word} control"
    # One download button and one reset button; nothing else to configure.
    assert 'id="download"' in page
    assert 'id="again"' in page


def test_page_renders_the_report_offline(client):
    """No external assets: the page must work with the network unplugged."""
    page = client.get("/").text
    for marker in ("http://", "https://", "cdn.", "<link", "<script src"):
        assert marker not in page, f"page depends on {marker}"


# --------------------------------------------------------------------------
# FastQC: the happy path
# --------------------------------------------------------------------------
def test_drop_a_fastqc_report_gets_a_verdict(client):
    response = upload(client, fx("fastqc", "sample_fastqc.zip"))
    assert response.status_code == 200
    data = response.json()
    assert data["subject"] == "sample_fastqc.zip"
    assert data["kind"] == "fastqc_zip"
    assert data["decision"] == "TRIM_AND_PROCEED"
    assert data["headline"]
    assert data["findings"], "the sample fixture must produce findings"
    assert [s["heading"] for s in data["sections"]] == [
        "What this is",
        "What matters",
        "What to do",
        "Receipts",
    ]
    assert data["tool_version"] == TOOL_VERSION
    assert data["ruleset_version"] == RULESET_VERSION


def test_the_report_downloads_as_a_standalone_document(client):
    data = upload(client, fx("fastqc", "sample_fastqc.zip")).json()
    report = data["report_html"]
    assert report.lstrip().startswith("<!DOCTYPE html>")
    assert data["input_sha256"] in report
    assert TOOL_VERSION in report and RULESET_VERSION in report
    for finding in data["findings"]:
        assert finding["title"] in report
        for receipt in finding["receipts"]:
            assert receipt["source"] in report


def test_clean_report_comes_back_healthy(client):
    data = upload(client, fx("fastqc", "clean_fastqc.zip")).json()
    assert data["decision"] == "HEALTHY"
    assert data["findings"] == []
    assert any("Nothing stood out" in line for s in data["sections"] for line in s["lines"])


def test_messy_report_comes_back_resequence(client):
    data = upload(client, fx("fastqc", "messy_fastqc.zip")).json()
    assert data["decision"] == "RESEQUENCE"


def test_sidecar_json_is_included(client):
    import json

    data = upload(client, fx("fastqc", "sample_fastqc.zip")).json()
    payload = json.loads(data["report_json"])
    assert payload["verdict"]["subject"]
    assert payload["verdict"]["findings"]


# --------------------------------------------------------------------------
# Traps: no crash, no hallucinated interpretation
# --------------------------------------------------------------------------
def test_vcf_gives_an_honest_i_cannot_interpret_this(client):
    data = upload(client, fx("vcf", "sample.vcf")).json()
    assert data["decision"] == "UNKNOWN"
    assert data["findings"] == []
    assert data["unknown"]
    assert "out of scope" in " ".join(data["unknown"]).lower()


def test_vcf_renamed_to_txt_gives_the_same_honest_answer(client):
    data = upload(client, fx("vcf", "mystery.txt")).json()
    assert data["kind"] == "vcf"
    assert data["decision"] == "UNKNOWN"
    assert data["findings"] == []


def test_gzipped_vcf_gives_the_same_honest_answer(client):
    data = upload(client, fx("vcf", "sample.vcf.gz")).json()
    assert data["kind"] == "vcf"
    assert data["decision"] == "UNKNOWN"


def test_empty_file_gives_an_honest_answer_not_a_crash(client):
    response = client.post(
        "/analyze", files={"file": ("empty.txt", b"", "application/octet-stream")}
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "UNKNOWN"


def test_garbage_bytes_give_an_honest_answer(client):
    response = client.post(
        "/analyze",
        files={"file": ("noise.bin", bytes(range(256)) * 8, "application/octet-stream")},
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "UNKNOWN"


def test_a_bam_on_its_own_is_not_invented_into_a_verdict(client):
    data = upload(client, fx("bam", "complete.bam")).json()
    assert data["kind"] == "bam"
    assert data["findings"] == []
    assert data["decision"] == "UNKNOWN"


def test_uploaded_filename_does_not_change_the_answer(client):
    """Same bytes, two names: identical verdict, because content decides."""
    honest = upload(client, fx("fastqc", "sample_fastqc.zip"), name="sample_fastqc.zip").json()
    sneaky = upload(client, fx("fastqc", "sample_fastqc.zip"), name="totally_a.vcf").json()
    assert honest["decision"] == sneaky["decision"]
    assert honest["findings"] == sneaky["findings"]


def test_the_verdict_shows_the_name_the_user_uploaded(client):
    """The subject on screen is the file the user dropped, not a temp name."""
    data = upload(client, fx("fastqc", "clean_fastqc.zip"), name="my run (final).zip").json()
    assert data["subject"] == "my_run__final_.zip"


def test_upload_without_a_file_is_rejected(client):
    response = client.post("/analyze", files={"file": ("", b"", "application/octet-stream")})
    assert response.status_code in (400, 422)


# --------------------------------------------------------------------------
# The Golden Rule: no logic in the door
# --------------------------------------------------------------------------
def test_the_door_imports_no_rule_or_parser_logic():
    """The GUI may call core.assess/answer/report — never the rules themselves."""
    source = Path("doors/gui/app.py").read_text(encoding="utf-8")
    for banned in ("core.rules", "core.parse", "core.sniff", "qc_rules", "parse_fastqc"):
        assert banned not in source, f"door reaches into {banned}"
    for required in ("core.assess", "core.answer", "core.report"):
        assert required in source, f"door does not use {required}"


def test_the_door_holds_no_thresholds():
    """No magic numbers: if a rule changes, no door should need to change."""
    source = Path("doors/gui/app.py").read_text(encoding="utf-8")
    for banned in ("Q_DROP", "ADAPTER_WARN", "DUP_FAIL", "0.05", "20.0", "70"):
        assert banned not in source, f"door hard-codes {banned}"


def test_the_page_and_the_app_live_together():
    assert PAGE_PATH.exists()
    assert PAGE_PATH.name == "index.html"
