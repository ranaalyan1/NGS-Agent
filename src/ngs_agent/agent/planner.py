from __future__ import annotations

from pathlib import Path
from typing import Any

from anthropic import Anthropic
from rich.console import Console

from ngs_agent.agent.models import ExperimentContext, Plan, PlanStep
from ngs_agent.config.settings import NGSSettings
from ngs_agent.tools.permissions import SafetyLevel


class PlannerAgent:
    def __init__(self, settings: NGSSettings, console: Console) -> None:
        self.settings = settings
        self.console = console

    def discover_context(self, working_directory: Path, resume_token: str | None = None) -> ExperimentContext:
        samplesheets = sorted(
            [
                *working_directory.glob("*samplesheet*.csv"),
                *working_directory.glob("*samplesheet*.tsv"),
                *working_directory.glob("samples*.csv"),
                *working_directory.glob("samples*.tsv"),
            ]
        )
        references = sorted(
            [
                *working_directory.glob("**/*.fa"),
                *working_directory.glob("**/*.fasta"),
                *working_directory.glob("**/*.gtf"),
                *working_directory.glob("**/*.gff"),
                *working_directory.glob("**/*.fai"),
            ]
        )
        prior_runs = sorted([*working_directory.glob("runs/*"), *working_directory.glob("output/*")])
        return ExperimentContext(
            working_directory=working_directory,
            samplesheets=samplesheets[:10],
            references=references[:10],
            prior_runs=prior_runs[:10],
            resume_token=resume_token,
            metadata={
                "samplesheet_count": len(samplesheets),
                "reference_count": len(references),
                "prior_run_count": len(prior_runs),
            },
        )

    def _heuristic_plan(self, objective: str, workflow: str, context: ExperimentContext, dry_run: bool) -> Plan:
        is_rnaseq = workflow == "rnaseq" or "rna" in objective.lower()
        workflow_name = "RNA-Seq" if is_rnaseq else "variant"
        steps = [
            PlanStep(
                name="discover-context",
                description="Inspect the working directory for samplesheets, references, and existing checkpoints.",
                command_preview=f"scan {context.working_directory}",
                safety_level=SafetyLevel.READ,
                estimated_duration_minutes=1,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="discover-context",
                expected_outputs=["context.json"],
            ),
            PlanStep(
                name="qc-decision",
                description="Run QC and decide whether trimming is required.",
                command_preview="fastqc -> AI decision -> trimming policy",
                safety_level=SafetyLevel.EXPENSIVE,
                estimated_duration_minutes=15,
                estimated_cost_label="medium",
                requires_confirmation=True,
                checkpoint_key="qc-decision",
                expected_outputs=["qc_metrics.json", "trim_decision.json"],
            ),
        ]

        if is_rnaseq:
            steps.extend(
                [
                    PlanStep(
                        name="align",
                        description="Align reads against the selected reference genome.",
                        command_preview="hisat2 | samtools sort",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=60,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key="align",
                        expected_outputs=["aligned.bam", "aligned.bam.bai"],
                    ),
                    PlanStep(
                        name="quantify",
                        description="Generate gene-level counts for downstream differential expression.",
                        command_preview="featureCounts",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=20,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key="quantify",
                        expected_outputs=["counts.tsv", "summary.txt"],
                    ),
                    PlanStep(
                        name="report",
                        description="Render publication-ready HTML report and AI summary.",
                        command_preview="report.html",
                        safety_level=SafetyLevel.WRITE,
                        estimated_duration_minutes=5,
                        estimated_cost_label="low",
                        requires_confirmation=False,
                        checkpoint_key="report",
                        expected_outputs=["report.html"],
                    ),
                ]
            )
        else:
            steps.extend(
                [
                    PlanStep(
                        name="variant-call",
                        description="Call variants and annotate findings.",
                        command_preview="bwa-mem2 | gatk | snpEff",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=120,
                        estimated_cost_label="high",
                        requires_confirmation=True,
                        checkpoint_key="variant-call",
                        expected_outputs=["variants.vcf", "annotated.vcf"],
                    ),
                    PlanStep(
                        name="report",
                        description="Generate final variant report and executive summary.",
                        command_preview="report.html",
                        safety_level=SafetyLevel.WRITE,
                        estimated_duration_minutes=8,
                        estimated_cost_label="low",
                        requires_confirmation=False,
                        checkpoint_key="report",
                        expected_outputs=["report.html"],
                    ),
                ]
            )

        estimated_duration = sum(step.estimated_duration_minutes for step in steps)
        risks = [
            "Missing reference indexes will force a manual stop before execution.",
            "Expensive steps require confirmation unless explicitly suppressed by policy.",
        ]
        if context.samplesheets:
            next_actions = [f"Use discovered samplesheet: {context.samplesheets[0].name}"]
        else:
            next_actions = ["Provide a samplesheet or run in dry-run mode to inspect context."]

        plan = Plan(
            title=f"{workflow_name} pipeline plan",
            objective=objective,
            workflow=workflow,
            summary=f"{workflow_name} analysis plan prepared from the current experiment context.",
            estimated_duration_minutes=estimated_duration,
            estimated_cost_label="medium" if estimated_duration < 120 else "high",
            context=context,
            steps=steps,
            risks=risks,
            next_actions=next_actions,
            dry_run=dry_run,
        )
        return plan

    def _anthropic_plan(self, objective: str, workflow: str, context: ExperimentContext, dry_run: bool) -> Plan | None:
        if not self.settings.anthropic_api_key:
            return None

        # Claude Opus 4.7 is the default for heavy reasoning; fall back to heuristic if unavailable.
        try:
            client = Anthropic(api_key=self.settings.anthropic_api_key)
            response = client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=800,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "You are an expert NGS workflow planner. Produce a concise JSON plan with steps, "
                            "duration estimates, risks, next_actions, and safety flags. "
                            "Use the current experiment context only. "
                            f"Objective: {objective}\n"
                            f"Workflow: {workflow}\n"
                            f"Context: {context.model_dump(mode='json')}\n"
                            f"Dry run: {dry_run}\n"
                            "Return only valid JSON."
                        ),
                    }
                ],
            )
            text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
            # Keep the first phase resilient: if the model returns anything but valid JSON, we fall back.
            import json

            parsed: dict[str, Any] = json.loads(text)
            steps = [PlanStep.model_validate(step) for step in parsed.get("steps", [])]
            return Plan(
                title=parsed.get("title", f"{workflow} pipeline plan"),
                objective=objective,
                workflow=workflow,
                summary=parsed.get("summary", "AI-generated execution plan."),
                estimated_duration_minutes=int(parsed.get("estimated_duration_minutes", 0)),
                estimated_cost_label=str(parsed.get("estimated_cost_label", "medium")),
                context=context,
                steps=steps,
                risks=list(parsed.get("risks", [])),
                next_actions=list(parsed.get("next_actions", [])),
                dry_run=dry_run,
            )
        except Exception:
            return None

    def create_plan(self, objective: str, workflow: str, context: ExperimentContext, dry_run: bool = False) -> Plan:
        plan = self._anthropic_plan(objective, workflow, context, dry_run)
        if plan is not None:
            return plan
        return self._heuristic_plan(objective, workflow, context, dry_run)
