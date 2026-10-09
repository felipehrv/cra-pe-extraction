PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: env index index-replay fetch parse overrides overrides-check locate locator-check-draw locator-check-sheet locator-check-score templates rules baseline tuning-docs census-draw census-sheet census-validate census-score census-double census-compare group-a smoke-llm draw-dev draw-test draw-audit strata-show label-init label-validate label-hash pilot project dev-rerun eval-dev freeze eval-test eval-pair ablation closed-book scale s5-routing s5-routing-provisional s5-apply s5-test group-a-corpus risk-corpus audit-sheet audit reliability-draw reliability-sheet reliability-compare report reproduce test lint

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

# Documented manifest overrides and exclusions (config/manifest_overrides.yaml). Pipeline order:
# index, fetch, parse, overrides. `make index` or `make index-replay` rebuilds the manifest with
# the original doc_ids and empty split and sha256 cells, so fetch, parse and overrides must
# follow it every time. apply refuses a non-evaluable row left outside split excluded.
overrides:
	$(PY) -m cra_extract.overrides apply --manifest config/corpus.csv

overrides-check:
	$(PY) -m cra_extract.overrides check

locate:
	$(PY) -m cra_extract.locate --manifest config/corpus.csv --report results/locator_coverage.csv

# Locator hand check (design 6.10, plan "Locator recall"): 100 corpus documents, one FRB PE per
# Reserve Bank district (district approximated by the bank's state), no document of a test or
# audit bank and no tuning document, drawn once into config/locator_check_sample.yaml (the draw
# refuses to replace a record without --force). The plan names no seed: LOCATOR_SEED and
# LOCATOR_DATE are required. locator-check-sheet writes data/gold/locator_check_sheet.csv (refuses
# to overwrite reader entries); the reader fills it from the PDFs; locator-check-score writes
# results/locator_recall.csv and results/locator_recall_misses.csv.
locator-check-draw:
	@test -n "$(LOCATOR_SEED)" -a -n "$(LOCATOR_DATE)" || (echo "set LOCATOR_SEED=<seed> LOCATOR_DATE=YYYY-MM-DD" && exit 2)
	$(PY) -m cra_extract.locator_check draw --seed $(LOCATOR_SEED) --date $(LOCATOR_DATE)

locator-check-sheet:
	$(PY) -m cra_extract.locator_check sheet

locator-check-score:
	$(PY) -m cra_extract.locator_check score

templates:
	$(PY) -m cra_extract.templates --manifest config/corpus.csv --out results/census.csv

rules:
	$(PY) -m cra_extract.rules --manifest config/corpus.csv --out results/rules.csv

baseline:
	$(PY) -m cra_extract.baseline --manifest config/corpus.csv --out results/baseline.csv

# Corpus documents read while tuning (config/tuning_documents.yaml); their banks leave the test
# and audit pools. Lists cited documents missing from the file; TUNING_FLAGS=--write adds them.
TUNING_FLAGS ?=
tuning-docs:
	$(PY) -m cra_extract.strata tuning-docs $(TUNING_FLAGS)

# Group C census (design 5.2, plan "Group C census"). The random sample (and, with 100 or more
# locator misses, the sample of 50 misses) is drawn once into config/census_sample.yaml. The
# plan names no census seed yet (docs/plan_gaps.md): CENSUS_SEED and CENSUS_DATE are required.
# The draw refuses before draw-test unless CENSUS_FLAGS=--before-test-draw; census-sheet refuses
# more than 300 sections to read (CENSUS_SHEET_FLAGS=--fallback-cap for 200) and to overwrite
# a sheet that already holds labels.
CENSUS_FLAGS ?=
CENSUS_SHEET_FLAGS ?=
census-draw:
	@test -n "$(CENSUS_SEED)" -a -n "$(CENSUS_DATE)" || (echo "set CENSUS_SEED=<seed> CENSUS_DATE=YYYY-MM-DD" && exit 2)
	$(PY) -m cra_extract.census draw --seed $(CENSUS_SEED) --date $(CENSUS_DATE) $(CENSUS_FLAGS)

census-sheet:
	$(PY) -m cra_extract.census sheet --out data/gold/census_sheet.csv $(CENSUS_SHEET_FLAGS)

census-validate:
	$(PY) -m cra_extract.census validate data/gold/census_sheet.csv

census-score:
	$(PY) -m cra_extract.census score --sheet data/gold/census_sheet.csv

# Group C reliability (plan "Reliability"): every census positive plus 20 template negatives,
# a blind second sheet, then kappa per field (results/census_reliability.csv).
census-double:
	@test -n "$(DOUBLE_SEED)" -a -n "$(DOUBLE_DATE)" || (echo "set DOUBLE_SEED=<seed> DOUBLE_DATE=YYYY-MM-DD" && exit 2)
	$(PY) -m cra_extract.census double --seed $(DOUBLE_SEED) --date $(DOUBLE_DATE)

