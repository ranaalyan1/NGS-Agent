"""Unit tests for HTML and Markdown report generation."""


from ngs_agent.analyzer import Variant
from ngs_agent.debate import ConsultationSummary, DebateResult, PersonaOpinion
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
        # The consultation layer is narrative only: no persona carries a stance
        # and the result carries no classification. See tests/test_debate.py.
        debates = [
            DebateResult(
                variant=variants[1],
                opinions=[
                    PersonaOpinion(
                        persona="Population Geneticist",
                        reasoning="A low allele frequency was reported, but coverage at this "
                        "position should be confirmed before rarity is relied on.",
                    ),
                    PersonaOpinion(
                        persona="Clinical Geneticist",
                        reasoning="The reported phenotype is consistent with a BRCA2 disorder; "
                        "segregation data was not supplied.",
                    ),
                ],
                summary=ConsultationSummary(
                    points_of_agreement=("2 of 3 personas contributed.",),
                    evidence_a_reviewer_should_check=(
                        "Confirm the variant call, the transcript, and the genome build.",
                    ),
                ),
                boundary_violations=("Population Geneticist: tier_language:VUS",),
            )
        ]

        out_html = tmp_path / "test_report.html"
        html = generate_html_report(variants, qc_metrics=qc_metrics, debates=debates, output_path=out_html)

        assert out_html.exists()
        assert "BRCA1" in html
        assert "BRCA2" in html
        assert "Mapping Rate" in html
        assert "badge-pathogenic" in html
        # The section was renamed when it stopped producing a classification:
        # it is narrative now, and the report says so at the top of the block.
        assert "Multi-Agent VUS Debates" not in html
        assert "Model Consultation (narrative only)" in html
        assert "Not a classification" in html
        # This fixture carries run-level violations but no per-opinion
        # redactions, so only the run-level block renders. The per-opinion
        # "Removed by guardrail" line is covered in tests/test_debate.py.
        assert "Boundary violations detected" in html
        assert "tier_language:VUS" in html
