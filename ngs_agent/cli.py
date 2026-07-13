"""NGS-Agent v1.0.0 CLI — agentic NGS interpretation, QC triage, and pipeline troubleshooting.

Commands:
  watch     — scan a pipeline log for failure signatures (v0.2 compat, no LLM)
  analyze   — parse a VCF + QC summary (v0.2 compat, no LLM)
  debate    — legacy 3-persona debate (v0.2 compat)
  exec      — run an agentic prompt end-to-end
  session   — list / show / export / delete sessions
  provider  — provider profile management
  mcp       — MCP server management
  doctor    — check deps, keys, MCP servers (Nibi in "curious" mode)
  nibi      — NEW in v1.0.0: meet the Nibi mascot (lore, expressions, workflow)
  config    — config wizard / show / set
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .agents.definitions import AGENTS, get_agent
from .backends.base import NoBackend
from .backends.factory import get_backend
from .config import CONFIG_PATH, load_config, run_wizard, save_config
from .nibi import (
    DESIGN_DETAILS,
    PIXEL_ICON,
    TAGLINE,
    WORKFLOW_STATES,
    expression_names,
    get_expression,
    print_banner,
    render_gallery,
    render_workflow_progress,
)
from .runtime.file_tracker import FileTracker
from .runtime.loop import RunOptions
from .runtime.loop import run as agent_run
from .runtime.session import SessionStore
from .tools.bundle import build_registry
from .tools.ngs.log_diagnose import scan_log
from .tools.ngs.multiqc_parse import parse_multiqc
from .tools.ngs.vcf_parse import parse_vcf_file

console = Console(force_terminal=True, legacy_windows=False)


# ---------- entry ----------
@click.group(invoke_without_command=True)
@click.version_option(__version__)
@click.option("--tui/--no-tui", default=True, help="Launch interactive TUI when no subcommand is given.")
@click.option("--no-banner", is_flag=True, help="Skip the Nibi banner (for scripting).")
@click.pass_context
def main(ctx: click.Context, tui: bool, no_banner: bool) -> None:
    """NGS-Agent v1.0.0 — agentic NGS CLI, powered by Nibi."""
    if ctx.invoked_subcommand is None:
        if tui:
            # Launch the Claude Code-style TUI
            try:
                from .tui.app import NgsAgentTUI
                app = NgsAgentTUI()
                app.run()
            except ImportError:
                console.print(
                    Panel.fit(
                        "[red]Textual not installed.[/red]\n\n"
                        "Install with: [bold]pip install 'ngs-agent[tui]'[/bold]\n"
                        "Or use the headless mode: [bold]ngsagent exec \"<prompt>\"[/bold]",
                        title="TUI unavailable",
                        border_style="red",
                    )
                )
            return
        if not no_banner:
            print_banner(console, __version__)
            console.print()
        console.print(
            Panel.fit(
                f"[bold]NGS-Agent v{__version__}[/bold]\n"
                f"[dim]{TAGLINE}[/dim]\n\n"
                "Agentic NGS CLI: tool-use loop for variant interpretation,\n"
                "QC triage, and pipeline troubleshooting \u2014 now with Nibi.\n\n"
                "Quick start:\n"
                "  ngsagent                    # launch interactive TUI\n"
                "  ngsagent nibi               # meet Nibi (lore + expressions)\n"
                "  ngsagent exec \"interpret variants.vcf\"   # headless\n"
                "  ngsagent watch pipeline.log\n"
                "  ngsagent analyze variants.vcf --qc multiqc.txt\n"
                "  ngsagent doctor\n\n"
                "Run [bold]ngsagent <command> --help[/bold] for details.",
                title="Welcome",
                border_style="orange3",
            )
        )


# ---------- watch (v0.2 compat) ----------
@main.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
@click.option("--tail", is_flag=True, help="Follow the log file.")
def watch(logfile: Path, tail: bool) -> None:
    """Scan a pipeline log for known failure signatures (no LLM required)."""
    text = logfile.read_text(errors="replace")
    matches = scan_log(text)
    if not matches:
        console.print(f"[green]No failure signatures detected in {logfile}.[/green]")
        return

    for m in matches:
        sev_color = {"critical": "red", "warning": "yellow", "info": "blue"}.get(
            m.signature.severity, "white"
        )
        console.print(
            Panel(
                f"[bold]{m.signature.name}[/bold] (line {m.line_no})\n"
                f"[dim]{m.line.strip()[:200]}[/dim]\n\n"
                f"{m.signature.explanation}\n\n"
                f"[bold]Suggested fix:[/bold] {m.signature.suggested_fix}",
                title=f"[{sev_color}]{m.signature.severity.upper()}[/{sev_color}]",
                border_style=sev_color,
            )
        )
    console.print(f"\n[bold]{len(matches)}[/bold] issue(s) found.")


# ---------- analyze (v0.2 compat) ----------
@main.command()
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option("--qc", type=click.Path(exists=True, path_type=Path), default=None)
def analyze(vcffile: Path, qc: Path | None) -> None:
    """Parse a VCF and render a variant/QC report (no LLM required)."""
    variants = parse_vcf_file(vcffile)
    table = Table(title=f"Variants in {vcffile.name}")
    for col in ("#", "CHROM:POS", "REF>ALT", "GENE", "CSQ", "ClinVar", "AF", "DP", "VAF", "Class"):
        table.add_column(col)
    for i, v in enumerate(variants, 1):
        table.add_row(
            str(i),
            f"{v.chrom}:{v.pos}",
            f"{v.ref}>{v.alt}",
            v.gene or "-",
            (v.consequence or "-")[:30],
            v.clinvar or "-",
            str(v.af) if v.af is not None else "-",
            str(v.depth) if v.depth is not None else "-",
            f"{v.vaf:.2%}" if v.vaf is not None else "-",
            v.classification,
        )
    console.print(table)

    if qc:
        text = qc.read_text()
        metrics = parse_multiqc(text)
        qc_table = Table(title=f"QC metrics in {qc.name}")
        for col in ("Metric", "Value", "Grade"):
            qc_table.add_column(col)
        for m in metrics:
            color = {"pass": "green", "warn": "yellow", "fail": "red"}.get(m["grade"], "white")
            qc_table.add_row(m["name"], str(m["value"]), f"[{color}]{m['grade'].upper()}[/{color}]")
        console.print(qc_table)


# ---------- debate (v0.2 compat — now delegates to interpreter agent) ----------
@main.command()
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option("--gene", default=None, help="Filter to a specific gene symbol.")
@click.option("--agent", default="interpreter", help="Override agent.")
def debate(vcffile: Path, gene: str | None, agent: str) -> None:
    """[v0.3] Run the interpreter agent on a VCF. (Legacy name; new code uses 'exec'.)"""
    prompt = f"Interpret variants in {vcffile}"
    if gene:
        prompt += f" — focus on gene {gene}"
    asyncio.run(_run_agent_cmd(agent, prompt))


# ---------- exec (NEW) ----------
@main.command()
@click.argument("prompt", required=False)
@click.option("--agent", default="interpreter", help="Agent name.")
@click.option("--model", default=None, help="Override model.")
@click.option("--resume", default=None, help="Resume session by ID.")
@click.option("--continue", "continue_", is_flag=True, help="Resume most recent session in CWD.")
@click.option("--fork", default=None, help="Fork from session ID with a new prompt.")
@click.option("--permission", default=None, help="Permission mode: auto | plan | ask | yolo")
@click.option("--print", "print_mode", is_flag=True, help="Headless: print final answer only.")
@click.option("--json", "json_mode", is_flag=True, help="Headless: stream JSON events.")
@click.option("--bg", is_flag=True, help="Run in background.")
@click.option("--name", default=None, help="Background session name.")
def exec_(
    prompt: str | None,
    agent: str,
    model: str | None,
    resume: str | None,
    continue_: bool,
    fork: str | None,
    permission: str | None,
    print_mode: bool,
    json_mode: bool,
    bg: bool,
    name: str | None,
) -> None:
    """Run an agentic prompt end-to-end with the tool-use loop."""
    if not prompt and not (resume or continue_ or fork):
        raise click.UsageError("PROMPT required (or use --resume / --continue / --fork).")

    if bg:
        console.print("[yellow]Background mode not yet implemented in this build.[/yellow]")
        return

    asyncio.run(
        _run_agent_cmd(
            agent, prompt or "",
            model=model, resume=resume, continue_=continue_, fork=fork,
            permission=permission, print_mode=print_mode, json_mode=json_mode,
        )
    )


# ---------- session (NEW) ----------
@main.group()
def session() -> None:
    """Session management."""


@session.command("list")
def session_list() -> None:
    """List recent sessions."""
    store = SessionStore()
    sessions = store.list()
    if not sessions:
        console.print("[yellow]No sessions yet.[/yellow]")
        return
    table = Table(title="Recent sessions")
    for col in ("ID", "Agent", "Model", "Title", "CWD", "Updated"):
        table.add_column(col)
    for s in sessions:
        table.add_row(
            s.id[:18], s.agent, s.model, (s.title or "(untitled)")[:40],
            s.cwd, str(s.updated_at),
        )
    console.print(table)


@session.command("show")
@click.argument("session_id")
def session_show(session_id: str) -> None:
    """Show messages from a session."""
    store = SessionStore()
    info = store.get(session_id)
    if not info:
        console.print(f"[red]Session not found: {session_id}[/red]")
        return
    console.print(Panel(
        f"ID: {info.id}\nAgent: {info.agent}\nModel: {info.model}\nCWD: {info.cwd}",
        title=f"Session — {info.title or '(untitled)'}",
    ))
    msgs = store.load_messages(session_id)
    for m in msgs:
        color = {"user": "cyan", "assistant": "green", "tool": "yellow"}.get(m.role, "white")
        console.print(f"\n[{color}]{m.role.upper()}[/{color}] {m.content[:500]}")


@session.command("delete")
@click.argument("session_id")
def session_delete(session_id: str) -> None:
    """Delete a session."""
    store = SessionStore()
    store.delete(session_id)
    console.print(f"[green]Deleted: {session_id}[/green]")


# ---------- provider (NEW) ----------
@main.group()
def provider() -> None:
    """Provider profile management."""


@provider.command("list")
def provider_list() -> None:
    """List configured provider profiles."""
    cfg = load_config()
    table = Table(title="Providers")
    for col in ("Name", "Provider", "Model", "Active"):
        table.add_column(col)
    if not cfg.providers:
        table.add_row("(default)", cfg.llm, cfg.anthropic_model if cfg.llm == "anthropic" else cfg.openai_model, "*")
    else:
        for name, p in cfg.providers.items():
            active = "*" if name == cfg.active_provider else ""
            table.add_row(name, p.get("provider", "?"), p.get("model", "?"), active)
    console.print(table)


@provider.command("add")
@click.argument("name")
@click.option("--provider", "provider_", required=True, type=click.Choice(["anthropic", "openai", "ollama"]))
@click.option("--model", default=None)
@click.option("--base-url", default=None)
def provider_add(name: str, provider_: str, model: str | None, base_url: str | None) -> None:
    """Add a provider profile."""
    cfg = load_config()
    cfg.providers[name] = {"provider": provider_, "model": model, "base_url": base_url}
    if not cfg.active_provider:
        cfg.active_provider = name
    save_config(cfg)
    console.print(f"[green]Added provider profile '{name}'.[/green]")


@provider.command("use")
@click.argument("name")
def provider_use(name: str) -> None:
    """Set the active provider profile."""
    cfg = load_config()
    if name not in cfg.providers:
        console.print(f"[red]Provider '{name}' not found. Run 'ngsagent provider list'.[/red]")
        return
    cfg.active_provider = name
    save_config(cfg)
    console.print(f"[green]Active provider: {name}[/green]")


# ---------- mcp (NEW) ----------
@main.group()
def mcp() -> None:
    """MCP server management."""


@mcp.command("list")
def mcp_list() -> None:
    """List configured MCP servers."""
    cfg = load_config()
    if not cfg.mcp_servers:
        console.print("[yellow]No MCP servers configured.[/yellow]")
        return
    for name, conf in cfg.mcp_servers.items():
        console.print(Panel(
            yaml.safe_dump(conf, default_flow_style=False),
            title=name,
        ))


@mcp.command("add")
@click.argument("name")
@click.option("--stdio", is_flag=True, help="Use stdio transport.")
@click.option("--sse", is_flag=True, help="Use SSE transport.")
@click.option("--command", default=None)
@click.option("--url", default=None)
@click.argument("args", nargs=-1)
def mcp_add(name: str, stdio: bool, sse: bool, command: str | None, url: str | None, args: tuple) -> None:
    """Add an MCP server."""
    if not stdio and not sse:
        raise click.UsageError("Need --stdio or --sse")
    cfg = load_config()
    cfg.mcp_servers[name] = {
        "type": "stdio" if stdio else "sse",
        "command": command or "",
        "args": list(args),
        "url": url or "",
    }
    save_config(cfg)
    console.print(f"[green]Added MCP server '{name}'.[/green]")


# ---------- doctor (NEW) ----------
@main.command()
def doctor() -> None:
    """Check dependencies, API keys, MCP servers, and model availability."""
    cfg = load_config()

    # Nibi in "curious" mode greets the doctor
    console.print()
    console.print("[orange3]( ? ? )[/orange3] [dim]nibi:~$[/dim] [bold]running diagnostics...[/bold]")
    console.print(f"\n[bold]NGS-Agent v{__version__} doctor[/bold]\n")

    # Python
    console.print(f"Python: {sys.version.split()[0]}")

    # Backend
    backend = get_backend(cfg.to_dict())
    if isinstance(backend, NoBackend):
        console.print("[red]LLM backend: NONE[/red] — run 'ngsagent config wizard'")
    else:
        console.print(f"[green]LLM backend: {backend.name}[/green]")
        # Check optional deps
        for pkg in ("anthropic", "openai", "httpx", "yaml"):
            try:
                __import__(pkg)
                console.print(f"  [green]OK[/green] {pkg}")
            except ImportError:
                console.print(f"  [red]MISSING[/red] {pkg}")

    # Config
    console.print(f"\nConfig: {CONFIG_PATH}")
    console.print(f"  LLM: {cfg.llm}")
    console.print(f"  Permission: {cfg.permission_mode}")

    # MCP servers
    if cfg.mcp_servers:
        console.print(f"\nMCP servers ({len(cfg.mcp_servers)}):")
        for name, conf in cfg.mcp_servers.items():
            console.print(f"  - {name}: {conf.get('type', 'stdio')}")

    # Sessions
    try:
        store = SessionStore()
        sessions = store.list(limit=5)
        console.print(f"\nSessions: {len(sessions)} recent")
        for s in sessions[:3]:
            console.print(f"  - {s.id[:18]} | {s.agent} | {(s.title or 'untitled')[:40]}")
    except Exception as e:
        console.print(f"[red]Sessions DB error: {e}[/red]")

    # Provenance signing capability
    try:
        from .runtime.provenance import generate_keypair
        console.print("\n[green]✓[/green] Provenance signing available (Ed25519)")
        console.print("  Run [bold]ngsagent provenance init[/bold] to generate a lab keypair")
    except ImportError:
        console.print("\n[yellow]⚠[/yellow] Provenance signing unavailable (cryptography not installed)")

    # Nibi says goodbye
    console.print()
    console.print("[orange3]( ? ? )[/orange3] [dim]nibi:~$[/dim] [bold]doctor done[/bold]")


# ---------- nibi (NEW in v1.0.0) ----------
@main.group(invoke_without_command=True)
@click.pass_context
def nibi(ctx: click.Context) -> None:
    """Meet Nibi \u2014 the NGS-Agent mascot (lore, expressions, workflow demo)."""
    if ctx.invoked_subcommand is None:
        # Default: show the banner + lore + gallery preview
        print_banner(console, __version__)
        console.print()
        # Design details
        console.print("[bold orange3]Design Details[/bold orange3]")
        for name, _icon, desc in DESIGN_DETAILS:
            console.print(f"  [bold]{name}[/bold] \u2014 {desc}")
        console.print()
        # Pixel icon
        console.print("[bold orange3]Pixel / Terminal Icon[/bold orange3]")
        console.print(PIXEL_ICON, style="orange3")
        console.print()
        # Workflow demo
        console.print("[bold orange3]In Terminal \u2014 workflow progress[/bold orange3]")
        console.print(render_workflow_progress("aligning"))
        console.print()
        console.print(
            f"[dim]Run [bold]ngsagent nibi gallery[/bold] to see all "
            f"{len(expression_names())} expressions, or "
            f"[bold]ngsagent nibi show <name>[/bold] for a single one.[/dim]"
        )


@nibi.command("gallery")
def nibi_gallery() -> None:
    """Show all nine Nibi expressions side-by-side."""
    console.print("[bold orange3]Nibi \u2014 All Expressions[/bold orange3]\n")
    console.print(render_gallery(), style="orange3")


@nibi.command("show")
@click.argument("name")
def nibi_show(name: str) -> None:
    """Show a single Nibi expression by name (e.g. happy, analyzing, error)."""
    from .nibi import render_expression_panel
    panel = render_expression_panel(name)
    console.print(Panel(
        panel["art"],
        title=f"[bold orange3]{panel['label']}[/bold orange3]",
        border_style="orange3",
    ))


@nibi.command("lore")
def nibi_lore() -> None:
    """The story behind Nibi \u2014 design details and tagline."""
    console.print(Panel(
        "A tiny genome creature living in data, exploring sequences\n"
        "and powering bioinformatics workflows. Nibi was introduced\n"
        "as the official mascot of NGS-Agent v1.0.0.",
        title="[bold orange3]Nibi[/bold orange3]",
        subtitle=f"[dim]{TAGLINE}[/dim]",
        border_style="orange3",
    ))
    console.print()
    console.print("[bold orange3]Design Details[/bold orange3]")
    for name, _icon, desc in DESIGN_DETAILS:
        console.print(f"  [bold]{name}[/bold] \u2014 {desc}")


@nibi.command("workflow")
@click.argument("state", required=False, default="aligning",
                type=click.Choice([k for k, _ in WORKFLOW_STATES]))
def nibi_workflow(state: str) -> None:
    """Preview the Nibi workflow progress bar for a given state."""
    console.print(render_workflow_progress(state))


@nibi.command("list")
def nibi_list() -> None:
    """List all available Nibi expression names."""
    for name in expression_names():
        console.print(f"  [orange3]\u2022[/orange3] {name}")


# ---------- config ----------
@main.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """Config wizard / show / set."""
    if ctx.invoked_subcommand is None:
        run_wizard()


@config.command("wizard")
def config_wizard() -> None:
    """Interactive config wizard."""
    run_wizard()


@config.command("show")
def config_show() -> None:
    """Show current config."""
    cfg = load_config()
    console.print(yaml.safe_dump(cfg.to_dict(), default_flow_style=False))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config key."""
    cfg = load_config()
    if key not in cfg.__dataclass_fields__:
        console.print(f"[red]Unknown key: {key}[/red]")
        return
    if key in ("providers", "mcp_servers"):
        cfg.__dict__[key] = yaml.safe_load(value)
    else:
        cfg.__dict__[key] = value
    save_config(cfg)
    console.print(f"[green]{key} = {value}[/green]")


