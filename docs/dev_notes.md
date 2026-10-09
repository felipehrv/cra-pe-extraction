# Developer notes

Working notes for the pipeline as integrated on 2026-10-07. The specification is
`docs/design.md`; this file records how to run each stage, the two conventions every
module shares (character offsets and traces), and the list of things the author still has
to verify by hand before the gold set is labeled.

## 1. Running the stages

Every stage is a module with a `main(argv) -> int` and can be run three ways: through the
Makefile, as `python -m cra_extract.<stage>`, or through the `cra` console script
(`cra <stage> [options]`, defined in `src/cra_extract/cli.py` and `pyproject.toml`).
`cra <stage> --help` prints the options of a stage. Stage order:

| Stage | Make target | Reads | Writes |
|---|---|---|---|
| index | `make index` (live) or `make index-replay` (saved raw pull, `RAW_STAMP=20261007`) | agency APIs, or `data/index/raw/*_{stamp}.*` | `data/index/{occ,fdic,frb,frb_state_rows}.csv`, `config/corpus.csv`, `data/index/manifest_exclusions.csv`, `results/corpus_counts.csv` |
| fetch | `make fetch` | `config/corpus.csv` | `data/pdfs/{doc_id}.pdf`, sha256 and pages in the manifest, `data/index/fetch_failures.csv` |
| parse | `make parse` | `data/pdfs/` | `data/text/{doc_id}.pages.json`, `data/text/{doc_id}.txt`, pages, chars_per_page, text_extractable in the manifest |
| overrides | `make overrides`, `make overrides-check` | `config/manifest_overrides.yaml`, `config/corpus.csv`, `data/pdfs/`, `data/text/` | `config/corpus.csv` (corrected cells, renamed doc_ids, split `excluded`, `exam_date_source`), renamed and replaced data files |
| locate | `make locate` | `data/text/` | `results/locator_coverage.csv` |
| templates | `make templates` | `data/text/` | `results/census.csv` (Group C template-residual census) |
| rules | `make rules` | `data/text/` | `results/rules.csv` (system S1) |
| baseline | `make baseline` | `data/text/` | `results/baseline.csv` (system S0) |
| census | `make census-draw CENSUS_SEED=.. CENSUS_DATE=..`, `make census-sheet`, `make census-validate`, `make census-score`, `make census-double DOUBLE_SEED=.. DOUBLE_DATE=..`, `make census-compare` | `results/census.csv`, `config/corpus.csv`, `results/baseline.csv`, `results/predictions_corpus.csv` (when present, `llm:` and `s5` rows) | `config/census_sample.yaml`, `data/gold/census_sheet.csv`, `data/gold/census_double.csv`, `results/census_summary.csv`, `results/census_positives.csv`, `results/census_reliability.csv` |
| group-a | `make group-a` | `results/rules.csv`, `config/corpus.csv`, `config/group_a_triage.yaml` (doc_ids resolved through the override aliases) | `results/group_a_agreement.csv`, `group_a_triage.csv`, `group_a_triage_summary.csv`, `fdic_exam_date_lag.csv`, `group_a_abstention_sample.csv` |
| strata | `make draw-dev`, `make draw-test DRAW_DATE=..` (seed 20261013), `make draw-audit DRAW_DATE=..` (seed 20261016), `make strata-show`, `make tuning-docs` | `config/corpus.csv`, `data/index/frb.csv` (dev), the aa-counts CSV (test), `results/risk_corpus.csv` (audit), `config/tuning_documents.yaml` (test, audit) | `split` column of `config/corpus.csv`, `config/strata.yaml` |
| label | `make label-init SPLIT=dev`, `make label-validate SPLIT=dev`, `make label-hash SPLIT=test` | `config/corpus.csv`, `data/gold/labels_{split}.csv` | the label sheet, validation problems, `docs/hashes.md` |
| llm | `make smoke-llm BACKEND=anthropic MODEL=claude-haiku-4-5` | one sample PE, API key from `.env` | `data/traces/smoke/{backend}.jsonl` |
| run | `make pilot` (dev), `make scale` (corpus), `make project` (cost projection, no calls), `make dev-rerun` (second live dev run and agreement), `make ablation` and `make closed-book` (test), `RUN_FLAGS=--allow-api` for live calls | `config/corpus.csv`, `data/text/`, `data/traces/{split}/` | `results/predictions_{split}.csv`, `results/sections_{split}.csv`, `results/run_log_{split}.jsonl`, `results/cost_projection_{split}.csv`, `results/run_agreement_dev.csv`, new traces (with `Trace.mode` and `Trace.effort`) |
| routing | `make s5-routing` (after draw-test), `make s5-routing-provisional`, `make s5-apply` (corpus), `make s5-test` (test, before eval-test) | `results/rules.csv` or a predictions file, `config/corpus.csv`, `config/s5_routing.yaml` | `results/s5_routing.{csv,md}`, `config/s5_routing.yaml` (provisional tables under `results/provisional/`), `results/predictions_corpus_s5.csv`, S5 rows appended to `results/predictions_test.csv` |
| corpus-scoring | `make group-a-corpus`, `make risk-corpus` | predictions files, `config/corpus.csv`, `results/group_a_triage.csv`, `results/sections_corpus.csv`, `config/s5_routing.yaml`, `data/gold/labels_{dev,test}.csv` (FDIC exam_date gate only) | `results/group_a_corpus.csv`, `results/fdic_exam_date_gate.csv`, `results/risk_corpus.csv`, `results/risk_values_corpus.csv`, `results/risk_validation_group_a.csv` |
| evaluate | `make eval-dev`, `make eval-test`, `make eval-pair` (S1 and S3 pair for the calibration input) | `data/gold/labels_{split}.csv`, `results/predictions_{split}.csv`, `data/gold/text/` | `results/table1_{split}.csv`, `errors_{split}.csv`, `risk_{split}.csv`, `calibration_{split}.csv`, `scored_{split}.csv`; the pair run writes the same names under `results/pair/` |
| audit | `make audit-sheet`, `cra audit validate`, `make audit` (`AUDIT_SYSTEM=s5`) | `config/strata.yaml` (audit draw), `results/predictions_corpus.csv`, `results/predictions_corpus_s5.csv`, `data/gold/audit_sheet.csv`, `results/risk_values_corpus.csv`, `results/risk_corpus.csv` | `data/gold/audit_sheet.csv`, `results/audit_summary.csv`, `audit_by_field.csv`, `audit_by_tertile.csv`, `review_curve_group_b.csv` |
| reliability | `make reliability-draw REL_BRANCH=outside REL_SEED=.. REL_DATE=..`, `make reliability-sheet`, `make reliability-compare` | `config/corpus.csv`, `data/gold/labels_{test,dev}.csv`, `data/gold/reliability_sheet.csv` | `config/reliability_sample.yaml`, `data/gold/reliability_sheet.csv`, `results/reliability.csv` |
| freeze | `make freeze` (`cra freeze --check-only` to rehearse) | `docs/analysis_plan.md`, `docs/hashes.md`, `config/corpus.csv`, git tree | Freeze record in `docs/hashes.md` (RULES_VERSION, PROMPT_VERSION, CLOSED_BOOK_PROMPT_VERSION, effort per backend, config hashes), commit, annotated tag `plan-v1.0` |
| report | `make report` (`REPORT_SPLIT=test`, `CORPUS_SYSTEM=..`), `make reproduce` | `results/*` (Table 1 inputs, `pair/calibration_{split}.csv`, review curves, `reliability.csv`, census and audit outputs), `config/corpus.csv`, `data/index/frb.csv`, `data/gold/labels_{dev,test}.csv`, `data/gold/audit_sheet.csv`, `data/traces/{split}/`, `config/prices.yaml` | `table1_{split}_report.csv`, `table1_{split}.md`, `table_probes_{split}.*`, `table1_{split}_appendix.csv`, `table_grounding_{split}.csv`, `table_ablation_{split}.*`, `table_closed_book_{split}.*`, `figure1.*`, `table2.*`, `table3_{split}*`, descriptive tables |

