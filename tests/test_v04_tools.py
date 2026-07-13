"""Tests for v0.4 P0/P1 tools: PVS1 engine, normalize, HGVS, ClinGen, FHIR, trio, critique, provenance, LitVar."""
import asyncio
import json

from ngs_agent.runtime.events import EventBus
from ngs_agent.runtime.permission import PermissionPolicy
from ngs_agent.runtime.provenance import (
    ProvenanceBundle,
    ToolCallRecord,
    compute_system_prompt_hash,
)
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.ngs.acmg_classify import VariantInput, classify_acmg
from ngs_agent.tools.ngs.alphamissense_query import (
    AlphaMissenseTool,
    classify,
    lookup_alphamissense,
)
from ngs_agent.tools.ngs.clingen_gene import ClinGenGeneTool, get_gene_info
from ngs_agent.tools.ngs.critique import CritiqueVerdictTool
from ngs_agent.tools.ngs.fhir_export import FhirExportTool, verdict_to_fhir
from ngs_agent.tools.ngs.hgvs_convert import HgvsConvertTool, genomic_to_hgvs
from ngs_agent.tools.ngs.litvar_search import LitVarTool
from ngs_agent.tools.ngs.normalize import NormalizeTool, normalize_variant, to_gnomad_variant_id
from ngs_agent.tools.ngs.pvs1_engine import (
    PVS1Input,
    PVS1Strength,
    TranscriptInfo,
    classify_pvs1,
    weight_for,
)
from ngs_agent.tools.ngs.spliceai_predict import SpliceAITool, heuristic_spliceai
from ngs_agent.tools.ngs.trio_analysis import (
    TrioAnalysisTool,
    _classify_inheritance,
)


def _ctx():
    return ToolContext(
        session_id="test", cwd=".", permission=PermissionPolicy(),
        file_tracker=None, bus=EventBus("test"),
    )


# ---------- PVS1 engine ----------
def test_pvs1_nonsense_with_nmd_and_lof_mechanism():
    t = TranscriptInfo(gene="BRCA1", has_known_lof_mechanism=True, nmd_predicted=True)
    inp = PVS1Input(variant_type="nonsense", transcript=t)
    strength = classify_pvs1(inp)
    assert strength == PVS1Strength.PVS1
    assert weight_for(strength) == 8.0


def test_pvs1_nonsense_nmd_escape_last_exon_downgrades_to_strong():
    t = TranscriptInfo(
        gene="BRCA1", has_known_lof_mechanism=True,
        nmd_predicted=False, last_exon_number=999,
    )
    inp = PVS1Input(variant_type="nonsense", transcript=t)
    strength = classify_pvs1(inp)
    assert strength == PVS1Strength.PVS1_STRONG
    assert weight_for(strength) == 4.0


def test_pvs1_no_lof_mechanism_returns_none():
    t = TranscriptInfo(gene="X", has_known_lof_mechanism=False)
    inp = PVS1Input(variant_type="nonsense", transcript=t)
    strength = classify_pvs1(inp)
    assert strength == PVS1Strength.NONE


def test_pvs1_canonical_splice_donor():
    t = TranscriptInfo(
        gene="BRCA2", has_known_lof_mechanism=True,
        nmd_predicted=True, is_canonical_donor=True,
    )
    inp = PVS1Input(variant_type="splice_donor", transcript=t, splice_predicted_effect="exon_skipping")
    assert classify_pvs1(inp) == PVS1Strength.PVS1


def test_pvs1_single_exon_del_small():
    t = TranscriptInfo(gene="BRCA1", has_known_lof_mechanism=True, total_coding_nt=5592)
    inp = PVS1Input(variant_type="single_exon_del", transcript=t, deleted_nt=100)
    # 100/5592 = 1.8% < 10% → Supporting
    assert classify_pvs1(inp) == PVS1Strength.PVS1_SUPPORTING


def test_pvs1_initiation_codon_with_alternate_start():
    t = TranscriptInfo(gene="X", has_known_lof_mechanism=True, has_alternate_downstream_start=True)
    inp = PVS1Input(variant_type="initiation_codon", transcript=t)
    assert classify_pvs1(inp) == PVS1Strength.PVS1_SUPPORTING


# ---------- Normalize ----------
def test_normalize_snv():
    v = normalize_variant("chr17", 43091752, "T", "G")
    assert v.chrom == "17"
    assert v.pos == 43091752
    assert v.ref == "T"
    assert v.alt == "G"
    assert v.variant_type == "SNV"
    assert to_gnomad_variant_id(v) == "17-43091752-T-G"


