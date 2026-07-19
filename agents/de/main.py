import logging
from pathlib import Path

from base_agent import BaseAgent

logger = logging.getLogger(__name__)


class DEAgent(BaseAgent):
    def execute(self, inputs, routing_ctx):
        """
        Differential expression analysis agent.
        
        In production, this should call the real DESeq2 R script.
        This mock implementation provides realistic fallback values
        when running without the full R/DESeq2 environment.
        """
        run_id = routing_ctx.get("run_id", "unknown")
        
        # Check if we're running with real DE results from upstream
        payload = inputs.get("payload", {})
        de_summary = payload.get("de_summary", {})
        
        # If real DE results are available, use them
        if de_summary and isinstance(de_summary, dict):
            n_sig = de_summary.get("n_sig", 0)
            pc1_variance = de_summary.get("pc1_variance", 0)
            warnings = de_summary.get("warnings", [])
            
            logger.info(f"Using real DE results: {n_sig} significant genes")
            
            return {
                "agent": "de",
                "status": "ok",
                "payload": {
                    "n_sig": n_sig,
                    "pc1_variance": pc1_variance,
                    "warnings": warnings,
                    "de_summary": de_summary,
                },
                "reasoning": f"DESeq2 analysis completed with {n_sig} significant genes",
            }
        
        # Fallback mode: provide realistic mock values with clear indication
        # These are NOT random - they represent typical RNA-Seq experiment outcomes
        n_sig = 250  # Typical number of DE genes in controlled experiments
        pc1_variance = 65  # Typical PC1 variance percentage
        
        logger.warning(
            "DE analysis running in mock mode - no real DESeq2 results available. "
            "For production use, ensure R/DESeq2 environment is configured."
        )
        
        return {
            "agent": "de",
            "status": "mock",
            "payload": {
                "n_sig": n_sig,
                "pc1_variance": pc1_variance,
                "warnings": ["Mock mode: DE results are estimates, not from real analysis"],
                "is_mock": True,
            },
            "reasoning": f"Mock DE analysis (typical values: {n_sig} sig genes, PC1={pc1_variance}%)",
        }


if __name__ == "__main__":
    DEAgent().run()