Order before the freeze: index, fetch, parse, overrides, locate, templates, rules, baseline,
group-a, tuning-docs, scale (run on the corpus), draw-test, s5-routing, census-draw, then the
plan TODOs and `make freeze`. After the freeze: label test, run test once (pilot systems,
ablation, closed-book, s5-test), eval-test, eval-pair, risk-corpus, draw-audit, audit,
reliability, report.

Notes on running:

- `locate`, `templates`, `rules` and `baseline` accept `--samples-dir tests/fixtures/samples`
  to run on the eight pdftotext fixtures without `make parse`; with the default manifest they
  exit 1 until `data/text/` is populated.
- `fetch` returns exit 1 when any row failed; failures are appended to
  `data/index/fetch_failures.csv`. `--limit N` counts rows that needed network work, not the
  first N rows. A PDF whose sha256 differs from the manifest is logged as `sha256_mismatch`
  and left alone; accepting a regulator-side replacement means clearing the sha256 cell.
- `evaluate --split test` is guarded: the git tag `plan-v1.0` must exist (or
  `CRA_ALLOW_TEST_EVAL=1` for CI on traces), every run is appended to
  `results/test_invocations.log`, and a second scoring run needs a dated entry in
  `docs/deviations.md`. `--replay-only` regenerates tables without counting as a run.
