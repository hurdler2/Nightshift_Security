.PHONY: help install lint fmt test test-edge test-backend up down logs ps probe clean

PY ?= python

help:
	@echo "install     - install dev deps for backend, edge-agent, ai-service"
	@echo "lint        - ruff check + format --check"
	@echo "fmt         - ruff format + fix"
	@echo "test        - run all python test suites"
	@echo "up/down     - docker compose up/down (infra/docker-compose.yml)"
	@echo "probe       - run the Dahua probe (pass ARGS=...)"

install:
	$(PY) -m pip install -e "backend[dev]"
	$(PY) -m pip install -e "edge-agent[dev]"
	$(PY) -m pip install -e "ai-service[dev]"

lint:
	$(PY) -m ruff check backend edge-agent ai-service scripts
	$(PY) -m ruff format --check backend edge-agent ai-service scripts

fmt:
	$(PY) -m ruff format backend edge-agent ai-service scripts
	$(PY) -m ruff check --fix backend edge-agent ai-service scripts

test: test-edge test-backend
	$(PY) -m pytest ai-service/tests -q

test-edge:
	$(PY) -m pytest edge-agent/tests -q

test-backend:
	$(PY) -m pytest backend/tests -q

up:
	docker compose -f infra/docker-compose.yml up -d --build

down:
	docker compose -f infra/docker-compose.yml down

logs:
	docker compose -f infra/docker-compose.yml logs -f --tail=100

ps:
	docker compose -f infra/docker-compose.yml ps

probe:
	$(PY) scripts/dahua_probe.py $(ARGS)

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
