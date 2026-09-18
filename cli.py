#!/usr/bin/env python3
"""NGS-Agent swarm CLI: submit Temporal-orchestrated RNA-Seq / WGS / WES runs."""
import asyncio
import csv
import os
import sys
import uuid
from collections import Counter
from pathlib import Path

import click
from dotenv import load_dotenv
from temporalio.client import Client

from workflows.pipeline_workflow import NGSPipelineWorkflow, RunInput

load_dotenv()

ORGANISMS = ["human", "mouse", "rat", "zebrafish", "yeast", "other", "mixed"]

# Suffixes that prove a basename is a real aligner index, not a typo.
INDEX_SUFFIXES = (
    [f".{i}.ht2" for i in range(1, 9)]
    + [".ht2", ".bwt", ".pac", ".ann", ".amb", ".sa", ".fai", ".dict"]
)


@click.group()
def cli() -> None:
    """NGS Agent Swarm CLI — full pipelines via Temporal + Docker.

    Before submitting: start Temporal + MinIO (`docker compose up -d`),
    build the agents (`bash scripts/build-agents.sh`), and run a worker
    (`python worker.py`). Then submit with `quick` / `submit` / `submit-batch`.
    """


def ensure_file(path_value: str, label: str) -> None:
    if not Path(path_value).exists() or not Path(path_value).is_file():
        raise click.BadParameter(f"{label} does not exist or is not a file: {path_value}")


def ensure_ref_genome(value: str) -> None:
    """Accept a reference file OR an aligner index basename (HISAT2/BWA)."""
    p = Path(value)
    if p.exists() and p.is_file():
        return
    if not p.exists():
        for suffix in INDEX_SUFFIXES:
            if Path(str(p) + suffix).exists():
                return
    raise click.BadParameter(
        f"--ref-genome '{value}' is not a file and no index files "
        f"('{value}.1.ht2' … or '{value}.bwt' …) exist next to it. "
        "Pass the HISAT2 index basename (e.g. data/ref/grch38_idx with "
        ".ht2 siblings) or a reference FASTA."
    )


async def _connect_temporal() -> Client:
    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    try:
        return await Client.connect(temporal_host)
    except Exception as exc:  # bridge raises bare RuntimeError on refused conn
        raise click.ClickException(
            f"Cannot reach Temporal at {temporal_host}.\n"
            f"({exc})\n"
            "Is the server running? Start it with `docker compose up -d` "
            "(local) or set TEMPORAL_HOST=host:port to point at yours."
        ) from exc


def _monitor_url(handle_id: str) -> str:
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233").split(":")[0]
    ui_host = "localhost" if host in ("localhost", "127.0.0.1") else host
    return f"Monitor at http://{ui_host}:8080/namespaces/default/workflows/{handle_id}"


def _normalize_sample_row(row: dict) -> dict:
    """Accept both `fastq` (CSV header) and `fastq_path` (ingest agent key)."""
    row = dict(row)
    if not row.get("fastq_path") and row.get("fastq"):
        row["fastq_path"] = row["fastq"]
    return row


@cli.command()
@click.option("--fastq", required=False, help="Path to single-end FASTQ")
@click.option("--fastq-r1", required=False, help="Path to paired-end R1 FASTQ")
@click.option("--fastq-r2", required=False, help="Path to paired-end R2 FASTQ")
@click.option("--experiment", default="RNA-Seq", type=click.Choice(["RNA-Seq", "WGS", "WES"]))
@click.option(
    "--organism",
    required=True,
    type=click.Choice(ORGANISMS),
    help="Target organism",
)
@click.option("--ref-genome", required=True, help="HISAT2 index basename or reference FASTA path")
@click.option("--reference-fasta", required=False, help="Reference FASTA path for DNA branch tools")
@click.option("--gtf", required=False,
              help="Annotation GTF path (RNA-Seq counting; omit for align-only)")
@click.option("--panel-bed", required=False, help="Optional panel BED for DNA coverage plots")
@click.option("--known-sites", required=False, multiple=True,
              help="Known sites VCFs for GATK BQSR (repeatable)")
