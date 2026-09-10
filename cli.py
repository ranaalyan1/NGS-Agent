#!/usr/bin/env python3
"""NGS Agent Swarm CLI.

By default runs the swarm pipeline **locally** — in-process, with no Temporal
server, no Postgres, no Redis, and no external worker. Temporal orchestration
remains available for shared/core-facility deployments via ``--temporal``
(or ``NGS_MODE=temporal``).
"""
import asyncio
import csv
import os
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional

import click

try:  # optional — only needed if you want .env loading
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv() -> None:
        return None

load_dotenv()

from swarm.engine import RunInput as LocalRunInput
from swarm.engine import run_pipeline
from swarm.runner import AgentRunner
from swarm.store import RunStore

_store = RunStore()


def _mode(use_temporal: Optional[bool]) -> str:
    """Resolve execution mode from the CLI flag, then the environment."""
    if use_temporal is not None:
        return "temporal" if use_temporal else "local"
    env = os.environ.get("NGS_MODE", "").strip().lower()
    if env in {"temporal", "local"}:
        return env
    return "local"


def _runner(no_cache: bool) -> AgentRunner:
    return AgentRunner(cache=None) if no_cache else AgentRunner()


@click.group()
def cli() -> None:
    """NGS Agent Swarm CLI (local-first, Temporal optional)."""


def ensure_file(path_value: str, label: str) -> None:
    if not Path(path_value).exists() or not Path(path_value).is_file():
        raise click.BadParameter(f"{label} does not exist or is not a file: {path_value}")


# ---------------------------------------------------------------------------
# Shared input building
# ---------------------------------------------------------------------------

def _build_submission(
    experiment: str,
    organism: str,
    ref_genome: str,
    reference_fasta: Optional[str],
    gtf: Optional[str],
    panel_bed: Optional[str],
    known_sites: tuple[str, ...],
    paired: bool,
    samples: list[dict],
    run_id: str,
    sample_sheet: Optional[str] = None,
) -> tuple[LocalRunInput, Dict[str, Any]]:
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
    }
    if sample_sheet:
        routing_ctx["sample_sheet"] = sample_sheet

    inputs: Dict[str, Any] = {
        "ref_genome": ref_genome,
        "gtf": gtf,
        "reference_fasta": reference_fasta,
        "samples": samples,
    }
    if panel_bed:
        inputs["panel_bed"] = panel_bed
    if known_sites:
        inputs["known_sites"] = list(known_sites)

    return LocalRunInput(run_id, experiment, routing_ctx, inputs), routing_ctx


def _print_progress(line: str) -> None:
    click.echo(f"  {line}")


def _run_local(input_data: LocalRunInput, no_cache: bool) -> Dict[str, Any]:
    _store.create(input_data.run_id, {"experiment": input_data.experiment_type, "mode": "local"})

    async def _run() -> Dict[str, Any]:
        _store.update(input_data.run_id, status="running")
        return await run_pipeline(input_data, _runner(no_cache).run_agent, progress=_print_progress)

    try:
        result = asyncio.run(_run())
    except KeyboardInterrupt:
        _store.update(input_data.run_id, status="failed", reason="interrupted by user")
        click.echo("Interrupted. Run marked as failed.")
        raise click.Abort()
    except Exception as exc:
        _store.update(input_data.run_id, status="failed", reason=str(exc))
        click.echo(f"Run failed: {exc}", err=True)
        raise click.ClickException(f"Run failed: {exc}")

    status = result.get("status", "complete")
    _store.update(
        input_data.run_id,
        status="halted" if status == "halted" else status,
        result=result,
        report_html=result.get("report_html"),
        narrative=result.get("report_narrative"),
    )
    return result


def _summarize_local_result(result: Dict[str, Any]) -> None:
    click.echo(f"  Status: {result.get('status')}")
    click.echo(f"  Samples processed: {result.get('samples_processed')}")
    for sample in result.get("sample_results", []):
        click.echo(f"    - {sample.get('sample_id')}: {sample.get('status')}")
    if result.get("report_html"):
        click.echo(f"  Report: {result['report_html']}")
    if result.get("report_narrative"):
        click.echo(f"  Narrative: {result['report_narrative']}")