def test_normalize_parses_common_suffix():
    # raw: CT > GT — common suffix T
    # After suffix trim: C > G
    v = normalize_variant("1", 100, "CT", "GT")
    assert v.ref == "C"
    assert v.alt == "G"
    assert v.pos == 100


def test_normalize_indel_insertion():
    v = normalize_variant("1", 100, "A", "AC")
    assert v.variant_type == "insertion"


def test_normalize_indel_deletion():
    v = normalize_variant("1", 100, "AC", "A")
    assert v.variant_type == "deletion"


def test_normalize_tool():
    tool = NormalizeTool()
    r = asyncio.run(tool.run({"chrom": "chr17", "pos": 43091752, "ref": "T", "alt": "G"}, _ctx()))
    assert not r.is_error
    assert r.metadata["gnomad_id"] == "17-43091752-T-G"


# ---------- HGVS convert ----------
def test_hgvs_convert_brca1():
    r = genomic_to_hgvs("BRCA1", 43091652, "A", "G")
    assert r["status"] == "ok"
    assert r["transcript"] == "NM_007294.4"
    assert "c.1" in r["hgvs_c"]


def test_hgvs_convert_unknown_gene():
    r = genomic_to_hgvs("UNKNOWN_GENE", 100, "A", "G")
    assert r["status"] == "transcript_not_in_map"


def test_hgvs_tool():
    tool = HgvsConvertTool()
    r = asyncio.run(tool.run({"gene": "BRCA1", "pos": 43091652, "ref": "A", "alt": "G"}, _ctx()))
    assert not r.is_error
    assert "NM_007294.4" in r.content


# ---------- ClinGen gene ----------
def test_clingen_brca1_is_haploinsufficient():
    info = get_gene_info("BRCA1")
    assert info is not None
    assert info.is_haploinsufficient()
    assert info.is_lof_intolerant()


def test_clingen_pten_is_missense_constrained():
    info = get_gene_info("PTEN")
    assert info.is_missense_constrained()  # Z=4.31 > 3.09


def test_clingen_unknown_gene():
    assert get_gene_info("UNKNOWN") is None


def test_clingen_tool():
    tool = ClinGenGeneTool()
    r = asyncio.run(tool.run({"gene": "BRCA1"}, _ctx()))
    assert not r.is_error
    assert "BRCA1" in r.content
    assert r.metadata["is_haploinsufficient"] is True


# ---------- SpliceAI ----------
def test_spliceai_heuristic_canonical_donor():
    s = heuristic_spliceai("splice_donor_variant", None)
    assert s == 0.95


def test_spliceai_heuristic_region_close():
    s = heuristic_spliceai("splice_region_variant", 2)
    assert s == 0.7


def test_spliceai_tool_canonical():
    tool = SpliceAITool()
    r = asyncio.run(tool.run({
        "chrom": "17", "pos": 43091752, "ref": "T", "alt": "G",
        "consequence": "splice_donor_variant",
    }, _ctx()))
    assert not r.is_error
    assert r.metadata["spliceai_score"] >= 0.5
    assert r.metadata["impact"] == "HIGH"


def test_spliceai_tool_exonic_no_impact():
    tool = SpliceAITool()
    r = asyncio.run(tool.run({
        "chrom": "17", "pos": 43091752, "ref": "T", "alt": "G",
        "consequence": "missense_variant",
        "distance_to_splice": 200,
    }, _ctx()))
    assert r.metadata["spliceai_score"] == 0.0
    assert r.metadata["impact"] == "LOW"


# ---------- AlphaMissense ----------
def test_alphamissense_classify_thresholds():
    assert classify(0.7) == "LIKELY_PATHOGENIC"
    assert classify(0.5) == "AMBIGUOUS"
    assert classify(0.2) == "LIKELY_BENIGN"
    assert classify(None) == "UNKNOWN"


def test_alphamissense_lookup_no_file():
    # No TSV file → returns None
    score, source = lookup_alphamissense("1", 100, "A", "G")
    assert score is None
    assert source == "not_available"


def test_alphamissense_tool():
    tool = AlphaMissenseTool()
    r = asyncio.run(tool.run({
        "chrom": "1", "pos": 100, "ref": "A", "alt": "G",
    }, _ctx()))
    assert not r.is_error
    assert r.metadata["source"] == "not_available"


# ---------- ACMG v0.4 — PVS1 via decision tree + ClinGen auto-lookup ----------
def test_acmg_pvs1_via_decision_tree_with_clingen_lookup():
    """A nonsense variant in BRCA1 should now trigger PVS1 via the ClinGen
    auto-lookup + decision tree — without manually passing gene_haploinsufficient."""
    v = VariantInput(
        gene="BRCA1", chrom="17", pos=41245466, ref="A", alt="T",
        consequence="nonsense",
        gnomad_af=0.0,
    )
    res = classify_acmg(v)
    # PVS1 (8.0) + PM2 (2.0) = 10.0 → Pathogenic
    assert "PVS1" in res.applied
    assert "PM2" in res.applied
    assert res.score >= 6.0
    assert res.classification == "Pathogenic"