- `run` writes `results/predictions_{split}.csv` with the `FieldValue` columns plus
  `grounding_failure` (empty means grounded; any other value must be one of the three
  categories or `evaluate.read_predictions` refuses the file) and `locator_miss` (0 or 1).
  Pairs already in the file are skipped; `--force` recomputes the selected pairs; `--limit N`
  counts documents that still had work. An `llm.DailyLimitExceeded` from a backend
  propagates out of `llm.extract` and halts the run (`summary.halted`); a truncated or
  refused response is recorded with `Trace.error` set to the stop reason and abstentions
  noted `truncated`.
- `strata` draws the splits in order dev, test, audit (`draw-dev` needs `data/index/frb.csv`
  for the district rule, `draw-test` the aa-counts CSV from the run stage, `draw-audit` a
  risk file from evaluate). A split's pool excludes the splits drawn before it, so a redraw
  with the same seed is byte-identical.
- `freeze` refuses while `docs/analysis_plan.md` has `TODO` markers, the tree is dirty, the
  test split is undrawn or its doc_id hash is not in `docs/hashes.md`; on success it commits
  the Freeze record and creates the annotated tag (`--no-commit` tags the prior HEAD).
- `draw-test` and `draw-audit` refuse while a non-evaluable row (no sha256 or
  `text_extractable` not true) is outside split `excluded`, or a doc_id cited in
  `config/headings.yaml` or `config/review_templates.yaml` is missing from
  `config/tuning_documents.yaml`; the banks of the tuning documents leave the test and audit
  pools. `routing compute` and `census draw` refuse before the test draw unless
  `--provisional` (routing, output under `results/provisional/`) or `--before-test-draw`
  (census) is passed.
- Open plan decisions the code needed are listed with proposed wording in
  `docs/plan_gaps.md`.
- Tests: `make test` (`pytest -m "not network"`) and `make lint`. Live-service tests are
  marked `network` and run only with `CRA_NETWORK_TESTS=1` (index fetchers) or
  `CRA_LIVE_LLM=1` (LLM smoke test, backend chosen by `CRA_SMOKE_BACKEND` and
  `CRA_SMOKE_MODEL`).

## 2. Offset convention

All character offsets (`Section.char_start`, `Section.char_end`, `FieldValue.char_start`,
`FieldValue.char_end`, `Trace.section_offsets`) index one string per document, the document
text:

    doc_text(pages) == "\f".join(page.text for page in pages)

defined once in `parse.doc_text` and imported by `locate` (and through it by `rules`,
`baseline` and `templates`). It is byte-identical to `data/text/{doc_id}.txt`, which is
written with `newline=""` so no line-ending translation occurs. Page k (1-based) starts at
`sum(len(text) + 1 for every earlier page)`; the form feed between pages belongs to the
page before it. `parse.page_offsets` and `parse.page_of_offset` implement this and the
round trip is tested on all eight samples. Page text is stored verbatim (curly quotes and
bullet glyphs included); matching runs on a straightened copy and quotes are compared after
whitespace collapsing. The `locate.pages_from_text` helper splits pdftotext output on the
same form feed, so fixtures and parsed documents share the convention.

