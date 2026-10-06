PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: env index fetch parse locate pilot eval-dev freeze eval-test scale audit report reproduce test lint

# Needs a native (arm64 on Apple silicon) Python 3.11+. With an x86_64 Anaconda install,
# use: CONDA_SUBDIR=osx-arm64 conda create -p ./.venv python=3.12 pip && make env PYTHON=.venv/bin/python
PYTHON ?= python3
env:
	test -x .venv/bin/python || $(PYTHON) -m venv .venv
	$(PIP) install -e ".[dev]"

index:
	$(PY) -m cra_extract.index --window 2023-01-01 2025-12-31 --prior-from 2018-01-01

fetch:
	$(PY) -m cra_extract.fetch --manifest config/corpus.csv

parse:
	$(PY) -m cra_extract.parse --manifest config/corpus.csv

locate:
	$(PY) -m cra_extract.locate --manifest config/corpus.csv --report results/locator_coverage.csv

pilot:
	$(PY) -m cra_extract.run --split dev --systems rules,llm

eval-dev:
	$(PY) -m cra_extract.evaluate --split dev

freeze:
	$(PY) -m cra_extract.freeze --tag plan-v1.0

eval-test:
	$(PY) -m cra_extract.evaluate --split test

scale:
	$(PY) -m cra_extract.run --split corpus --systems rules,llm

audit:
	$(PY) -m cra_extract.audit --draw --n-per-stratum 10

report:
	$(PY) -m cra_extract.report

reproduce:
	$(PY) -m cra_extract.evaluate --split test --replay-only
	$(PY) -m cra_extract.report --check results/table1.csv

test:
	$(PY) -m pytest -m "not network"

lint:
	.venv/bin/ruff check src tests