census-compare:
	$(PY) -m cra_extract.census double --compare data/gold/census_double.csv

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

# Split draws (dev < test < audit; a redraw with the same seed is byte-identical). Seeds of the
# plan: dev 20261007 (drawn 2026-10-07), test 20261013, audit 20261016. draw-test and
# draw-audit need DRAW_DATE (no default); both refuse while a non-evaluable row is not excluded
# or a cited tuning document is missing from config/tuning_documents.yaml.
DEV_SEED ?= 20261007
DEV_DRAW_DATE ?= 2026-10-07
TEST_SEED ?= 20261013
AUDIT_SEED ?= 20261016
DRAW_DATE ?=
draw-dev:
	$(PY) -m cra_extract.strata draw-dev --seed $(DEV_SEED) --date $(DEV_DRAW_DATE)

# Run after `make scale`: the per-document n_aa_sections column of results/sections_corpus.csv
# is the multi-AA count. With the 25-document fallback add TEST_FLAGS="--n-test 25 --per-cell 2".
AA_COUNTS ?= results/sections_corpus.csv
TEST_FLAGS ?=
draw-test:
	@test -n "$(DRAW_DATE)" || (echo "set DRAW_DATE=YYYY-MM-DD (the date of the draw)" && exit 2)
	$(PY) -m cra_extract.strata draw-test --seed $(TEST_SEED) --date $(DRAW_DATE) --aa-counts $(AA_COUNTS) $(TEST_FLAGS)

RISK ?= results/risk_corpus.csv
draw-audit:
	@test -n "$(DRAW_DATE)" || (echo "set DRAW_DATE=YYYY-MM-DD (the date of the draw)" && exit 2)
	$(PY) -m cra_extract.strata draw-audit --risk $(RISK) --seed $(AUDIT_SEED) --date $(DRAW_DATE)

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
# S3 corpus tier (analysis plan, Decisions item 2), used by the ablation, the closed-book
# baseline, S5 routing, the pair run and the corpus scoring targets.
S3_CORPUS ?= llm:gemini:gemini-3.8-flash
# LLM calls per document (run --field-groups): all = one call for every field (default, sized
# for the free-tier daily request cap), split = one call per llm_field_groups entry of
# config/fields.yaml (four). Closed book is one call either way and takes no flag.
FIELD_GROUPS ?= all
RUN_FLAGS ?=
pilot:
	$(PY) -m cra_extract.run --split dev --systems $(SYSTEMS) --field-groups $(FIELD_GROUPS) $(RUN_FLAGS)

# Cost projection without calls (chars / 4 of every prompt): PROJECT_FLAGS=--whole-document for
# the ablation estimate, --closed-book for the closed-book baseline.
PROJECT_FLAGS ?=
project:
	$(PY) -m cra_extract.run --split $(SPLIT) --systems $(SYSTEMS) --field-groups $(FIELD_GROUPS) --project $(PROJECT_FLAGS)

# Second live dev run for run-to-run agreement (plan, Leakage): its own trace and results
# directories, so replay never serves the first run's traces; then agreement per field and tier.
DEV_RERUN_SYSTEMS ?= $(S3_CORPUS)
dev-rerun:
	$(PY) -m cra_extract.run --split dev --systems $(DEV_RERUN_SYSTEMS) --field-groups $(FIELD_GROUPS) --trace-dir data/traces/dev_rerun --results-dir results/dev_rerun --allow-api
	$(PY) -m cra_extract.run agreement results/predictions_dev.csv results/dev_rerun/predictions_dev.csv --out results/run_agreement_dev.csv

eval-dev:
	$(PY) -m cra_extract.evaluate --split dev

freeze:
	$(PY) -m cra_extract.freeze --tag plan-v1.0

# Before the single test scoring run: the ablation (if Decision 3 says yes), the closed-book
# baseline and the S5 rows (make ablation closed-book s5-test), all into predictions_test.csv.
eval-test:
	$(PY) -m cra_extract.evaluate --split test

# Calibration input of the plan (Risk score, flag 2): the disagreement flag restricted to the
# pair S1 and the S3 corpus tier, written apart from the main evaluate outputs.
PAIR_SPLIT ?= test
PAIR_FLAGS ?= --replay-only
eval-pair:
	$(PY) -m cra_extract.evaluate --split $(PAIR_SPLIT) $(PAIR_FLAGS) --systems rules,$(S3_CORPUS) --out-dir results/pair

# Whole-document ablation on the first 20 test doc_ids by sha256 (plan, Decisions item 3) and the
# closed-book contamination baseline (plan, Leakage); both use the S3 corpus tier.
ablation:
	$(PY) -m cra_extract.run --split test --systems $(S3_CORPUS) --field-groups $(FIELD_GROUPS) --whole-document --hash-sample 20 $(RUN_FLAGS)