Line citations in `docs/labeling_guide.md` and `tests/fixtures/expected_samples.yaml`
count lines on newlines only (editor or `sed -n 'N p'` numbering); Python `splitlines()`
also splits on the form feed and drifts by one line per page.

## 3. Trace layout

Every LLM call is one `models.Trace` appended as a JSON line to
`data/traces/{split}/{backend}.jsonl` (`llm.trace_path`), where `backend` is `gemini`,
`openai_compat`, `ollama` or `anthropic`. Fields: `trace_id` (first 16 hex chars of the
sha256 of doc_id, field_group, model and prompt_hash), `doc_id`, `field_group`, `backend`,
`model`, `model_reported`, `prompt_hash` (sha256 over `prompts.PROMPT_VERSION`, the system
prompt and the full user prompt, which embeds the located section text), `section_offsets`
(document-text spans of the sections sent), `response_json`, `response_text`,
`input_tokens`, `output_tokens`, `cost_usd` (from `config/prices.yaml`, 0.0 when
unpriced), `latency_s`, `called_at` and `error`. A failed call is recorded with `error`
set and every field of the group abstains with `notes="call_failed"`.

`llm.ReplayBackend` serves traces keyed by `(model, prompt_hash)` from the same file and
raises `TraceMissError` on a miss unless `--allow-api` is set. Any change to `prompts.py`,
to the located sections or to `PROMPT_VERSION` invalidates every stored trace; bump the
version deliberately and re-run the dev split with `--allow-api`. Smoke runs go to the
`smoke` split so the `dev` files contain only pilot calls. The design plans
`data/traces/corpus.jsonl.gz` for the corpus pass; `append_trace` writes uncompressed JSONL
only, and `response_text` holds the full model output, so that file is what needs gzip.

## 4. Integration edits (2026-10-07)

- `src/cra_extract/cli.py` (new): argparse dispatcher for `cra`, lazy module imports.
- `src/cra_extract/locate.py`: `doc_text` and the form feed are now imported from
  `parse` instead of being redefined; `rules`, `baseline` and the tests keep importing
  `locate.doc_text`.
- `src/cra_extract/config.py`: `functools.cache` instead of `lru_cache(maxsize=None)`
  (ruff UP033, the only lint failure of the merged tree).
- `Makefile`: targets `index-replay`, `templates`, `rules`, `baseline`, `smoke-llm`,
  `label-init`, `label-validate`, `label-hash`.
- `docs/design.md` 5.1: Wilson interval for 29/30 corrected to [83.3, 99.4]
  (`stats.wilson_ci(29, 30)` gives 0.8333).
- `docs/labeling_guide.md` field 11: Liberty Bank's description does print the rounded
  "$12.6 million" (LB:100-101); the sentence saying it has no figure was reworded.

Proposals not applied, and why: adding `prior_evaluation_date_index` and
`prior_rating_index` to `DocRecord` (pydantic ignores unknown manifest columns and design
section 2 already lists fewer columns than the manifest carries; decide with the design
text); writing blank split cells as `corpus` (the current manifest has no blank cells, all
2,793 rows read `corpus`); explicit `tolerance` keys on Group C enums and `laws_cited` in
`fields.yaml` (no behaviour change); `jsonschema` in the dev extra (not needed by the
tests); `CRA_ALLOW_TEST_EVAL` in `ci.yml` (CI does not run `evaluate` yet); filling Gemini
prices (the author must look them up); the design-text additions about feed anomalies and
the primary-product data window (author decisions, listed below).

## 5. Things the author must verify

Corpus and index

- Live corpus is 2,793 documents (FDIC 1,770, OCC 605, FRB 418); the design estimate was
  2,600 to 2,800. `results/corpus_counts.csv` has the regulator x method x year table.
- Manifest row `FDIC_445_20280408` (Mercer County State Bank) carried an impossible exam
  date from the FDIC file id `2024/445_280408.PDF`. Corrected on 2026-10-08 by
  `config/manifest_overrides.yaml`: exam_date 2024-04-08 (cover p1, Scope p7) and doc_id
  regenerated to `FDIC_445_20240408` (alias kept, `exam_date_source` override). The 8 FRB
  rows (2006 to 2017) whose exam date is after the public date stay in the data-quality table.
