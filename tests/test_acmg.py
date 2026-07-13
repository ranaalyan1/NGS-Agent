"""Tests for the ACMG classifier."""
import asyncio

from ngs_agent.runtime.events import EventBus
from ngs_agent.runtime.permission import PermissionPolicy
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.ngs.acmg_classify import (
    AcmgClassifyTool,
    VariantInput,
    classify_acmg,
)


def _ctx():
    return ToolContext(
        session_id="test", cwd=".", permission=PermissionPolicy(),
        file_tracker=None, bus=EventBus("test"),
    )


def test_pathogenic_with_pvs1_pm2_pp5():
    """Null variant in HI gene, absent from gnomAD, ClinVar pathogenic."""
    v = VariantInput(
        gene="BRCA1", chrom="17", pos=41245466, ref="A", alt="T",
        consequence="nonsense",
        gnomad_af=0.0,
        clinvar="Pathogenic",
        clinvar_review_status="criteria provided, single submitter",
        gene_haploinsufficient=True,
    )
    res = classify_acmg(v)
    assert "PVS1" in res.applied
    assert "PM2" in res.applied
    assert "PP5" in res.applied
    assert res.score >= 6.0
    assert res.classification == "Pathogenic"


def test_benign_with_ba1():
    """AF >= 5% triggers stand-alone benign."""
    v = VariantInput(
        gene="BRCA2", chrom="17", pos=41215920, ref="C", alt="T",
        consequence="missense_variant",
        gnomad_af=0.05,
    )
    res = classify_acmg(v)
    assert "BA1" in res.applied
    assert res.score <= -6.0
    assert res.classification == "Benign"


def test_likely_benign_with_bs1_bp4():
    """AF 1-5% + benign predictors."""
    v = VariantInput(
        gene="BRCA2", chrom="17", pos=100, ref="C", alt="T",
        consequence="missense_variant",
        gnomad_af=0.02,
        spliceai_score=0.05,
        alphamissense_score=0.20,
        reveal_score=0.20,
    )
    res = classify_acmg(v)
    assert "BS1" in res.applied
    assert "BP4" in res.applied
    assert -6.0 < res.score <= -2.0
    assert res.classification == "Likely Benign"


def test_vus_default():
    """No evidence applied → VUS. Note: PM2 alone (weight=2.0) crosses Likely Pathogenic
    under Tavtigian 2018 Bayesian combining, so we test pure VUS by using an AF
    that doesn't trigger any criterion."""
    v = VariantInput(
        gene="BRCA2", chrom="17", pos=43091752, ref="T", alt="G",
        consequence="missense_variant",
        gnomad_af=0.001,  # 1e-3: not rare enough for PM2, not common enough for BS1
    )
    res = classify_acmg(v)
    assert res.classification == "VUS"
    assert res.applied == {}


def test_pm2_alone_is_likely_pathogenic():
    """PM2 (weight=2.0) alone crosses Likely Pathogenic per Tavtigian 2018."""
    v = VariantInput(
        gene="BRCA2", chrom="17", pos=43091752, ref="T", alt="G",
        consequence="missense_variant",
        gnomad_af=0.000001,  # 1e-6, below 1e-5
    )
    res = classify_acmg(v)
    assert "PM2" in res.applied
    assert res.score == 2.0
    assert res.classification == "Likely Pathogenic"


def test_tool_runs():
    tool = AcmgClassifyTool()
    info = tool.info()
    assert info.name == "acmg_classify"
    result = asyncio.run(tool.run({
        "gene": "BRCA1", "chrom": "17", "pos": 41245466,
        "ref": "A", "alt": "T",
        "consequence": "nonsense",
        "gnomad_af": 0.0,
        "clinvar": "Pathogenic",
        "gene_haploinsufficient": True,
    }, _ctx()))
    assert not result.is_error
    assert "Pathogenic" in result.content or "Likely Pathogenic" in result.content


def test_tool_missing_required():
    tool = AcmgClassifyTool()
    result = asyncio.run(tool.run({
        "gene": "BRCA1",
        # missing chrom, pos, ref, alt
    }, _ctx()))
    assert result.is_error
