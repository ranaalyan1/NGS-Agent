"""Docker agent runner and local filesystem cache for the NGS swarm pipeline.

This module deliberately imports **no** Temporal, Redis, or S3 client code, so
the local execution path works on a machine that has only Docker (plus the
per-tool agent images) installed. It provides:

* :class:`AgentRunner` — runs a pipeline stage by executing its agent Docker
  image with host inputs mounted read-only, mirroring the behaviour of
  ``workflows/activities.run_agent_container`` but with an injectable cache
  and per-stage timeouts.
* :class:`LocalCache` — a content-addressed filesystem cache that replaces the
  Redis + MinIO cache for single-machine runs, preserving the "identical
  re-runs return instantly" behaviour without any external services.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def ngs_home() -> Path:
    """Root directory for local NGS-Agent state (cache + run records)."""
    return Path(os.environ.get("NGS_HOME", str(Path.home() / ".ngsagent")))


def replace_local_file_paths(
    obj: Any, mounts: list[tuple[str, str]], mount_index: list[int]
) -> Any:
    """Rewrite host file paths inside an inputs object into container mounts.

    Every string that points at an existing local file is replaced with a
    ``/mnt/inputs/<n>_<name>`` container path and a corresponding read-only
    mount is appended to ``mounts``. Mirrors the helper in
    ``workflows/activities.py``.
    """
    if isinstance(obj, dict):
        return {k: replace_local_file_paths(v, mounts, mount_index) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_local_file_paths(v, mounts, mount_index) for v in obj]
    if isinstance(obj, str):
        p = Path(obj)
        if p.exists() and p.is_file():
            idx = mount_index[0]
            mount_index[0] += 1
            container_path = f"/mnt/inputs/{idx}_{p.name}"
            mounts.append((str(p.resolve()), container_path))
            return container_path
    return obj


def _with_file_stats(value: Any) -> Any:
    """Enrich file paths with cheap stat metadata for content-aware hashing.

    Avoids reading (possibly huge) FASTQ/BAM payloads: a path plus
    ``(size, mtime_ns)`` changes whenever the underlying file is replaced or
    modified, which is sufficient to avoid stale cache hits in practice.
    """
    if isinstance(value, dict):
        return {k: _with_file_stats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_with_file_stats(v) for v in value]
    if isinstance(value, str):
        p = Path(value)
        if p.exists() and p.is_file():
            st = p.stat()
            return {"path": value, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return value


def compute_input_hash(agent_name: str, inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> str:
    """Content-addressed cache key for an agent invocation."""
    payload = {
        "agent": agent_name,
        "inputs": _with_file_stats(inputs),
        "routing_ctx": routing_ctx,
    }
    content = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.blake2b(content.encode("utf-8")).hexdigest()[:16]


class LocalCache:
    """Content-addressed filesystem cache (drop-in for ``shared.cache.CacheManager``)."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = base_dir or (ngs_home() / "cache")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def compute_hash(self, agent_name: str, inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> str:
        return compute_input_hash(agent_name, inputs, routing_ctx)

    async def get(self, cache_key: str, ttl_days: int = 30) -> Optional[Dict[str, Any]]:
        path = self.base_dir / f"{cache_key}.json"
        if not path.exists():
            return None
        try:
            if time.time() - path.stat().st_mtime > ttl_days * 24 * 3600:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def set(self, cache_key: str, data: Dict[str, Any], ttl_days: int = 30) -> None:
        path = self.base_dir / f"{cache_key}.json"
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)


