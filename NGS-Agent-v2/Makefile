# NGS-Agent v2: Makefile
# Convenient commands for common tasks

.PHONY: help setup install test clean run run-verbose docs

help:
	@echo "NGS-Agent v2: Available Commands"
	@echo "=================================="
	@echo "make setup         - Initialize environment (one-time)"
	@echo "make install       - Install Python dependencies"
	@echo "make install-r     - Install R packages"
	@echo "make test          - Run quick test"
	@echo "make run           - Run pipeline on data/input"
	@echo "make run-verbose   - Run with verbose logging"
	@echo "make clean         - Remove results and logs"
	@echo "make help          - Show this help message"

setup:
	@echo "Setting up NGS-Agent v2..."
	bash scripts/setup.sh

install:
	@echo "Installing Python dependencies..."
	pip install -r requirements.txt

install-r:
	@echo "Installing R packages..."
	Rscript scripts/install_r_packages.R

test:
	@echo "Running quick test..."
	bash scripts/quick_test.sh

run:
	@echo "Running pipeline..."
	python scripts/run_ngs_agent.py --input ./data/input --output ./results

run-verbose:
	@echo "Running pipeline (verbose)..."
	python scripts/run_ngs_agent.py --input ./data/input --output ./results --verbose

clean:
	@echo "Cleaning up..."
	rm -rf results logs test_data
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	@echo "Clean complete"

.DEFAULT_GOAL := help