- FDIC cert 22081 (Magnolia State Bank, file id `2022/PPE22081-000000008.PDF`) was dropped
  for having no parseable exam date; it only affects the prior lookup.
- The OCC API returns no release date for 642 rows with 2018 to 2019 exams and 3 rows with
  2023 exams (charters 14510, 25088, 707506); FRB rows with "Not available" public dates are
  index-only. Both only affect the prior lookup and the exclusion log.
- Prior-exam coverage from the indexes: OCC 96.0, FRB 89.7, FDIC 88.0 percent of manifest
  rows; the rest need gold for `prior_evaluation_date` and `prior_rating`.
- FDIC component ratings from CRAPES are kept only as `fdic_*_rating_raw`; One American
  Bank confirms the scale error (feed says Low Satisfactory, PE says Needs to Improve at
  OA:57).
- Two sample PEs with 2022 exam dates (SouthStar, Liberty) are manifest rows because their
  release dates fall inside the window.
- `data/index/raw/` still holds the interrupted 2026-10-05 pulls next to the complete
  2026-10-07 pull; nothing was deleted.
- Decide whether `DocRecord` should declare `prior_evaluation_date_index` and
  `prior_rating_index` (see section 4).

Fetch and parse

- Page counts of the eight samples match pymupdf (GN Bank 17, Lemont 20, First Neighbor
  26, SouthStar 45, Liberty 14, One American 23, FieldPoint 95, Small Business Bank 21);
  mean chars per page 1,871 to 2,218, minimum page 133 chars, so all eight are
  text-extractable under the 200 and 50 thresholds. Revisit the thresholds if scanned PEs
  appear (design section 2 sets the OCR decision at 2 percent).
- The rate limiter is per process; do not run two `fetch` processes at once.

Locator and templates

- Expected pages and snippets in `tests/test_locate.py` were hand-derived; spot-check the
  citations (SouthStar L56 and L599, Liberty L353, One American L658, FieldPoint L96-100,
  L579-583, L592, L637, L1210, L1731, L2200, L2590, Small Business Bank L67 and L373,
  Lemont L50, GN Bank L54, First Neighbor L60).
- OCC `description_of_assessment_area` is reported missing on all three OCC samples
  because the heading only appears under State Rating; decide whether to locate it inside
  state scope (confidence 0.5) for single-state OCC banks.
- The furniture-trimming rule treats a column-gap line among the first three non-blank
  lines of a page as a running header; a PE whose section table continues at the very top
  of a page with no header could lose up to three rows. Check on the 100-document locator
  run (design 6.10).
- Only the New York and Kansas City FRB templates are seeded; pull one PE per remaining
  district and add entries with the district field. FieldPoint's residual is the bank name
  ("fieldpoint", 10 chars) by construction.
- FRB Small applicability cells (lending_test_rating and cd_test_rating absent) rest on one
  Kansas City document (SB:54-64); confirm on the 12-district pilot.

Rules and baseline

- Verify every line citation in `tests/fixtures/expected_samples.yaml` against the .txt
  files; the tests pass against the fixture, so an error there would pass silently.
- Known abstentions: `n_assessment_areas` on Lemont (count only in the State of Illinois
  section, LM:222, and Appendix A); `inside_aa_share_number_pct` and
  `inside_aa_share_product` on SouthStar (no Total row, SS:363-368; gold is the home
  mortgage subtotal 75.2 at SS:367). Decide whether the rules should read Appendix A and
  primary-product subtotals.
- Genuinely missing sentences (First Neighbor `prior_evaluation_date`, `prior_rating`,
  `total_assets_asof`) give abstain, not absent, on purpose.
- FDIC total assets come from the LTD table's "Total Assets as of" column (SS:340,
  LB:300) in preference to the rounded description figure; OCC and FRB use the description
  sentence only. Confirm this matches the guide.
- Lemont prints 54.5 percent inside the AA in the sentence (LM:99) and 54.4 in the table
  (LM:114); the rule and the guide follow the sentence. Candidate data-quality row.