@click.option("--paired/--single", default=False, help="Use paired-end mode")
def submit(
    fastq: str | None,
    fastq_r1: str | None,
    fastq_r2: str | None,
    experiment: str,
    organism: str,
    ref_genome: str,
    reference_fasta: str | None,
    gtf: str | None,
    panel_bed: str | None,
    known_sites: tuple[str, ...],
    paired: bool,
) -> None:
    """Submit a single pipeline run."""
    if paired:
        if not fastq_r1 or not fastq_r2:
            raise click.BadParameter("--paired requires both --fastq-r1 and --fastq-r2")
        ensure_file(fastq_r1, "--fastq-r1")
        ensure_file(fastq_r2, "--fastq-r2")
    else:
        if not fastq:
            raise click.BadParameter("--single requires --fastq")
        ensure_file(fastq, "--fastq")

    ensure_ref_genome(ref_genome)
    if experiment in {"WGS", "WES"} and not reference_fasta:
        raise click.BadParameter("DNA-Seq analysis requires --reference-fasta")
    if reference_fasta:
        ensure_file(reference_fasta, "--reference-fasta")

    skip_quantification = False
    if gtf:
        ensure_file(gtf, "--gtf")
    elif experiment == "RNA-Seq":
        # Honest align-only mode: counting/DE need a GTF, so skip them
        # instead of crashing in the count agent.
        skip_quantification = True
        click.echo("Note: no --gtf given, running align-only (counting/DE skipped).")
    if panel_bed:
        ensure_file(panel_bed, "--panel-bed")
    for known_site in known_sites:
        ensure_file(known_site, "--known-sites")

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    routing_ctx = {
        "experiment_type": experiment,
        "organism": organism,
        "paired_end": paired,
        "reference_genome": ref_genome,
        "reference_fasta": reference_fasta,
        "gtf": gtf,
        "panel_bed": panel_bed,
        "known_sites": list(known_sites),
        "run_id": run_id,
        "skip_quantification": skip_quantification,
    }
    inputs = {"ref_genome": ref_genome, "gtf": gtf, "reference_fasta": reference_fasta}
    if panel_bed:
        inputs["panel_bed"] = panel_bed
    if known_sites:
        inputs["known_sites"] = list(known_sites)

    # Pack as a single-sample batch to interface with NGSPipelineWorkflow
    samples = [{
        "sample_id": "sample-01",
        "condition": "unknown",
        "replicate_group": "1",
        "species": organism,
        "fastq_path": fastq,
        "fastq_r1": fastq_r1,
        "fastq_r2": fastq_r2,
    }]
    inputs["samples"] = samples

    async def run_submit() -> None:
        client = await _connect_temporal()
        handle = await client.start_workflow(
            NGSPipelineWorkflow.run,
            RunInput(run_id, experiment, routing_ctx, inputs),
            id=f"ngs-{run_id}",
            task_queue="ngs-pipeline",
        )
        click.echo(f"Run submitted: {run_id}")
        click.echo(_monitor_url(handle.id))

    asyncio.run(run_submit())