# ---------------------------------------------------------------------------
# Temporal (opt-in) helpers — imported lazily so the local path never needs
# the temporalio dependency.
# ---------------------------------------------------------------------------

def _temporal_submit(input_data: LocalRunInput) -> None:
    from temporalio.client import Client

    from workflows.pipeline_workflow import NGSPipelineWorkflow, RunInput

    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")

    async def run_submit() -> None:
        client = await Client.connect(temporal_host)
        handle = await client.start_workflow(
            NGSPipelineWorkflow.run,
            RunInput(
                input_data.run_id,
                input_data.experiment_type,
                input_data.routing_context,
                input_data.initial_inputs,
            ),
            id=f"ngs-{input_data.run_id}",
            task_queue="ngs-pipeline",
        )
        click.echo(f"Run submitted: {input_data.run_id}")
        click.echo(f"Monitor at http://localhost:8080/namespaces/default/workflows/{handle.id}")

    try:
        asyncio.run(run_submit())
    except RuntimeError as exc:
        if "Connection" in str(exc) or "connect" in str(exc).lower():
            raise click.ClickException(
                f"Could not reach Temporal at {temporal_host}. "
                "Start `docker compose up -d` + `python worker.py`, or drop --temporal "
                "to run locally (no Temporal required)."
            ) from exc
        raise


