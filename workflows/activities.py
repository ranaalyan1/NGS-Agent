import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
from temporalio import activity

from shared.cache import CacheManager

load_dotenv()
cache = CacheManager()

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Directories that must be on PYTHONPATH so agent scripts can resolve their imports
_AGENT_PYTHONPATH = os.pathsep.join([
    str(_REPO_ROOT / "agents" / "base"),
    str(_REPO_ROOT / "shared"),
])


@activity.defn
async def ingest_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("ingest", inputs, routing_ctx)


@activity.defn
async def qc_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("qc", inputs, routing_ctx)


@activity.defn
async def ai_decider_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("ai_decider", inputs, routing_ctx)


@activity.defn
async def trim_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("trim", inputs, routing_ctx)


@activity.defn
async def align_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("align", inputs, routing_ctx)


@activity.defn
async def bwa_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("bwa_agent", inputs, routing_ctx)


@activity.defn
async def gatk_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("gatk_agent", inputs, routing_ctx)


@activity.defn
async def annotation_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("annotation_agent", inputs, routing_ctx)


@activity.defn
async def count_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("count", inputs, routing_ctx)


@activity.defn
async def de_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("de_agent", inputs, routing_ctx)


@activity.defn
async def insight_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("insight_agent", inputs, routing_ctx)


@activity.defn
async def report_builder_activity(inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
    return await run_agent_native("report_builder", inputs, routing_ctx)


async def run_agent_native(
    agent_name: str, inputs: Dict[str, Any], routing_ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """Run an agent script natively as a Python subprocess (no Docker required)."""
    cache_key = cache.compute_hash(agent_name, {"inputs": inputs, "routing_ctx": routing_ctx})
    cached = await cache.get(cache_key)
    if cached:
        return cached

    # Build the subprocess environment
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _AGENT_PYTHONPATH + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    )
    env["AGENT_INPUTS"] = json.dumps(inputs)
    env["ROUTING_CONTEXT"] = json.dumps(routing_ctx)
    env["RUN_ID"] = routing_ctx.get("run_id", "unknown")
    # Honour resource-governor hints via AGENT_THREADS.  Unlike Docker's --cpus
    # flag, native processes are not hard-constrained; each agent script is
    # expected to read AGENT_THREADS and apply its own thread limits (e.g.
    # pass -p $AGENT_THREADS to hisat2/samtools/featureCounts).
    cpus = os.environ.get("AGENT_CPUS", "2")
    if agent_name in {"align", "bwa_agent", "gatk_agent"}:
        cpus = os.environ.get("HIGH_AGENT_CPUS", cpus)
    env["AGENT_THREADS"] = cpus

    agent_script = _REPO_ROOT / "agents" / agent_name / "main.py"
    result = subprocess.run(
        [sys.executable, str(agent_script)],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Agent {agent_name} failed: {result.stderr.strip()}")

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"Agent {agent_name} returned empty output")

    output = json.loads(stdout)
    await cache.set(cache_key, output)
    return output
