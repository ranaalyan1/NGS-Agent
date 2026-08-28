"""Unit tests for ACMG/AMP criteria evaluation engine."""

import pytest
from ngs_agent.acmg import (
    compute_acmg_classification,
    ACMG_CRITERIA,
    ACMGEvaluation,
)


class TestACMGClassification:
    def test_standalone_benign_ba1(self):
        """BA1 (>5% allele frequency) alone should classify as Benign."""
        res = compute_acmg_classification(["BA1"])
        assert res.classification == "Benign"
        assert res.confidence >= 0.95

    def test_strong_pathogenic_pvs1_and_ps(self):
        """PVS1 + PS1 should classify as Pathogenic."""
        res = compute_acmg_classification(["PVS1", "PS1"])
        assert res.classification == "Pathogenic"

    def test_pathogenic_multiple_ps(self):
        """>=2 PS codes should classify as Pathogenic."""
        res = compute_acmg_classification(["PS1", "PS2"])
        assert res.classification == "Pathogenic"

    def test_likely_pathogenic_pvs1_pm(self):
        """PVS1 + 1 PM should classify as Likely Pathogenic."""
        res = compute_acmg_classification(["PVS1", "PM1"])
        assert res.classification == "Likely Pathogenic"

    def test_likely_pathogenic_multiple_pm(self):
        """>=3 PM codes should classify as Likely Pathogenic."""
        res = compute_acmg_classification(["PM1", "PM2", "PM4"])
        assert res.classification == "Likely Pathogenic"

    def test_benign_multiple_bs(self):
        """>=2 BS codes should classify as Benign."""
        res = compute_acmg_classification(["BS1", "BS2"])
        assert res.classification == "Benign"

    def test_likely_benign_bs_and_bp(self):
        """1 BS + 1 BP should classify as Likely Benign."""
        res = compute_acmg_classification(["BS1", "BP1"])
        assert res.classification == "Likely Benign"

    def test_vus_isolated_evidence(self):
        """Single PP or single PM should remain VUS."""
        res = compute_acmg_classification(["PP3"])
        assert res.classification == "VUS"

    def test_vus_conflicting_evidence(self):
        """Conflicting pathogenic and benign codes should result in VUS."""
        res = compute_acmg_classification(["PVS1", "BA1"])
        assert res.classification == "VUS"
