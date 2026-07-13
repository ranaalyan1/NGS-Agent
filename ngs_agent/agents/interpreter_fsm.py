"""Interpreter typed state machine — replaces the brittle 13-step system prompt.

In v0.4 the interpreter protocol lives in the system prompt:
"Call vcf_parse → clingen_gene → normalize → hgvs_convert → acmg_classify → ...".

This is brittle. A model swap, a slight prompt change, or an eager LLM
that decides to skip a step, and the protocol drifts.

v0.5 fix: the protocol is a typed state machine. The agent loop advances
through the states; at each state, the LLM is asked to call ONE specific
tool (or a small set), not "follow these 13 steps." The LLM only fills in
free-text reasoning, not drives the flow.

This is the same architectural direction as LangGraph (2024) and the
2028 ClinGen agent grammar standard.

States:
  START
    → PARSE_VCF (call vcf_parse)
    → LOOKUP_GENE (call clingen_gene, per variant's gene)
    → NORMALIZE (call normalize_variant)
    → HGVS_CONVERT (call hgvs_convert)
    → INITIAL_CLASSIFY (call acmg_classify with all known fields)
    → GATHER_FREQUENCY (call gnomad_query)
    → GATHER_CLINVAR (call clinvar_rcv)
    → GATHER_LITERATURE (call litvar_search, fallback pubmed_search)
    → GATHER_PREDICTORS (call spliceai_predict + alphamissense_query)
    → OPTIONAL_TRIO (call trio_analysis if family VCFs provided)
    → RECLASSIFY (re-call acmg_classify with new evidence)
    → EMIT_VERDICT (call emit_verdict)
    → CRITIQUE (call critique_verdict; if RECLASSIFY, loop back)
    → FINALIZE (call fhir_export + patient_report)
  END

Each state has:
  - required_tools: tools that must be called before advancing
  - llm_guidance: prompt fragment the LLM sees at this state
  - transition: function(state, tool_results) -> next_state
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class State(str, Enum):
    START = "start"
    PARSE_VCF = "parse_vcf"
    LOOKUP_GENE = "lookup_gene"
    NORMALIZE = "normalize"
    HGVS_CONVERT = "hgvs_convert"
    INITIAL_CLASSIFY = "initial_classify"
    GATHER_FREQUENCY = "gather_frequency"
    GATHER_CLINVAR = "gather_clinvar"
    GATHER_LITERATURE = "gather_literature"
    GATHER_PREDICTORS = "gather_predictors"
    OPTIONAL_TRIO = "optional_trio"
    RECLASSIFY = "reclassify"
    EMIT_VERDICT = "emit_verdict"
    CRITIQUE = "critique"
    FINALIZE = "finalize"
    END = "end"


@dataclass
class StateSpec:
    """Definition of one state in the interpreter FSM."""

    name: State
    required_tools: list[str]        # tools that must be called at this state
    llm_guidance: str                # what to tell the LLM at this state
    next: State                      # next state after required_tools complete
    optional: bool = False           # if True, can be skipped (e.g. trio if no family data)
    skip_if: Callable[[dict], bool] | None = None  # condition to skip


# The canonical interpreter protocol
PROTOCOL: list[StateSpec] = [
    StateSpec(
        name=State.PARSE_VCF,
        required_tools=["vcf_parse"],
        llm_guidance=(
            "Parse the input VCF and identify all VUS or Pathogenic variants. "
            "Use filter='vus' to focus on variants needing interpretation."
        ),
        next=State.LOOKUP_GENE,
    ),
    StateSpec(
        name=State.LOOKUP_GENE,
        required_tools=["clingen_gene"],
        llm_guidance=(
            "For each variant's gene, look up ClinGen gene-level info to determine "
            "if PVS1 applies (LOF mechanism) and if PP2 applies (missense constraint)."
        ),
        next=State.NORMALIZE,
    ),
    StateSpec(
        name=State.NORMALIZE,
        required_tools=["normalize_variant"],
        llm_guidance=(
            "Normalize each variant to left-aligned parsimonious form. "
            "Get the gnomAD variant ID for downstream queries."
        ),
        next=State.HGVS_CONVERT,
    ),
    StateSpec(
        name=State.HGVS_CONVERT,
        required_tools=["hgvs_convert"],
        llm_guidance=(
            "Convert each variant to HGVS c./p. notation using the canonical transcript."
        ),
        next=State.INITIAL_CLASSIFY,
    ),
    StateSpec(
        name=State.INITIAL_CLASSIFY,
        required_tools=["acmg_classify"],
        llm_guidance=(
            "Run the deterministic ACMG rules engine on each variant. Note which "
            "criteria are flagged as 'not yet evaluated' — those are the next steps."
        ),
        next=State.GATHER_FREQUENCY,
    ),
    StateSpec(
        name=State.GATHER_FREQUENCY,
        required_tools=["gnomad_query"],
        llm_guidance=(
            "Query gnomAD for population allele frequency. Use the normalized coordinates."
        ),
        next=State.GATHER_CLINVAR,
    ),
    StateSpec(
        name=State.GATHER_CLINVAR,
        required_tools=["clinvar_rcv"],
        llm_guidance=(
            "Query ClinVar for RCV-level assertions. Pass the rsID if known, else gene+HGVS."
        ),
        next=State.GATHER_LITERATURE,
    ),
    StateSpec(
        name=State.GATHER_LITERATURE,
        required_tools=["litvar_search"],
        llm_guidance=(
            "Search LitVar for variant-specific publications. If no rsID available, "
            "fall back to pubmed_search with `gene AND hgvs_c` and a date filter."
        ),
        next=State.GATHER_PREDICTORS,
    ),
    StateSpec(
        name=State.GATHER_PREDICTORS,
        required_tools=["spliceai_predict", "alphamissense_query"],
        llm_guidance=(
            "For splice-region variants: call spliceai_predict. For missense: call "
            "alphamissense_query. If AlphaMissense is unavailable, note this and use REVEL if present."
        ),
        next=State.OPTIONAL_TRIO,
    ),
    StateSpec(
        name=State.OPTIONAL_TRIO,
        required_tools=["trio_analysis"],
        llm_guidance=(
            "If family VCFs were provided, run trio analysis to detect de novo (PS2) "
            "or compound heterozygote (PM3) evidence."
        ),
        next=State.RECLASSIFY,
        optional=True,
        skip_if=lambda ctx: not ctx.get("family_vcfs"),
    ),
    StateSpec(
        name=State.RECLASSIFY,
        required_tools=["acmg_classify"],
        llm_guidance=(
            "Re-run acmg_classify with all newly gathered evidence (gnomAD AF, ClinVar, "
            "predictors, trio). This is the DRAFT classification."
        ),
        next=State.EMIT_VERDICT,
    ),
    StateSpec(
        name=State.EMIT_VERDICT,
        required_tools=["emit_verdict"],
        llm_guidance=(
            "Emit a structured verdict with the draft classification, ACMG criteria, "
            "evidence summary, citations, recommendation, confidence, and limitations."
        ),
        next=State.CRITIQUE,
    ),
    StateSpec(
        name=State.CRITIQUE,
        required_tools=["critique_verdict"],
        llm_guidance=(
            "Critique the draft verdict. The critique agent looks for missed evidence "
            "(under-classified) and over-interpreted evidence (over-classified). "
            "If critique says RECLASSIFY, loop back to RECLASSIFY. Otherwise proceed."
        ),
        next=State.FINALIZE,
    ),
    StateSpec(
        name=State.FINALIZE,
        required_tools=["fhir_export", "patient_report"],
        llm_guidance=(
            "Generate the FHIR R4 Observation for LIMS integration and the patient-facing "
            "report (8th-grade reading level). Also call design_validation_assay if verdict is VUS."
        ),
        next=State.END,
    ),
]


@dataclass
class FSMState:
    """Runtime state of the FSM."""

    current: State = State.START
    history: list[tuple[State, str]] = field(default_factory=list)  # (state, tool_call_id)
    context: dict[str, Any] = field(default_factory=dict)  # carries variant data between states
    critique_loops: int = 0
    max_critique_loops: int = 1


def next_state(state: FSMState, completed_tool: str) -> State:
    """Advance the FSM after a tool completes."""
    spec = next((s for s in PROTOCOL if s.name == state.current), None)
    if spec is None:
        return State.END

    # Check if all required tools for this state have been called
    state.history.append((state.current, completed_tool))
    called_at_this_state = [
        t for s, t in state.history if s == state.current
    ]

    all_required_called = all(t in called_at_this_state for t in spec.required_tools)
    if not all_required_called:
        return state.current  # stay until all required tools are called

    # Move to next state
    state.current = spec.next

    # Skip optional states if their skip_if condition is met
    while state.current != State.END:
        next_spec = next((s for s in PROTOCOL if s.name == state.current), None)
        if next_spec is None:
            break
        if next_spec.optional and next_spec.skip_if and next_spec.skip_if(state.context):
            state.current = next_spec.next
            continue
        break

    return state.current


def guidance_for(state: State) -> str:
    """Get the LLM guidance prompt for a state."""
    spec = next((s for s in PROTOCOL if s.name == state), None)
    if spec is None:
        return ""
    return spec.llm_guidance


def required_tools_for(state: State) -> list[str]:
    spec = next((s for s in PROTOCOL if s.name == state), None)
    return spec.required_tools if spec else []


def build_system_prompt_with_fsm() -> str:
    """Build a system prompt that tells the LLM about the FSM.

    The LLM is told: 'You are at state X. Call tool Y next.' instead of
    'Follow these 13 steps.' This is more robust to model swaps.
    """
    return (
        "You are a clinical variant interpreter operating under ACMG/AMP 2015 "
        "guidelines with ClinGen SVI 2018/2023 revisions.\n\n"
        "Your workflow is governed by a typed state machine. At each turn, you will "
        "be told which state you are in and which tool(s) to call. Call ONLY those "
        "tools. Do not skip ahead. Do not call tools that are not in the current "
        "state's required list.\n\n"
        "States and required tools:\n"
        + "\n".join(
            f"  - {s.name.value}: call {s.required_tools}"
            for s in PROTOCOL
        )
        + "\n\nYou MUST:\n"
        "  - Cite every claim with a PMID, ClinVar RCV, or gnomAD variant ID.\n"
        "  - Never classify without calling acmg_classify at least once.\n"
        "  - Always call critique_verdict before finalizing.\n"
        "  - Disclose uncertainty: if confidence is low, say so.\n"
        "  - Use PP5/BP6 only as informational — they are deprecated per 2023 SVI.\n"
    )
