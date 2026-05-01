from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console

from ngs_agent.agent.orchestrator import AgentOrchestrator
from ngs_agent.config.settings import NGSSettings
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.tools.registry import create_default_registry


class RNASeqPipelineAdapter:
    """High-level RNA-Seq pipeline facade built on the agentic orchestrator."""

    def __init__(self, settings: NGSSettings, console: Console | None = None) -> None:
        self.settings = settings
        self.console = console or Console()
        self.registry = create_default_registry()
        self.backend_selector = BackendSelector()
        self.orchestrator = AgentOrchestrator(
            settings=settings,
            backend_selector=self.backend_selector,
            tool_registry=self.registry,
            console=self.console,
        )

    def _objective(self, prompt: str | None, samplesheet: Path | None, reference: str | None) -> str:
        fragments = [prompt or "Run an RNA-Seq analysis"]
        if samplesheet is not None:
            fragments.append(f"using samplesheet {samplesheet}")
        if reference is not None:
            fragments.append(f"with reference {reference}")
        return " ".join(fragments)

    def run(
        self,
        objective: str | None = None,
        working_directory: Path | None = None,
        output_dir: Path | None = None,
        samplesheet: Path | None = None,
        reference: str | None = None,
        dry_run: bool = False,
        resume: bool = False,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        working_directory = working_directory or Path.cwd()
        objective_text = self._objective(objective, samplesheet, reference)
        result = self.orchestrator.run(
            objective=objective_text,
            workflow="rnaseq",
            working_directory=working_directory,
            dry_run=dry_run,
            resume=resume,
        )
        result["pipeline"] = "rnaseq"
        if output_dir is not None:
            result["output_dir"] = str(output_dir)
        if samplesheet is not None:
            result.setdefault("context", {})
            if isinstance(result["context"], dict):
                result["context"]["samplesheet"] = str(samplesheet)
        if reference is not None:
            result.setdefault("context", {})
            if isinstance(result["context"], dict):
                result["context"]["reference"] = reference
        if extra_metadata:
            result.setdefault("metadata", {})
            if isinstance(result["metadata"], dict):
                result["metadata"].update(extra_metadata)
        return result
