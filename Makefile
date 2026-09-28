.PHONY: help install test lint format build ci

help:
	@echo "Targets: install, test, lint, format, build, ci"

install:
	python -m pip install -e ".[box]"

test:
	python -m pytest

lint:
	ruff check core doors tests
	ruff format --check core doors tests

format:
	ruff format core doors tests
	ruff check --fix core doors tests

build:
	python -m build

ci: lint test
