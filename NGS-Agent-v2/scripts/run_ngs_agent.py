#!/usr/bin/env python3
"""
NGS-Agent v2: Main CLI Entry Point
Complete one-command RNA-Seq analysis pipeline.

Usage:
  run_ngs_agent --input ./data --output ./results
  run_ngs_agent --config config/custom.yaml --input ./data
  run_ngs_agent --help
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from modules.config import init_config, get_config
from modules.logging_config import get_logger, LoggerFactory
from pipelines.controller import PipelineController


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="NGS-Agent v2: Autonomous RNA-Seq Analysis System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage with FASTQ input
  run_ngs_agent --input ./data --output ./results

  # With custom configuration
  run_ngs_agent --config config/custom.yaml --input ./data

  # With explicit organism
  run_ngs_agent --input ./data --organism human --output ./results

  # Dry-run (print commands without executing)
  run_ngs_agent --input ./data --dry-run
        """,
    )
    
    # Required arguments
    parser.add_argument(
        '--input', '-i',
        required=True,
        help='Input directory containing FASTQ files'
    )
    
    # Optional arguments
    parser.add_argument(
        '--output', '-o',
        default='./results',
        help='Output directory for results (default: ./results)'
    )
    
    parser.add_argument(
        '--config', '-c',
        default=None,
        help='Path to configuration YAML file (default: config/default.yaml)'
    )
    
    parser.add_argument(
        '--organism',
        default=None,
        help='Organism name (human, mouse, zebrafish, etc.)'
    )
    
    parser.add_argument(
        '--threads', '-t',
        type=int,
        default=8,
        help='Number of threads to use (default: 8)'
    )
    
    parser.add_argument(
        '--skip-qc',
        action='store_true',
        help='Skip quality control step'
    )
    
    parser.add_argument(
        '--skip-trimming',
        action='store_true',
        help='Skip read trimming even if QC recommends it'
    )
    
    parser.add_argument(
        '--skip-de',
        action='store_true',
        help='Skip differential expression analysis'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Print commands without executing'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    
    parser.add_argument(
        '--version',
        action='version',
        version='NGS-Agent v2.0'
    )
    
    return parser.parse_args()


def validate_inputs(args) -> bool:
    """Validate command line arguments."""
    input_dir = Path(args.input)
    
    if not input_dir.exists():
        print(f"ERROR: Input directory not found: {args.input}", file=sys.stderr)
        return False
    
    if not input_dir.is_dir():
        print(f"ERROR: Input path is not a directory: {args.input}", file=sys.stderr)
        return False
    
    # Check for FASTQ files
    fastq_files = list(input_dir.glob('*.fastq')) + list(input_dir.glob('*.fastq.gz'))
    
    if not fastq_files:
        print(
            f"WARNING: No FASTQ files found in {args.input}\n"
            f"Expected .fastq or .fastq.gz files",
            file=sys.stderr
        )
        return False
    
    return True


def discover_fastq_files(input_dir: str) -> list[str]:
    """Discover FASTQ files in input directory."""
    input_path = Path(input_dir)
    
    fastq_files = sorted(
        list(input_path.glob('*.fastq')) +
        list(input_path.glob('*.fastq.gz')) +
        list(input_path.glob('*.fq')) +
        list(input_path.glob('*.fq.gz'))
    )
    
    return [str(f) for f in fastq_files]


def print_banner():
    """Print startup banner."""
    banner = """
╔═══════════════════════════════════════════════════════════╗
║                                                           ║
║          NGS-Agent v2: RNA-Seq Analysis System            ║
║                                                           ║
║   Autonomous Quality Control → Alignment → Quantification ║
║     → Differential Expression → Report Generation        ║
║                                                           ║
╚═══════════════════════════════════════════════════════════╝
    """
    print(banner)


def main():
    """Main entry point."""
    print_banner()
    
    # Parse arguments
    args = parse_arguments()
    
    # Validate inputs
    if not validate_inputs(args):
        sys.exit(1)
    
    # Initialize configuration
    config = init_config(args.config)
    
    # Update config with CLI arguments
    if args.organism:
        config.set('organism.name', args.organism)
    
    if args.threads:
        config.set('parallel.max_threads', args.threads)
    
    if args.skip_qc:
        config.set('qc.enabled', False)
    
    if args.skip_trimming:
        config.set('trimming.enabled', False)
    
    if args.skip_de:
        config.set('differential_expression.enabled', False)
    
    if args.dry_run:
        config.set('advanced.dry_run', True)
    
    if args.verbose:
        config.set('logging.level', 'DEBUG')
    
    # Update output paths
    config.set('paths.output_dir', args.output)
    
    # Configure logging
    LoggerFactory.configure(config)
    logger = get_logger(__name__)
    
    logger.info(f"Configuration: {args.config or 'default.yaml'}")
    logger.info(f"Input directory: {args.input}")
    logger.info(f"Output directory: {args.output}")
    logger.info(f"Organism: {config.get('organism.name')}")
    
    # Discover FASTQ files
    fastq_files = discover_fastq_files(args.input)
    logger.info(f"Found {len(fastq_files)} FASTQ file(s)")
    
    for f in fastq_files[:5]:
        logger.info(f"  - {Path(f).name}")
    if len(fastq_files) > 5:
        logger.info(f"  ... and {len(fastq_files) - 5} more")
    
    # Create pipeline controller
    pipeline = PipelineController(args.config)
    
    # Run pipeline
    try:
        results = pipeline.run(fastq_files)
        
        # Print summary
        logger.info("\n" + pipeline.get_summary())
        
        # Check overall status
        if results.get('status') == 'SUCCESS':
            logger.info(f"\n✓ Pipeline completed successfully!")
            logger.info(f"Results saved to: {args.output}")
            logger.info(f"Report: {results.get('report_path')}")
            return 0
        else:
            logger.error(f"\n✗ Pipeline {results.get('status', 'FAILED')}")
            if 'error' in results:
                logger.error(f"Error: {results['error']}")
            return 1
    
    except KeyboardInterrupt:
        logger.warning("\nPipeline interrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Pipeline failed with exception: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
