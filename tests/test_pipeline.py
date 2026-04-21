import json
import os
import shutil
import subprocess
from pathlib import Path

import boto3
import pytest


pytestmark = pytest.mark.integration


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"Missing required env var for integration test: {name}")
    return value


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run_container(agent: str, inputs: dict, routing: dict, mounts: list[tuple[str, str]] | None = None):
    cmd = ["docker", "run", "--rm"]
    if mounts:
        for src, dst in mounts:
            cmd.extend(["-v", f"{src}:{dst}:ro"])

    cmd.extend(
        [
            "-e",
            f"AGENT_INPUTS={json.dumps(inputs)}",
            "-e",
            f"ROUTING_CONTEXT={json.dumps(routing)}",
            "-e",
            f"RUN_ID={routing['run_id']}",
            "-e",
            f"S3_ENDPOINT={os.environ.get('S3_ENDPOINT', 'http://localhost:9000')}",
            "-e",
            f"S3_ACCESS_KEY={os.environ.get('S3_ACCESS_KEY', 'minioadmin')}",
            "-e",
            f"S3_SECRET_KEY={os.environ.get('S3_SECRET_KEY', 'minioadmin')}",
            "-e",
            f"ARTIFACT_BUCKET={os.environ.get('ARTIFACT_BUCKET', 'ngs-artifacts')}",
            f"ngs/{agent}-agent:latest",
        ]
    )
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout.strip())


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "minioadmin"),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "minioadmin"),
    )


def _head_size(s3_uri: str) -> int:
    bucket, key = s3_uri[5:].split("/", 1)
    meta = _s3_client().head_object(Bucket=bucket, Key=key)
    return int(meta.get("ContentLength", 0))


def test_real_agents_pipeline_outputs():
    if os.environ.get("RUN_NGS_FUNCTIONAL") != "1":
        pytest.skip("Set RUN_NGS_FUNCTIONAL=1 to run integration test")
    if not _docker_available():
        pytest.skip("Docker is not available in PATH")

    host_r1 = _require_env("TEST_FASTQ_R1")
    host_r2 = _require_env("TEST_FASTQ_R2")
    host_ref = _require_env("TEST_HISAT2_INDEX_DIR")
    host_gtf = _require_env("TEST_GTF")

    if not Path(host_r1).exists() or not Path(host_r2).exists():
        pytest.skip("Provided TEST_FASTQ_R1/R2 files do not exist")
    if not Path(host_gtf).exists():
        pytest.skip("Provided TEST_GTF file does not exist")

    run_id = "itest-run"
    routing = {
        "run_id": run_id,
        "paired_end": True,
        "reference_genome": "/mnt/ref/genome_index",
        "gtf": "/mnt/ref/genes.gtf",
    }

    ingest = _run_container(
        "ingest",
        {"fastq_r1": "/mnt/inputs/r1.fastq.gz", "fastq_r2": "/mnt/inputs/r2.fastq.gz"},
        routing,
        mounts=[
            (host_r1, "/mnt/inputs/r1.fastq.gz"),
            (host_r2, "/mnt/inputs/r2.fastq.gz"),
        ],
    )

    qc = _run_container(
        "qc",
        ingest,
        routing,
        mounts=[
            (host_r1, "/mnt/inputs/r1.fastq.gz"),
        ],
    )
    assert qc["payload"].get("report_html") is not None

    align = _run_container(
        "align",
        ingest,
        routing,
        mounts=[
            (host_r1, "/mnt/inputs/r1.fastq.gz"),
            (host_r2, "/mnt/inputs/r2.fastq.gz"),
            (host_ref, "/mnt/ref"),
        ],
    )

    count = _run_container(
        "count",
        align,
        routing,
        mounts=[
            (host_gtf, "/mnt/ref/genes.gtf"),
        ],
    )

    assert _head_size(qc["payload"]["report_html"]) > 0
    assert _head_size(align["payload"]["bam_path"]) > 0
    assert _head_size(count["payload"]["count_matrix"]) > 0
    assert int(count["payload"].get("n_genes", 0)) >= 1