- Rules 0.2.0 (2026-10-08) come from the corpus-wide Group A comparison with the index
  (design 6.3): `python -m cra_extract.group_a --rules results/rules_0.2.0_head_locator.csv
  --before results/rules_0.1.0.csv` writes `results/group_a_agreement.csv`,
  `group_a_triage.csv` (every disagreement of either version with category and evidence),
  `group_a_triage_summary.csv`, `group_a_abstention_sample.csv` and
  `fdic_exam_date_lag.csv`; hand triage lives in `config/group_a_triage.yaml` and the script
  exits 1 while any disagreement is untriaged. `exam_method` now reads the first Scope
  sentence that names the procedures (abbreviations, negation, prior-evaluation sentences
  and mid-period transitions handled; a transition abstains), lets an FDIC or FRB CD Test
  rating override a Small Bank Scope sentence, and falls back to the CD Test sentence, the
  FDIC appendix heading and the OCC Lending-Test-only rating page.
- Rules 0.2.1 (2026-10-08): the regulator rule reads the cover line after `normalize_line`
  (FRB Chicago U+00A0 covers); otherwise the version records the locator and headings
  tuning of the same day. `results/rules.csv` and `results/group_a_*.csv` now hold 0.2.1 on
  the pass-2 locator (`make rules`, `make group-a`); the 0.2.0 attribution run on the HEAD
  locator stays in `results/rules_0.2.0_head_locator.csv`. Before and after tables of the
  second corpus pass are in `docs/corpus_pass_2.md`.
- Decide which exam date the manifest carries for FDIC. The file-id date (YYMMDD in
  `SYS_EXTRN_FILE_ID`) equals the printed cover date for 1,641 of 1,770 FDIC PEs (92.7
  percent); where it differs it is later by a median of 5 days (97 within a week, 15 more
  than 90 days, 9 of those on the first of a month, one impossible 2028 date). The
  recommendation is to carry the printed evaluation date (cover, cross-checked with the
  Scope "current evaluation dated" sentence) as `exam_date` and keep the file-id date as a
  separate provenance column; doc_ids stay as they are (one exception: FDIC_445, renamed by
  the manifest overrides because its file-id date was impossible). 25 FDIC PEs print a Scope date that
  differs from their own cover (9 by exactly one year); the rules note it.
- OCC_9183_20230213: the OCC index row (2023-02-13 exam) links `/apr19/9183.pdf`, the PE of
  the December 3, 2018 exam, so every value read from that document describes 2018. Excluded
  on 2026-10-08 (`config/manifest_overrides.yaml`, reason wrong_pdf_link).
- Baseline false positives on boilerplate are zero on the samples because negated hits are
  absorbed; the corpus-wide negated-hit count per regulator (`n_negated_hits` in
  `results/baseline.csv`) is what the OCC-separate false-positive rate should use.
- `tests/test_rules.py` contains two literal U+F0B7 bullet glyphs inside string literals;
  `rules.py` builds its bullet class with `chr()` so the source stays ASCII.

Labeling

- `init_sheet` prefills `lending_test_rating` for OCC Small documents from the manifest
  `overall_rating` (GN:52-54, LM:48-50); confirm you want it prefilled.
- Hand-derived guide values to confirm before dev labeling: FN total_assets 593700000 with
  as-of absent (FN:121, FN:135-136 is another paragraph); SS total_assets 1159094000 as of
  2022-06-30 (SS:340) instead of "approximately $1.2 billion" (SS:123); SS inside_aa_share
  75.2, product home_mortgage (SS:367); LB inside_aa_share 0.0 (LB:325) coded as a value;
  FP n_assessment_areas 4 (FP:213-219 excludes the Miami MD); GN primary_product unstated
  (GN:162-163); LB practice_basis absent (LB:352-355); LB rating_effect
  substantive_not_lowered (LB:358-360).
- When products have different data windows the guide codes the primary product's window
  (SS:264-266 versus SS:273-274); neither design section 3 row 12 nor `fields.yaml` says
  which window wins. Decide and record it.
- `distractor_laws_named` is true for the Kansas City template (SB:531-535) and false for
  New York (FP:581-583).
- The `matches_aa_section_count` consistency rule is not checked by the sheet validator and
  never flags in `evaluate.consistency_fail` because the section count is not in the
  predictions file; the `run` stage should write it, or `risk_table` should take a
  per-document count.
- `config/corpus.csv` carries the dev split (seed 20261007, `config/strata.yaml`); test and
  audit are undrawn until `make draw-test` and `make draw-audit` run.

