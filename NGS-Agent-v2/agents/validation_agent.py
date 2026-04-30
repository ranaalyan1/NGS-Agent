"""
NGS-Agent v2: Validation Agent
Performs comprehensive validation of pipeline results.
"""

from typing import Any, Dict

from modules.base_agent import BaseAgent, AgentResult, AgentStatus


class ValidationAgent(BaseAgent):
    """Validation Agent - Checks quality thresholds across pipeline."""
    
    def __init__(self):
        super().__init__("validation")
    
    def _check_read_depth(self, sample_info: Dict[str, Any], config: Any) -> tuple[bool, str]:
        """Check if minimum reads per sample is met."""
        min_reads = config.get('validation.min_reads_per_sample', 10000)
        reads = sample_info.get('read_count', 0)
        
        passed = reads >= min_reads
        reason = (
            f"Sample has {reads:,} reads (threshold: {min_reads:,}). "
            f"{'PASS' if passed else 'FAIL'}"
        )
        return passed, reason
    
    def _check_mapping_rate(self, alignment_result: Dict[str, Any], config: Any) -> tuple[bool, str]:
        """Check mapping rate threshold."""
        min_mapping = config.get('validation.min_mapping_rate', 0.50)
        mapping_rate = alignment_result.get('mapping_rate', 0)
        
        passed = mapping_rate >= min_mapping
        reason = (
            f"Mapping rate: {mapping_rate*100:.1f}% (threshold: {min_mapping*100:.0f}%). "
            f"{'PASS' if passed else 'FAIL'}"
        )
        return passed, reason
    
    def _check_gene_coverage(self, quant_result: Dict[str, Any], config: Any) -> tuple[bool, str]:
        """Check gene coverage - what fraction of genes are expressed."""
        min_coverage = config.get('validation.min_gene_coverage', 0.80)
        total_genes = quant_result.get('total_genes', 1)
        expressed = quant_result.get('expressed_genes', 0)
        
        coverage_rate = expressed / total_genes if total_genes > 0 else 0
        passed = coverage_rate >= min_coverage
        
        reason = (
            f"Gene coverage: {expressed:,} / {total_genes:,} genes "
            f"({coverage_rate*100:.1f}%, threshold: {min_coverage*100:.0f}%). "
            f"{'PASS' if passed else 'FAIL'}"
        )
        return passed, reason
    
    def execute(self, inputs: Dict[str, Any], config: Any) -> AgentResult:
        """
        Execute comprehensive validation.
        
        Args:
            inputs: Pipeline results so far
            config: Configuration object
        
        Returns:
            AgentResult with validation status
        """
        self.log_info("Running pipeline validation")
        
        checks = {
            'read_depth': False,
            'mapping_rate': False,
            'gene_coverage': False,
        }
        check_reasons = {}
        
        # Check read depth
        if 'sample_info' in inputs:
            checks['read_depth'], check_reasons['read_depth'] = self._check_read_depth(
                inputs['sample_info'], config
            )
        
        # Check mapping rate
        if 'alignment_result' in inputs:
            checks['mapping_rate'], check_reasons['mapping_rate'] = self._check_mapping_rate(
                inputs['alignment_result'], config
            )
        
        # Check gene coverage
        if 'quant_result' in inputs:
            checks['gene_coverage'], check_reasons['gene_coverage'] = self._check_gene_coverage(
                inputs['quant_result'], config
            )
        
        # Determine overall status
        all_passed = all(checks.values()) if checks else True
        
        reasoning_parts = [check_reasons.get(k, "") for k in checks if check_reasons.get(k)]
        reasoning = "Validation checks: " + " | ".join(reasoning_parts)
        
        if all_passed:
            status = AgentStatus.OK
            halt = False
        else:
            halt_on_fail = config.get('validation.halt_on_validation_failure', False)
            status = AgentStatus.WARNING
            halt = halt_on_fail
        
        return AgentResult(
            status=status,
            payload={
                'checks': checks,
                'passed': all_passed,
            },
            reasoning=reasoning,
            halt=halt,
            halt_reason="Validation failed" if halt else ""
        )