class AgentRunner:
    """Runs a pipeline stage by executing the matching agent Docker image."""

    # Agents whose stage is CPU/memory heavy get the "high" resource profile.
    _HIGH_RESOURCE_AGENTS = frozenset({"align", "bwa_agent", "gatk_agent"})

    def __init__(
        self,
        cache: Any | None = None,
        docker: str = "docker",
        run_fn: Optional[Callable[..., subprocess.CompletedProcess]] = None,
    ) -> None:
        # ``cache=None`` disables caching entirely (``--no-cache``).
        self.cache = LocalCache() if cache is None and not os.environ.get("NGS_NO_CACHE") else cache
        if os.environ.get("NGS_NO_CACHE") == "1":
            self.cache = None
        self.docker = docker
        self._run_fn = run_fn or subprocess.run

    def _resource_profile(self, agent_name: str) -> tuple[str, str]:
        cpus = os.environ.get("AGENT_CPUS", "2")
        memory = os.environ.get("AGENT_MEMORY", "4g")
        if agent_name in self._HIGH_RESOURCE_AGENTS:
            cpus = os.environ.get("HIGH_AGENT_CPUS", cpus)
            memory = os.environ.get("HIGH_AGENT_MEMORY", "6g")
        return cpus, memory

    def _docker_command(
        self, agent_name: str, inputs: Dict[str, Any], routing_ctx: Dict[str, Any]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        mounts: list[tuple[str, str]] = []
        mount_index = [0]
        container_inputs = replace_local_file_paths(inputs, mounts, mount_index)

        cpus, memory = self._resource_profile(agent_name)
        cmd = [self.docker, "run", "--rm", f"--cpus={cpus}", f"--memory={memory}"]

        for host_path, container_path in mounts:
            cmd.extend(["-v", f"{host_path}:{container_path}:ro"])

        cmd.extend(
            [
                "-e",
                f"AGENT_THREADS={cpus}",
                "-e",
                f"AGENT_INPUTS={json.dumps(container_inputs)}",
                "-e",
                f"ROUTING_CONTEXT={json.dumps(routing_ctx)}",
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
                f"ANTHROPIC_MODEL={os.environ.get('ANTHROPIC_MODEL', 'claude-3-5-sonnet-20241022')}",
                "-e",
                f"OPENROUTER_API_KEY={os.environ.get('OPENROUTER_API_KEY', '')}",
                "-e",
                f"OPENROUTER_MODEL={os.environ.get('OPENROUTER_MODEL', 'deepseek/deepseek-chat-v3-0324')}",
                f"ngs/{agent_name}-agent:latest",
            ]
        )
        return cmd, mounts

    async def run_agent(
        self,
        agent_name: str,
        inputs: Dict[str, Any],
        routing_ctx: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute an agent stage, using the cache when available."""
        cache_key = None
        if self.cache is not None:
            cache_key = self.cache.compute_hash(agent_name, inputs, routing_ctx)
            cached = await self.cache.get(cache_key)
            if cached:
                cached.setdefault("status", "ok")
                cached.setdefault("payload", {})
                cached.setdefault("reasoning", f"{agent_name} completed (cached)")
                cached.setdefault("halt", False)
                cached.setdefault("halt_reason", "")
                return cached

        cmd, _mounts = self._docker_command(agent_name, inputs, routing_ctx)
        result = await asyncio.to_thread(
            self._run_fn, cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            stderr = getattr(result, "stderr", "") or ""
            raise RuntimeError(f"Agent {agent_name} failed: {stderr.strip()}")

        stdout = (getattr(result, "stdout", "") or "").strip()
        if not stdout:
            raise RuntimeError(f"Agent {agent_name} returned empty output")

        output = json.loads(stdout)
        if isinstance(output, dict):
            output.setdefault("status", "ok")
            output.setdefault("payload", {})
            output.setdefault("reasoning", f"{agent_name} completed")
            output.setdefault("halt", False)
            output.setdefault("halt_reason", "")

        if self.cache is not None and cache_key is not None:
            await self.cache.set(cache_key, output)
        return output


def docker_available() -> bool:
    """Return True when a ``docker`` binary is on PATH."""
    return shutil.which(os.environ.get("NGS_DOCKER_BIN", "docker")) is not None