LLM

- No live call has been made yet. Run the smoke CLI once per backend before the
  three-tier run (`make smoke-llm BACKEND=anthropic MODEL=claude-haiku-4-5`, then
  `gemini` with `gemini-3.8-flash`, then `ollama` with `llama3.1:8b-instruct-q4_K_M`).
  Gemini receives `portable_schema` (anyOf with null); if Anthropic or the OpenAI-compatible
  endpoint rejects the canonical schema with a 400, route that backend through
  `portable_schema` as well (one line in `_call`).
- Confirm that Ollama honours `options.num_ctx` on `/v1/chat/completions`: Ollama's
  OpenAI-compatibility documentation says the OpenAI API cannot set the context size and
  unknown body fields are ignored, so before any S4 run start the server with
  `OLLAMA_DEBUG=1`, send one `make smoke-llm BACKEND=ollama` call and check the runner log
  for `n_ctx=32768` and the absence of `truncating input prompt`. If the default context is
  used, create a model alias with a Modelfile (`FROM llama3.1:8b-instruct-q4_K_M`,
  `PARAMETER num_ctx 32768`) and point `--model` at it, or switch the backend to the
  native `/api/chat` with `options.num_ctx` and `format=<schema>`. The whole S4
  comparison rests on this.
- `OpenAICompatibleBackend` falls back from `response_format=json_schema` to
  `json_object` only when the 400 body names that parameter; the mode used is recorded in
  `Trace.response_format`. Any other 400 propagates.
- Output budget: `DEFAULT_MAX_OUTPUT_TOKENS` is 16000 for the hosted APIs (thinking tokens
  count toward it) and 4096 for Ollama; `Trace.stop_reason` records the stop or finish
  reason and `max_tokens` or a refusal becomes `Trace.error`.
- `config/prices.yaml` has null prices and free-tier limits for `gemini-3.8-flash`, so its
  traces carry `cost_usd` 0.0 until filled; wire the free-tier limits into `RateLimiter`
  in the run stage.
- Decide the Anthropic effort level per tier; the trace records `model_reported` but not
  effort (adding `effort` to `models.Trace` is the change if it should be recorded).
- The system prompt states the applicability rule, so the fabrication probes test whether
  the model follows the rule, not whether it discovers absence unaided; drop the
  applicability sentence for the probe fields and bump `PROMPT_VERSION` for the harder
  probe.

Evaluation

- Hallucination rate counts any misgrounded answered item (quote not found, out of scope,
  or not supporting the value), matching design 6.4; the fabrication denominator is gold
  NA or ABS items (`fabrication_denominator` column). Confirm both for
  `docs/analysis_plan.md` before the freeze.
- `metrics_table` reports recall conditional on answering and `recall_overall` (abstentions
  count as misses) side by side; decide which Table 1 shows.
- `grounding.WORD_FORMS` for Group C enums are regular expressions seeded from two positive
  samples; a quote is boilerplate when the template residual has no sentence that asserts
  a finding (no character threshold). Revisit after the census positives are labeled.
- Scoring conventions fixed in this pass: a fabricated absence is wrong in accuracy and a
  false negative in binary and set recall; set elements produced on a gold-null item are
  false positives; `within_1pct_same_quarter` scores wrong when gold has an as-of date and
  the system gives none; `name_fuzzy_90` is `token_sort_ratio` on punctuation-stripped
  names; risk flags never read gold (exam_date comes from the doc_id); calibration is one
  ECE per system. Copy these into `docs/analysis_plan.md` before the freeze.
- `evaluate.RISK_GROUPS` is `('B',)`: the A_or_B component ratings are not in `doc_risk`
  because the applicability cell needs the manifest method; decide whether to add them.
- `calibration_table` uses probability `n_flags / 4` with 5 bins, so the ECE is
  indicative only (design 6.5).
- scipy is not installed, so only the stdlib Clopper-Pearson fallback is tested.
- The test-split guard logs only invocations that pass the tag check; a refused run exits 2
  without logging.
- CI (`.github/workflows/ci.yml`) does not fetch tags; when `make reproduce` is added to
  CI, set `CRA_ALLOW_TEST_EVAL=1` there.
