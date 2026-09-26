"""Pipeline logs beyond Nextflow: recognised by content, diagnosis planned.

Snakemake and Cromwell/WDL users get an honest verdict that names their runner
and points at the public roadmap — never a guess, never a generic shrug.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from core.answer import answer_verdict
from core.assess import assess_path
from core.models import (
    CONFIDENCE_MEDIUM,
    DECISION_UNKNOWN,
    KIND_CROMWELL_LOG,
    KIND_NEXTFLOW_LOG,
    KIND_SNAKEMAKE_LOG,
    KIND_UNKNOWN,
)
from core.parse.folder import parse_folder
from core.sniff import (
    ACTION_UNSUPPORTED_CROMWELL,
    ACTION_UNSUPPORTED_SNAKEMAKE,
    sniff,
)
from doors.cli import EXIT_UNKNOWN, main
from doors.gui.app import app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    path = FIXTURES.joinpath(*parts)
    assert path.exists(), f"missing fixture: {path} (run scripts/make_fixtures.py)"
    return path


# --------------------------------------------------------------------------
# Sniffing
# --------------------------------------------------------------------------
def test_snakemake_log_is_detected():
    result = sniff(fx("logs", "snakemake.log"))
    assert result.kind == KIND_SNAKEMAKE_LOG
    assert result.confidence == CONFIDENCE_MEDIUM
    assert result.suggested_action == ACTION_UNSUPPORTED_SNAKEMAKE


def test_cromwell_log_is_detected():
    result = sniff(fx("logs", "cromwell.log"))
    assert result.kind == KIND_CROMWELL_LOG
    assert result.confidence == CONFIDENCE_MEDIUM
    assert result.suggested_action == ACTION_UNSUPPORTED_CROMWELL


def test_wdl_source_is_detected():
    result = sniff(fx("wdl", "example.wdl"))
    assert result.kind == KIND_CROMWELL_LOG
    assert any("WDL source" in note for note in result.notes)


def test_renamed_engine_logs_are_still_detected(tmp_path):
    cases = [
        (fx("logs", "snakemake.log"), KIND_SNAKEMAKE_LOG),
        (fx("logs", "cromwell.log"), KIND_CROMWELL_LOG),
        (fx("wdl", "example.wdl"), KIND_CROMWELL_LOG),
    ]
    for source, kind in cases:
        for disguise in ("renamed.txt", "renamed.log", "no_extension"):
            renamed = tmp_path / disguise
            shutil.copyfile(source, renamed)
            assert sniff(renamed).kind == kind, f"{source.name} as {disguise}"


def test_nextflow_logs_still_win_their_own_fingerprints():
    assert sniff(fx("logs", "nextflow.log")).kind == KIND_NEXTFLOW_LOG
    assert sniff(fx("logs", "nextflow_star_index.log")).kind == KIND_NEXTFLOW_LOG


def test_plain_text_is_still_unknown():
    assert sniff(fx("misc", "plain.txt")).kind == KIND_UNKNOWN


def test_gzipped_snakemake_log_is_still_detected(tmp_path):
    gzipped = tmp_path / "snakemake.log.gz"
    gzipped.write_bytes(gzip.compress(fx("logs", "snakemake.log").read_bytes()))
    assert sniff(gzipped).kind == KIND_SNAKEMAKE_LOG


# --------------------------------------------------------------------------
# Assessment: honest, specific, and pointed at the roadmap
# --------------------------------------------------------------------------
def test_snakemake_verdict_names_the_runner_and_the_roadmap():
    verdict = assess_path(fx("logs", "snakemake.log"))
    assert verdict.kind == KIND_SNAKEMAKE_LOG
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []
    joined = " ".join(verdict.unknown)
    assert "Snakemake" in joined
    assert "planned" in joined
    assert "ROADMAP.md" in joined


def test_cromwell_verdict_names_the_runner_and_the_roadmap():
    verdict = assess_path(fx("logs", "cromwell.log"))
    assert verdict.kind == KIND_CROMWELL_LOG
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []
    joined = " ".join(verdict.unknown)
    assert "Cromwell" in joined
    assert "planned" in joined


def test_wdl_source_verdict_explains_why_code_is_not_judged():
    verdict = assess_path(fx("wdl", "example.wdl"))
    assert verdict.kind == KIND_CROMWELL_LOG
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []
    joined = " ".join(verdict.unknown)
    assert "WDL" in joined
    assert "workflow code" in joined


def test_answer_writer_describes_each_runner():
    snakemake = answer_verdict(assess_path(fx("logs", "snakemake.log")))
    assert (
        snakemake.section("What this is").lines[0].startswith("This is a log file from a Snakemake")
    )
    cromwell = answer_verdict(assess_path(fx("logs", "cromwell.log")))
    assert "Cromwell" in cromwell.section("What this is").lines[0]
    wdl = answer_verdict(assess_path(fx("wdl", "example.wdl")))
    assert wdl.section("What this is").lines[0] == "This is a WDL workflow definition file."
    nextflow = answer_verdict(assess_path(fx("logs", "nextflow.log")))
    assert (
        nextflow.section("What this is").lines[0].startswith("This is a log file from a Nextflow")
    )


# --------------------------------------------------------------------------
# Doors and folders
# --------------------------------------------------------------------------
def test_cli_exits_two_without_guessing(capsys):
    for name in ("snakemake.log", "cromwell.log"):
        code = main([str(fx("logs", name))])
        out = capsys.readouterr().out
        assert code == EXIT_UNKNOWN, name
        assert "planned" in out.lower()
    assert main([str(fx("wdl", "example.wdl"))]) == EXIT_UNKNOWN
    capsys.readouterr()


def test_box_names_each_runner():
    client = TestClient(app)
    for parts, kind in [
        (("logs", "snakemake.log"), KIND_SNAKEMAKE_LOG),
        (("logs", "cromwell.log"), KIND_CROMWELL_LOG),
        (("wdl", "example.wdl"), KIND_CROMWELL_LOG),
    ]:
        path = fx(*parts)
        response = client.post("/analyze", files={"file": (path.name, path.read_bytes())})
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == kind
        assert payload["decision"] == DECISION_UNKNOWN
        assert payload["findings"] == []


def test_engine_logs_count_as_logs_inside_folders(tmp_path):
    shutil.copyfile(fx("logs", "snakemake.log"), tmp_path / "snakemake.log")
    shutil.copyfile(fx("logs", "cromwell.log"), tmp_path / "run.err")
    run = parse_folder(tmp_path)
    assert len(run.logs) == 2