@cli.command()
@click.option("--sample-sheet", required=True, help="Path to CSV sample sheet")
@click.option("--experiment", default="RNA-Seq", type=click.Choice(["RNA-Seq", "WGS", "WES"]))
@click.option(
    "--organism",
    required=True,
    type=click.Choice(ORGANISMS),
    help="Default target organism (can be overridden in sample sheet)",
)
@click.option("--ref-genome", required=True, help="HISAT2 index basename or reference FASTA path")
@click.option("--reference-fasta", required=False, help="Reference FASTA path for DNA branch tools")
@click.option("--gtf", required=False, help="Annotation GTF path (omit for align-only)")
@click.option("--paired/--single", default=False, help="Use paired-end mode")
def submit_batch(
    sample_sheet: str,
    experiment: str,
    organism: str,
    ref_genome: str,
    reference_fasta: str | None,
    gtf: str | None,
    paired: bool,
) -> None:
    """Submit a batch pipeline run using a CSV sample sheet."""
    ensure_file(sample_sheet, "--sample-sheet")
    ensure_ref_genome(ref_genome)
    if reference_fasta:
        ensure_file(reference_fasta, "--reference-fasta")

    skip_quantification = False
    if gtf:
        ensure_file(gtf, "--gtf")
    elif experiment == "RNA-Seq":
        skip_quantification = True
        click.echo("Note: no --gtf given, running align-only (counting/DE skipped).")

    samples = []
    with open(sample_sheet, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples.append(_normalize_sample_row(row))

    if not samples:
        raise click.BadParameter("Sample sheet is empty")

    # Pre-flight check: uniqueness
    sample_ids = [s.get("sample_id", "") for s in samples]
    duplicates = [item for item, count in Counter(sample_ids).items() if count > 1]
    if duplicates:
        raise click.BadParameter(f"Sample IDs must be unique. Found duplicates: {duplicates}")

    # Pre-flight check: path validation
    for i, row in enumerate(samples):
        if not row.get("sample_id"):
            raise click.BadParameter(f"Row {i+1}: missing sample_id")

        if paired:
            if not row.get("fastq_r1") or not row.get("fastq_r2"):
                raise click.BadParameter(f"Row {i+1}: missing fastq_r1 or fastq_r2 for paired mode")
            ensure_file(row["fastq_r1"], f"Row {i+1} fastq_r1")
            ensure_file(row["fastq_r2"], f"Row {i+1} fastq_r2")
        else:
            if not row.get("fastq_path"):
                raise click.BadParameter(f"Row {i+1}: missing fastq/fastq_path for single mode")
            ensure_file(row["fastq_path"], f"Row {i+1} fastq")

    run_id = f"batch-{uuid.uuid4().hex[:8]}"
    routing_ctx = {
        "experiment_type": experiment,
        "organism": organism,
        "paired_end": paired,
        "reference_genome": ref_genome,
        "reference_fasta": reference_fasta,
        "gtf": gtf,
        "run_id": run_id,
        "sample_sheet": sample_sheet,
        "skip_quantification": skip_quantification,
    }
    inputs = {
        "ref_genome": ref_genome,
        "gtf": gtf,
        "reference_fasta": reference_fasta,
        "samples": samples
    }

    async def run_submit() -> None:
        client = await _connect_temporal()
        handle = await client.start_workflow(
            NGSPipelineWorkflow.run,
            RunInput(run_id, experiment, routing_ctx, inputs),
            id=f"ngs-{run_id}",
            task_queue="ngs-pipeline",
        )
        click.echo(f"Batch run submitted: {run_id} ({len(samples)} samples)")
        click.echo(_monitor_url(handle.id))

    asyncio.run(run_submit())


@cli.command()
@click.argument("run_id")
def status(run_id: str) -> None:
    """Get status of a run."""
    async def run_status() -> None:
        client = await _connect_temporal()
        handle = client.get_workflow_handle(f"ngs-{run_id}")
        try:
            desc = await handle.describe()
        except Exception as exc:
            raise click.ClickException(
                f"Could not find run '{run_id}' ({exc}). "
                "Check the run id and TEMPORAL_HOST."
            ) from exc
        click.echo(f"Status: {desc.status.name}")
        if desc.status.name == "COMPLETED":
            result = await handle.result()
            click.echo(f"Result: {result}")

    asyncio.run(run_status())


@cli.command()
@click.option("--output-env", default=".env", show_default=True)
@click.option("--output-csv", default="sample_sheet.csv", show_default=True)
def wizard(output_env: str, output_csv: str) -> None:
    """Interactive setup wizard for batch analysis."""
    experiment_type = click.prompt("Analysis type", type=click.Choice(["RNA-Seq", "WGS", "WES"]))
    paired = click.confirm("Is the dataset paired-end?", default=True)
    organism = click.prompt(
        "Default organism",
        type=click.Choice(["human", "mouse", "mixed"]),
        default="human",
    )

    num_samples = click.prompt("How many samples to configure now?", type=int, default=2)

    samples = []
    for i in range(num_samples):
        click.echo(f"\n--- Configuring Sample {i+1} ---")
        sample_id = click.prompt("Sample ID", default=f"S{i+1}")
        condition = click.prompt(
            "Condition (e.g., control, treated)",
            default="control" if i == 0 else "treated",
        )
        replicate_group = click.prompt("Replicate group (e.g., 1, 2)", default="1")
        species = click.prompt("Species", default=organism)

        if paired:
            fastq_r1 = click.prompt("Path to R1 FASTQ", type=str)
            fastq_r2 = click.prompt("Path to R2 FASTQ", type=str)
            fastq = ""
        else:
            fastq = click.prompt("Path to FASTQ", type=str)
            fastq_r1 = fastq_r2 = ""

        samples.append({
            "sample_id": sample_id,
            "condition": condition,
            "replicate_group": replicate_group,
            "species": species,
            "fastq": fastq,
            "fastq_r1": fastq_r1,
            "fastq_r2": fastq_r2
        })

    ref_genome = click.prompt(
        "\nReference genome index basename (e.g. data/ref/grch38_idx)", type=str
    )
    gtf = click.prompt(
        "Annotation GTF path (blank = align-only, skip counting)",
        type=str, default="", show_default=False,
    )

    env_lines = [
        f"EXPERIMENT_TYPE={experiment_type}",
        f"ORGANISM={organism}",
        f"PAIRED_END={str(paired).lower()}",
        f"REF_GENOME={ref_genome}",
        f"GTF={gtf}",
    ]
    Path(output_env).write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    with open(output_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sample_id", "condition", "replicate_group", "species",
                          "fastq", "fastq_r1", "fastq_r2"],
        )
        writer.writeheader()
        writer.writerows(samples)

    paired_flag = "--paired" if paired else "--single"
    click.echo(f"\nWrote {output_env} and {output_csv}")
    gtf_flag = f" --gtf {gtf}" if gtf else ""
    click.echo(
        f"Next: run `python cli.py submit-batch --sample-sheet {output_csv} "
        f"--organism {organism} --ref-genome {ref_genome} {paired_flag}{gtf_flag}`"
    )


