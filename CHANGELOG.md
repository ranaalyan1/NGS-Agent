# Changelog

## 1.1.0 — 2026-09-28

Cleanup release aligning the public repository and package with the no-LLM NGS results interpreter.

- Removed the legacy LLM product, pipeline-execution implementation, and legacy-only tests.
- Kept `ngs` as the sole console command, backed by `doors.cli:main`.
- Removed legacy-only dependencies and installation/configuration artifacts; `pyproject.toml` is the packaging source of truth.
- Corrected v1 scope and release-note language: VCF call-quality QC is supported; variant interpretation remains permanently out of scope.
- Documented QC and audit thresholds as defaults pending expert sign-off; rule values and semantics are unchanged.
- Updated user, release, and developer documentation to reflect current verdicts, install paths, and receipt requirements.
