# Changelog

All notable changes to NGS-Agent. Versions here match the tool version
stamped on every receipt (`core/version.py`); rule-set changes are tracked
separately as `RULESET_VERSION`.

## 1.1.0 — 2026-09-28

Cleanup release: the repository now presents one product, the no-LLM NGS
results interpreter. No new features, no threshold or rule changes.

### Removed

- The entire legacy pre-v1 tree: the old LLM-based analysis package, the
  container agents, the workflow-orchestration layer, the shared helpers,
  the tool containers, the pipeline submission CLI, the background worker,
  the report builder, the v2 scratch directory, and the demo files.
- Legacy configuration and environment collateral: `docker-compose.yml`,
  `.env.example`, `ngs.toml.example`, `environment.yml` (the Box
  environment file remains), and `requirements.txt` — `pyproject.toml` is
  the single source of truth for dependencies.
- The legacy-only console script and the legacy agentic/LLM
  extras; the wheel and sdist now ship `core/` and `doors/` only.
- `BUGS_FOUND.md`: every item triaged (fixed, stale, or deleted with the
  code it lived in); the record is in `NOTES.md`.

### Fixed

- CI is green on the maintained tree: Ruff findings in `core/`, `doors/`,
  `tests/`, and `scripts/` paid with wrap-only edits (no message text
  changed), mypy passes on `core doors`, and the Test/Pylint workflows
  were retargeted at the v1 packages instead of deleted paths.
- All dangling references to removed paths and commands purged from
  `pyproject.toml`, `conftest.py`, `Makefile`, `Dockerfile`,
  `.dockerignore`, `.gitignore`, and the GitHub Actions workflows.

### Changed

- Version 1.0.0 → 1.1.0 (packaging surface changed: the legacy console
  script is gone; `ngs` → `doors.cli:main` is the only entry point).
- Threshold honesty: README gained a "Thresholds" section and both rule
  modules a comment block stating that the consolidated defaults
  (duplication 20/50/70%, freemix 3/5%, alignment 75/50%, assignment
  30%) encode opinions pending expert sign-off. Labels only — no value
  changed.
- Docs: `CLI_USER_GUIDE.md` rewritten for the v1 command surface;
  `NOTES.md` decisions marked DECIDED with dates and test counts
  corrected; `docs/PUBLISHING.md` updated to the current release flow;
  release notes for v1.0.0 and v0.2.0 corrected on GitHub.

### Unchanged

- Rule semantics, thresholds, receipts, verdicts, exit codes, and the
  install recipes (`pipx`, conda, Docker, editable clone). The suite is
  339 tests, all green, with the receipts audit reporting
  `0 findings without a valid receipt`.
