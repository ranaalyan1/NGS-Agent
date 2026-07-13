# NGS-Agent v1.0.0 — powered by Nibi 🧬

```
  NGS Agent v1.0.0
  Analyze • Automate • Accelerate

     /\  /\
      |  |
    .------.
   |  o  o  |
   |   __   |
   |  (◉)  |
    \______/
     |__|
    ATCG~

  A tiny genome creature living in data.
```

**Nibi** is the official mascot of NGS-Agent — a tiny genome creature that lives in your data, explores sequences, and powers bioinformatics workflows. Nibi ships with nine expressions that mirror the agent's runtime state, so you always know at a glance whether Nibi is thinking, analyzing, running, or just taking a coffee break.

NGS-Agent itself is an agentic NGS CLI with a tool-use loop for variant interpretation, QC triage, and pipeline troubleshooting. It supports Anthropic and OpenAI-compatible backends, an optional Textual TUI, MCP server bridging, provenance-signed verdicts, and a GIAB benchmark harness.

## Quick start

```bash
pip install -e ".[dev]"
ngsagent                # launch the interactive TUI (Nibi greets you)
ngsagent --no-tui       # headless banner + welcome panel
ngsagent nibi           # meet Nibi — lore, design details, expressions
ngsagent nibi gallery   # all nine expressions side-by-side
ngsagent nibi show happy
ngsagent doctor         # diagnostics (Nibi in "curious" mode)
ngsagent exec "interpret demo_data/sample.vcf"
ngsagent watch demo_data/sample.log
ngsagent analyze demo_data/sample.vcf --qc demo_data/multiqc.txt
```

## Nibi — the mascot

Nibi was introduced in v1.0.0 as the friendly face of NGS-Agent. Per the official character sheet, Nibi has five design details:

| Part            | Meaning                                                |
|-----------------|--------------------------------------------------------|
| DNA Antennae    | Connects to life's code                                |
| Big Eyes        | Sees everything                                        |
| Cell Nucleus    | Always processing                                      |
| Adapter Feet    | Moves through data                                     |
| Sequence Tail   | Made of reads (ATCG)                                   |

### The nine expressions

| Expression | When you'll see it                                            |
|------------|--------------------------------------------------------------|
| Happy      | Default / ready state                                        |
| Thinking   | During context compaction or planning                        |
| Analyzing  | Tool results being inspected (often with 🔍)                 |
| Running    | A tool call is in flight (often with ✨)                     |
| Success    | A run finished cleanly (often with ✨)                       |
| Error      | A tool or run failed                                         |
| Curious    | `ngsagent doctor` and permission prompts                     |
| Coffee     | Long-running operations (☕)                                 |
| Sleeping   | Idle / no recent activity                                    |

### The workflow progress bar

When Nibi walks through a pipeline, the status line mirrors the design sheet's "In Terminal" panel:

```
  ✓ FASTQ Loaded  ->  ✓ QC Complete  ->  ▶ Aligning...  ->  ○ Almost there!
  ( o.o )  nibi:~$ Aligning...
```

## Architecture

- `ngs_agent/nibi.py` — Nibi mascot: ASCII art, expressions, banner, workflow bar
- `ngs_agent/runtime/` — agent loop, context mgmt, compaction, sessions, events, permission
- `ngs_agent/tools/` — BaseTool + Registry; built-in NGS tools
- `ngs_agent/backends/` — Anthropic (cache_control + tool-use), OpenAI-compatible, Ollama
- `ngs_agent/agents/` — interpreter / qc_triage / title definitions
- `ngs_agent/tui/` — Textual TUI with Nibi-aware status bar
- `ngs_agent/mcp/` — MCP bridge

## LLM setup

```bash
export ANTHROPIC_API_KEY=sk-ant-...
ngsagent config wizard
```

## What's new in v1.0.0

- **Nibi mascot** — nine expressions, banner, gallery, lore, workflow bar
- **`ngsagent nibi`** subcommand group: `gallery`, `show`, `lore`, `workflow`, `list`
- **`--no-banner`** flag on the root command for scripting
- **State-driven TUI status bar** — Nibi's face changes with the agent's runtime state
- **Production/Stable** classifier (was Beta)
- All v0.5 features retained: provenance signing, GIAB benchmark, evidence graph, FHIR export

## Acknowledgements

NGS-Agent builds on ideas and patterns from several open-source agent projects.
Core runtime components are adapted (ported to Python) from the following upstreams:

- **OpenCode** — `BaseTool` interface, tool registry, session store, pubsub event bus, Anthropic backend pattern
- **OpenClaude** — per-model context window management
- **"Zero"** — agent loop, tool partitioning, compaction, file tracker, safety/permission model

These projects are permissively licensed (Apache 2.0 / MIT). Their original designs
are gratefully credited; any bugs in the ports are ours, not theirs.

The Nibi mascot and the NGS-specific tooling (variant interpretation, ACMG
classification, ClinVar/gnomAD/HGVS tools, GIAB benchmark) are original to this project.

> **Note on prior versions:** the previous v0.2.0 line of this repository
> (pipeline log watcher + VCF/QC interpreter) is archived on the `archive/v0.2` branch.

## License

Apache 2.0
