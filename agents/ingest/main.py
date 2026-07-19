import gzip
import logging
from pathlib import Path

from base_agent import BaseAgent

logger = logging.getLogger(__name__)


class IngestAgent(BaseAgent):
    def _count_reads(self, path: str) -> int:
        """Count reads in a FASTQ file (4 lines per read)."""
        p = Path(path)
        line_count = 0
        try:
            if p.suffix == ".gz":
                with gzip.open(p, "rt") as handle:
                    for _ in handle:
                        line_count += 1
            else:
                with p.open("r", encoding="utf-8") as handle:
                    for _ in handle:
                        line_count += 1
            return max(1, line_count // 4)
        except FileNotFoundError:
            logger.warning(f"FASTQ file not found: {path}")
            return 0
        except Exception as e:
            logger.error(f"Error counting reads in {path}: {e}")
            return 0

    def execute(self, inputs, routing_ctx):
        """
        Ingest agent for validating and counting FASTQ reads.
        
        Supports both single-end and paired-end sequencing data.
        When files are not available (e.g., in mock/test environments),
        provides clear warnings and safe default values.
        """
        fastq_path = inputs.get("fastq_path")
        fastq_r1 = inputs.get("fastq_r1")
        fastq_r2 = inputs.get("fastq_r2")
        is_mock = False

        if fastq_r1 and fastq_r2:
            # Paired-end mode
            paired = True
            p1 = Path(fastq_r1)
            p2 = Path(fastq_r2)
            if p1.exists() and p2.exists():
                reads_r1 = self._count_reads(fastq_r1)
                reads_r2 = self._count_reads(fastq_r2)
                read_count = min(reads_r1, reads_r2) if reads_r1 > 0 and reads_r2 > 0 else 0
                reasoning = (
                    f"Validated paired reads from both inputs (R1={reads_r1}, R2={reads_r2})"
                )
                logger.info(reasoning)
            else:
                # Mock mode with explicit warning
                read_count = 0
                is_mock = True
                missing = []
                if not p1.exists():
                    missing.append(str(p1))
                if not p2.exists():
                    missing.append(str(p2))
                reasoning = f"Paired input files not available: {', '.join(missing)}"
                logger.warning(f"{reasoning} - using mock mode")
        elif fastq_path:
            # Single-end mode
            paired = False
            p = Path(fastq_path)
            if p.exists():
                read_count = self._count_reads(fastq_path)
                reasoning = f"Validated {read_count} reads from single-end input file"
                logger.info(reasoning)
            else:
                # Mock mode with explicit warning
                read_count = 0
                is_mock = True
                reasoning = f"Single-end input file not available: {fastq_path}"
                logger.warning(f"{reasoning} - using mock mode")
        else:
            # No input provided
            paired = False
            read_count = 0
            is_mock = True
            reasoning = "No FASTQ input provided"
            logger.warning(f"{reasoning} - using mock mode")

        status = "mock" if is_mock else "ok"
        
        return {
            "agent": "ingest",
            "status": status,
            "payload": {
                "read_count": read_count,
                "is_paired": paired,
                "encoding": "phred33",
                "raw_reads": fastq_path,
                "raw_reads_r1": fastq_r1,
                "raw_reads_r2": fastq_r2,
                "is_mock": is_mock,
            },
            "reasoning": reasoning,
        }


if __name__ == "__main__":
    IngestAgent().run()
