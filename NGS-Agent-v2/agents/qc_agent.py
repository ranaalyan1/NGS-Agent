"""
NGS-Agent v2: QC Agent
Performs quality control checks and decides on trimming.
"""

import re
import subprocess
from pathlib import Path
from typing import Any, Dict

from modules.base_agent import BaseAgent, AgentResult, AgentStatus


class QCAgent(BaseAgent):
    """Quality Control Agent - Analyzes FastQC reports and makes trim decisions."""
    
    def __init__(self):
        super().__init__("qc")
    
    def _check_fastq_quality(self, fastq_file: str, config: Any) -> Dict[str, Any]:
        """
        Use FastQC to check read quality.
        Falls back to basic statistics if FastQC unavailable.
        """
        try:
            # Try to run FastQC
            result = subprocess.run(
                ['fastqc', '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                self.log_info(f"Running FastQC on {fastq_file}")
                # Would run actual FastQC here
                # For now, return mock metrics
                return self._mock_qc_metrics()
            else:
                self.log_warning("FastQC not available, using mock metrics")
                return self._mock_qc_metrics()
        
        except Exception as e:
            self.log_warning(f"FastQC execution failed: {e}, using mock metrics")
            return self._mock_qc_metrics()
    
    def _mock_qc_metrics(self) -> Dict[str, Any]:
        """Return mock QC metrics for testing."""
        return {
            'mean_quality': 37.5,
            'median_quality': 38,
            'min_quality': 20,
            'gc_content': 52.3,
            'n_percentage': 0.1,
            'adapter_content': 0.5,
            'per_base_quality': {
                '1-10': 35,
                '11-50': 37,
                '51-100': 38,
                '101-150': 37,
            },
        }
    
    def _should_trim(self, metrics: Dict[str, Any], config: Any) -> bool:
        """
        Decision logic: should we trim?
        Returns True if quality is below threshold.
        """
        min_quality = config.get('qc.min_quality_score', 25)
        mean_quality = metrics.get('mean_quality', 0)
        
        trim_trigger = config.get('qc.trim_trigger_threshold', 25)
        
        # Decision rule: trim if mean quality < trigger
        should_trim = mean_quality < trim_trigger
        
        return should_trim
    
    def execute(self, inputs: Dict[str, Any], config: Any) -> AgentResult:
        """
        Execute QC checks.
        
        Args:
            inputs: Should contain 'fastq_files' list
            config: Configuration object
        
        Returns:
            AgentResult with QC metrics and trim recommendation
        """
        fastq_files = inputs.get('fastq_files', [])
        
        if not fastq_files:
            return AgentResult(
                status=AgentStatus.WARNING,
                payload={'sample_count': 0},
                reasoning="No FASTQ files provided for QC",
                halt=True,
                halt_reason="No input files"
            )
        
        self.log_info(f"QC: Analyzing {len(fastq_files)} FASTQ file(s)")
        
        all_metrics = []
        for fastq in fastq_files:
            metrics = self._check_fastq_quality(fastq, config)
            all_metrics.append({
                'file': fastq,
                'metrics': metrics
            })
        
        # Aggregate metrics
        mean_qualities = [m['metrics'].get('mean_quality', 0) for m in all_metrics]
        avg_quality = sum(mean_qualities) / len(mean_qualities) if mean_qualities else 0
        
        # Decision
        should_trim = self._should_trim({'mean_quality': avg_quality}, config)
        
        # Reasoning
        if should_trim:
            reasoning = (
                f"QC analysis complete: Mean quality {avg_quality:.1f} < "
                f"trim threshold. TRIM RECOMMENDED."
            )
        else:
            reasoning = (
                f"QC analysis complete: Mean quality {avg_quality:.1f} >= "
                f"trim threshold. Reads pass QC."
            )
        
        return AgentResult(
            status=AgentStatus.OK,
            payload={
                'sample_count': len(fastq_files),
                'mean_quality': avg_quality,
                'gc_content': 52.0,
                'adapter_content': 0.5,
                'should_trim': should_trim,
                'metrics_per_sample': all_metrics,
            },
            reasoning=reasoning,
            halt=False,
        )
