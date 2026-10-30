.PHONY: help install lint format test check smoke run clean

PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip
RUFF ?= .venv/bin/ruff
PYTEST ?= .venv/bin/pytest
ENVMON ?= .venv/bin/envmon

help:
	@echo "install  Create .venv and install the project with dev extras"
	@echo "lint     Run ruff check"
	@echo "format   Format the code with ruff"
	@echo "test     Run pytest"
	@echo "check    Lint, format check and test (the release gate)"
	@echo "smoke    Simulator smoke test against a temporary SQLite file"
	@echo "run      Start the server in simulator mode"

install:
	python3 -m venv .venv
	$(PIP) install -e '.[dev]'

lint:
	$(RUFF) check .

format:
	$(RUFF) format .

test:
	$(PYTEST)

check:
	$(RUFF) check .
	$(RUFF) format --check .
	$(PYTEST)

smoke:
	mkdir -p /tmp/opencode
	ENVMON_DATABASE__URL=sqlite:////tmp/opencode/envmon-smoke.db $(ENVMON) init-db
	ENVMON_DATABASE__URL=sqlite:////tmp/opencode/envmon-smoke.db $(ENVMON) sample-once --mode simulator --json
	ENVMON_DATABASE__URL=sqlite:////tmp/opencode/envmon-smoke.db $(ENVMON) doctor --skip-sensors --mode simulator

run:
	$(ENVMON) run --mode simulator

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
