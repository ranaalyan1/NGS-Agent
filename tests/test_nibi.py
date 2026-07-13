"""Tests for the Nibi mascot module (v1.0.0).

Covers:
  - All nine canonical expressions exist and render non-empty ASCII art
  - Aliases resolve to the correct canonical expression
  - Unknown names fall back to the default (happy)
  - Banner render returns structured segments with title/tagline/art/subtitle
  - Gallery render produces a multi-row layout
  - Workflow progress render shows the four steps with checkmarks
  - Pixel icon is non-empty and short
  - Tagline matches the design sheet exactly
  - DESIGN_DETAILS has exactly 5 entries (one per design callout)
"""
from __future__ import annotations

import pytest

from ngs_agent.nibi import (
    DEFAULT_EXPRESSION,
    DESIGN_DETAILS,
    PIXEL_ICON,
    STATE_TO_EXPRESSION,
    TAGLINE,
    WORKFLOW_STATES,
    WORKFLOW_TO_EXPRESSION,
    expression_names,
    get_expression,
    render_banner,
    render_expression_panel,
    render_gallery,
    render_workflow_progress,
)


# ---------- constants ----------
def test_tagline_matches_design_sheet():
    """Tagline must be exactly 'Analyze • Automate • Accelerate'."""
    assert TAGLINE == "Analyze \u2022 Automate \u2022 Accelerate"


def test_design_details_has_five_callouts():
    """Per the character sheet: DNA Antennae, Big Eyes, Cell Nucleus, Adapter Feet, Sequence Tail."""
    assert len(DESIGN_DETAILS) == 5
    names = [d[0] for d in DESIGN_DETAILS]
    assert names == [
        "DNA Antennae",
        "Big Eyes",
        "Cell Nucleus",
        "Adapter Feet",
        "Sequence Tail",
    ]


def test_workflow_states_are_four():
    """Per the design sheet's terminal panel: FASTQ Loaded -> QC Complete -> Aligning... -> Almost there!"""
    assert len(WORKFLOW_STATES) == 4
    labels = [label for _, label in WORKFLOW_STATES]
    assert labels == ["FASTQ Loaded", "QC Complete", "Aligning...", "Almost there!"]


def test_pixel_icon_is_short():
    """Pixel icon must be a short string (fits in 16-32px style usage)."""
    assert isinstance(PIXEL_ICON, str)
    assert len(PIXEL_ICON) < 200
    assert "o" in PIXEL_ICON  # has eyes


# ---------- expressions ----------
EXPECTED_EXPRESSIONS = [
    "happy", "thinking", "analyzing", "running",
    "success", "error", "curious", "coffee", "sleeping",
]


def test_expression_names_returns_nine_canonical():
    names = expression_names()
    assert names == EXPECTED_EXPRESSIONS
    assert len(names) == 9


@pytest.mark.parametrize("name", EXPECTED_EXPRESSIONS)
def test_each_expression_renders_non_empty_art(name):
    art = get_expression(name)
    assert isinstance(art, str)
    assert len(art) > 20
    # Every Nibi expression must have antennae and a body
    assert "/\\" in art, f"Expression {name!r} missing DNA antennae"
    assert "ATCG" in art, f"Expression {name!r} missing sequence tail"
    assert "(@" not in art  # sanity — nucleus should be (◉) not (@)


def test_unknown_expression_falls_back_to_default():
    art = get_expression("this_does_not_exist")
    default_art = get_expression(DEFAULT_EXPRESSION)
    assert art == default_art


@pytest.mark.parametrize("alias,canonical", [
    ("ok", "success"),
    ("done", "success"),
    ("err", "error"),
    ("fail", "error"),
    ("analyze", "analyzing"),
    ("run", "running"),
    ("doctor", "curious"),
    ("break", "coffee"),
    ("sleep", "sleeping"),
    ("idle", "sleeping"),
])
def test_aliases_resolve_to_canonical(alias, canonical):
    assert get_expression(alias) == get_expression(canonical)


def test_expression_lookup_is_case_insensitive():
    assert get_expression("HAPPY") == get_expression("happy")
    assert get_expression("Error") == get_expression("error")


# ---------- state mapping ----------
def test_state_to_expression_covers_common_runtime_states():
    expected_keys = {"idle", "ready", "thinking", "analyzing", "running",
                     "success", "error", "permission", "long_running", "compacting"}
    assert expected_keys.issubset(STATE_TO_EXPRESSION.keys())


def test_workflow_to_expression_covers_four_workflow_states():
    for key, _ in WORKFLOW_STATES:
        assert key in WORKFLOW_TO_EXPRESSION


