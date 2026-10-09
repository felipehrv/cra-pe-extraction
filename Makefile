PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: env index index-replay fetch parse locate templates rules baseline group-a smoke-llm draw-dev draw-test draw-audit strata-show label-init label-validate label-hash pilot eval-dev freeze eval-test scale audit report reproduce test lint

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

# Group A agreement of the rules with the agency index and the triage of every disagreement
# (design 6.3). RULES_BEFORE is an earlier rules output kept for the before/after table.
RULES_BEFORE ?= results/rules_0.1.0.csv
group-a:
	$(PY) -m cra_extract.group_a --rules results/rules.csv --before $(RULES_BEFORE)

# One live structured-output call against a sample PE (needs the backend's API key in .env).
BACKEND ?= anthropic
MODEL ?= claude-haiku-4-5
smoke-llm:
	$(PY) -m cra_extract.llm --smoke --backend $(BACKEND) --model $(MODEL) --split smoke

# Split draws (dev < test < audit; a redraw with the same seed is byte-identical).
SEED ?= 20261007
DRAW_DATE ?= 2026-10-07
draw-dev:
	$(PY) -m cra_extract.strata draw-dev --seed $(SEED) --date $(DRAW_DATE)

# Run after `make scale`: the per-document n_aa_sections column of results/sections_corpus.csv
# is the multi-AA proxy. With the 25-document fallback add --per-cell 2.
AA_COUNTS ?= results/sections_corpus.csv
draw-test:
	$(PY) -m cra_extract.strata draw-test --seed $(SEED) --date $(DRAW_DATE) --aa-counts $(AA_COUNTS)

RISK ?= results/risk_corpus.csv
draw-audit:
	$(PY) -m cra_extract.strata draw-audit --risk $(RISK) --seed $(SEED) --date $(DRAW_DATE)

strata-show:
	$(PY) -m cra_extract.strata show

SPLIT ?= dev
label-init:
	$(PY) -m cra_extract.label init --split $(SPLIT) --out data/gold/labels_$(SPLIT).csv

label-validate:
	$(PY) -m cra_extract.label validate data/gold/labels_$(SPLIT).csv

label-hash:
	$(PY) -m cra_extract.label hash --split $(SPLIT) --labels data/gold/labels_$(SPLIT).csv

# Extraction systems over a split: rules (S1), baseline (S0) and llm:<backend>:<model> entries.
# LLM calls replay from data/traces/{split}/ by default; add RUN_FLAGS=--allow-api for live calls.
SYSTEMS ?= rules,baseline,llm:gemini:gemini-3.8-flash
RUN_FLAGS ?=
pilot:
	$(PY) -m cra_extract.run --split dev --systems $(SYSTEMS) $(RUN_FLAGS)

eval-dev:
	$(PY) -m cra_extract.evaluate --split dev

freeze:
	$(PY) -m cra_extract.freeze --tag plan-v1.0

eval-test:
	$(PY) -m cra_extract.evaluate --split test

scale:
	$(PY) -m cra_extract.run --split corpus --systems $(SYSTEMS) --parse-missing $(RUN_FLAGS)

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
