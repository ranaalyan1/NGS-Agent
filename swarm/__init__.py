"""Local (Temporal-free) execution engine for the NGS-Agent swarm pipeline.

This package runs the same pipeline stages as :mod:`workflows.pipeline_workflow`
but as plain ``asyncio`` coroutines that invoke agent Docker images directly.
No Temporal server, Postgres, Redis, or external worker process is required;
Docker (for the per-tool agent images) and MinIO (for artifact exchange) remain
the only infrastructure dependencies, and even the shared Redis/MinIO cache is
replaced with a local content-addressed filesystem cache by default.
"""

from swarm.engine import RunInput, SampleRunInput, run_pipeline
from swarm.runner import AgentRunner, LocalCache
from swarm.store import RunStore

__all__ = [
    "AgentRunner",
    "LocalCache",
    "RunInput",
    "RunStore",
    "SampleRunInput",
    "run_pipeline",
]
