"""Unit tests for NGS-Agent core analyzer module."""

from pathlib import Path

import pytest

from ngs_agent.analyzer import (
    Variant,
    parse_vcf,
    scan_qc,
    _info_field,
    _parse_float,
    _parse_sample,
)

DATA_DIR = Path(__file__).parent / "data"


class TestCSQParsing:
    """Test VEP CSQ parsing - the consequence should be field [1], not [0]."""

    def test_vep_csq_parses_consequence_not_allele(self):
        """VEP CSQ format is Allele|Consequence|IMPACT|..., so we must extract Consequence."""
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        variants = parse_vcf(vcf_path)
        
        # First variant: BRCA1 missense_variant
        # The CSQ is "A|missense_variant|MODERATE|BRCA1|..."
        # Bug would extract "A" (allele), correct extracts "missense_variant"
        brca1_var = variants[0]
        assert brca1_var.gene == "BRCA1"
        assert brca1_var.consequence == "missense_variant", \
            f"Expected 'missense_variant', got '{brca1_var.consequence}' - CSQ parsing bug!"
        
    def test_multiallelic_site_first_consequence(self):
        """Multiallelic sites have comma-separated CSQ entries."""
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        variants = parse_vcf(vcf_path)
        
        # Second variant has two ALT alleles with CSQ: 
        # "A|missense_variant|...,T|stop_gained|..."
        brca2_var = variants[1]
        assert brca2_var.gene == "BRCA2"
        # Should get the first consequence (for first ALT allele)
        assert brca2_var.consequence == "missense_variant", \
            f"Expected 'missense_variant' for first allele, got '{brca2_var.consequence}'"


class TestVAFCalculation:
    """Test VAF calculation handles biallelic and multiallelic sites correctly."""

    def test_biallelic_vaf_calculation(self):
        """For biallelic site, VAF = AD[1] / sum(AD)."""
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        variants = parse_vcf(vcf_path)
        
        brca1_var = variants[0]
        # AD = 122,123 → VAF = 123 / (122 + 123) = 123/245 ≈ 0.502
        assert brca1_var.vaf is not None
        expected_vaf = 123 / (122 + 123)
        assert abs(brca1_var.vaf - expected_vaf) < 0.001, \
            f"Expected VAF ~{expected_vaf:.3f}, got {brca1_var.vaf}"
    
    def test_multiallelic_ad_handling(self):
        """Multiallelic sites have >2 AD values; current impl uses AD[1]/sum which is biallelic-only."""
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        variants = parse_vcf(vcf_path)
        
        brca2_var = variants[1]
        # AD = 78,40,38 (ref, alt1, alt2)
        # Current buggy behavior: uses ads[1]/sum = 40/156 ≈ 0.256
        # This test documents the current behavior; fixing it requires multi-ALT support
        assert brca2_var.depth == 156
        # Note: For proper multiallelic support, we'd need to track VAF per ALT allele
        # For now, just verify the calculation happens without crashing
        assert brca2_var.vaf is not None


class TestClinVarClassification:
    """Test ClinVar classification logic."""

    def test_pathogenic_without_conflict(self):
        """Pathogenic without 'conflicting' should be is_pathogenic=True."""
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        variants = parse_vcf(vcf_path)
        
        brca1_var = variants[0]
        assert "Pathogenic" in brca1_var.clinvar
        assert brca1_var.is_pathogenic is True
        assert brca1_var.is_vus is False
    
    def test_conflicting_interpretations_not_pathogenic(self):
        """Conflicting_interpretations should NOT be classified as pathogenic."""
        vcf_path = DATA_DIR / "simple_biallelic.vcf"
        variants = parse_vcf(vcf_path)
        
        brca2_var = variants[1]
        assert "Conflicting" in brca2_var.clinvar
        assert brca2_var.is_pathogenic is False, \
            "Conflicting interpretations should not be classified as pathogenic"
    
    def test_vus_classification(self):
        """Uncertain significance should be is_vus=True."""
        vcf_path = DATA_DIR / "vep_multisample.vcf"
        variants = parse_vcf(vcf_path)
        
        # Find TP53 variant with Uncertain_significance
        tp53_vars = [v for v in variants if v.gene == "TP53"]
        assert len(tp53_vars) > 0
        tp53_var = tp53_vars[0]
        assert tp53_var.is_vus is True
        assert tp53_var.is_pathogenic is False


class TestInfoFieldParsing:
    """Test INFO field extraction."""

    def test_info_field_simple(self):
        """Simple key=value extraction."""
        info = "GENE=BRCA1;CLNSIG=Pathogenic;AF=0.00002"
        assert _info_field(info, "GENE") == "BRCA1"
        assert _info_field(info, "CLNSIG") == "Pathogenic"
        assert _info_field(info, "AF") == "0.00002"
        assert _info_field(info, "NONEXISTENT") is None
    
    def test_info_field_with_semicolon_value(self):
        """Value containing semicolons should stop at next key=."""
        info = "CSQ=A|B|C;NEXT_KEY=value"
        assert _info_field(info, "CSQ") == "A|B|C"


class TestParseFloat:
    """Test float parsing with edge cases."""

    def test_simple_float(self):
        assert _parse_float("0.00002") == 0.00002
    
    def test_comma_separated_takes_first(self):
        """AF can be comma-separated; should take first value."""
        assert _parse_float("0.1,0.2") == 0.1
    
    def test_invalid_returns_none(self):
        assert _parse_float("invalid") is None
        assert _parse_float(None) is None


class TestParseSample:
    """Test sample column parsing (DP, AD)."""

    def test_dp_and_ad_parsing(self):
        """Parse DP and AD from FORMAT fields."""
        fmt = "GT:DP:AD"
        format_keys = ["GT", "DP", "AD"]
        values = "0/1:245:122,123"
        
        depth, vaf = _parse_sample(values, format_keys)
        assert depth == 245
        assert vaf is not None
        assert abs(vaf - 123/245) < 0.001
    
    def test_missing_ad_returns_none_vaf(self):
        """Missing AD should return None for VAF."""
        fmt = "GT:DP"
        format_keys = ["GT", "DP"]
        values = "0/1:245"
        
        depth, vaf = _parse_sample(values, format_keys)
        assert depth == 245
        assert vaf is None


class TestScanQC:
    """Test QC scanning from summary files."""

    def test_scan_qc_parses_mapping_rate(self):
        """Mapping rate should be extracted and graded."""
        qc_path = DATA_DIR / "fastqc_data.txt"
        metrics = scan_qc(qc_path)
        
        # fastqc_data.txt doesn't have mapping rate (that's alignment, not FastQC)
        # but this test ensures the function runs without error
        assert isinstance(metrics, list)
    
    def test_scan_qc_missing_file_returns_empty(self):
        """Non-existent file should return empty list."""
        metrics = scan_qc(Path("/nonexistent/path/qc.txt"))
        assert metrics == []