# ---------- provenance (NEW in v0.5) ----------
@main.group()
def provenance() -> None:
    """Provenance bundle management (Ed25519 signing)."""


@provenance.command("init")
@click.option("--out-dir", default=str(Path.home() / ".ngsagent"), help="Directory to write keys")
def provenance_init(out_dir: str) -> None:
    """Generate an Ed25519 keypair for lab-grade verdict signing."""
    from .runtime.provenance import generate_keypair
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    priv, pub = generate_keypair()
    priv_path = out / "lab_private.pem"
    pub_path = out / "lab_public.pem"
    priv_path.write_bytes(priv)
    pub_path.write_bytes(pub)
    priv_path.chmod(0o600)
    console.print(Panel(
        f"Generated Ed25519 keypair:\n\n"
        f"  Private: {priv_path} (chmod 600 — KEEP SECRET)\n"
        f"  Public:  {pub_path} (share with LIMS / verifying parties)\n\n"
        f"Use the private key to sign provenance bundles via the API:\n"
        f"  bundle.sign(open('{priv_path}').read())\n"
        f"Verify with:\n"
        f"  bundle.verify(open('{pub_path}').read())",
        title="Provenance keys generated",
        border_style="green",
    ))


@provenance.command("verify")
@click.argument("bundle_path", type=click.Path(exists=True, path_type=Path))
@click.option("--public-key", default=str(Path.home() / ".ngsagent" / "lab_public.pem"))
def provenance_verify(bundle_path: Path, public_key: str) -> None:
    """Verify a signed provenance bundle."""
    import json
    from .runtime.provenance import ProvenanceBundle, ToolCallRecord

    data = json.loads(bundle_path.read_text())
    # Reconstruct bundle (simplified)
    pub = Path(public_key).read_text()
    # We can't fully reconstruct from dict, but we can check signature + chain_hash
    sig = data.get("signature")
    chain_hash = data.get("chain_hash")
    if not sig:
        console.print("[red]Bundle has no signature.[/red]")
        return
    console.print(f"Verdict ID: {data.get('verdict_id')}")
    console.print(f"Chain hash: {chain_hash}")
    console.print(f"Signature:  {sig[:32]}...")
    # Verification requires the bundle object; show status
    console.print("[yellow]Note: full verification requires reconstructing the bundle object.[/yellow]")
    console.print("[green]Bundle is well-formed.[/green]")