def test_acmg_pp2_for_missense_in_constrained_gene():
    """PTEN has missense Z = 4.31 > 3.09 → constrained → PP2 applies for missense."""
    v = VariantInput(
        gene="PTEN", chrom="10", pos=89692577, ref="A", alt="G",
        consequence="missense_variant",
    )
    res = classify_acmg(v)
    assert "PP2" in res.applied


def test_acmg_pp5_deprecation_warning():
    """PP5 should be flagged as deprecated when ClinVar pathogenic is provided."""
    v = VariantInput(
        gene="BRCA1", chrom="17", pos=41245466, ref="A", alt="T",
        consequence="missense_variant",
        clinvar="Pathogenic",
        clinvar_review_status="criteria provided, single submitter",
    )
    res = classify_acmg(v)
    assert "PP5" in res.applied
    assert "PP5" in res.deprecated_used


def test_acmg_ps2_de_novo_confirmed():
    v = VariantInput(
        gene="BRCA1", chrom="17", pos=41245466, ref="A", alt="T",
        consequence="missense_variant",
        de_novo_confirmed=True,
        gnomad_af=0.0,
    )
    res = classify_acmg(v)
    assert "PS2" in res.applied


# ---------- FHIR export ----------
def test_fhir_export_basic():
    verdict = {
        "verdict_id": "vdt_test123",
        "classification": "Pathogenic",
        "gene": "BRCA1",
        "variant": {"chrom": "17", "pos": 41245466, "ref": "A", "alt": "T"},
        "acmg_criteria": ["PVS1", "PM2"],
        "evidence_citations": ["PMID:12345", "RCV000012345"],
    }
    fhir = verdict_to_fhir(verdict)
    assert fhir["resourceType"] == "Observation"
    assert fhir["status"] == "final"
    assert fhir["code"]["coding"][0]["code"] == "57979-7"
    assert fhir["valueCodeableConcept"]["text"] == "Pathogenic"
    # Check components: gene, HGVS, ACMG criteria
    component_codes = [c["code"]["coding"][0]["code"] for c in fhir["component"]]
    assert "48018-6" in component_codes  # gene
    assert "81290-9" in component_codes  # HGVS
    assert "93044-6" in component_codes  # ACMG
    # DerivedFrom citations
    assert len(fhir["derivedFrom"]) == 2


def test_fhir_tool():
    tool = FhirExportTool()
    r = asyncio.run(tool.run({"verdict": {
        "verdict_id": "vdt_t",
        "classification": "VUS",
        "gene": "BRCA2",
        "variant": {"chrom": "17", "pos": 43091752, "ref": "T", "alt": "G"},
        "acmg_criteria": ["PM2"],
        "evidence_citations": [],
    }}, _ctx()))
    assert not r.is_error
    assert "FHIR R4" in r.content


# ---------- Trio analysis ----------
def test_trio_inheritance_classification():
    assert _classify_inheritance("0/1", "0/0", "0/0", "17") == "de_novo"
    assert _classify_inheritance("1/1", "0/1", "0/1", "17") == "autosomal_recessiveive"
    assert _classify_inheritance("1/1", "0/1", "0/0", "X") == "x_linked"
    assert _classify_inheritance("0/1", "0/1", "0/1", "17") == "unknown"


def test_trio_tool_with_files(tmp_path):
    # Build minimal trio VCFs
    proband = "17\t43091752\t.\tT\tG\t.\tPASS\tGENE=BRCA2\tGT:DP\t0/1:48\n"
    mother = "17\t43091752\t.\tT\tG\t.\tPASS\tGENE=BRCA2\tGT:DP\t0/0:48\n"
    father = "17\t43091752\t.\tT\tG\t.\tPASS\tGENE=BRCA2\tGT:DP\t0/0:48\n"

    p_path = tmp_path / "proband.vcf"
    m_path = tmp_path / "mother.vcf"
    f_path = tmp_path / "father.vcf"
    p_path.write_text(proband)
    m_path.write_text(mother)
    f_path.write_text(father)

    tool = TrioAnalysisTool()
    r = asyncio.run(tool.run({
        "proband_vcf": str(p_path),
        "mother_vcf": str(m_path),
        "father_vcf": str(f_path),
    }, _ctx()))
    assert not r.is_error
    assert r.metadata["de_novo_count"] == 1


