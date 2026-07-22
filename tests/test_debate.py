"""Unit tests for debate module - stance extraction and consensus logic."""

import pytest

from ngs_agent.debate import (
    _extract_stance,
    _build_consensus,
    _build_recommendation,
    PersonaOpinion,
)


class TestExtractStance:
    """Test stance extraction from LLM responses.
    
    BUG: Current implementation uses naive substring matching which misclassifies
    negations like "not likely pathogenic" as "Likely pathogenic".
    """

    def test_explicit_pathogenic(self):
        """Clear pathogenic call should be extracted."""
        text = "This variant is Pathogenic based on ACMG criteria."
        assert _extract_stance(text) == "Pathogenic"
    
    def test_explicit_likely_pathogenic(self):
        """Clear likely pathogenic call should be extracted."""
        text = "I classify this as Likely Pathogenic."
        assert _extract_stance(text) == "Likely Pathogenic"
    
    def test_explicit_benign(self):
        """Clear benign call should be extracted."""
        text = "This variant is Benign in my assessment."
        assert _extract_stance(text) == "Benign"
    
    def test_explicit_vus(self):
        """VUS classification should be extracted."""
        text = "Remains a Variant of Uncertain Significance (VUS)."
        assert _extract_stance(text) in ("Vus", "Uncertain")
    
    def test_negation_not_pathogenic_should_not_match_pathogenic(self):
        """BUG TEST: 'not likely pathogenic' should NOT be classified as 'Likely Pathogenic'.
        
        Current buggy behavior: substring match finds "likely pathogenic" in 
        "not likely pathogenic" and returns "Likely Pathogenic".
        
        Expected: Should recognize the negation and classify appropriately.
        """
        text = "This variant is NOT likely pathogenic; evidence points to benign."
        result = _extract_stance(text)
        # This test documents the bug - currently fails
        # After fix, should NOT return "Likely Pathogenic"
        assert result != "Likely Pathogenic", \
            f"BUG: Negated 'not likely pathogenic' incorrectly classified as '{result}'"
    
    def test_negation_not_benign(self):
        """BUG TEST: 'not benign' should NOT be classified as 'Benign'."""
        text = "The variant is not benign; it shows pathogenic features."
        result = _extract_stance(text)
        # Currently buggy - will match "benign" substring
        assert result != "Benign", \
            f"BUG: Negated 'not benign' incorrectly classified as '{result}'"
    
    def test_no_stance_returns_uncertain(self):
        """When no stance keywords found, should return 'Uncertain'."""
        text = "The data is inconclusive and requires more study."
        assert _extract_stance(text) == "Uncertain"


class TestBuildConsensus:
    """Test consensus building from multiple opinions."""

    def test_all_pathogenic_consensus(self):
        """All personas agree on pathogenic → strong consensus."""
        opinions = [
            PersonaOpinion("Pop Gen", "Pathogenic", "reasoning1"),
            PersonaOpinion("Clin Gen", "Pathogenic", "reasoning2"),
            PersonaOpinion("Func Gen", "Pathogenic", "reasoning3"),
        ]
        consensus = _build_consensus(opinions)
        assert "pathogenic" in consensus.lower()
        assert "All" in consensus
    
    def test_all_benign_consensus(self):
        """All personas agree on benign → strong consensus."""
        opinions = [
            PersonaOpinion("Pop Gen", "Benign", "reasoning1"),
            PersonaOpinion("Clin Gen", "Benign", "reasoning2"),
            PersonaOpinion("Func Gen", "Benign", "reasoning3"),
        ]
        consensus = _build_consensus(opinions)
        assert "benign" in consensus.lower()
        assert "All" in consensus
    
    def test_mixed_opinions_no_consensus(self):
        """Mixed opinions → no consensus."""
        opinions = [
            PersonaOpinion("Pop Gen", "Pathogenic", "reasoning1"),
            PersonaOpinion("Clin Gen", "Benign", "reasoning2"),
            PersonaOpinion("Func Gen", "Vus", "reasoning3"),
        ]
        consensus = _build_consensus(opinions)
        assert "Mixed" in consensus or "no consensus" in consensus.lower()
    
    def test_all_vus_consensus(self):
        """All agree on VUS → remains VUS."""
        opinions = [
            PersonaOpinion("Pop Gen", "Vus", "reasoning1"),
            PersonaOpinion("Clin Gen", "Uncertain", "reasoning2"),
            PersonaOpinion("Func Gen", "Vus", "reasoning3"),
        ]
        consensus = _build_consensus(opinions)
        assert "VUS" in consensus or "uncertain" in consensus.lower()


class TestBuildRecommendation:
    """Test recommendation generation based on consensus."""

    def test_pathogenic_recommendation(self):
        """Pathogenic consensus → clinical correlation recommendation."""
        consensus = "All personas lean pathogenic."
        # Create a mock variant object
        class MockVariant:
            gene = "BRCA1"
        rec = _build_recommendation(consensus, MockVariant())  # type: ignore
        assert "clinical" in rec.lower() or "prioritize" in rec.lower()
    
    def test_benign_recommendation(self):
        """Benign consensus → deprioritize recommendation."""
        consensus = "All personas lean benign."
        class MockVariant:
            gene = "TP53"
        rec = _build_recommendation(consensus, MockVariant())  # type: ignore
        assert "deprioritize" in rec.lower() or "benign" in rec.lower()
    
    def test_vus_recommendation(self):
        """VUS → further study recommendation."""
        consensus = "All personas agree: remains VUS."
        class MockVariant:
            gene = "KRAS"
        rec = _build_recommendation(consensus, MockVariant())  # type: ignore
        assert "VUS" in rec or "reclassification" in rec.lower() or "segregation" in rec.lower()