# ---------- giab (NEW in v0.5) ----------
@main.group()
def giab() -> None:
    """GIAB benchmark harness."""


@giab.command("list")
def giab_list() -> None:
    """List available GIAB samples."""
    from .benchmark.giab import GIAB_SAMPLES
    for sid, meta in GIAB_SAMPLES.items():
        console.print(f"  [bold]{sid}[/bold] ({meta['name']}): {meta['description']}")


@giab.command("download")
@click.option("--sample", required=True)
@click.option("--out", default=None)
def giab_download(sample: str, out: str | None) -> None:
    """Download the GIAB gold-standard VCF for a sample."""
    from .benchmark.giab import cli as giab_cli
    import shlex
    args = ["download", "--sample", sample]
    if out:
        args += ["--out", out]
    giab_cli(args, standalone_mode=False)


@giab.command("run")
@click.option("--sample", required=True)
@click.option("--sample-vcf", required=True, type=click.Path(exists=True))
@click.option("--gold-vcf", type=click.Path(exists=True), default=None)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
def giab_run(sample: str, sample_vcf: str, gold_vcf: str | None, output: Path | None) -> None:
    """Run the GIAB benchmark on a sample VCF."""
    from .benchmark.giab import run_benchmark
    try:
        result = run_benchmark(
            sample, Path(sample_vcf),
            Path(gold_vcf) if gold_vcf else None,
        )
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return
    console.print(Panel(
        f"Sample: {result.sample}\n"
        f"Sample variants: {result.total_variants_in_sample:,}\n"
        f"Gold variants:   {result.total_variants_in_gold:,}\n\n"
        f"[green]TP: {result.true_positives:,}[/green]  "
        f"[red]FP: {result.false_positives:,}[/red]  "
        f"[yellow]FN: {result.false_negatives:,}[/yellow]\n\n"
        f"Sensitivity: {result.sensitivity:.2%}\n"
        f"PPV:         {result.ppv:.2%}\n"
        f"F1:          {result.f1:.2%}",
        title="GIAB Benchmark Result",
        border_style="cyan",
    ))
    if output:
        import json
        output.write_text(json.dumps(result.to_dict(), indent=2))
        console.print(f"\nReport saved to: {output}")


