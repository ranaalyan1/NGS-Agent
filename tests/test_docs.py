"""Station 8 — the docs state.

Docs are part of the product: a front page that apologises for itself tells a PI
not to trust the tool. These tests keep the README honest (real quickstart, real
screenshot, real scope) and free of hedging.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

#: Words that make a front page sound unsure of itself.
APOLOGIES = (
    "experimental",
    "known issue",
    "known limitation",
    "work in progress",
    "coming soon",
    "sorry",
    "apolog",
    "unfortunately",
    "beta",
    "alpha",
    "todo",
    "fixme",
    "not yet implemented",
    "may not work",
    "unstable",
    "rough edges",
    "please bear with",
)


def read(path: Path) -> str:
    assert path.exists(), f"missing doc: {path}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Zero apologies
# --------------------------------------------------------------------------
def test_readme_has_no_apologies():
    text = read(README).lower()
    found = [word for word in APOLOGIES if word in text]
    assert not found, f"README apologises: {found}"


def test_docs_directory_has_no_apologies():
    docs = ROOT / "docs"
    offenders = {}
    for path in sorted(docs.rglob("*.md")):
        text = path.read_text(encoding="utf-8").lower()
        found = [word for word in APOLOGIES if word in text]
        if found:
            offenders[path.name] = found
    assert not offenders, offenders


def test_no_source_file_is_marked_todo():
    """The product's own code must not ship hedging comments as documentation."""
    offenders = []
    for path in list((ROOT / "core").rglob("*.py")) + list((ROOT / "doors").rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        for word in ("not implemented yet", "for now we pretend", "sorry", "coming soon"):
            if word in text:
                offenders.append(f"{path}: {word}")
    assert not offenders, offenders


# --------------------------------------------------------------------------
# The README says real things
# --------------------------------------------------------------------------
def test_readme_has_a_60_second_quickstart():
    text = read(README)
    assert "60-second quickstart" in text
    block = text.split("## 60-second quickstart")[1].split("##")[0]
    commands = re.findall(
        r"^\s*(?:#\s*)?(?:python|\.venv/bin/python|git|ngs)\b.*$", block, flags=re.MULTILINE
    )
    assert len(commands) >= 4, "quickstart should be a handful of copy-paste steps"


def test_readme_quickstart_commands_are_real():
    """Every command in the quickstart must reference something that exists."""
    text = read(README)
    block = text.split("## 60-second quickstart")[1].split("##")[0]
    for referenced in (
        "doors.cli",
        "fixtures/fastqc/sample_fastqc.zip",
        "fixtures/runs/contig_mismatch",
    ):
        assert referenced in block, f"quickstart does not mention {referenced}"
    assert "doors.gui.app" in block or "doors/gui/app.py" in block, "quickstart omits the Box"
    assert (ROOT / "doors" / "cli.py").exists()
    assert (ROOT / "doors" / "gui" / "app.py").exists()
    assert (ROOT / "fixtures" / "fastqc" / "sample_fastqc.zip").exists()


def test_readme_has_a_screenshot_that_exists():
    text = read(README)
    images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    assert images, "README has no screenshot"
    for image in images:
        path = ROOT / image
        assert path.exists(), f"README references a missing image: {image}"
        assert path.stat().st_size > 5000, f"{image} looks empty"


def test_screenshot_is_generated_from_real_output():
    """The image is rendered by a script, not hand-drawn: the script must exist."""
    script = ROOT / "scripts" / "make_screenshot.py"
    assert script.exists()
    body = script.read_text(encoding="utf-8")
    assert "doors.cli" in body, "screenshot must be generated from real CLI output"


def test_readme_has_an_honest_scope_section():
    text = read(README).lower()
    assert "## scope" in text
    scope = text.split("## scope")[1]
    for refused in ("vcf", "pipeline", "mcp", "account", "cloud"):
        assert refused in scope, f"scope section does not mention {refused}"
    assert "does not do" in scope or "not do" in scope


def test_readme_mentions_the_versions_the_tool_reports():

    text = read(README)
    assert "receipts" in text.lower()


def test_readme_documents_exit_codes():
    text = read(README)
    assert "0" in text and "1" in text and "2" in text
    assert "exit code" in text.lower()


def test_readme_lists_every_rule_the_product_ships():
    from core.rules.audit_rules import RULE_IDS as AUDIT_IDS
    from core.rules.qc_rules import RULE_IDS as QC_IDS

    text = read(README)
    for rule_id in QC_IDS + AUDIT_IDS:
        assert rule_id in text, f"README does not document {rule_id}"
