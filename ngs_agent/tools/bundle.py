"""Tool bundle — assembles the standard tool registry for an agent."""
from __future__ import annotations

from collections.abc import Iterable

from .base import BaseTool
from .ngs.acmg_classify import AcmgClassifyTool
from .ngs.alphamissense_query import AlphaMissenseTool
from .ngs.clingen_gene import ClinGenGeneTool
from .ngs.clinvar_query import ClinvarQueryTool
from .ngs.clinvar_rcv import ClinvarRcvTool
from .ngs.critique import CritiqueVerdictTool
from .ngs.emit_verdict import EmitVerdictTool
from .ngs.evidence_graph_query import EvidenceGraphQueryTool
from .ngs.fhir_export import FhirExportTool
from .ngs.gnomad_query import GnomadQueryTool
from .ngs.hgvs_convert import HgvsConvertTool
from .ngs.litvar_search import LitVarTool
from .ngs.log_diagnose import LogDiagnoseTool
from .ngs.multiqc_parse import MultiQcParseTool
from .ngs.normalize import NormalizeTool
from .ngs.patient_report import PatientReportTool
from .ngs.pubmed_search import PubMedSearchTool
from .ngs.spliceai_predict import SpliceAITool
from .ngs.trio_analysis import TrioAnalysisTool
from .ngs.validation_assay import DesignValidationAssayTool
from .ngs.vcf_parse import VcfParseTool
from .registry import Registry

# All built-in tools by name
BUILTIN_TOOLS: dict[str, type[BaseTool]] = {
    # v0.3 tools
    "vcf_parse": VcfParseTool,
    "multiqc_parse": MultiQcParseTool,
    "log_diagnose": LogDiagnoseTool,
    "gnomad_query": GnomadQueryTool,
    "clinvar_query": ClinvarQueryTool,
    "pubmed_search": PubMedSearchTool,
    "acmg_classify": AcmgClassifyTool,
    "emit_verdict": EmitVerdictTool,
    # v0.4 tools — close the P0/P1 gaps
    "normalize_variant": NormalizeTool,
    "hgvs_convert": HgvsConvertTool,
    "clinvar_rcv": ClinvarRcvTool,
    "clingen_gene": ClinGenGeneTool,
    "spliceai_predict": SpliceAITool,
    "alphamissense_query": AlphaMissenseTool,
    "litvar_search": LitVarTool,
    "trio_analysis": TrioAnalysisTool,
    "fhir_export": FhirExportTool,
    "critique_verdict": CritiqueVerdictTool,
    # v0.5 tools — 2040 foresight in 2026
    "evidence_graph_query": EvidenceGraphQueryTool,
    "patient_report": PatientReportTool,
    "design_validation_assay": DesignValidationAssayTool,
}


def build_registry(tool_names: Iterable[str]) -> Registry:
    """Build a registry from a list of tool names. Unknown names are skipped."""
    reg = Registry()
    for name in tool_names:
        cls = BUILTIN_TOOLS.get(name)
        if cls is None:
            continue
        reg.register(cls())
    return reg


def build_full_registry() -> Registry:
    """Build a registry with all built-in tools. For testing."""
    reg = Registry()
    for cls in BUILTIN_TOOLS.values():
        reg.register(cls())
    return reg
