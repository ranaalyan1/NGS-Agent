# Local (Temporal-free) mode — the default.
local-infra:
	docker compose -f docker-compose.lite.yml up -d
	bash scripts/build-agents.sh

# Full Temporal stack for core-facility deployments.
temporal-infra:
	docker compose up -d
	bash scripts/build-agents.sh

worker:
	python worker.py

wizard:
	python cli.py wizard

test:
	pytest

.PHONY: local-infra temporal-infra worker wizard test
