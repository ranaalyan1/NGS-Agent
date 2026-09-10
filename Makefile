.PHONY: help wizard install test test-unit test-integration test-all lint format typecheck build ci

help:
	@echo "Targets:"
	@echo "  install          Create the pinned conda environment (mamba env create -f environment.yml)"
	@echo "  test             Run the full test suite (unit + integration, integration self-skips without docker)"
	@echo "  test-unit        Run only unit tests (excludes integration)"
	@echo "  test-integration Run only integration tests (requires docker; the swarm functional test also needs RUN_NGS_FUNCTIONAL=1)"
	@echo "  lint             Ruff lint + format check"
	@echo "  format           Auto-format with ruff"
	@echo "  typecheck        Run mypy on the ngs_agent package"
	@echo "  ci               Everything CI runs: lint, test-unit, test-integration"

wizard:
	python cli.py wizard

install:
	mamba env create -f environment.yml

# Full suite by default: integration tests skip themselves when docker or the
# required env vars are unavailable, so `make test` is safe everywhere.
test:
	pytest

test-unit:
	pytest -m "not integration"

test-integration:
	pytest -m "integration"

lint:
	ruff check ngs_agent tests
	ruff format --check ngs_agent tests

format:
	ruff format ngs_agent tests
	ruff check --fix ngs_agent tests

typecheck:
	mypy ngs_agent

build:
	python -m build

ci: lint test-unit test-integration