# Quick submit a single RNA-Seq run with minimal options
@cli.command("quick")
@click.option("--fastq", required=True, help="Path to single-end FASTQ file")
@click.option("--ref-genome", required=True, help="HISAT2 index basename or reference FASTA path")
@click.option("--gtf", required=False, default=None,
              help="Annotation GTF path (omit for align-only)")
@click.option("--organism", default="human", type=click.Choice(ORGANISMS),
              help="Organism (default: human)")
def quick(fastq: str, ref_genome: str, gtf: str | None, organism: str) -> None:
    """Quick submit a single RNA-Seq run with minimal flags.

    Without --gtf the run is align-only (counting/DE skipped); with --gtf
    you get counting too. Single-sample runs never run DE (DESeq2 needs
    >=2 samples across >=2 conditions — use submit-batch for that).
    """
    experiment = "RNA-Seq"

    ensure_file(fastq, "--fastq")
    ensure_ref_genome(ref_genome)
    skip_quantification = False
    if gtf:
        ensure_file(gtf, "--gtf")
    else:
        skip_quantification = True
        click.echo("Note: no --gtf given, running align-only (counting/DE skipped).")

    run_id = f"quick-{uuid.uuid4().hex[:8]}"
    routing_ctx = {
        "experiment_type": experiment,
        "organism": organism,
        "paired_end": False,
        "reference_genome": ref_genome,
        "reference_fasta": None,
        "gtf": gtf,
        "panel_bed": None,
        "known_sites": [],
        "run_id": run_id,
        "skip_quantification": skip_quantification,
    }
    inputs = {"ref_genome": ref_genome, "gtf": gtf, "reference_fasta": None}
    samples = [
        {
            "sample_id": "sample-01",
            "condition": "unknown",
            "replicate_group": "1",
            "species": organism,
            "fastq_path": fastq,
            "fastq_r1": None,
            "fastq_r2": None,
        }
    ]
    inputs["samples"] = samples

    async def run_submit() -> None:
        client = await _connect_temporal()
        handle = await client.start_workflow(
            NGSPipelineWorkflow.run,
            RunInput(run_id, experiment, routing_ctx, inputs),
            id=f"ngs-{run_id}",
            task_queue="ngs-pipeline",
        )
        click.echo(f"Quick run submitted: {run_id}")
        click.echo(_monitor_url(handle.id))

    asyncio.run(run_submit())


if __name__ == "__main__":
    try:
        cli()
    except click.ClickException as exc:
        click.echo(f"Error: {exc.message}", err=True)
        sys.exit(1)