closed-book:
	$(PY) -m cra_extract.run --split test --systems $(S3_CORPUS) --closed-book $(RUN_FLAGS)

scale:
	$(PY) -m cra_extract.run --split corpus --systems $(SYSTEMS) --field-groups $(FIELD_GROUPS) --parse-missing $(RUN_FLAGS)

# S5 routing (analysis plan, Decisions item 1): after the test draw and before the freeze, with
# S1 at the frozen RULES_VERSION (make rules). Writes results/s5_routing.{csv,md} and
# config/s5_routing.yaml; refuses before the test draw. s5-routing-provisional writes a table
# under results/provisional/ only, never config/. S3_CORPUS is the tier of Decisions item 2.
S1_OUT ?= results/rules.csv
s5-routing:
	$(PY) -m cra_extract.routing compute --predictions $(S1_OUT) --s3 $(S3_CORPUS)

s5-routing-provisional:
	$(PY) -m cra_extract.routing compute --predictions $(S1_OUT) --s3 $(S3_CORPUS) --provisional

# S5 rows from the corpus predictions (S1 and S3 corpus tier) and config/s5_routing.yaml.
s5-apply:
	$(PY) -m cra_extract.routing apply --predictions results/predictions_corpus.csv --out results/predictions_corpus_s5.csv

# S5 rows of the test documents appended to predictions_test.csv before the single scoring run;
# S1-sourced values are marked not graded for groundedness (grounding_failure na).
s5-test:
	$(PY) -m cra_extract.routing apply --predictions results/predictions_test.csv --append-to results/predictions_test.csv

# Group A at corpus scale (S1, S3 corpus tier, S5) on every evaluable document; S5 rows are
# built from config/s5_routing.yaml when the predictions carry none.
CORPUS_PREDICTIONS ?= results/predictions_corpus.csv $(wildcard results/predictions_dev.csv results/predictions_test.csv)
group-a-corpus:
	$(PY) -m cra_extract.corpus_scoring score --predictions $(CORPUS_PREDICTIONS) --s3 $(S3_CORPUS)

# Document risk for the audit draw (results/risk_corpus.csv) and the Group A flag validation.
risk-corpus:
	$(PY) -m cra_extract.corpus_scoring risk --predictions results/predictions_corpus.csv --s3 $(S3_CORPUS) --sections results/sections_corpus.csv

# Audit after `make draw-audit`: write the blinded sheet once, label it, then estimate.
# AUDIT_SYSTEM is the released system, S5 (rows from make s5-apply), so the n_flags of the
# audited values are the ones in results/risk_values_corpus.csv that drew the tertiles;
# estimate refuses when a document's n_flags sum differs from its doc_risk.
AUDIT_SYSTEM ?= s5
AUDIT_PREDICTIONS ?= results/predictions_corpus.csv results/predictions_corpus_s5.csv
audit-sheet:
	$(PY) -m cra_extract.audit sheet --system $(AUDIT_SYSTEM) --predictions $(AUDIT_PREDICTIONS)

audit:
	$(PY) -m cra_extract.audit estimate --system $(AUDIT_SYSTEM) --predictions $(AUDIT_PREDICTIONS)

# Group B reliability (plan "Reliability"): REL_BRANCH=outside (8 test documents, outside coder)
# or fallback (6 dev documents, author re-codes from 2026-10-13); writes results/reliability.csv,
# the manual-review error line of Figure 1.
REL_BRANCH ?= outside
reliability-draw:
	@test -n "$(REL_SEED)" -a -n "$(REL_DATE)" || (echo "set REL_SEED=<seed> REL_DATE=YYYY-MM-DD" && exit 2)
	$(PY) -m cra_extract.reliability draw --branch $(REL_BRANCH) --seed $(REL_SEED) --date $(REL_DATE)

reliability-sheet:
	$(PY) -m cra_extract.reliability sheet

reliability-compare:
	$(PY) -m cra_extract.reliability compare

# Tables and figures from results/ (Table 1, Figure 1, Tables 2 and 3, section 7 tables).
# CORPUS_SYSTEM names the corpus tier whose cost enters the review operating points.
REPORT_SPLIT ?= test
CORPUS_SYSTEM ?=
report:
	$(PY) -m cra_extract.report --split $(REPORT_SPLIT) $(if $(CORPUS_SYSTEM),--corpus-system $(CORPUS_SYSTEM),)

reproduce:
	$(PY) -m cra_extract.evaluate --split test --replay-only
	$(PY) -m cra_extract.evaluate --split test --replay-only --systems rules,$(S3_CORPUS) --out-dir results/pair
	$(PY) -m cra_extract.report --check results/table1_test.csv

test:
	$(PY) -m pytest -m "not network"

lint:
	.venv/bin/ruff check src tests