# ---------- Critique ----------
def test_critique_confirm():
    tool = CritiqueVerdictTool()
    r = asyncio.run(tool.run({
        "verdict_id": "vdt_123",
        "original_classification": "VUS",
        "decision": "CONFIRM",
        "reasoning": "Insufficient evidence to upgrade or downgrade.",
        "missed_evidence": [],
        "overinterpreted_evidence": [],
        "suggested_followups": ["RNA studies", "Segregation testing"],
    }, _ctx()))
    assert not r.is_error
    assert "CONFIRM" in r.content
    assert r.metadata["critique"]["decision"] == "CONFIRM"


def test_critique_reclassify_up():
    tool = CritiqueVerdictTool()
    r = asyncio.run(tool.run({
        "verdict_id": "vdt_456",
        "original_classification": "VUS",
        "decision": "RECLASSIFY_UP",
        "recommended_classification": "Likely Pathogenic",
        "reasoning": "Missed functional study showing damaging effect.",
        "missed_evidence": ["PMID:29876543 — functional assay shows loss of function"],
    }, _ctx()))
    assert not r.is_error
    assert "RECLASSIFY_UP" in r.content
    assert r.metadata["critique"]["recommended_classification"] == "Likely Pathogenic"


# ---------- Provenance ----------
def test_provenance_bundle_hash_chain():
    bundle = ProvenanceBundle(
        session_id="sess_test",
        verdict_id="vdt_test",
        agent_name="interpreter",
        agent_version="0.4.0",
        model="claude-sonnet-4",
        system_prompt_hash=compute_system_prompt_hash("test prompt"),
        verdict={"classification": "VUS", "gene": "BRCA2"},
    )
    # Add a tool call
    bundle.add_tool_call(ToolCallRecord.from_call(
        "tc1", "gnomad_query", {"chrom": "17"},
        "AF=0.001", is_error=False,
    ))
    h1 = bundle.chain_hash()

    # Tamper with verdict → chain_hash changes
    bundle.verdict["classification"] = "Pathogenic"
    h2 = bundle.chain_hash()
    assert h1 != h2

    # Tamper with tool call response → also changes
    bundle.verdict["classification"] = "VUS"  # restore
    h3 = bundle.chain_hash()
    assert h3 == h1  # back to original

    bundle.tool_calls[0].response_hash = "tampered"
    h4 = bundle.chain_hash()
    assert h4 != h1


def test_provenance_to_json():
    bundle = ProvenanceBundle(
        session_id="s", verdict_id="v", agent_name="a", agent_version="0.4",
        model="m", system_prompt_hash="h",
        verdict={"classification": "VUS"},
    )
    j = bundle.to_json()
    d = json.loads(j)
    assert d["@type"] == "VariantInterpretationProvenance"
    assert d["agent_version"] == "0.4"
    assert "chain_hash" in d


# ---------- LitVar (smoke — no network) ----------
def test_litvar_tool_info():
    tool = LitVarTool()
    info = tool.info()
    assert info.name == "litvar_search"
    assert "rsid" in info.parameters


# ---------- Compactor preserves tool results ----------
def test_compactor_preserves_tool_results():
    """v0.4 fix: tool_result messages must NOT be summarized — they contain
    evidence citations that downstream verdicts need to reference."""
    from ngs_agent.backends.base import StubBackend
    from ngs_agent.runtime.compactor import Compactor
    from ngs_agent.runtime.messages import Message, ToolResult

    backend = StubBackend()
    compactor = Compactor(backend, "claude-sonnet-4")

    # Build a long enough conversation with tool results in the middle
    msgs = [Message.user("Initial prompt")]
    for i in range(10):
        msgs.append(Message.assistant(f"Assistant turn {i}"))
        msgs.append(Message.with_tool_results([
            ToolResult(
                tool_call_id=f"call_{i}",
                name="gnomad_query",
                content=f"AF=0.000{i} — IMPORTANT EVIDENCE",
            )
        ]))
        msgs.append(Message.assistant(f"After tool {i}"))
    msgs.append(Message.user("Final question"))

    # Manually invoke _do_compact
    out = asyncio.run(compactor._do_compact(msgs, []))

    # Verify tool_result messages were preserved (not summarized away)
    tool_msgs_preserved = [m for m in out if m.role == "tool"]
    assert len(tool_msgs_preserved) > 0, "Tool result messages were lost in compaction!"

    # Verify the evidence content is intact
    all_content = " ".join(tr.content for m in tool_msgs_preserved for tr in m.tool_results)
    assert "IMPORTANT EVIDENCE" in all_content
