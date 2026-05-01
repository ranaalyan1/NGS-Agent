import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
import pytest


pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_PYTHONPATH = os.pathsep.join([
    str(_REPO_ROOT / "agents" / "base"),
    str(_REPO_ROOT / "shared"),
])


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"Missing required env var for integration test: {name}")
    return value


def _run_agent(agent: str, inputs: dict, routing: dict):
    """Run an agent script natively as a Python subprocess."""
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _AGENT_PYTHONPATH + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    )
    env["AGENT_INPUTS"] = json.dumps(inputs)
    env["ROUTING_CONTEXT"] = json.dumps(routing)
    env["RUN_ID"] = routing["run_id"]

    agent_script = _REPO_ROOT / "agents" / agent / "main.py"
    res = subprocess.run(
        [sys.executable, str(agent_script)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
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
        "reference_genome": host_ref,
        "gtf": host_gtf,
    }

    ingest = _run_agent(
        "ingest",
        {"fastq_r1": host_r1, "fastq_r2": host_r2},
        routing,
    )

    qc = _run_agent("qc", ingest, routing)
    assert qc["payload"].get("report_html") is not None

    align = _run_agent("align", ingest, routing)

    count = _run_agent("count", align, routing)

    assert _head_size(qc["payload"]["report_html"]) > 0
    assert _head_size(align["payload"]["bam_path"]) > 0
    assert _head_size(count["payload"]["count_matrix"]) > 0
    assert int(count["payload"].get("n_genes", 0)) >= 1
