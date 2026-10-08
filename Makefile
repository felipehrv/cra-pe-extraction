PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: env index index-replay fetch parse locate templates rules baseline smoke-llm label-init label-validate label-hash pilot eval-dev freeze eval-test scale audit report reproduce test lint

# Needs a native (arm64 on Apple silicon) Python 3.11+. With an x86_64 Anaconda install,
# use: CONDA_SUBDIR=osx-arm64 conda create -p ./.venv python=3.12 pip && make env PYTHON=.venv/bin/python
PYTHON ?= python3
env:
	test -x .venv/bin/python || $(PYTHON) -m venv .venv
	$(PIP) install -e ".[dev]"

index:
	$(PY) -m cra_extract.index --window 2023-01-01 2025-12-31 --prior-from 2018-01-01

# Rebuild the manifest from a saved raw pull (data/index/raw/*_YYYYMMDD.*) without network.
RAW_STAMP ?= 20261007
index-replay:
	$(PY) -m cra_extract.index --window 2023-01-01 2025-12-31 --prior-from 2018-01-01 --from-raw $(RAW_STAMP)

fetch:
	$(PY) -m cra_extract.fetch --manifest config/corpus.csv

parse:
	$(PY) -m cra_extract.parse --manifest config/corpus.csv

locate:
	$(PY) -m cra_extract.locate --manifest config/corpus.csv --report results/locator_coverage.csv

templates:
	$(PY) -m cra_extract.templates --manifest config/corpus.csv --out results/census.csv

rules:
	$(PY) -m cra_extract.rules --manifest config/corpus.csv --out results/rules.csv

baseline:
	$(PY) -m cra_extract.baseline --manifest config/corpus.csv --out results/baseline.csv

# One live structured-output call against a sample PE (needs the backend's API key in .env).
BACKEND ?= anthropic
MODEL ?= claude-haiku-4-5
smoke-llm:
	$(PY) -m cra_extract.llm --smoke --backend $(BACKEND) --model $(MODEL) --split smoke

SPLIT ?= dev
label-init:
	$(PY) -m cra_extract.label init --split $(SPLIT) --out data/gold/labels_$(SPLIT).csv

label-validate:
	$(PY) -m cra_extract.label validate data/gold/labels_$(SPLIT).csv

label-hash:
	$(PY) -m cra_extract.label hash --split $(SPLIT) --labels data/gold/labels_$(SPLIT).csv

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
