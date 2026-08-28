"""Unit tests for HTML and Markdown report generation."""

from pathlib import Path
import pytest

from ngs_agent.analyzer import Variant
from ngs_agent.debate import DebateResult, PersonaOpinion
from ngs_agent.qc import QCMetric
from ngs_agent.reports import generate_html_report


class TestReportBuilder:
    def test_generate_html_report(self, tmp_path):
        variants = [
            Variant(
                chrom="17",
                pos=43094692,
                ref="G",
                alt="A",
                gene="BRCA1",
                consequence="missense_variant",
                clinvar="Pathogenic",
                af=0.00002,
                depth=245,
                vaf=0.50,
                is_pathogenic=True,
                is_vus=False,
            ),
            Variant(
                chrom="13",
                pos=32338077,
                ref="G",
                alt="A",
                gene="BRCA2",
                consequence="missense_variant",
                clinvar="Uncertain_significance",
                af=0.001,
                depth=156,
                vaf=0.50,
                is_pathogenic=False,
                is_vus=True,
            ),
        ]
        qc_metrics = [
            QCMetric(name="Mapping Rate", value="98.5%", status="pass"),
            QCMetric(name="Duplication Rate", value="35.0%", status="warn"),
        ]
        debates = [
            DebateResult(
                variant=variants[1],
                opinions=[
                    PersonaOpinion(persona="Pop Gen", stance="VUS", reasoning="Low AF but present in gnomAD.", acmg_criteria=["PM2"]),
                    PersonaOpinion(persona="Clin Gen", stance="Likely Pathogenic", reasoning="Phenotype matches BRCA2 syndrome.", acmg_criteria=["PP4"]),
                ],
                consensus="Debate resolved via ACMG criteria: Likely Pathogenic.",
                recommendation="Prioritize BRCA2 for clinical correlation.",
            )
        ]

        out_html = tmp_path / "test_report.html"
        html = generate_html_report(variants, qc_metrics=qc_metrics, debates=debates, output_path=out_html)

        assert out_html.exists()
        assert "BRCA1" in html
        assert "BRCA2" in html
        assert "Mapping Rate" in html
        assert "badge-pathogenic" in html
        assert "Multi-Agent VUS Debates" in html
