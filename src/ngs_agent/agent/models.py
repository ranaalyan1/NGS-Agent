from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ngs_agent.tools.permissions import SafetyLevel


class AgentStage(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    VERIFIER = "verifier"
    REPORTER = "reporter"


class PlanStep(BaseModel):
    name: str
    description: str
    command_preview: str
    safety_level: SafetyLevel = SafetyLevel.READ
    estimated_duration_minutes: int = 0
    estimated_cost_label: str = "low"
    requires_confirmation: bool = False
    checkpoint_key: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    tool_name: str | None = None
    tool_payload: dict[str, Any] = Field(default_factory=dict)


class ExperimentContext(BaseModel):
    working_directory: Path
    samplesheets: list[Path] = Field(default_factory=list)
    references: list[Path] = Field(default_factory=list)
    prior_runs: list[Path] = Field(default_factory=list)
    config_path: Path | None = None
    resume_token: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    title: str
    objective: str
    workflow: str
    summary: str
    estimated_duration_minutes: int
    estimated_cost_label: str
    context: ExperimentContext
    steps: list[PlanStep] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    dry_run: bool = False


class StepOutcome(BaseModel):
    step_name: str
    status: str
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationIssue(BaseModel):
    severity: str
    message: str
    remediation: str = ""


class VerificationReport(BaseModel):
    passed: bool
    issues: list[VerificationIssue] = Field(default_factory=list)
    missing_artifacts: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ReportBundle(BaseModel):
    markdown_summary: str
    html_report: str
    ai_insights: str
    artifact_paths: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ResumeState:
    checkpoint_dir: Path
    checkpoint_file: Path
