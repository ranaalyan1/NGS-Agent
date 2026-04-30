"""
NGS-Agent v2: Pipeline Controller
Orchestrates the entire analysis workflow using agents.
"""

from typing import Any, Dict, Optional
from pathlib import Path

from modules.config import get_config
from modules.logging_config import get_logger
from modules.base_agent import AgentStatus

from agents.qc_agent import QCAgent
from agents.alignment_agent import AlignmentAgent
from agents.quantification_agent import QuantificationAgent
from agents.de_agent import DEAgent
from agents.validation_agent import ValidationAgent
from agents.report_agent import ReportAgent


logger = get_logger(__name__)


class PipelineController:
    """
    Main pipeline orchestrator.
    Coordinates all agents and manages the analysis workflow.
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """Initialize controller with configuration."""
        self.config = get_config()
        if config_path:
            from modules.config import init_config
            self.config = init_config(config_path)
        
        # Initialize agents
        self.qc_agent = QCAgent()
        self.alignment_agent = AlignmentAgent()
        self.quantification_agent = QuantificationAgent()
        self.de_agent = DEAgent()
        self.validation_agent = ValidationAgent()
        self.report_agent = ReportAgent()
        
        # Results storage
        self.pipeline_results: Dict[str, Any] = {}
        self.execution_log = []
    
    def _check_halt(self, agent_name: str, result: Dict[str, Any]) -> bool:
        """Check if result indicates pipeline should halt."""
        if result.get('halt', False):
            logger.error(f"Pipeline halted by {agent_name}: {result.get('halt_reason', 'Unknown')}")
            self.execution_log.append({
                'agent': agent_name,
                'status': 'HALTED',
                'reason': result.get('halt_reason', '')
            })
            return True
        return False
    
    def run(
        self,
        fastq_files: list[str],
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Run complete pipeline.
        
        Args:
            fastq_files: List of FASTQ file paths
            metadata: Optional sample metadata
        
        Returns:
            Dictionary with complete results or error information
        """
        logger.info("=" * 60)
        logger.info("NGS-Agent v2: Starting Pipeline")
        logger.info("=" * 60)
        
        try:
            # Stage 1: QC
            logger.info("\n[STAGE 1/6] Quality Control")
            qc_result = self.qc_agent.run(
                {'fastq_files': fastq_files},
                self.config
            )
            self.pipeline_results['qc'] = qc_result.to_dict()
            
            if self._check_halt('qc_agent', qc_result.to_dict()):
                return self._halt_response('qc')
            
            # Stage 2: Decision - should we trim?
            logger.info("\n[STAGE 2/6] Trim Decision")
            should_trim = qc_result.payload.get('should_trim', False)
            
            if should_trim:
                logger.warning("QC recommends trimming")
                # In full implementation, would run trim agent here
                trimmed_files = fastq_files  # Use trimmed files
            else:
                logger.info("Reads pass QC, skipping trimming")
                trimmed_files = fastq_files
            
            # Stage 3: Alignment
            logger.info("\n[STAGE 3/6] Read Alignment")
            align_result = self.alignment_agent.run(
                {'fastq_r1': trimmed_files[0], 'fastq_r2': trimmed_files[1] if len(trimmed_files) > 1 else None},
                self.config
            )
            self.pipeline_results['alignment'] = align_result.to_dict()
            
            if self._check_halt('alignment_agent', align_result.to_dict()):
                return self._halt_response('alignment')
            
            # Stage 4: Quantification
            logger.info("\n[STAGE 4/6] Gene Quantification")
            quant_result = self.quantification_agent.run(
                {'bam_file': align_result.payload.get('bam_file', './results/aligned.bam')},
                self.config
            )
            self.pipeline_results['quantification'] = quant_result.to_dict()
            
            if self._check_halt('quantification_agent', quant_result.to_dict()):
                return self._halt_response('quantification')
            
            # Stage 5: Differential Expression
            logger.info("\n[STAGE 5/6] Differential Expression Analysis")
            de_result = self.de_agent.run(
                {
                    'count_matrix': quant_result.payload.get('count_matrix_file', './results/counts.tsv'),
                    'metadata': metadata
                },
                self.config
            )
            self.pipeline_results['de'] = de_result.to_dict()
            
            if self._check_halt('de_agent', de_result.to_dict()):
                return self._halt_response('de')
            
            # Stage 6: Validation & Reporting
            logger.info("\n[STAGE 6/6] Validation & Report Generation")
            validation_result = self.validation_agent.run(
                {
                    'sample_info': {'read_count': 50000000},
                    'alignment_result': align_result.payload,
                    'quant_result': quant_result.payload,
                },
                self.config
            )
            self.pipeline_results['validation'] = validation_result.to_dict()
            
            # Generate report
            report_result = self.report_agent.run(
                self.pipeline_results,
                self.config
            )
            self.pipeline_results['report'] = report_result.to_dict()
            
            logger.info("\n" + "=" * 60)
            logger.info("✓ Pipeline Complete!")
            logger.info("=" * 60)
            
            return self._success_response()
        
        except Exception as e:
            logger.error(f"Pipeline failed: {e}")
            self.execution_log.append({
                'error': str(e),
                'type': type(e).__name__
            })
            return self._error_response(str(e))
    
    def _halt_response(self, stage: str) -> Dict[str, Any]:
        """Return halt response."""
        return {
            'status': 'HALTED',
            'stage': stage,
            'message': f'Pipeline halted at {stage} stage',
            'results': self.pipeline_results,
            'log': self.execution_log,
        }
    
    def _error_response(self, error: str) -> Dict[str, Any]:
        """Return error response."""
        return {
            'status': 'ERROR',
            'error': error,
            'results': self.pipeline_results,
            'log': self.execution_log,
        }
    
    def _success_response(self) -> Dict[str, Any]:
        """Return success response."""
        return {
            'status': 'SUCCESS',
            'results': self.pipeline_results,
            'report_path': './results/report.html',
            'log': self.execution_log,
        }
    
    def get_summary(self) -> str:
        """Get text summary of results."""
        lines = ["PIPELINE SUMMARY", "=" * 50]
        
        for stage, result in self.pipeline_results.items():
            if isinstance(result, dict):
                status = result.get('status', 'unknown')
                reasoning = result.get('reasoning', '')
                lines.append(f"\n{stage.upper()}: {status}")
                lines.append(f"  {reasoning}")
        
        return "\n".join(lines)
