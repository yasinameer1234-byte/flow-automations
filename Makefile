.PHONY: setup test lint fmt demo api docker clean
PY ?= python3

setup:            ## create venv and install dev deps
	$(PY) -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check . && ruff format --check .

fmt:
	ruff format . && ruff check --fix .

demo:             ## run the demo on synthetic data (fresh DB each time)
	rm -f var/flow.db* && FLOW_RETRY_BASE_DELAY=0 flow demo

api:
	uvicorn flow.api.server:app --reload

docker:
	docker build -t flow-automations:dev .

clean:
	rm -rf var .pytest_cache .ruff_cache build dist src/*.egg-info
