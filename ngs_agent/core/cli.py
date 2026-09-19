"""CLI commands for the evidence-backed review path.

    ngsagent normalize   VCF -> stable identities
    ngsagent review      VCF -> evidence -> classification -> contract -> audit
    ngsagent replay      audit record -> reproduced decision + diff
    ngsagent sign-off    audit record -> human decision -> new signed contract
    ngsagent audit       inspect the audit log

Every command emits the same versioned JSON contract
(:mod:`ngs_agent.core.contract`) so that a report, an MCP tool, and a pipeline
step can all consume one another's output.

Default source policy
---------------------

``review`` defaults to ``--source recorded``: the ClinVar recordings bundled
with the package. That default is deliberately unhelpful-looking, because the
alternative — silently contacting NCBI with a customer's variant coordinates —
is a data-egress decision that must be made explicitly. ``--source live`` opts
into network retrieval and prints what it is about to send.

Every output carries the research-use-only banner. It is printed to stderr for
human-readable formats and embedded in the JSON contract, so it cannot be
separated from the result by piping.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ngs_agent.core.acmg.rule_sets import LABEL_DISPLAY
from ngs_agent.core.acmg.rule_sets import REGISTRY as RULE_SETS
from ngs_agent.core.audit import AuditLog, replay_decision
from ngs_agent.core.contract import RESEARCH_USE_ONLY_BANNER, VariantReviewResult
from ngs_agent.core.errors import CoreError, ReplayDivergenceError
from ngs_agent.core.evidence.recordings import DEFAULT_CLINVAR_RECORDINGS, load_manifest
from ngs_agent.core.evidence.registry import EvidenceConfiguration
from ngs_agent.core.pipeline import ReviewPipeline, VariantReview, resolve_build, summarize_run
from ngs_agent.core.reference import build_reference
from ngs_agent.core.review import TIERS, ReviewDecision
from ngs_agent.core.vcf import read_vcf

console = Console(stderr=True)
out_console = Console()

_RUO_STYLE = "bold yellow"


def _print_banner(console_: Console = console) -> None:
    console_.print(Panel(RESEARCH_USE_ONLY_BANNER, title="NGS-Agent", border_style="yellow"))


def _build_configuration(
    source: str,
    *,
    offline_pack: Path | None,
    cache_dir: Path | None,
    cache_hours: int | None,
    gene_table: Path | None,
    recordings_dir: Path | None,
    clinvar_release: str | None,
    ncbi_api_key: str | None,
) -> EvidenceConfiguration:
    adapters: list[str] = ["gene_mechanism"]
    if source == "live":
        adapters.append("clinvar")
    elif source == "recorded":
        adapters.append("recorded_clinvar")
    elif source == "pack":
        if offline_pack is None:
            raise click.UsageError("--source pack requires --offline-pack PATH")
        adapters.append("offline_pack")
    elif source == "none":
        pass
    else:
        raise click.UsageError(f"unknown --source {source!r}")

    return EvidenceConfiguration(
        adapters=tuple(adapters),
        offline_pack_path=offline_pack,
        gene_mechanism_table_path=gene_table,
        recordings_dir=recordings_dir,
        clinvar_source_version=clinvar_release,
        cache_dir=cache_dir,
        cache_max_age_hours=cache_hours,
        allow_network=source == "live",
        ncbi_api_key=ncbi_api_key,
    )


def _build_pipeline(
    source: str,
    *,
    rule_set: str | None,
    audit_dir: Path | None,
    reference: Path | None,
    offline_pack: Path | None = None,
    cache_dir: Path | None = None,
    cache_hours: int | None = None,
    gene_table: Path | None = None,
    recordings_dir: Path | None = None,
    clinvar_release: str | None = None,
    ncbi_api_key: str | None = None,
    clock: Any = None,
) -> ReviewPipeline:
    configuration = _build_configuration(
        source,
        offline_pack=offline_pack,
        cache_dir=cache_dir,
        cache_hours=cache_hours,
        gene_table=gene_table,
        recordings_dir=recordings_dir,
        clinvar_release=clinvar_release,
        ncbi_api_key=ncbi_api_key,
    )
    return ReviewPipeline(
        configuration=configuration,
        rule_set=rule_set,
        sequence_provider=build_reference(fasta=reference),
        audit_log=AuditLog(audit_dir) if audit_dir else None,
        clock=clock,
    )


def _write_json(payload: Any, destination: Path | None) -> None:
    """Emit the contract as byte-exact JSON.

    Written with :func:`click.echo`, never through a ``rich`` console. Rich
    word-wraps to the terminal width, and it will insert a line break *inside*
    a JSON string value -- which produces output that looks right on screen and
    fails to parse. A contract that ``jq`` cannot read is not a machine
    interface. Human-facing commentary goes to stderr; stdout is the payload.
    """
    text = json.dumps(payload, indent=2, sort_keys=False)
    if destination is None:
        click.echo(text)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text + "\n", encoding="utf-8")
        console.print(f"[green]wrote[/green] {destination}")


def _render_review(review: VariantReview) -> None:
    result = review.result
    classification = result.classification
    label_style = {
        "pathogenic": "bold red",
        "likely_pathogenic": "red",
        "uncertain_significance": "bold yellow",
        "likely_benign": "cyan",
        "benign": "green",
    }.get(classification.label, "white")

    console.print(
        Panel(
            f"[bold]{result.variant.gene or 'gene unresolved'}[/bold]  "
            f"{result.variant.chromosome}:{result.variant.position} "
            f"{result.variant.reference}>{result.variant.alternate}  "
            f"({result.variant.genome_build})\n"
            f"[dim]id[/dim] {result.variant.id}\n"
            f"[dim]spdi[/dim] {result.variant.spdi or 'n/a'}   "
            f"[dim]hgvs_g[/dim] {result.variant.hgvs_g or 'n/a'}",
            title="Variant",
            border_style="blue",
        )
    )

    table = Table(title="Classification", show_header=False, box=None, pad_edge=False)
    table.add_column("field", style="dim")
    table.add_column("value")
    table.add_row("label", f"[{label_style}]{classification.display_label}[/{label_style}]")
    table.add_row("decision_state", classification.decision_state)
    table.add_row("decision_basis", classification.decision_basis)
    table.add_row("abstained", str(classification.abstained))
    table.add_row("requires_human_review", str(classification.requires_human_review))
    table.add_row("rule_set", f"{classification.rule_set} v{classification.rule_set_version}")
    table.add_row("concordance", classification.concordance)
    table.add_row("review_status", result.review.status)
    console.print(table)

    if result.applied_criteria:
        applied = Table(title="Applied criteria", show_header=True, header_style="bold red")
        applied.add_column("Code")
        applied.add_column("Strength")
        applied.add_column("Evidence")
        applied.add_column("Reason")
        for item in result.applied_criteria:
            applied.add_row(
                item.code,
                item.strength.value if item.strength else "-",
                ", ".join(item.evidence_ids),
                item.reason,
            )
        console.print(applied)
    else:
        console.print(
            "[yellow]No ACMG/AMP criterion could be applied from the configured evidence.[/yellow]"
        )

    for rule in classification.fired_rules:
        console.print(f"[dim]rule[/dim] {rule['rule_id']}: {rule['text']}")

    if result.indeterminate_criteria:
        indeterminate = Table(
            title="Indeterminate criteria (open questions, not negative findings)",
            show_header=True,
            header_style="bold yellow",
        )
        indeterminate.add_column("Code")
        indeterminate.add_column("Why it could not be decided")
        for item in result.indeterminate_criteria:
            indeterminate.add_row(item.code, item.reason)
        console.print(indeterminate)

    if result.rejected_criteria:
        rejected = Table(title="Rejected criteria", show_header=True, header_style="dim")
        rejected.add_column("Code")
        rejected.add_column("Reason")
        for item in result.rejected_criteria:
            rejected.add_row(item.code, item.reason)
        console.print(rejected)

    if result.external_classifications:
        external = Table(
            title="Authoritative external classifications", show_header=True,
                header_style="bold magenta"
        )
        external.add_column("Source")
        external.add_column("Classification")
        external.add_column("Review status")
        external.add_column("Last evaluated")
        external.add_column("Accession")
        for item in result.external_classifications:
            external.add_row(
                f"{item.source_name} {item.source_version}",
                item.description_raw,
                item.review_status,
                item.last_evaluated or "-",
                item.accession or "-",
            )
        console.print(external)

    if result.conflicts:
        conflicts = Table(title="Conflicts", show_header=True, header_style="bold red")
        conflicts.add_column("Kind")
        conflicts.add_column("Severity")
        conflicts.add_column("Description")
        for item in result.conflicts:
            conflicts.add_row(item.kind, item.severity, item.description)
        console.print(conflicts)

    if result.missing_evidence:
        missing = Table(title="Missing evidence", show_header=True, header_style="bold yellow")
        missing.add_column("Gap")
        for item in result.missing_evidence:
            missing.add_row(item)
        console.print(missing)

    not_evaluated = [item.code for item in result.not_evaluated_criteria]
    if not_evaluated:
        console.print(
            f"[dim]Not evaluated ({len(not_evaluated)} criteria, no configured source): "
            f"{', '.join(not_evaluated)}[/dim]"
        )

    if result.normalization.warnings:
        for warning in result.normalization.warnings:
            console.print(
                f"[yellow]normalization[{warning['severity']}][/yellow] {warning['message']}")

    if result.explanation.present:
        console.print(
            Panel(
                result.explanation.text or "",
                title=(
                    f"LLM explanation "
                    f"({result.explanation.provider}/{result.explanation.model or 'unknown'})"
                ),
                border_style="magenta",
            )
        )
        if result.explanation.boundary_violations or result.explanation.unsupported_citations:
            console.print(
                f"[bold red]Model boundary violations:[/bold red] "
                f"{list(result.explanation.boundary_violations)}; "
                f"[bold red]unsupported citations:[/bold red] "
                f"{list(result.explanation.unsupported_citations)}"
            )
        console.print(f"[dim]{result.explanation.disclaimer}[/dim]")

    console.print(f"[dim]limitations ({len(result.limitations)}):[/dim]")
    for item in result.limitations[:12]:
        console.print(f"  [dim]- {item}[/dim]")
    if len(result.limitations) > 12:
        console.print(f"  [dim]... and {len(result.limitations) - 12} more (see --json)[/dim]")
    if result.provenance.audit_id:
        console.print(f"[dim]audit_id[/dim] {result.provenance.audit_id}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.command("normalize")
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--genome-build",
    default=None,
    help="GRCh37 or GRCh38. Required unless the VCF declares a reference assembly.",
)
@click.option("--reference", type=click.Path(exists=True, path_type=Path), default=None,
              help="Indexed FASTA used to left-align indels.")
@click.option("--json", "json_out", is_flag=True, help="Emit JSON instead of a table.")
def normalize_command(
    vcffile: Path, genome_build: str | None, reference: Path | None, json_out: bool
) -> None:
    """Normalize a VCF into stable variant identities.

    Reports equivalent representations that collapse to the same identity, and
    warns loudly when an allele could not be normalized completely.
    """
    from ngs_agent.core.normalization import normalize_raw

    try:
        document = read_vcf(vcffile)
        build, build_source = resolve_build(document, genome_build)
        provider = build_reference(fasta=reference)

        rows: list[dict[str, Any]] = []
        for raw in document.records:
            for variant in normalize_raw(raw, genome_build=build, sequence_provider=provider):
                rows.append(
                    {
                        "line": raw.line_number,
                        "input": f"{raw.chromosome}:{raw.position} "
                        f"{raw.reference}>{','.join(raw.alternates)}",
                        "normalized": f"{variant.chromosome}:{variant.position} "
                        f"{variant.reference}>{variant.alternate}",
                        "variant_id": variant.variant_id,
                        "identity": variant.identity,
                        "variant_type": variant.variant_type.value,
                        "spdi": variant.spdi,
                        "hgvs_g": variant.hgvs_g,
                        "complete": variant.normalization.complete,
                        "left_aligned": variant.normalization.left_aligned,
                        "warnings": [item.model_dump() for item in variant.normalization.warnings],
                    }
                )
    except CoreError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise SystemExit(2) from exc

    if json_out:
        _write_json(
            {
                "genome_build": build.value,
                "genome_build_source": build_source,
                "input_sha256": document.facts.sha256,
                "reference_used": provider is not None,
                "variants": rows,
            },
            None,
        )
        return

    console.print(
        f"[dim]genome build[/dim] {build.value} [dim](from {build_source})[/dim]   "
        f"[dim]reference provider[/dim] {provider.name if provider else 'none'}"
    )
    table = Table(title="Normalized identities", show_header=True, header_style="bold cyan")
    for column in ("line", "input", "normalized", "type", "variant_id", "spdi", "complete"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["line"]),
            row["input"],
            row["normalized"],
            row["variant_type"],
            row["variant_id"],
            row["spdi"] or "-",
            "[green]yes[/green]" if row["complete"] else "[red]NO[/red]",
        )
    console.print(table)
    identities = [row["identity"] for row in rows]
    duplicates = len(identities) - len(set(identities))
    if duplicates:
        console.print(
            f"[yellow]{duplicates} record(s) collapsed onto an identity already present in this "
            "file (multiallelic split or a redundant representation).[/yellow]"
        )
    for row in rows:
        for warning in row["warnings"]:
            console.print(
                f"[yellow]line {row['line']} [{warning['severity']}][/yellow] {warning['message']}")


@click.command("review")
@click.argument("vcffile", type=click.Path(exists=True, path_type=Path))
@click.option("--genome-build", default=None, help="GRCh37 or GRCh38.")
@click.option("--gene", default=None,
              help="Review only variants annotated with this gene symbol. A filter: it never "
                   "changes the gene a variant is annotated with.")
@click.option("--gene-of-record", default=None,
              help="Assert this gene symbol for records the VCF does not annotate. Unlike "
                   "--gene this DOES change gene-level criteria (PVS1 mechanism, PP2, BP1), "
                   "so it is recorded in provenance as resolved_from=cli. It never overrides a "
                   "symbol the VCF already states.")
@click.option(
    "--source",
    type=click.Choice(["recorded", "live", "pack", "none"]),
    default="recorded",
    show_default=True,
    help="Evidence source. 'recorded' uses bundled ClinVar recordings (offline, demo-scale). "
    "'live' contacts NCBI E-utilities. 'pack' reads a local evidence pack. 'none' uses only the "
    "local gene mechanism table.",
)
@click.option("--offline-pack", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--recordings-dir", type=click.Path(exists=True, path_type=Path), default=None)
@click.option(
    "--clinvar-release", default=None, help="Explicit ClinVar release stamp for live retrieval.")
@click.option("--reference", type=click.Path(exists=True, path_type=Path), default=None,
              help="Indexed FASTA for indel left-alignment.")
@click.option("--rule-set", default=None, type=click.Choice(sorted(RULE_SETS)), show_default=False,
              help="ACMG rule set. Default: acmg-amp-2015.")
@click.option("--audit-dir", type=click.Path(path_type=Path), default=None,
              help="Directory for the append-only audit log. Without it the run is not auditable.")
@click.option("--cache-dir", type=click.Path(path_type=Path), default=None)
@click.option("--cache-hours", type=int, default=None)
@click.option("--gene-table", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--max-variants", type=int, default=None)
@click.option("--json", "json_out", type=click.Path(path_type=Path), default=None,
              help="Write the versioned JSON contract(s) to this path (array of results).")
@click.option("--quiet", is_flag=True, help="Suppress the human-readable rendering.")
@click.option(
    "--explain", is_flag=True,
        help="Attach an optional LLM narrative (never affects the classification).")
@click.option("--ncbi-api-key-env", default="NCBI_API_KEY", show_default=True,
              help="Environment variable holding an NCBI API key.")
def review_command(
    vcffile: Path,
    genome_build: str | None,
    gene: str | None,
    gene_of_record: str | None,
    source: str,
    offline_pack: Path | None,
    recordings_dir: Path | None,
    clinvar_release: str | None,
    reference: Path | None,
    rule_set: str | None,
    audit_dir: Path | None,
    cache_dir: Path | None,
    cache_hours: int | None,
    gene_table: Path | None,
    max_variants: int | None,
    json_out: Path | None,
    quiet: bool,
    explain: bool,
    ncbi_api_key_env: str,
) -> None:
    """Review variants in a VCF against structured evidence and emit the JSON contract."""
    import os

    _print_banner()
    if source == "recorded":
        directory = recordings_dir or DEFAULT_CLINVAR_RECORDINGS
        try:
            manifest = load_manifest(directory)
        except CoreError as exc:
            console.print(f"[bold red]{exc}[/bold red]")
            raise SystemExit(2) from exc
        console.print(
            Panel(
                f"Evidence source: [bold]bundled recordings[/bold] ({directory})\n"
                f"Recording version: {manifest.recording_version}\n"
                f"ClinVar release: {manifest.clinvar_release}   recorded_at: "
                f"{manifest.recorded_at}\n"
                f"Covered alleles: {len(manifest.alleles_covered)}\n\n"
                "[yellow]These recordings cover a handful of loci only. Variants outside them will "
                "abstain with 'no record for this variant' — that is the correct behaviour, not a "
                "failure.[/yellow]\n"
                "Use [bold]--source live[/bold] to retrieve from ClinVar over the network, or "
                "[bold]--source pack[/bold] for a deployment-built evidence pack.",
                title="Offline evidence source",
                border_style="yellow",
            )
        )
    elif source == "live":
        console.print(
            Panel(
                "Evidence source: [bold red]live network retrieval[/bold red] from "
                "https://eutils.ncbi.nlm.nih.gov\n\n"
                "Variant coordinates and gene symbols for every record in this VCF will be sent "
                "to NCBI. Do not use this mode on a network where that egress is not permitted.\n"
                "No sample identifiers, genotypes, or FASTQ data are transmitted.",
                title="Network egress",
                border_style="red",
            )
        )
        if not clinvar_release:
            console.print(
                "[yellow]No --clinvar-release supplied; the source version will be stamped with "
                "today's date. Pin a release for reproducible runs.[/yellow]"
            )

    if audit_dir is None:
        console.print(
            "[yellow]No --audit-dir supplied: this run will not be auditable and cannot be "
            "replayed. Pass --audit-dir for any run whose result you intend to keep.[/yellow]"
        )

    api_key = os.environ.get(ncbi_api_key_env) if source == "live" else None
    backend = None
    provider = model = None
    if explain:
        from ngs_agent.backends.factory import get_backend
        from ngs_agent.config import load_config

        config = load_config()
        backend = get_backend(config)
        provider = str(config.get("llm") or "unknown")
        model = str(
            config.get(f"{provider}_model")
            or config.get("openai_compat_model")
            or "unknown"
        )
        console.print(
            f"[dim]LLM explanation enabled via {provider}/{model}. The narrative is generated "
            "after the classification is signed and recorded, and cannot change it.[/dim]"
        )

    try:
        pipeline = _build_pipeline(
            source,
            rule_set=rule_set,
            audit_dir=audit_dir,
            reference=reference,
            offline_pack=offline_pack,
            cache_dir=cache_dir,
            cache_hours=cache_hours,
            gene_table=gene_table,
            recordings_dir=recordings_dir,
            clinvar_release=clinvar_release,
            ncbi_api_key=api_key,
        )
        run = pipeline.review_vcf(
            vcffile,
            genome_build=genome_build,
            gene_filter=gene,
            gene_of_record=gene_of_record,
            max_variants=max_variants,
            explain_backend=backend,
            explain_provider=provider,
            explain_model=model,
        )
    except CoreError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise SystemExit(2) from exc

    for warning in run.run_warnings:
        console.print(f"[yellow]run warning:[/yellow] {warning}")

    if not quiet:
        for review in run.reviews:
            _render_review(review)
        summary = summarize_run(run)
        console.print(
            Panel(
                f"run_id {summary['run_id']}\n"
                f"variants {summary['variants']}   abstention_rate "
                f"{summary['abstention_rate']:.2f}   "
                f"conflicts {summary['conflicts']}\n"
                f"labels {json.dumps(summary['labels'], sort_keys=True)}\n"
                f"decision_states {json.dumps(summary['decision_states'], sort_keys=True)}\n"
                f"databases {json.dumps(summary['database_versions'], sort_keys=True)}\n"
                f"all results require human review: "
                f"{summary['requires_human_review']}/{summary['variants']}",
                title="Run summary",
                border_style="blue",
            )
        )

    if json_out is not None:
        _write_json(
            [json.loads(review.result.model_dump_json()) for review in run.reviews], json_out)
    elif quiet:
        _write_json([json.loads(review.result.model_dump_json()) for review in run.reviews], None)

    if any(review.result.classification.abstained for review in run.reviews):
        raise SystemExit(3)
    raise SystemExit(0)


@click.command("replay")
@click.argument("audit_id")
@click.option("--audit-dir", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--json", "json_out", type=click.Path(path_type=Path), default=None)
@click.option("--strict/--no-strict", default=True, show_default=True,
              help="Exit non-zero when the replay does not reproduce the recorded result.")
def replay_command(audit_id: str, audit_dir: Path, json_out: Path | None, strict: bool) -> None:
    """Reconstruct why a prior result was produced, from its evidence snapshot.

    Re-runs the deterministic engine against the evidence recorded in the audit
    entry and diffs the reproduced contract against the stored one. Any
    divergence means the signed classification path is not deterministic.
    """
    log = AuditLog(audit_dir)
    try:
        record = log.find(audit_id)
    except CoreError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise SystemExit(2) from exc

    console.print(
        Panel(
            f"[bold]{record.audit_id}[/bold]\n"
            f"action {record.action}   created_at {record.created_at.isoformat()}\n"
            f"variant {record.variant_identity} ({record.genome_build})   gene "
            f"{record.gene or '-'}\n"
            f"engine {record.engine_version}   rule_set {record.rule_set} "
            f"v{record.rule_set_version}\n"
            f"databases {json.dumps(record.database_versions, sort_keys=True)}\n"
            f"input_hashes {json.dumps(record.input_hashes, sort_keys=True)}\n"
            f"evidence records in snapshot: {len(record.evidence_snapshot)}",
            title="Audit record",
            border_style="blue",
        )
    )

    lineage = log.lineage(audit_id)
    if len(lineage) > 1:
        table = Table(title="Decision lineage", show_header=True, header_style="bold")
        table.add_column("audit_id")
        table.add_column("action")
        table.add_column("created_at")
        table.add_column("reviewer")
        table.add_column("decision")
        table.add_column("override_reason")
        for item in lineage:
            table.add_row(
                item.audit_id,
                item.action,
                item.created_at.isoformat(),
                item.reviewer or "-",
                item.decision or "-",
                item.override_reason or "-",
            )
        console.print(table)

    evidence = Table(title="Evidence snapshot", show_header=True, header_style="bold cyan")
    evidence.add_column("source")
    evidence.add_column("version")
    evidence.add_column("data_type")
    evidence.add_column("status")
    evidence.add_column("verification")
    evidence.add_column("retrieved_at")
    evidence.add_column("evidence_id")
    for entry in record.evidence_snapshot:
        evidence.add_row(
            str((entry.get("source") or {}).get("name")),
            str((entry.get("source") or {}).get("version")),
            str(entry.get("data_type")),
            str(entry.get("status")),
            str(entry.get("verification")),
            str(entry.get("retrieved_at")),
            str(entry.get("evidence_id")),
        )
    console.print(evidence)

    try:
        replay = replay_decision(record, audit_log=log)
    except ReplayDivergenceError as exc:
        console.print(Panel(str(exc), title="REPLAY DIVERGED", border_style="bold red"))
        if json_out is not None:
            _write_json({"reproduced": False, "error": str(exc)}, json_out)
        raise SystemExit(4) from exc
    except CoreError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise SystemExit(2) from exc

    console.print(
        Panel(
            f"reproduced: [bold green]{replay.reproduced}[/bold green]\n"
            f"recorded label: {LABEL_DISPLAY.get(replay.original_label, replay.original_label)} "
            f"({replay.original_decision_state})\n"
            f"replayed label: {LABEL_DISPLAY.get(replay.replayed_label, replay.replayed_label)} "
            f"({replay.replayed_decision_state})\n"
            f"evidence records replayed: {replay.evidence_record_count}\n"
            f"rule set: {replay.rule_set}\n"
            + ("".join(f"note: {note}\n" for note in replay.notes)),
            title="Replay",
            border_style="green" if replay.reproduced else "red",
        )
    )
    if json_out is not None:
        _write_json(json.loads(replay.model_dump_json()), json_out)
    if not replay.reproduced and strict:
        raise SystemExit(4)


@click.command("sign-off")
@click.argument("audit_id")
@click.option("--audit-dir", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--reviewer", required=True, help="Identity of the human reviewer.")
@click.option("--role", default=None, help="Reviewer role, e.g. clinical_laboratory_scientist.")
@click.option("--action", required=True, type=click.Choice(["approve", "reject", "request-review"]))
@click.option("--decision", default=None, type=click.Choice(list(TIERS)),
              help="Reviewer's ACMG/AMP tier. Differs from the engine label => override.")
@click.option("--reason", default=None, help="Required when overriding the engine label.")
@click.option("--notes", default=None, help="Required when rejecting.")
@click.option("--json", "json_out", type=click.Path(path_type=Path), default=None)
def sign_off_command(
    audit_id: str,
    audit_dir: Path,
    reviewer: str,
    role: str | None,
    action: str,
    decision: str | None,
    reason: str | None,
    notes: str | None,
    json_out: Path | None,
) -> None:
    """Record a human review decision against a prior review."""
    from ngs_agent.core.audit import record_sign_off

    _print_banner()
    log = AuditLog(audit_dir)
    try:
        record = log.find(audit_id)
        result = VariantReviewResult.model_validate(record.result_snapshot)
        signed, audit_record = record_sign_off(
            audit_log=log,
            result=result,
            decision=ReviewDecision(
                reviewer=reviewer,
                action=action.replace("-", "_"),  # type: ignore[arg-type]
                decision=decision,
                reviewer_role=role,
                override_reason=reason,
                notes=notes,
            ),
        )
    except CoreError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise SystemExit(2) from exc

    console.print(
        Panel(
            f"audit_id {audit_record.audit_id}\n"
            f"action {audit_record.action}\n"
            f"reviewer {audit_record.reviewer} "
            f"({audit_record.reviewer_role or 'role not stated'})\n"
            f"engine label {audit_record.classification_before}\n"
            f"reviewer decision {audit_record.classification_after}\n"
            f"override_reason {audit_record.override_reason or '-'}\n"
            f"signed_at {signed.review.signed_at.isoformat() if signed.review.signed_at else '-'}",
            title="Sign-off recorded",
            border_style="green",
        )
    )
    if json_out is not None:
        _write_json(json.loads(signed.model_dump_json()), json_out)


@click.command("audit")
@click.option("--audit-dir", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--json", "json_out", is_flag=True)
def audit_command(audit_dir: Path, json_out: bool) -> None:
    """List the audit log and print its integrity hash."""
    log = AuditLog(audit_dir)
    records = log.read_all()
    if json_out:
        _write_json([json.loads(record.model_dump_json()) for record in records], None)
        return
    table = Table(title=f"Audit log ({log.path})", show_header=True, header_style="bold")
    for column in ("audit_id", "action", "created_at", "variant", "label", "reviewer", "decision"):
        table.add_column(column)
    for record in records:
        label = (record.result_snapshot.get("classification") or {}).get("label", "-")
        table.add_row(
            record.audit_id,
            record.action,
            record.created_at.isoformat(),
            record.variant_identity,
            str(label),
            record.reviewer or "-",
            record.decision or "-",
        )
    console.print(table)
    console.print(f"[dim]audit log sha256[/dim] {log.hash_file()}")
    console.print(f"[dim]records[/dim] {len(records)}")


REVIEW_COMMANDS: Sequence[click.Command] = (
    normalize_command,
    review_command,
    replay_command,
    sign_off_command,
    audit_command,
)


def register_review_commands(group: click.Group) -> None:
    """Attach the review commands to the legacy ``ngsagent`` click group."""
    for command in REVIEW_COMMANDS:
        group.add_command(command)


__all__ = ["REVIEW_COMMANDS", "register_review_commands"]