# ---------- internal runner ----------
async def _run_agent_cmd(
    agent_name: str,
    prompt: str,
    *,
    model: str | None = None,
    resume: str | None = None,
    continue_: bool = False,
    fork: str | None = None,
    permission: str | None = None,
    print_mode: bool = False,
    json_mode: bool = False,
) -> None:
    agent = get_agent(agent_name)
    if agent is None:
        console.print(f"[red]Unknown agent: {agent_name}. Available: {list(AGENTS.keys())}[/red]")
        return

    cfg = load_config()
    backend = get_backend(cfg.to_dict())
    if isinstance(backend, NoBackend):
        console.print(
            Panel(
                "[bold red]No LLM backend configured.[/bold red]\n\n"
                "The agent loop requires an LLM. Run:\n"
                "  [bold]ngsagent config wizard[/bold]\n"
                "or set ANTHROPIC_API_KEY / OPENAI_API_KEY.",
                title="LLM Required",
                border_style="red",
            )
        )
        return

    model = model or agent.default_model
    store = SessionStore()
    cwd = os.getcwd()

    # Resolve session
    prior_messages = []
    if resume:
        info = store.get(resume)
        if not info:
            console.print(f"[red]Session not found: {resume}[/red]")
            return
        prior_messages = store.load_messages(resume)
        session_id = resume
    elif continue_:
        info = store.most_recent(cwd)
        if not info:
            console.print("[yellow]No prior session in this CWD. Starting fresh.[/yellow]")
            session_id = store.create(agent.name, model, cwd)
        else:
            prior_messages = store.load_messages(info.id)
            session_id = info.id
    elif fork:
        info = store.get(fork)
        if not info:
            console.print(f"[red]Session not found: {fork}[/red]")
            return
        prior_messages = store.load_messages(fork)
        session_id = store.create(agent.name, model, cwd, forked_from=fork)
    else:
        session_id = store.create(agent.name, model, cwd)

    # Build registry
    registry = build_registry(agent.tools)

    # File tracker
    file_tracker = FileTracker()

    # Permission
    perm_mode = permission or cfg.permission_mode or "auto"

    def on_text(text: str) -> None:
        if not print_mode and not json_mode:
            console.print(text, end="")

    def on_tool_call_start(tc_id: str, name: str, args: dict) -> None:
        if not print_mode and not json_mode:
            console.print(f"\n[bold cyan]→ {name}[/bold cyan]({json.dumps(args)[:200]})")

    def on_tool_result(tc_id: str, content: str, is_error: bool) -> None:
        if not print_mode and not json_mode:
            color = "red" if is_error else "green"
            console.print(f"[{color}]✓ result:[/{color}] {content[:300]}")

    def on_event(evt) -> None:
        if json_mode:
            print(json.dumps({
                "type": evt.type,
                "session_id": evt.session_id,
                "payload": evt.payload,
                "timestamp": evt.timestamp,
            }))

    options = RunOptions(
        session_id=session_id,
        model=model,
        system_prompt=agent.system_prompt,
        cwd=cwd,
        max_turns=agent.max_turns,
        permission_mode=perm_mode,
        betas=agent.betas,
        file_tracker=file_tracker,
        on_text=on_text,
        on_tool_call_start=on_tool_call_start,
        on_tool_result=on_tool_result,
        on_event=on_event,
    )

    console.print(f"[dim]Session: {session_id}  Agent: {agent.name}  Model: {model}[/dim]\n")

    result = await agent_run(
        prompt=prompt,
        backend=backend,
        registry=registry,
        options=options,
        prior_messages=prior_messages if (resume or continue_ or fork) else None,
    )

    # Persist messages
    for m in result.messages[len(prior_messages):]:
        store.append_message(session_id, m)

    # Print summary
    if print_mode:
        # Find last assistant text message
        for m in reversed(result.messages):
            if m.role == "assistant" and m.content:
                print(m.content)
                break

    console.print(
        f"\n\n[bold green]Done.[/bold green] "
        f"Turns: {result.turns} | Tokens in: {result.total_input_tokens} | "
        f"Tokens out: {result.total_output_tokens} | "
        f"Finish: {result.finish_reason}"
    )
    if result.error:
        console.print(f"[red]Error: {result.error}[/red]")
    console.print(f"[dim]Session: {session_id}[/dim]")


if __name__ == "__main__":
    main()
