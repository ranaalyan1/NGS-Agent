# v1.1.0 — repository cleanup

This cleanup release removes the legacy LLM-based CLI and pipeline-execution implementation so the public repository presents one product: the no-LLM NGS results interpreter. `ngs` is the sole console command and continues to use `doors.cli:main`.

- Findings retain their rule and evidence receipts; `UNKNOWN` remains a valid verdict.
- VCF call-quality QC is supported. Variant interpretation (pathogenicity, gene context, ACMG) is permanently out of scope — see [ROADMAP.md](ROADMAP.md).
- QC and audit thresholds are labeled as defaults pending expert sign-off. No threshold values or rule semantics changed.
- Packaging, install guidance, and documentation now describe the supported v1 product only.

## Install

The installation method is unchanged:

```bash
pipx install "ngs-agent[box] @ git+https://github.com/ranaalyan1/NGS-Agent.git"
ngs sample_fastqc.zip
```

The `box` extra includes the web interface. This is a release-notes draft; nothing is tagged or published by this change.
