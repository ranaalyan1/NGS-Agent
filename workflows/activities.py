"""Temporal activities that run each agent in its own Docker container.

File staging strategy (the part that used to break every multi-hop run):

* Every host file found in ``inputs`` **or** ``routing_ctx`` is bind-mounted
  into the container at a stable ``/mnt/inputs/N_<name>`` path, and the
  payload is rewritten to the container path.
* Reference *families* are mounted together: a HISAT2 basename pulls in its
  8 ``.ht2`` siblings, a FASTA pulls in ``.fai``/``.dict``/BWA index files,
  etc. — otherwise aligners cannot load their indexes.
* Container paths echoed back by agents (e.g. ingest returns
  ``/mnt/inputs/0_R1.fastq`` as ``raw_reads_r1``) are recognised on later
  hops and re-mounted from the same host file, instead of being passed
  through as dangling paths.
* ``s3://`` URIs pass through untouched; agents download them via MinIO.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from temporalio import activity

from shared.cache import CacheManager

load_dotenv()
cache = CacheManager()

CONTAINER_INPUT_DIR = "/mnt/inputs"

# Index/metadata siblings that must travel with a reference file. When the
# main path is mounted, every existing sibling is mounted alongside it so
# tools find their indexes next to the file inside the container.
HISAT2_SUFFIXES = [f".{i}.ht2" for i in range(1, 9)] + [".ht2"]
BWA_SUFFIXES = [".bwt", ".pac", ".ann", ".amb", ".sa"]
FASTA_SIBLINGS = [".fai", ".dict"]
ALL_INDEX_SUFFIXES = HISAT2_SUFFIXES + BWA_SUFFIXES + FASTA_SIBLINGS

# host path -> container path (stable across activities in this worker), and
# the reverse map used to re-mount container paths echoed back by agents.
_HOST_TO_CONTAINER: dict[str, str] = {}
_CONTAINER_TO_HOST: dict[str, str] = {}
_MOUNT_COUNTER = [0]


def _next_index() -> int:
    idx = _MOUNT_COUNTER[0]
    _MOUNT_COUNTER[0] += 1
    return idx


def _mount_file(host_file: Path, mounts: list[tuple[str, str]], index: int | None = None) -> str:
    """Bind-mount a host file; return its stable container path."""
    host_str = str(host_file.resolve())
    if host_str in _HOST_TO_CONTAINER:
        container_path = _HOST_TO_CONTAINER[host_str]
    else:
        idx = _next_index() if index is None else index
        container_path = f"{CONTAINER_INPUT_DIR}/{idx}_{host_file.name}"
        _HOST_TO_CONTAINER[host_str] = container_path
        _CONTAINER_TO_HOST[container_path] = host_str
    pair = (host_str, container_path)
    if pair not in mounts:
        mounts.append(pair)
    return container_path


def _mount_with_family(host_path: Path, mounts: list[tuple[str, str]]) -> str:
    """Mount a file plus any existing index siblings; return container path."""
    container_path = _mount_file(host_path, mounts)
    # Siblings share the container directory; tools resolve them by name.
    for suffix in ALL_INDEX_SUFFIXES:
        sibling = Path(str(host_path) + suffix)
        if sibling.exists() and sibling.is_file():
            sib_container = container_path + suffix
            sib_host = str(sibling.resolve())
            _HOST_TO_CONTAINER.setdefault(sib_host, sib_container)
            _CONTAINER_TO_HOST.setdefault(sib_container, sib_host)
            pair = (sib_host, sib_container)
            if pair not in mounts:
                mounts.append(pair)
    return container_path


def _mount_index_prefix(prefix: Path, mounts: list[tuple[str, str]]) -> str | None:
    """Mount a reference basename (e.g. HISAT2 ``grch38_idx``) and siblings.

    Returns the container-side prefix, or None if no index files exist.
    """
    siblings = [
        Path(str(prefix) + suffix)
        for suffix in ALL_INDEX_SUFFIXES
        if Path(str(prefix) + suffix).exists() and Path(str(prefix) + suffix).is_file()
    ]
    if not siblings:
        return None
    idx = _next_index()
    container_prefix = f"{CONTAINER_INPUT_DIR}/{idx}_{prefix.name}"
    for sib in siblings:
        suffix = str(sib)[len(str(prefix)):]
        sib_container = container_prefix + suffix
        sib_host = str(sib.resolve())
        _HOST_TO_CONTAINER.setdefault(sib_host, sib_container)
        _CONTAINER_TO_HOST.setdefault(sib_container, sib_host)
        pair = (sib_host, sib_container)
        if pair not in mounts:
            mounts.append(pair)
    # Remember the prefix mapping itself for later hops.
    _CONTAINER_TO_HOST.setdefault(container_prefix, str(prefix))
    return container_prefix


def _replace_local_file_paths(
    obj: Any, mounts: list[tuple[str, str]], mount_index: list[int]
) -> Any:
    # NOTE: mount_index is kept for backwards compatibility but the stable
    # module-level counter is used for naming; the list is still advanced so
    # any external callers observing it keep working.
    if isinstance(obj, dict):
        return {k: _replace_local_file_paths(v, mounts, mount_index) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_local_file_paths(v, mounts, mount_index) for v in obj]
    if isinstance(obj, str):
        if not obj or obj.startswith("s3://"):
            return obj
        # A container path echoed back by an earlier agent: re-mount the
        # original host file (same container path) so this hop can see it.
        if obj in _CONTAINER_TO_HOST:
            host_str = _CONTAINER_TO_HOST[obj]
            if Path(host_str).exists():
                pair = (host_str, obj)
                if pair not in mounts:
                    mounts.append(pair)
                return obj
            # Prefix-only mapping (index basenames): re-mount siblings.
            remounted = _mount_index_prefix(Path(host_str), mounts)
            if remounted:
                return remounted
            return obj
        p = Path(obj)
        if p.exists() and p.is_file():
            mount_index[0] += 1
            return _mount_with_family(p, mounts)
        # Reference basename rather than a file (HISAT2/BWA index prefix)?
        if not p.exists():
            remounted = _mount_index_prefix(p, mounts)
            if remounted:
                mount_index[0] += 1
                return remounted
    return obj


@activity.defn
async def ingest_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("ingest", inputs, routing_ctx)


@activity.defn
async def qc_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("qc", inputs, routing_ctx)


@activity.defn
async def ai_decider_activity(
    inputs: dict[str, Any], routing_ctx: dict[str, Any]
) -> dict[str, Any]:
    return await run_agent_container("ai_decider", inputs, routing_ctx)


@activity.defn
async def trim_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("trim", inputs, routing_ctx)


@activity.defn
async def align_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("align", inputs, routing_ctx)


@activity.defn
async def bwa_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("bwa_agent", inputs, routing_ctx)


@activity.defn
async def gatk_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("gatk_agent", inputs, routing_ctx)


@activity.defn
async def annotation_activity(
    inputs: dict[str, Any], routing_ctx: dict[str, Any]
) -> dict[str, Any]:
    return await run_agent_container("annotation_agent", inputs, routing_ctx)


@activity.defn
async def annotate_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("annotation_agent", inputs, routing_ctx)


@activity.defn
async def count_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("count", inputs, routing_ctx)


@activity.defn
async def de_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("de_agent", inputs, routing_ctx)


@activity.defn
async def insight_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("insight_agent", inputs, routing_ctx)


@activity.defn
async def report_builder_activity(
    inputs: dict[str, Any], routing_ctx: dict[str, Any]
) -> dict[str, Any]:
    return await run_agent_container("report_builder", inputs, routing_ctx)


@activity.defn
async def coverage_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("coverage_agent", inputs, routing_ctx)


@activity.defn
async def report_activity(inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
    return await run_agent_container("report_agent", inputs, routing_ctx)


async def run_agent_container(
    agent_name: str, inputs: dict[str, Any], routing_ctx: dict[str, Any]
) -> dict[str, Any]:
    cache_key = cache.compute_hash(agent_name, {"inputs": inputs, "routing_ctx": routing_ctx})
    try:
        cached = await cache.get(cache_key)
    except Exception:
        cached = None
    if cached:
        return cached

    mounts: list[tuple[str, str]] = []
    mount_index = [0]
    container_inputs = _replace_local_file_paths(inputs, mounts, mount_index)
    # routing_ctx carries reference paths (HISAT2 basename, GTF, FASTA, BED,
    # known-sites) that agents read directly — they must be staged too.
    container_routing_ctx = _replace_local_file_paths(routing_ctx, mounts, mount_index)

    # --- Resource Governor ---
    # Default boundaries (Low/Standard profile)
    cpus = os.environ.get("AGENT_CPUS", "2")
    memory = os.environ.get("AGENT_MEMORY", "4g")

    # High resource profile for intensive mapping/calling agents
    if agent_name in {"align", "bwa_agent", "gatk_agent"}:
        cpus = os.environ.get("HIGH_AGENT_CPUS", cpus)
        memory = os.environ.get("HIGH_AGENT_MEMORY", "6g")

    cmd = [
        "docker",
        "run",
        "--rm",
        f"--cpus={cpus}",
        f"--memory={memory}",
    ]

    for host_path, container_path in mounts:
        cmd.extend(["-v", f"{host_path}:{container_path}:ro"])

    cmd.extend([
        "-e",
        f"AGENT_THREADS={cpus}",
        "-e",
        f"AGENT_INPUTS={json.dumps(container_inputs)}",
        "-e",
        f"ROUTING_CONTEXT={json.dumps(container_routing_ctx)}",
        "-e",
        f"RUN_ID={routing_ctx.get('run_id', 'unknown')}",
        "-e",
        f"S3_ENDPOINT={os.environ.get('S3_ENDPOINT', 'http://localhost:9000')}",
        "-e",
        f"S3_ACCESS_KEY={os.environ.get('S3_ACCESS_KEY', 'minioadmin')}",
        "-e",
        f"S3_SECRET_KEY={os.environ.get('S3_SECRET_KEY', 'minioadmin')}",
        "-e",
        f"ARTIFACT_BUCKET={os.environ.get('ARTIFACT_BUCKET', 'ngs-artifacts')}",
        "-e",
        f"ANTHROPIC_API_KEY={os.environ.get('ANTHROPIC_API_KEY', '')}",
        "-e",
        f"ANTHROPIC_MODEL={os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5')}",
        "-e",
        f"OPENROUTER_API_KEY={os.environ.get('OPENROUTER_API_KEY', '')}",
        "-e",
        f"OPENROUTER_MODEL={os.environ.get('OPENROUTER_MODEL', 'deepseek/deepseek-chat-v3-0324')}",
        f"ngs/{agent_name}-agent:latest",
    ])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Docker is not available on the worker host. Install Docker Engine "
            "and make sure the worker runs on a host with `docker` in PATH."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(f"Agent {agent_name} failed: {result.stderr.strip()}")

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"Agent {agent_name} returned empty output")

    output = json.loads(stdout)
    if isinstance(output, dict):
        output.setdefault("status", "ok")
        output.setdefault("payload", {})
        output.setdefault("reasoning", f"{agent_name} completed")
        output.setdefault("halt", False)
        output.setdefault("halt_reason", "")
    try:
        await cache.set(cache_key, output)
    except Exception:
        pass
    return output
