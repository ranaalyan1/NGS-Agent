"""Station 5 — the log diagnoser: 10 signatures, one root cause, honest unknown."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from core.assess import assess_path
from core.diagnose import (
    MIN_SCORE,
    SIGNATURE_DIR,
    Signature,
    SignatureError,
    best_match,
    diagnose,
    load_signatures,
    match_signatures,
)
from core.models import (
    DECISION_FIX_AND_RERUN,
    DECISION_HEALTHY,
    DECISION_UNKNOWN,
    KIND_NEXTFLOW_LOG,
    LogFacts,
    LogMatch,
)
from core.parse.nextflow_log import parse_nextflow_log, run_failed, run_succeeded
from core.version import RULESET_VERSION

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def write_log(tmp_path: Path, text: str, name: str = "nextflow.log") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


BANNER = " N E X T F L O W  ~  version 24.04.2\nLaunching `./main.nf` [test] DSL2\n"


# --------------------------------------------------------------------------
# The signature set
# --------------------------------------------------------------------------
def test_ten_signatures_load_and_follow_the_schema():
    signatures = load_signatures()
    assert len(signatures) == 10
    ids = [s.id for s in signatures]
    assert len(set(ids)) == 10
    for sig in signatures:
        assert sig.id.startswith("NF-")
        assert isinstance(sig.anchors, list) and sig.anchors
        assert all(isinstance(a, str) and a.strip() for a in sig.anchors)
        assert sig.severity in ("fatal", "warning")
        assert sig.title and sig.explanation and sig.fix
        assert sig.reference.startswith("http")


def test_every_signature_file_uses_the_exact_schema_keys():
    import yaml

    for path in sorted(SIGNATURE_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(data) == {
            "id",
            "anchors",
            "severity",
            "title",
            "explanation",
            "fix",
            "reference",
        }, path.name


def test_malformed_signature_files_are_rejected(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("id: NF-BAD\ntitle: incomplete\n", encoding="utf-8")
    with pytest.raises(SignatureError):
        load_signatures(tmp_path)


def test_signature_without_anchors_is_rejected(tmp_path):
    bad = tmp_path / "noanchor.yaml"
    bad.write_text(
        "id: NF-BAD\ntitle: t\nseverity: fatal\nanchors: []\n"
        "explanation: e\nfix: f\nreference: https://example.com\n",
        encoding="utf-8",
    )
    with pytest.raises(SignatureError):
        load_signatures(tmp_path)


# --------------------------------------------------------------------------
# The three gates
# --------------------------------------------------------------------------
def test_star_index_failure_returns_nf_star_003_with_line_numbers():
    verdict = diagnose(fx("logs", "nextflow_star_index.log"))
    assert verdict.kind == KIND_NEXTFLOW_LOG
    assert verdict.decision == DECISION_FIX_AND_RERUN
    assert len(verdict.findings) == 1
    finding = verdict.findings[0]
    assert finding.id == "NF-STAR-003"
    assert finding.severity == "fail"
    lines = finding.details["line_numbers"]
    assert lines, "no line numbers on the receipt"

    # The cited lines must really contain the evidence.
    log_lines = fx("logs", "nextflow_star_index.log").read_text(encoding="utf-8").splitlines()
    joined = "\n".join(log_lines[n - 1] for n in lines).lower()
    assert "genome index version" in joined or "fatal error" in joined


def test_fifty_harmless_warnings_and_one_fatal_surfaces_only_the_fatal():
    log = parse_nextflow_log(fx("logs", "nextflow_warnings.log"))
    assert sum(1 for line in log.lines if line.startswith("WARN")) >= 50
    assert run_failed(log)

    verdict = diagnose(fx("logs", "nextflow_warnings.log"))
    assert len(verdict.findings) == 1, "warnings that did not kill the run must be filtered"
    finding = verdict.findings[0]
    assert finding.severity == "fail"
    assert finding.id == "NF-OOM-001"
    assert finding.details["score"] >= MIN_SCORE


def test_log_matching_nothing_returns_unknown_with_the_tail_attached():
    path = fx("logs", "nextflow_nomatch.log")
    verdict = diagnose(path)
    assert verdict.decision == DECISION_UNKNOWN
    assert verdict.findings == []
    assert verdict.unknown
    tail = verdict.details["last_lines"]
    assert len(tail) == 20
    log_lines = path.read_text(encoding="utf-8").splitlines()
    assert tail == log_lines[-20:]
    assert "Segmentation fault" in "\n".join(tail)


def test_a_completed_run_is_not_reported_as_a_failure():
    verdict = diagnose(fx("logs", "nextflow.log"))
    assert verdict.decision == DECISION_HEALTHY
    assert verdict.findings == []


# --------------------------------------------------------------------------
# Every signature must be able to fire
# --------------------------------------------------------------------------
def test_each_signature_is_detected_in_a_log_containing_its_anchors(tmp_path):
    for signature in load_signatures():
        body = "\n".join(signature.anchors)
        text = (
            BANNER
            + "executor >  local (4)\n"
            + "[1a/2b3c] process > MY_PROCESS (sample1) [100%] 1 of 1 ✘\n"
            + "ERROR ~ Error executing process > 'MY_PROCESS (sample1)'\n\n"
            + "Caused by:\n"
            + "  Process `MY_PROCESS (sample1)` terminated with an error exit status (1)\n\n"
            + "Command error:\n"
            + body
            + "\n\nExecution cancelled -- Finishing pending tasks before exit\n"
        )
        path = write_log(tmp_path, text, name=f"{signature.id}.log")
        matches = match_signatures(parse_nextflow_log(path), load_signatures())
        assert matches, f"{signature.id} did not fire on its own anchors"
        assert matches[0].signature_id == signature.id, (
            f"{signature.id}: {matches[0].signature_id} won"
        )


def test_only_one_root_cause_is_returned(tmp_path):
    text = (
        BANNER
        + "Command error:\n"
        + "java.lang.OutOfMemoryError: Java heap space\n"
        + "slurmstepd: error: Detected 1 oom-kill event(s) in step.\n"
        + "Process `X` terminated with an error exit status (137)\n"
        + "\nExecution cancelled\n"
    )
    verdict = diagnose(write_log(tmp_path, text))
    assert len(verdict.findings) == 1
    # Java heap is the more specific explanation of the two that could match.
    assert verdict.findings[0].id == "NF-JAVA-006"


def test_a_retryable_warning_is_dropped_when_the_run_finished(tmp_path):
    text = (
        BANNER
        + "WARN: Cannot pull singularity image docker://x/y:1.0 (retrying)\n"
        + "[1a/2b3c] process > MY_PROCESS (sample1) [100%] 1 of 1 ✔\n"
        + "Completed at: 01-Jun-2026 09:41:22\n"
        + "Pipeline completed successfully\n"
    )
    log = parse_nextflow_log(write_log(tmp_path, text))
    assert run_succeeded(log)
    assert best_match(log, load_signatures()) is None


def test_a_warning_signature_still_fires_when_nothing_else_does(tmp_path):
    """Warning-severity signatures exist to be reported when they are the answer."""
    text = (
        BANNER
        + "Cannot pull docker://quay.io/x/y:1.0\n"
        + "failed to pull the container image: manifest unknown\n"
        + "denied: access forbidden for the requested repository\n"
        + "\nExecution cancelled -- Finishing pending tasks before exit\n"
    )
    matches = match_signatures(parse_nextflow_log(write_log(tmp_path, text)), load_signatures())
    assert matches
    assert matches[0].signature_id == "NF-CONT-004"
    assert matches[0].severity == "warning"


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------
def test_findings_carry_a_signature_receipt_and_line_receipts():
    verdict = diagnose(fx("logs", "nextflow_star_index.log"))
    finding = verdict.findings[0]
    assert finding.has_valid_receipts()
    sources = [r.source for r in finding.receipts]
    assert sources[0] == "signature:NF-STAR-003"
    assert any(s.startswith("file:") for s in sources)
    signature_receipt = finding.receipts[0]
    assert signature_receipt.version == RULESET_VERSION
    assert "lines=" in signature_receipt.locator


def test_receipt_locators_name_real_lines(tmp_path):
    text = (
        BANNER
        + "Command error:\nNo space left on device\nDisk quota exceeded\n"
        + "\nExecution cancelled\n"
    )
    verdict = diagnose(write_log(tmp_path, text))
    finding = verdict.findings[0]
    log_lines = (tmp_path / "nextflow.log").read_text(encoding="utf-8").splitlines()
    file_receipts = [r for r in finding.receipts if r.source.startswith("file:")]
    assert file_receipts
    for receipt in file_receipts:
        line_no = int(receipt.locator.split("line=")[1])
        assert 0 < line_no <= len(log_lines)
        assert receipt.detail.strip() in log_lines[line_no - 1]


def test_diagnose_works_on_a_gzipped_log(tmp_path):
    raw = fx("logs", "nextflow_star_index.log").read_bytes()
    path = tmp_path / "nextflow.log.gz"
    path.write_bytes(gzip.compress(raw))
    verdict = assess_path(path)
    assert verdict.kind == KIND_NEXTFLOW_LOG
    assert verdict.findings and verdict.findings[0].id == "NF-STAR-003"


# --------------------------------------------------------------------------
# Models and scope
# --------------------------------------------------------------------------
def test_log_facts_and_match_round_trip():
    facts = parse_nextflow_log(fx("logs", "nextflow_star_index.log"))
    clone = LogFacts.from_dict(facts.to_dict())
    assert clone.lines == facts.lines
    assert clone.nextflow_version == facts.nextflow_version
    assert clone.error_blocks == facts.error_blocks

    match = LogMatch(
        signature_id="NF-X",
        title="t",
        severity="fatal",
        score=30,
        line_numbers=[1, 2],
        evidence=["a", "b"],
    )
    assert LogMatch.from_dict(match.to_dict()) == match


def test_nextflow_version_is_recorded_when_present():
    facts = parse_nextflow_log(fx("logs", "nextflow_star_index.log"))
    assert facts.nextflow_version == "24.04.2"


def test_no_llm_in_the_diagnoser():
    text = Path("core/diagnose.py").read_text(encoding="utf-8")
    for word in ("openai", "anthropic", "requests", "httpx", "urllib", "socket", "llm("):
        assert word not in text


def test_signature_model_is_serialisable():
    sig = Signature(
        id="NF-TEST",
        anchors=["a"],
        severity="fatal",
        title="t",
        explanation="e",
        fix="f",
        reference="https://example.com",
    )
    assert sig.to_dict()["id"] == "NF-TEST"
