"""Station 7 — the CLI door.

Same verdict as the Box, printed as terminal cards. The tests check the exit
codes (they are a contract with whoever wraps this) and that the door stays
free of logic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.assess import assess_path
from core.version import RULESET_VERSION, TOOL_VERSION
from doors.cli import EXIT_FAILED, EXIT_OK, EXIT_UNKNOWN, exit_code, main, render_terminal

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def fx(*parts: str) -> Path:
    return FIXTURES.joinpath(*parts)


def run(capsys, *args: str) -> tuple[int, str]:
    code = main(list(args))
    out = capsys.readouterr().out
    return code, out


SECTION_HEADINGS = ("WHAT THIS IS", "WHAT MATTERS", "WHAT TO DO", "RECEIPTS")


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------
def test_fastqc_report_prints_the_four_sections(capsys):
    code, out = run(capsys, str(fx("fastqc", "sample_fastqc.zip")))
    for heading in SECTION_HEADINGS:
        assert heading in out, f"missing {heading}"
    assert "sample_fastqc.zip" in out
    assert "Trim, then continue" in out
    assert code == EXIT_FAILED  # the fixture has a failing finding


def test_clean_report_exits_zero(capsys):
    code, out = run(capsys, str(fx("fastqc", "clean_fastqc.zip")))
    assert code == EXIT_OK
    assert "Looks healthy" in out or "Nothing stood out" in out


def test_unknown_input_exits_two_and_says_so(capsys):
    code, out = run(capsys, str(fx("misc", "plain.txt")))
    assert code == EXIT_UNKNOWN
    assert "could not" in out.lower() or "cannot" in out.lower()


def test_vcf_exits_two_without_guessing(capsys):
    code, out = run(capsys, str(fx("vcf", "sample.vcf")))
    assert code == EXIT_UNKNOWN
    assert "out of scope" in out.lower() or "could not" in out.lower()


def test_vcf_trap_files_exit_two_as_well(capsys):
    for name in ("mystery.txt", "vcf_no_extension", "sample.vcf.gz"):
        code, _ = run(capsys, str(fx("vcf", name)))
        assert code == EXIT_UNKNOWN, name


def test_missing_path_exits_two_without_a_traceback(capsys):
    code, out = run(capsys, str(fx("nope", "does_not_exist.zip")))
    assert code == EXIT_UNKNOWN
    assert "Traceback" not in out


def test_empty_file_exits_two(capsys):
    code, _ = run(capsys, str(fx("misc", "empty.txt")))
    assert code == EXIT_UNKNOWN


# --------------------------------------------------------------------------
# Folder and log routes
# --------------------------------------------------------------------------
def test_folder_runs_the_audit(capsys):
    code, out = run(capsys, str(fx("runs", "contig_mismatch")))
    assert "Run folder" in out
    assert "Chromosome" in out or "chromosome" in out
    assert "AUD-STRAND-01" in out
    assert code == EXIT_FAILED


def test_clean_folder_exits_zero(capsys):
    code, _ = run(capsys, str(fx("runs", "clean_run")))
    assert code == EXIT_OK


def test_empty_folder_exits_two(capsys, tmp_path):
    code, _ = run(capsys, str(tmp_path))
    assert code == EXIT_UNKNOWN


def test_log_diagnosis_prints_the_signature(capsys):
    code, out = run(capsys, str(fx("logs", "nextflow_star_index.log")))
    assert "NF-STAR-003" in out
    assert code == EXIT_FAILED


def test_completed_log_exits_zero(capsys):
    code, out = run(capsys, str(fx("logs", "nextflow.log")))
    assert code == EXIT_OK


# --------------------------------------------------------------------------
# --json
# --------------------------------------------------------------------------
def test_json_flag_prints_the_sidecar_verbatim(capsys):
    code, out = run(capsys, str(fx("fastqc", "sample_fastqc.zip")), "--json")
    payload = json.loads(out)
    assert payload["verdict"]["subject"] == "sample_fastqc.zip"
    assert payload["verdict"]["findings"]
    assert payload["tool_version"] == TOOL_VERSION
    assert payload["ruleset_version"] == RULESET_VERSION


def test_json_exit_codes_match_the_card_exit_codes(capsys):
    card_code, _ = run(capsys, str(fx("fastqc", "sample_fastqc.zip")))
    json_code, _ = run(capsys, str(fx("fastqc", "sample_fastqc.zip")), "--json")
    assert card_code == json_code == EXIT_FAILED


def test_json_matches_the_verdict_core_produces(capsys):
    """The sidecar is the verdict core produced, not a re-telling of it.

    Timestamps are generated per run, so they are compared separately.
    """
    _, out = run(capsys, str(fx("runs", "contig_mismatch")), "--json")
    payload = json.loads(out)["verdict"]
    direct = assess_path(fx("runs", "contig_mismatch")).to_dict()
    assert payload["subject"] == direct["subject"]
    assert payload["decision"] == direct["decision"]
    assert payload["headline"] == direct["headline"]
    assert [f["id"] for f in payload["findings"]] == [f["id"] for f in direct["findings"]]
    assert [f["severity"] for f in payload["findings"]] == [
        f["severity"] for f in direct["findings"]
    ]
    assert [r["locator"] for f in payload["findings"] for r in f["receipts"]] == [
        r["locator"] for f in direct["findings"] for r in f["receipts"]
    ]
    assert payload["details"]["input_sha256"] == direct["details"]["input_sha256"]


# --------------------------------------------------------------------------
# Parity with the Box
# --------------------------------------------------------------------------
def _flat(text: str) -> str:
    """Terminal output is word-wrapped, so compare without whitespace."""
    return re.sub(r"\s+", "", text)


def test_terminal_cards_carry_the_same_content_as_the_box(capsys):
    """The Box and the CLI call the same core and must not diverge."""
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    _, out = run(capsys, str(fx("fastqc", "sample_fastqc.zip")))
    flat_out = _flat(out)
    for finding in verdict.findings:
        assert _flat(finding.title) in flat_out, finding.id
        assert _flat(finding.action) in flat_out, finding.id
        assert _flat(finding.meaning) in flat_out, finding.id
        for receipt in finding.receipts:
            assert _flat(receipt.source) in flat_out
    assert _flat(verdict.headline) in flat_out


def test_footer_carries_versions_and_hash(capsys):
    _, out = run(capsys, str(fx("fastqc", "sample_fastqc.zip")))
    assert TOOL_VERSION in out
    assert RULESET_VERSION in out
    assert "SHA-256" in out


# --------------------------------------------------------------------------
# Unit bits
# --------------------------------------------------------------------------
def test_exit_code_mapping():
    clean = assess_path(fx("fastqc", "clean_fastqc.zip"))
    assert exit_code(clean) == EXIT_OK
    failing = assess_path(fx("fastqc", "sample_fastqc.zip"))
    assert exit_code(failing) == EXIT_FAILED
    unknown = assess_path(fx("misc", "plain.txt"))
    assert exit_code(unknown) == EXIT_UNKNOWN


def test_render_terminal_is_deterministic_without_colour():
    verdict = assess_path(fx("fastqc", "sample_fastqc.zip"))
    from core.answer import answer_verdict

    answer = answer_verdict(verdict)
    assert render_terminal(verdict, answer, colour=False) == render_terminal(
        verdict, answer, colour=False
    )


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert TOOL_VERSION in capsys.readouterr().out


# --------------------------------------------------------------------------
# The Golden Rule
# --------------------------------------------------------------------------
def test_the_cli_door_holds_no_logic():
    source = Path("doors/cli.py").read_text(encoding="utf-8")
    for banned in (
        "core.rules",
        "core.parse",
        "core.sniff",
        "qc_rules",
        "audit_rules",
        "load_signatures",
    ):
        assert banned not in source, f"door reaches into {banned}"
    for required in ("core.assess", "core.answer", "core.report"):
        assert required in source, f"door does not use {required}"


def test_the_cli_door_holds_no_thresholds():
    source = Path("doors/cli.py").read_text(encoding="utf-8")
    for banned in ("Q_DROP", "ADAPTER_WARN", "DUP_FAIL", "ASSIGNMENT_FAIL", "0.05"):
        assert banned not in source, f"door hard-codes {banned}"