def _temporal_status(run_id: str) -> None:
    from temporalio.client import Client

    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")

    async def run_status() -> None:
        client = await Client.connect(temporal_host)
        handle = client.get_workflow_handle(f"ngs-{run_id}")
        desc = await handle.describe()
        click.echo(f"Status: {desc.status.name}")
        if desc.status.name == "COMPLETED":
            result = await handle.result()
            click.echo(f"Result: {result}")

    try:
        asyncio.run(run_status())
    except RuntimeError as exc:
        if "Connection" in str(exc) or "connect" in str(exc).lower():
            raise click.ClickException(
                f"Could not reach Temporal at {temporal_host}. "
                "Start `docker compose up -d` + `python worker.py`, or check `python cli.py status <run-id>` "
                "for the local run store."
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--fastq", required=False, help="Path to single-end FASTQ")
@click.option("--fastq-r1", required=False, help="Path to paired-end R1 FASTQ")
@click.option("--fastq-r2", required=False, help="Path to paired-end R2 FASTQ")
@click.option("--experiment", default="RNA-Seq", type=click.Choice(["RNA-Seq", "WGS", "WES"]))
@click.option(
    "--organism",
    required=True,
    type=click.Choice(["human", "mouse", "rat", "zebrafish", "yeast", "other"]),
    help="Target organism",
)
@click.option("--ref-genome", required=True, help="HISAT2 index basename path")
@click.option("--reference-fasta", required=False, help="Reference FASTA path for DNA branch tools")
@click.option("--gtf", required=False, help="Annotation GTF path (optional, for RNA-Seq counting if needed)")
@click.option("--panel-bed", required=False, help="Optional panel BED for DNA coverage plots")
@click.option("--known-sites", required=False, multiple=True, help="Known sites VCFs for GATK BQSR (repeatable)")
@click.option("--paired/--single", default=False, help="Use paired-end mode")
@click.option(
    "--temporal",
    "use_temporal",
    is_flag=True,
    default=None,
    help="Submit via a Temporal server + worker instead of running locally.",
)
@click.option("--no-cache", is_flag=True, help="Disable the local content-addressed cache.")
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
    use_temporal: bool | None,
    no_cache: bool,
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

    if not Path(ref_genome).exists():
        raise click.BadParameter(f"--ref-genome path does not exist: {ref_genome}")
    if experiment in {"WGS", "WES"} and not reference_fasta:
        raise click.BadParameter("DNA-Seq analysis requires --reference-fasta")
    if reference_fasta:
        ensure_file(reference_fasta, "--reference-fasta")
    if gtf and not Path(gtf).exists():
        raise click.BadParameter(f"--gtf path does not exist: {gtf}")
    if panel_bed:
        ensure_file(panel_bed, "--panel-bed")
    for known_site in known_sites:
        ensure_file(known_site, "--known-sites")

    run_id = f"run-{uuid.uuid4().hex[:8]}"
    samples = [
        {
            "sample_id": "sample-01",
            "condition": "unknown",
            "replicate_group": "1",
            "species": organism,
            "fastq_path": fastq,
            "fastq_r1": fastq_r1,
            "fastq_r2": fastq_r2,
        }
    ]
    input_data, _ = _build_submission(
        experiment, organism, ref_genome, reference_fasta, gtf, panel_bed,
        known_sites, paired, samples, run_id,
    )

    if _mode(use_temporal) == "temporal":
        _temporal_submit(input_data)
        return

    click.echo(f"Run submitted: {run_id} (local mode — no Temporal server required)")
    result = _run_local(input_data, no_cache)
    _summarize_local_result(result)
    click.echo(f"Check status: python cli.py status {run_id}")


@cli.command()
@click.option("--sample-sheet", required=True, help="Path to CSV sample sheet")
@click.option("--experiment", default="RNA-Seq", type=click.Choice(["RNA-Seq", "WGS", "WES"]))
@click.option(
    "--organism",
    required=True,
    type=click.Choice(["human", "mouse", "rat", "zebrafish", "yeast", "other", "mixed"]),
    help="Default target organism (can be overridden in sample sheet)",
)
@click.option("--ref-genome", required=True, help="HISAT2 index basename path")
@click.option("--reference-fasta", required=False, help="Reference FASTA path for DNA branch tools")
@click.option("--gtf", required=False, help="Annotation GTF path")
@click.option("--paired/--single", default=False, help="Use paired-end mode")
@click.option(
    "--temporal",
    "use_temporal",
    is_flag=True,
    default=None,
    help="Submit via a Temporal server + worker instead of running locally.",
)
@click.option("--no-cache", is_flag=True, help="Disable the local content-addressed cache.")
def submit_batch(
    sample_sheet: str,
    experiment: str,
    organism: str,
    ref_genome: str,
    reference_fasta: str | None,
    gtf: str | None,
    paired: bool,
    use_temporal: bool | None,
    no_cache: bool,
) -> None:
    """Submit a batch pipeline run using a CSV sample sheet."""
    ensure_file(sample_sheet, "--sample-sheet")

    samples = []
    with open(sample_sheet, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples.append(row)

    if not samples:
        raise click.BadParameter("Sample sheet is empty")

    sample_ids = [s.get("sample_id", "") for s in samples]
    duplicates = [item for item, count in Counter(sample_ids).items() if count > 1]
    if duplicates:
        raise click.BadParameter(f"Sample IDs must be unique. Found duplicates: {duplicates}")

    for i, row in enumerate(samples):
        if not row.get("sample_id"):
            raise click.BadParameter(f"Row {i+1}: missing sample_id")
        if paired:
            if not row.get("fastq_r1") or not row.get("fastq_r2"):
                raise click.BadParameter(f"Row {i+1}: missing fastq_r1 or fastq_r2 for paired mode")
            ensure_file(row["fastq_r1"], f"Row {i+1} fastq_r1")
            ensure_file(row["fastq_r2"], f"Row {i+1} fastq_r2")
        else:
            if not row.get("fastq"):
                raise click.BadParameter(f"Row {i+1}: missing fastq for single mode")
            ensure_file(row["fastq"], f"Row {i+1} fastq")

    run_id = f"batch-{uuid.uuid4().hex[:8]}"
    input_data, _ = _build_submission(
        experiment, organism, ref_genome, reference_fasta, gtf, None,
        (), paired, samples, run_id, sample_sheet=sample_sheet,
    )

    if _mode(use_temporal) == "temporal":
        _temporal_submit(input_data)
        return

    click.echo(
        f"Batch run submitted: {run_id} ({len(samples)} samples, local mode — no Temporal server required)"
    )
    result = _run_local(input_data, no_cache)
    _summarize_local_result(result)
    click.echo(f"Check status: python cli.py status {run_id}")


@cli.command()
@click.argument("run_id", required=False)
@click.option(
    "--temporal",
    "use_temporal",
    is_flag=True,
    default=None,
    help="Query Temporal instead of the local run store.",
)
def status(run_id: str | None, use_temporal: bool | None) -> None:
    """Get status of a run (local store by default)."""
    if _mode(use_temporal) == "temporal":
        if not run_id:
            raise click.UsageError("run_id is required with --temporal")
        _temporal_status(run_id)
        return

    if not run_id:
        runs = _store.list_runs()
        if not runs:
            click.echo("No local runs found.")
            return
        click.echo(f"{'RUN ID':<18} {'STATUS':<22} {'UPDATED'}")
        for record in runs:
            click.echo(f"{record.get('run_id', '?'):<18} {record.get('status', '?'):<22} {record.get('updated_at', '')}")
        return

    record = _store.get(run_id)
    if not record:
        click.echo(f"No local run found for: {run_id}", err=True)
        raise click.ClickException(f"Unknown run: {run_id}")

    click.echo(f"Run ID: {record.get('run_id')}")
    click.echo(f"Status: {record.get('status')}")
    click.echo(f"Updated: {record.get('updated_at')}")
    if record.get("reason"):
        click.echo(f"Reason: {record['reason']}")
    if record.get("report_html"):
        click.echo(f"Report: {record['report_html']}")
    if record.get("narrative"):
        click.echo(f"Narrative: {record['narrative']}")
    if record.get("status") == "complete" and record.get("result"):
        _summarize_local_result(record["result"])


@cli.command()
@click.option("--output-env", default=".env", show_default=True)
@click.option("--output-csv", default="sample_sheet.csv", show_default=True)
def wizard(output_env: str, output_csv: str) -> None:
    """Interactive setup wizard for batch analysis."""
    experiment_type = click.prompt("Analysis type", type=click.Choice(["RNA-Seq", "WGS", "WES"]))
    paired = click.confirm("Is the dataset paired-end?", default=True)
    organism = click.prompt("Default genome preset", type=click.Choice(["hg38", "mm10", "mixed"]))

    num_samples = click.prompt("How many samples to configure now?", type=int, default=2)

    samples = []
    for i in range(num_samples):
        click.echo(f"\n--- Configuring Sample {i+1} ---")
        sample_id = click.prompt("Sample ID", default=f"S{i+1}")
        condition = click.prompt("Condition (e.g., control, treated)", default="control" if i == 0 else "treated")
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
            "fastq_r2": fastq_r2,
        })

    ref_genome = click.prompt("\nReference genome index basename", type=str)
    gtf = click.prompt("Annotation GTF path", type=str, default="", show_default=False)

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
            fieldnames=["sample_id", "condition", "replicate_group", "species", "fastq", "fastq_r1", "fastq_r2"],
        )
        writer.writeheader()
        writer.writerows(samples)

    click.echo(f"\nWrote {output_env} and {output_csv}")
    gtf_flag = f"--gtf {gtf}" if gtf else ""
    click.echo(
        f"Next: run `python cli.py submit-batch --sample-sheet {output_csv} "
        f"--organism {organism} --ref-genome {ref_genome} {gtf_flag}`"
    )


