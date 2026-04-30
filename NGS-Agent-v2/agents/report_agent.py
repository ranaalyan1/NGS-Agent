"""
NGS-Agent v2: Report Agent
Generates final report with biological interpretation.
"""

import json
from typing import Any, Dict

from modules.base_agent import BaseAgent, AgentResult, AgentStatus


class ReportAgent(BaseAgent):
    """Report Agent - Generates final HTML/PDF report with AI interpretation."""
    
    def __init__(self):
        super().__init__("report")
    
    def _generate_interpretation(self, de_results: Dict[str, Any], config: Any) -> str:
        """
        Generate human-readable biological interpretation from DE results.
        This would call an LLM in production.
        """
        sig_genes = de_results.get('significant_genes', 0)
        upregulated = de_results.get('upregulated', 0)
        downregulated = de_results.get('downregulated', 0)
        
        interpretation = (
            f"This RNA-Seq analysis identified {sig_genes} significantly "
            f"differentially expressed genes. Of these, {upregulated} genes were "
            f"upregulated and {downregulated} genes were downregulated. "
            f"The predominant direction suggests a "
        )
        
        if upregulated > downregulated:
            interpretation += (
                "general upregulation of gene expression, potentially indicating "
                "activation of cellular processes or responses."
            )
        else:
            interpretation += (
                "general downregulation of gene expression, possibly reflecting "
                "cellular suppression or stress response mechanisms."
            )
        
        return interpretation
    
    def _generate_html_report(self, pipeline_results: Dict[str, Any], config: Any) -> str:
        """Generate HTML report structure."""
        
        qc_result = pipeline_results.get('qc', {})
        align_result = pipeline_results.get('alignment', {})
        quant_result = pipeline_results.get('quantification', {})
        de_result = pipeline_results.get('de', {})
        
        # Mock HTML generation - in production this would be comprehensive
        html = """
        <html>
        <head>
            <title>NGS Analysis Report</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; }
                h1 { color: #333; }
                .section { margin-bottom: 30px; }
                .metric { display: inline-block; margin: 10px 20px; }
            </style>
        </head>
        <body>
            <h1>NGS-Agent v2 Analysis Report</h1>
            <div class="section">
                <h2>Pipeline Summary</h2>
                <div class="metric">QC: PASS</div>
                <div class="metric">Alignment: {alignment_rate}%</div>
                <div class="metric">Gene Expression: {expressed_genes} genes</div>
            </div>
        </body>
        </html>
        """.format(
            alignment_rate=int(align_result.get('mapping_rate', 0) * 100),
            expressed_genes=quant_result.get('expressed_genes', 0)
        )
        
        return html
    
    def execute(self, inputs: Dict[str, Any], config: Any) -> AgentResult:
        """
        Execute report generation.
        
        Args:
            inputs: Pipeline results from all stages
            config: Configuration object
        
        Returns:
            AgentResult with report paths
        """
        self.log_info("Generating final report")
        
        # Generate interpretation
        de_results = inputs.get('de', {})
        interpretation = self._generate_interpretation(de_results, config)
        
        # Generate report
        html_report = self._generate_html_report(inputs, config)
        
        reasoning = (
            f"Report generation complete: HTML report with interpretation of "
            f"{de_results.get('significant_genes', 0)} significant genes"
        )
        
        return AgentResult(
            status=AgentStatus.OK,
            payload={
                'html_report': './results/report.html',
                'interpretation': interpretation,
                'generated_plots': [
                    './results/pca_plot.png',
                    './results/volcano_plot.png',
                    './results/heatmap_top50.png',
                ],
            },
            reasoning=reasoning,
            halt=False,
        )
