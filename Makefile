.PHONY: help install test lint format typecheck build ci

help:
	@echo "Targets:"
	@echo "  install          Editable install with dev extras (pip install -e '.[dev]')"
	@echo "  test             Run the full test suite"
	@echo "  lint             Ruff lint + format check on the product and tests"
	@echo "  format           Auto-format with ruff"
	@echo "  typecheck        Run mypy on the core and doors packages"
	@echo "  ci               Everything CI runs: lint, typecheck, test"

install:
	python -m pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check core doors tests scripts conftest.py
	ruff format --check core doors tests scripts conftest.py

format:
	ruff format core doors tests scripts conftest.py
	ruff check --fix core doors tests scripts conftest.py

typecheck:
	mypy core doors

build:
	python -m build

ci: lint typecheck test
