"""Permission policy — ported from Zero's safety model.

4 modes (cycle with Shift+Tab in TUI):
  auto  — auto-approve safe read-only tools; ask for writes/exec
  plan  — never execute; the agent can only plan
  ask   — ask for every tool call
  yolo  — approve everything (CI / batch jobs)

Each tool declares its risk class via the registry. The policy gates execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PermissionMode = Literal["auto", "plan", "ask", "yolo"]

# tool_name -> ("allow" | "ask" | "deny", scope)
# scope is informational only (workspace | None)
DEFAULT_RULES: dict[str, tuple[str, str | None]] = {
    # Generic
    "file_read": ("allow", "workspace"),
    "file_write": ("ask", "workspace"),
    "file_edit": ("ask", "workspace"),
    "glob": ("allow", None),
    "grep": ("allow", None),
    "bash": ("ask", None),       # never auto-approve shell
    "web_fetch": ("allow", None),
    "task": ("allow", None),     # subagent dispatch
    # NGS read-only
    "vcf_parse": ("allow", None),
    "multiqc_parse": ("allow", None),
    "log_diagnose": ("allow", None),
    "gnomad_query": ("allow", None),
    "clinvar_query": ("allow", None),
    "clinvar_rcv": ("allow", None),
    "pubmed_search": ("allow", None),
    "litvar_search": ("allow", None),
    "alphamissense_query": ("allow", None),
    "spliceai_predict": ("allow", None),
    "hpo_match": ("allow", None),
    "gene_panel_lookup": ("allow", None),
    "clingen_gene": ("allow", None),
    "normalize_variant": ("allow", None),
    "hgvs_convert": ("allow", None),
    "bam_pileup": ("allow", None),
    "acmg_classify": ("allow", None),
    "trio_analysis": ("allow", None),
    "fhir_export": ("allow", None),
    "critique_verdict": ("allow", None),
    # v0.5 tools
    "evidence_graph_query": ("allow", None),
    "patient_report": ("allow", None),
    "design_validation_assay": ("allow", None),
    # NGS mutating
    "vcf_annotate": ("ask", None),
    "emit_verdict": ("allow", None),  # only writes to the transcript
    # MCP: unknown tools default to ask
    "mcp_*": ("ask", None),
}


@dataclass
class PermissionDecision:
    allow: bool
    deny_reason: str | None = None
    ask_prompt: str | None = None

    @classmethod
    def allow_(cls) -> PermissionDecision:
        return cls(allow=True)

    @classmethod
    def deny_(cls, reason: str) -> PermissionDecision:
        return cls(allow=False, deny_reason=reason)

    @classmethod
    def ask_(cls, prompt: str) -> PermissionDecision:
        return cls(allow=False, ask_prompt=prompt)


class PermissionPolicy:
    def __init__(self, mode: PermissionMode = "auto", rules: dict | None = None):
        self.mode = mode
        self.rules = rules or DEFAULT_RULES

    def decide(self, tool: str, args: dict[str, Any]) -> PermissionDecision:
        if self.mode == "yolo":
            return PermissionDecision.allow_()

        if self.mode == "plan":
            return PermissionDecision.deny_(
                "plan mode: no tool execution. The agent may only propose steps."
            )

        rule, _scope = self._lookup(tool)

        if rule == "allow":
            return PermissionDecision.allow_()

        if self.mode == "auto" and rule == "ask":
            # In auto mode, "ask" rules still ask — they're not auto-approved
            return PermissionDecision.ask_(
                f"Allow {tool}({self._fmt_args(args)})?"
            )

        if self.mode == "ask":
            return PermissionDecision.ask_(
                f"Allow {tool}({self._fmt_args(args)})?"
            )

        return PermissionDecision.deny_(f"Tool {tool} denied by policy")

    def _lookup(self, tool: str) -> tuple[str, str | None]:
        if tool in self.rules:
            return self.rules[tool]
        # wildcard MCP
        if tool.startswith("mcp_"):
            return self.rules.get("mcp_*", ("ask", None))
        return ("ask", None)  # unknown tools default to ask

    @staticmethod
    def _fmt_args(args: dict) -> str:
        s = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:4])
        return s if len(args) <= 4 else s + ", ..."