@cli.command("quick")
@click.option("--fastq", required=True, help="Path to FASTQ file")
@click.option(
    "--organism",
    default="human",
    type=click.Choice(["human", "mouse", "rat", "zebrafish", "yeast", "other"]),
    help="Organism (default: human)",
)
@click.option(
    "--temporal",
    "use_temporal",
    is_flag=True,
    default=None,
    help="Submit via a Temporal server + worker instead of running locally.",
)
@click.option("--no-cache", is_flag=True, help="Disable the local content-addressed cache.")
def quick(fastq: str, organism: str, use_temporal: bool | None, no_cache: bool) -> None:
    """Quick submit a single pipeline run with minimal flags."""
    experiment = "RNA-Seq"
    ref_genome = "hg38" if organism in {"human", "other"} else "mm10"

    if not Path(fastq).exists():
        raise click.BadParameter(f"--fastq path does not exist: {fastq}")

    run_id = f"quick-{uuid.uuid4().hex[:8]}"
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
    input_data, _ = _build_submission(
        experiment, organism, ref_genome, None, None, None, (), False, samples, run_id
    )

    if _mode(use_temporal) == "temporal":
        _temporal_submit(input_data)
        return

    click.echo(f"Quick run submitted: {run_id} (local mode — no Temporal server required)")
    result = _run_local(input_data, no_cache)
    _summarize_local_result(result)
    click.echo(f"Check status: python cli.py status {run_id}")


if __name__ == "__main__":
    cli()