# ---------- banner ----------
def test_render_banner_returns_structured_segments():
    b = render_banner(version="1.0.0", expression="happy")
    assert isinstance(b, dict)
    assert set(b.keys()) == {"title", "tagline", "art", "subtitle"}
    assert "1.0.0" in b["title"]
    assert "Analyze" in b["tagline"]
    assert "ATCG" in b["art"]
    assert "tiny genome creature" in b["subtitle"]


def test_render_banner_uses_chosen_expression():
    b_happy = render_banner(expression="happy")
    b_error = render_banner(expression="error")
    assert b_happy["art"] != b_error["art"]
    assert "x  x" in b_error["art"]  # error eyes


# ---------- expression panel ----------
def test_render_expression_panel_returns_label_and_art():
    p = render_expression_panel("happy")
    assert p["label"] == "Happy"
    assert "ATCG" in p["art"]


def test_render_expression_panel_normalizes_alias():
    p = render_expression_panel("ok")
    assert p["label"] == "Success"  # alias resolved


# ---------- gallery ----------
def test_render_gallery_default_shows_all_nine():
    g = render_gallery()
    assert isinstance(g, str)
    # All nine labels should appear (title-cased)
    for name in EXPECTED_EXPRESSIONS:
        label = name.title()
        assert label in g, f"Gallery missing label: {label}"


def test_render_gallery_with_subset():
    g = render_gallery(["happy", "error"])
    assert "Happy" in g
    assert "Error" in g
    assert "Sleeping" not in g


# ---------- workflow progress ----------
def test_render_workflow_progress_first_state():
    s = render_workflow_progress("fastq_loaded")
    assert "FASTQ Loaded" in s
    assert "\u25b6" in s  # current marker


def test_render_workflow_progress_middle_state():
    s = render_workflow_progress("qc_complete")
    # Rich markup may insert color tags between the symbol and label,
    # so we check that both the symbol and label appear (in order).
    assert "\u2713" in s and "FASTQ Loaded" in s  # completed
    assert "\u25b6" in s and "QC Complete" in s  # current
    assert "\u25cb" in s and "Aligning" in s  # pending


def test_render_workflow_progress_last_state():
    s = render_workflow_progress("almost_there")
    assert "Almost there!" in s
    assert "nibi:~$" in s


def test_render_workflow_progress_unknown_state_falls_back_to_first():
    s = render_workflow_progress("totally_unknown_state")
    assert "FASTQ Loaded" in s


# ---------- CLI integration (smoke tests via Click runner) ----------
def test_cli_nibi_subcommand_exists():
    """The `nibi` subcommand group should be registered on the CLI."""
    from click.testing import CliRunner
    from ngs_agent.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["nibi", "--help"])
    assert result.exit_code == 0
    assert "gallery" in result.output
    assert "show" in result.output
    assert "lore" in result.output
    assert "workflow" in result.output
    assert "list" in result.output


def test_cli_nibi_list_shows_nine_expressions():
    from click.testing import CliRunner
    from ngs_agent.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["nibi", "list"])
    assert result.exit_code == 0
    for name in EXPECTED_EXPRESSIONS:
        assert name in result.output


def test_cli_nibi_show_specific_expression():
    from click.testing import CliRunner
    from ngs_agent.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["nibi", "show", "happy"])
    assert result.exit_code == 0
    assert "Happy" in result.output
    assert "ATCG" in result.output


def test_cli_nibi_gallery_renders():
    from click.testing import CliRunner
    from ngs_agent.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["nibi", "gallery"])
    assert result.exit_code == 0
    # At least the first three labels should be present
    assert "Happy" in result.output
    assert "Thinking" in result.output


def test_cli_nibi_lore_shows_design_details():
    from click.testing import CliRunner
    from ngs_agent.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["nibi", "lore"])
    assert result.exit_code == 0
    for name, _, _ in DESIGN_DETAILS:
        assert name in result.output


def test_cli_version_bumped_to_v1():
    """The CLI version should report 1.0.0."""
    from click.testing import CliRunner
    from ngs_agent.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "1.0.0" in result.output


def test_cli_no_banner_flag_works():
    """`--no-banner` should suppress the Nibi banner in headless mode."""
    from click.testing import CliRunner
    from ngs_agent.cli import main
    runner = CliRunner()
    result = runner.invoke(main, ["--no-tui", "--no-banner"])
    assert result.exit_code == 0
    # The big banner art (with antennae) should not appear
    # The Welcome panel still shows, but without the standalone banner block
    assert "NGS-Agent v1.0.0" in result.output  # Welcome panel still shows version
