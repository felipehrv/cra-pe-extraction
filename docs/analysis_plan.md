# Analysis plan

Pre-registered evaluation protocol for cra-pe-extraction (specification: `docs/design.md` v1, sections 3 to 7 and 11). Written 2026-10-08, before the test split is drawn or labeled. `cra freeze` tags this file as `plan-v1.0`; every later change goes to `docs/deviations.md`. Where the code and the design differ, this plan states what the code does and lists the difference in the last section. File and line citations refer to commit f644a46; statements about the stages added on 2026-10-08 (commit 5eeaac8: overrides, locator scope rules, census, routing and corpus scoring, audit, report, run extras, reliability) cite modules and functions instead of lines. Intervals are Wilson 95 percent unless stated otherwise.

## Purpose and scope

The project extracts structured fields from every Small Bank and Intermediate Small Bank (ISB) CRA Performance Evaluation (PE) released by the OCC, FDIC and FRB in 2023 to 2025, and measures how far each extracted value can be trusted. This plan fixes, before the test set is labeled: the population and the three samples (dev as drawn, test and audit draw rules), the systems and versions compared, the truth source of each field group, the scoring matrix, the tolerances, every metric with its estimator and interval, the census protocol, the audit design, the risk flags and calibration, the cost accounting, the heterogeneity cuts, the leakage controls, the reliability check and the decision rules that depend on dev results.

Pre-registered (confirmatory) results: Table 1 on the test split (Group A as a smoke test, the component ratings and Group B) by system; Group A agreement with the agency indexes on the whole corpus; Group C census counts and system precision and recall against the census; the design-weighted Group B error from the audit; calibration of the risk flags (indicative only); the cost table at three review operating points. Exploratory, labeled as such wherever reported: the descriptive analyses of design section 7 (NI and SN decomposition, findings registry, FRB matrix-floor check, rating transitions, publication lag, data-quality table), Group D, the COULD items of design section 12, any cut not listed under "Heterogeneity", and any quantity computed after the tag that this plan does not name. Section 7 analyses are non-causal and use no comparative language across regulators.

## Data

### Population

- Manifest `config/corpus.csv`: 2,793 PEs, regulator OCC, FDIC or FRB, exam method Small Bank or ISB, public release date 2023-01-01 to 2025-12-31 (observed range 2023-01-01 to 2025-12-25). By regulator: FDIC 1,770, OCC 605, FRB 418. By cell: FDIC small 981, FDIC isb 789, OCC small 372, OCC isb 233, FRB small 216, FRB isb 202. The regulator x method x release-year table (18 cells, 52 to 412 documents) is `results/corpus_counts.csv`.
- Index rows outside the window or without a release date are not in the population; they are logged in `data/index/manifest_exclusions.csv` and used only for the prior-exam lookup.
- Unit: the document, `doc_id = {regulator}_{bank_id}_{YYYYMMDD}`.

### Exclusions

Exclusions are listed with reason, evidence and date in `config/manifest_overrides.yaml` and applied by `python -m cra_extract.overrides apply`, which sets split `excluded`. Criteria: no fetched PDF, `text_extractable` false, or a PDF link that points to another evaluation (`wrong_pdf_link`, evidence: the cover date of the linked PDF). An excluded document is counted in the population and reported by regulator, and is not evaluated by any system or metric. As of 2026-10-08:

- FRB_614948_20250902 (1NB Bank): HTTP 500 after 6 attempts in each of two fetch runs (`data/index/fetch_failures.csv`); no sha256 in the manifest. The five FDIC rows in the same log returned 404 on an earlier attempt and were fetched later (their manifest rows carry sha256).
- OCC_9183_20230213: the index links `/apr19/9183.pdf`, the PE of the December 3, 2018 exam.
- FRB_785473_20220418 is a PDF portfolio; its embedded 42-page PE was extracted and parsed (`pdf_replacements` in the overrides file), so it is evaluable. No scanned document exists in the corpus, so the 2 percent OCR trigger of design 2 is not reached and no OCR is done.
- Evaluable documents: 2,791 (FDIC 1,770, OCC 604, FRB 417; FDIC small 981, FDIC isb 789, OCC small 371, OCC isb 233, FRB small 215, FRB isb 202). The count on the day of the corpus pass is the one reported.
- A corrected regulator, bank_id or exam_date regenerates the doc_id; the original id stays an alias in `config/manifest_overrides.yaml` (FDIC_445_20280408, whose file-id date was impossible, is FDIC_445_20240408). The manifest exam_date of FDIC rows is the file-id date (`exam_date_source` fdic_file_id) except where `exam_date_source` is override (the printed date).
- Overrides refuse to exclude or rename a dev, test or audit row.
- Non-evaluable rows: `overrides apply` and the `draw-test` and `draw-audit` CLIs refuse while a row without sha256 or with `text_extractable` not true is outside split `excluded`, and `strata.eligible_pool` leaves excluded rows out of every draw, so no gold or audit draw can select them.

### Dev split (drawn)

Seed 20261007, drawn 2026-10-07, n = 10, doc_id list sha256 `982d47dc9aca806dae7b1c9b0677d9daa5cc50aeb21319b08f9e5bb808916546` (`config/strata.yaml`).

| doc_id | cell | stratum (pool) |
|---|---|---|
| FDIC_12299_20220912 | FDIC/isb | fixed fixture sample (8) |
| FDIC_16733_20240410 | FDIC/small | random_fill (2,783) |
| FDIC_26816_20220809 | FDIC/small | fixed fixture sample (8) |
| FDIC_6078_20240805 | FDIC/isb | fixed fixture sample (8) |
| FRB_3664588_20240116 | FRB/isb | fixed fixture sample (8) |
| FRB_663656_20241118 | FRB/small | fixed fixture sample (8) |
| FRB_722432_20230911 | FRB/small | frb_other_district, state WV (282) |
| OCC_11715_20240408 | OCC/small | fixed fixture sample (8) |
| OCC_13682_20240819 | OCC/isb | fixed fixture sample (8) |
| OCC_703395_20230327 | OCC/small | fixed fixture sample (8) |

The eight fixed members cover all six cells, so no cell-coverage draw was needed; 11 documents left the pool by bank exclusivity. Dev is never used for inference. The pools recorded in strata.yaml (frb_other_district 282, random_fill 2,783) predate the exclusions; a redraw on the current manifest gives the same 10 documents and hash with pools 281 and 2,781. draw-dev is not rerun.

### Test split (draw rule)

Command: `python -m cra_extract.strata draw-test --seed 20261013 --date <draw date> --aa-counts results/sections_corpus.csv` (seed from the usage line, strata.py:41; AA counts as in Makefile:53-55). As implemented in `strata.draw_test` (strata.py:487-568):

1. Pool: rows not `excluded`, minus every document of a dev bank and every document of a tuning-document bank ("Leakage and contamination controls"). Bank key = (regulator, bank_id without leading zeros). On the current manifest the pool has 2,780 documents after the exclusions and the dev banks, and 2,617 after the 163 documents of tuning-document banks leave (FRB/isb: 202 manifest documents, 159 in the pool); listing the Group A triage documents removes 26 more (2,591). The draw records the tuning-document count and hash. Documents are drawn one at a time with `numpy.random.default_rng(seed)` from candidates sorted by doc_id; after each draw every other document of that bank leaves the pool.
2. Cell stratum: 30 - 5 - 4 = 21 slots. Every regulator x method cell with at least 50 manifest documents (all six) gets 3; the 3 remaining slots go one each to the largest cells. On the current manifest (`strata.cell_allocation`): FDIC/small 4, FDIC/isb 4, OCC/small 4, OCC/isb 3, FRB/small 3, FRB/isb 3.
3. Low-rating stratum: 5 documents rated NI or SN from what remains (70 in the manifest: 65 NI, 5 SN).
4. Multi-AA stratum: 4 documents with `n_aa_sections >= 3` in the AA-counts file (`run.count_aa_sections`). n_aa_sections counts distinct assessment-area units (`locate.aa_units`): markers deduplicated by MSA or MD code or area name, a marker without a name counted per occurrence, markers in appendix scope ignored; a single-AA document without an assessment-area heading counts 0. The same rule decides the lone-AA scope rule (Metrics, groundedness). FDIC state markers follow config/headings.yaml at the tag. `make scale` regenerates `results/sections_corpus.csv` with this count before draw-test. Without the file the code uses a proxy (total assets in the top decile of the cell); this plan uses the AA-counts rule, and `multi_aa_rule` in strata.yaml records it.
5. Recorded per stratum: population, pool, drawn, and the stage probability n / pool. Strata are sequential, so a document's inclusion probability exceeds its stage probability. Test metrics are therefore unweighted, and corpus-level Group B error comes only from the audit. Census positives are not a stratum; a drawn positive stays.
6. Fallback (design 5.1, 5.5): if mean dev labeling time at the 10-09 checkpoint exceeds 35 minutes per document, n_test = 25 with `--n-test 25 --per-cell 2` (Makefile:52), which gives FDIC/small 3, FDIC/isb 3, OCC/small 3, OCC/isb 3, FRB/small 2, FRB/isb 2, with the low-rating (5) and multi-AA (4) strata unchanged.
7. Hashes: `cra label hash --split test` appends the sha256 of the sorted test doc_id list to `docs/hashes.md` before labeling (the freeze refuses otherwise); the label-file hash is appended when labeling ends.

### Audit split (draw rule)

Command: `python -m cra_extract.strata draw-audit --risk results/risk_corpus.csv --seed 20261016 --date <draw date>` (strata.py:42). Pool: corpus documents not excluded and not of a dev, test or tuning-document bank, restricted to documents with a `doc_risk` value. Strata, allocation and weights are in "Audit design". The audit doc_id hash is appended to `docs/hashes.md` when drawn, before audit labeling.

## Systems and versions

| system | definition | pinned at the freeze |
|---|---|---|
| S0 | `baseline.detect`: sentence-level triggers ("lowered", "violation(s) of", "identified ... illegal credit practice", "substantive violation") with sentence-wide negation over the located review section; outputs finding_present and rating_lowered | code at the tag |
| S1 | `rules.extract` over located sections: scope-aware locator, regular expressions, table parser | `RULES_VERSION` 0.2.2 at the time of writing (rules.py CHANGELOG; 0.1.0 was the first corpus pass, 0.2.x the corpus and dev tuning of 2026-10-08; 0.2.2 changes no pattern, its S1 inputs changed with the locator scope rules and the manifest overrides), bumped with a CHANGELOG entry on every change; the value at the tag is the pinned one |
| S3 | hosted LLM, one structured-output call per document (`run --field-groups all`, the pinned mode: the union schema of the four `llm_field_groups` entries of fields.yaml over every located section; the whole document is sent when any group's section is missing, and `locator_miss` is recorded per field as its own group's miss); the four-call `split` mode is kept for comparison and its traces never replay for `all` | `PROMPT_VERSION` v2 (prompts.py:24); closed-book variant `CLOSED_BOOK_PROMPT_VERSION` cb1; effort: low (Gemini thinking_level LOW, Anthropic effort low, openai_compat low) |
| S4 | local open-weight model through Ollama; same prompts, schema and grounding as S3 | `PROMPT_VERSION` v2; effort: none |
| S5 | composite of S1 and the S3 corpus tier, routing below | routing table in "Decisions that depend on dev results" (`config/s5_routing.yaml`) |

- S3 primary (decided 2026-10-06, after design v1): the Google AI Studio free tier through the `gemini` backend, model id recorded per trace (`gemini-3.8-flash` at the time of writing), on dev, test and the corpus. S3 optional tiers on gold only (dev and test), run if the Anthropic console credit is claimed in time: `claude-haiku-4-5`, `claude-sonnet-5-5`, `claude-opus-5-5` on the `anthropic` backend (`config/prices.yaml`). Every (backend, model) pair is its own system row; the corpus tier decision below considers every tier that ran on dev. The free-tier daily limits (`config/prices.yaml`) reset at midnight America/Los_Angeles (`gemini.free_tier.reset_timezone`), and the daily limiter counts retries.
- S4: `llama3.1:8b-instruct-q4_K_M` and/or `qwen2.5:7b-instruct-q4_K_M` (prices.yaml), `num_ctx` 32768 (llm.py:63) with the prompt-fit assertion; each local model run is its own system row. Scope: all gold documents plus a 100-document random corpus sample, wall clock and hardware recorded.
- Recorded per trace (`models.Trace`): backend, requested model id, `model_reported` (the response's model field), `prompt_hash` (sha256 of PROMPT_VERSION, system prompt, user prompt and JSON schema; llm.py:112-125), `effort`, `mode` (located, whole, closedbook), input and output tokens from the usage fields, `cost_usd`, latency, `called_at`, stop reason, error. The prompt header states only regulator and exam method. No temperature is sent.
- Effort (`llm.DEFAULT_EFFORT`): low for the gemini, anthropic and openai_compat backends, none for ollama (S4). It is sent with every call, is part of the replay key and of `trace_id`, is recorded in `Trace.effort` and is pinned in the Freeze block. `--effort none` is used for an openai_compat provider that rejects the parameter. Before the first pilot, one smoke call per tier verifies that the model accepts the parameter (a rejected parameter makes every field of the call abstain).
- Replay rule: scored predictions are produced from stored traces. `llm.ReplayBackend` serves a trace by (model, effort, prompt_hash) and raises `TraceMissError` on a miss unless `--allow-api`; any change to the prompt, the located sections, the schema, PROMPT_VERSION or the effort changes the key, so a stale trace is never replayed. Test documents are called live once; every later regeneration replays.
- S5 routing (design 4, pre-specified; `cra_extract.routing`): for each Group A field, and for the two component ratings in the FRB/isb cell (the only cell with an index value), S5 takes S1's value when, over the evaluable documents of split `corpus` (dev, test, audit and excluded rows left out) that have an index value, S1 coverage (share where S1 returns status `value`) is at least 95 percent and S1 agreement (share of those S1 values that match the raw index under the field's tolerance) is at least 98 percent. Thresholds are inclusive (integer comparison, so a share of exactly 95 or 98 percent passes). A field with no index value or no S1 value in the population routes to S3. FDIC exam_date has no index truth ("Group A at corpus scale"), so the exam_date route is decided on OCC and FRB documents and applies to FDIC documents. Otherwise S5 takes the S3 corpus tier's value and an S1 disagreement raises the disagreement flag. Group B and C fields, and the component ratings outside FRB/isb: S5 = S3 corpus tier. Computed once, after the test draw and before the freeze, with S1 at the frozen RULES_VERSION: `routing compute` refuses before the test draw unless `--provisional`, which writes under `results/provisional/` and never to `config/`, and `routing apply` refuses a provisional table.
- S5 rows: S5 takes the routed system's row as it is, so an abstention of the routed system is an S5 abstention (no fallback to the other system); S5 rows exist for the documents that have S3 rows. `make s5-test` appends the S5 rows to `predictions_test.csv` before the single test scoring run. Table 1 reports S5 as a system; its S1-sourced values have no grounding component (`grounding_failure` na, `evaluate.GROUNDING_NOT_GRADED`), so its groundedness covers only the S3-sourced values and a Table 1 row whose S5 values are all S1-sourced shows the error rate without grounding.

## Field groups and truth sources

| group | fields (config/fields.yaml) | truth | scored on |
|---|---|---|---|
| A | regulator, bank_name, bank_id, exam_date, exam_method, overall_rating, prior_evaluation_date, prior_rating | agency index in the manifest; prior fields from the bank's previous index row (coverage OCC 96.0, FRB 89.7, FDIC 88.0 percent, dev_notes 5); FDIC exam_date has no index truth and is scored on gold only | whole corpus (main); test (smoke test) |
| A_or_B | lending_test_rating, cd_test_rating | FRB CSV ISB main row in FRB/isb; gold for OCC and FDIC; applicability nulls elsewhere | corpus (FRB/isb and probes); test |
| B | total_assets_usd, total_assets_asof, lending_data_period_start, lending_data_period_end, n_assessment_areas, n_full_scope, loan_products_evaluated, primary_product, inside_aa_share_number_pct, inside_aa_share_product, avg_ltd_ratio_pct (11) | gold labels | test (Table 1); audit (corpus error) |
| C | finding_present, finding_type, rating_effect, rating_lowered, rating_lowered_level, rating_before, rating_after, laws_cited, practice_basis, finding_source; distractor_laws_named is a not-scored reporting flag | census | census; on test only as fabrication probes |
| D | per-AA home-mortgage distribution rows (not in fields.yaml) | gold, FDIC and OCC only | exploratory, only on a go decision on 10-18 |

Applicability (fields.yaml; any cell not listed is `value`):

| field | OCC small | OCC isb | FDIC small | FDIC isb | FRB small | FRB isb |
|---|---|---|---|---|---|---|
| lending_test_rating | value (equals overall) | value | ABS | value | ABS | value |
| cd_test_rating | ABS | value | ABS | value | ABS | value |

The FRB Small cells rest on one Kansas City PE until the 12-district pilot confirms them. Fabrication probes: cd_test_rating on every Small document; lending_test_rating on FDIC and FRB Small; rating_before, rating_after and laws_cited on every document with finding_present false. The prompt tells the model when a field is not printed for the document's cell (prompts.py:165-169), so the probes measure whether the model follows the stated rule.

Gold vocabulary: value, not_applicable (NA), absent_from_document (ABS); system vocabulary: value, absent, abstain. Codebook: `docs/labeling_guide.md`. Labeling is blind to system output. A sheet is used only when `cra label validate` (label.py:272-442) reports no problem: exactly one of value and null_reason; nulls only on nullable fields; a value has evidence_page and a verbatim quote; minutes on every row; ranges (total assets and AA counts at least 1, inside-AA share 0 to 100, LTD 0 to 300); lending_data_period_end on or before exam_date; n_full_scope at most n_assessment_areas and NA exactly when n_assessment_areas is 1; lending equals overall for OCC Small; rating_after equals overall when the lowered level is overall; rating_lowered equals (rating_effect == substantive_lowered); Group C attributes NA exactly when there is no finding; downgrade attributes NA exactly when not lowered; inside_aa_share_product NA exactly when the share is null; no value in an applicability ABS cell.

## Scoring matrix

`evaluate.score_cell` (evaluate.py:147-172), one cell per (doc_id, field, system):

| gold \ system | value | absent | abstain |
|---|---|---|---|
| value | `correct` if `values_match`, else `wrong` | `fabricated_absence` | `abstention` |
| not_applicable | `fabrication` | `correct_null` | `abstention` |
| absent_from_document | `fabrication` | `correct_null` | `abstention` |

- A gold row with no prediction from a system is an abstention of that system.
- A fabricated absence is wrong: it is in the accuracy denominator, it is a false negative for binary and set fields, and it counts toward the hallucination rate.
- Abstentions are excluded from every conditional metric and reported as the abstention rate.
- Fabrication rate = fabrication / (gold NA + gold ABS items of the field), with that denominator reported in its own column (`fabrication_denominator`).
- Accuracy is computed over gold-value items only: correct / (correct + wrong + fabricated_absence). Gold-null items enter only through the fabrication rate (evaluate.py:386).
- `not_scored` fields are dropped; `counts` fields (finding_type, practice_basis) get cell counts only.

## Tolerances

`evaluate.values_match` (evaluate.py:98-141). A missing value on either side never matches. The field type decides first, then the tolerance key:

| tolerance | fields | operational definition |
|---|---|---|
| exact, enum | regulator, exam_method, overall_rating, lending_test_rating, cd_test_rating, prior_rating, primary_product, inside_aa_share_product, Group C enums | casefolded and whitespace-collapsed codes equal; a printed rating word (Outstanding, Satisfactory, Needs to Improve, Substantial Noncompliance) maps to its code first |
| exact, date | exam_date, prior_evaluation_date, total_assets_asof, lending_data_period_start, lending_data_period_end | both sides parse to the same date (ISO, "March 31, 2024", "Mar 31, 2024", "Mar. 31, 2024", 03/31/2024, YYYYMMDD) |
| exact, int | n_assessment_areas, n_full_scope | numeric difference at most 1e-9 after removing commas, "$" and "%" |
| exact_strip_zeros | bank_id | normalized strings equal after stripping leading zeros |
| name_fuzzy_90 | bank_name | rapidfuzz `token_sort_ratio` at least 90 on names lowercased, stripped of punctuation other than "&" and whitespace-collapsed ("S.S.B." equals "S.S.B"; a prefix or an extension of the name fails) |
| within_0.1pp | inside_aa_share_number_pct, avg_ltd_ratio_pct | absolute difference at most 0.1 percentage points |
| within_1pct_same_quarter | total_assets_usd | absolute difference at most 1 percent of the gold value. If gold has a total_assets_asof date, the system's total_assets_asof must fall in the same calendar quarter, and a system that gives no as-of date scores wrong. If gold has no as-of date, only the numbers are compared, whatever date the system gives |
| exact_elements, set | loan_products_evaluated, laws_cited (practice_basis: counts only) | accuracy: parsed sets equal; precision and recall element-wise (see Metrics) |
| bool | finding_present, rating_lowered | parsed booleans equal |

The same rules apply to Group A corpus scoring (system against index), to the S5 routing agreement, and to the disagreement flag (where the as-of quarter is not checked). Grounding uses the matching numeric tolerance for value presence (`grounding.number_tolerance`).

## Metrics

Per (field, system), from `evaluate.metrics_table` (evaluate.py:345-453); every interval is `stats.wilson_ci` with z = 1.96.

- Accuracy (metric types accuracy and set_pr): as in the scoring matrix. Overall accuracy (abstentions as misses) = n_correct / n_gold_value, derived from the Table 1 columns, appendix only.
- Binary fields (positive = true): TP = gold true and system value true; FP = gold not true (false or null) and system value true; FN = gold true and system value false or absent. Precision TP/(TP+FP) and recall TP/(TP+FN), each with a Wilson CI; `recall_overall` = TP / all gold positives (abstentions as misses), no CI. F1 is secondary and has no CI.
- Set fields: TP, FP and FN summed over elements of non-abstained items. A gold-null item contributes an empty gold set (every predicted element is FP); a system `absent` contributes an empty predicted set (every gold element is FN). Wilson CIs on element counts are labeled element-level, since elements of one document are not independent.
- Recall in Table 1: recall conditional on answering, with the abstention rate beside it; the overall recall goes to the appendix table. For loan_products_evaluated, the only set field in Table 1, overall recall = TP / (TP + FN + gold elements on abstained items), computed from `scored_test.csv`.
- Abstention rate = abstentions / all gold items of the field. Fabrication rate with Wilson CI and denominator. Fabricated-absence rate = fabricated_absence / gold-value items.
- Groundedness (systems that quote: S3, S4, and the S3-sourced values of S5), `grounding.check` (grounding.py:505-543):
  - Stage 1, locate: quote and page text normalized (lowercase, quote and dash characters unified, whitespace collapsed); `partial_ratio_alignment` at least 90 on some page, cited page tried first. Empty quote or no page at 90: `quote_not_found`.
  - Stage 2, support: (a) scope: the matched page has institution scope or no scope tag and is not a table-of-contents page, or the quote lies in an institution-scope section; otherwise `quote_out_of_scope`. A located section's scope follows the scope rules of config/headings.yaml (scope_rules): the fair lending or other illegal credit practices review section is institution scope wherever its heading is printed, and in a document with exactly one assessment-area unit (locate.lone_aa_unit) the LTD ratio and AA concentration sections found in that unit's scope are institution scope; these rules set the section scope only, not the page scope. The rules apply to all three regulators, and the review reset also overrides appendix scope. FDIC state markers follow config/headings.yaml at the tag. (b) value presence in the quote or in the matched page span: enum word forms (`grounding.WORD_FORMS`), printed date forms, numbers within tolerance (scale words and "(000s)" multiples), sets element by element, bank names at partial ratio 90. (c) A Group C value that asserts a finding needs a finding predicate (identified, lowered, violation, substantive, cited, downgrade) and must not be boilerplate (a template matches at 90 or more and the residual has no sentence with a predicate and no negation). Failure of (b) or (c): `quote_does_not_support`.
  - Reported over answered items with known grounding: the rate of each failure category, the grounded rate, and `grounding_checked` (true when every answered item of a quoting system was checked).
- Correctness x groundedness 2x2 over answered gold-value items with known grounding: correct_grounded, correct_misgrounded, wrong_grounded, wrong_misgrounded. Fabrications are counted in the fabrication rate and the failure categories, not in the 2x2.
- Hallucination rate = (wrong + fabrication + fabricated_absence + correct_misgrounded) / non-abstained items, Wilson CI: a wrong value or a misgrounded quote, each item counted once. It is undefined (NaN) when grounding is known for some but not all answered items (evaluate.py:441-443) and is reported only where `grounding_checked` is true. For S0 and S1 the same quantity is reported as "error rate, no grounding component", never as a hallucination rate: the code gives S0 and S1 quotes and grounding outcomes, and the report shows those systems without a grounding component.
- Error causes (design 6.6): every non-correct cell of `errors_test.csv` gets one or more causes from parsing, locator_miss, wrong_scope, wrong_period, unit_or_format, boilerplate_confusion, reasoning, fabrication, each attributed to the stage that produced it (parse, locate, extract, label). A locator miss is a locate-stage cause shared by S1 and S3, not an LLM reasoning error. A gold error found here is a label-stage cause and goes to `docs/deviations.md` with before and after metrics. Reported as counts per cause, stage and system, both per value and per cause mention. `report.error_causes` reads the hand-filled `cause` (several causes separated by semicolons; `label` for a gold error) and `stage` columns of `errors_{split}.csv` and writes `results/table_error_causes_{split}.csv` and `.md`, counting a cell with a blank cause as unclassified.
- Locator recall (design 6.10), reported before any LLM metric: 100 random corpus documents including one PE per Reserve Bank district, drawn by `cra_extract.locator_check` (`make locator-check-draw`) from the evaluable corpus-split documents minus every document of a test or audit bank and minus the tuning documents: one FRB document per district (district approximated by the bank's state, `locator_check.STATE_DISTRICT`), the rest simple random; seed, date, pool hash, exclusions and doc_id hash in `config/locator_check_sample.yaml`. Found or not found for cover and rating, description of institution, review section, and the AA concentration and LTD tables, confirmed or corrected from the PDF on `data/gold/locator_check_sheet.csv`; recall (correct among sections printed in the PDF) and precision (correct among found sections) per section and per section type, overall and by regulator, with Wilson CIs, in `results/locator_recall.csv`, and the misses in `results/locator_recall_misses.csv`. The locator fallback rate (LLM calls with `locator_miss` = 1) is reported on test and on the corpus. Sample drawn 2026-10-08 with seed 20261010 (`config/locator_check_sample.yaml`: 100 documents, OCC 17, FDIC 64, FRB 19, one FRB document per district by the state map, test documents and tuning-document banks excluded; doc_id hash 3360b203...). The 100 documents are tuning documents by this plan's leakage rule and their banks leave the audit pool.

Table 1 (test): rows field x system (S0, S1, every S3 and S4 tier, S5) for Group A (labeled smoke test), the component ratings and Group B; columns n, accuracy or precision and recall with CIs, abstention rate, fabrication rate with denominator, grounded rate, hallucination rate. The whole-document (`:whole`) and closed-book (`:closedbook`) variants are left out of every Table 1 view. The fabrication-probe table (`table_probes_{split}`) holds the component-rating probe cells, which are the applicability ABS cells of fields.yaml, and the Group C probes rating_before, rating_after and laws_cited on documents whose gold finding_present is false; the other Group C rows that `metrics_table` writes into `table1_test.csv` are not reported on test, and Group C is not in Table 1. The overall rates go to `table1_{split}_appendix.csv` and the correctness x groundedness cells to `table_grounding_{split}.csv`. The byte-identical regeneration check runs on `results/table1_{split}.csv`.

## Group A at corpus scale

- Systems S1, S3 corpus tier and S5 on every evaluable document of every split (dev, test and audit included; `corpus_scoring.score`), plus a test-only block (scope `test`). Rules and prompts are tuned against corpus-wide index agreement (design 6.8), so these figures are in-sample for tuning and are labeled so: every row carries `in_sample` true.
- regulator and exam_method are given in the S3 prompt; for S3, and for S5 when S5 takes the field from S3, they are consistency checks, not extraction accuracy (`consistency_check` true on those rows only).
- FDIC exam_date has no index truth and is scored on gold only. The manifest exam_date of FDIC rows is the file-id date, which differs from the printed date on 127 FDIC documents corpus-wide (definitional); the hand override row is treated like every other FDIC row. `results/fdic_exam_date_gate.csv` (manifest exam_date against gold exam_date for every FDIC dev and test gold document; dev passes 4 of 4) is a reported diagnostic only and gives no FDIC row index truth. Corpus-wide exam_date figures therefore rest on OCC and FRB documents.
- prior_evaluation_date and prior_rating are scored only where a previous index row exists; other documents are counted as "no index truth".
- Component ratings: against the FRB CSV main row in FRB/isb. In the applicability ABS cells (cd in all Small cells; lending in FDIC and FRB Small) a returned value counts as a fabrication. Outside FRB/isb the component ratings take S3 in S5 and have no index truth (OCC Small lending equals overall but is not in the index; no truth is derived).
- Cells: regulator x method x release year (18 cells), plus regulator x method pooled. Per cell, field and system, all with Wilson CIs: accuracy = correct / (correct + wrong + fabricated_absence), among non-abstained documents with index truth; abstention rate over items with truth (index value or ABS-cell null); fabrication rate over every ABS-cell item, abstentions included (as `metrics_table`).
- Triage: every disagreement between a system and the index is read against the PE and assigned one root cause: our_error, source_error (the index contradicts the printed PE) or definitional (both right under different definitions, such as a release month for a date). Rows go to `results/data_quality_findings.csv` (source, field, n_affected, example_doc, root_cause, recommendation). Accuracy is reported against the raw index (primary) and against triaged truth (secondary). Triaged truth: a source_error disagreement takes the printed value read in the triage (rule_value), a definitional one has no truth, an our_error one keeps the index value. The triage covers the S1 disagreements on bank_id, exam_date, exam_method and overall_rating; the triaged block is reported for S1 only. Extending the triage to S3 and S5 disagreements needs hand reading after the corpus pass. A seed data-quality row reaches the README only after verification against at least three PEs.

## Group C census

Protocol (design 5.2; `cra_extract.templates`):

1. The locator extracts the review section of every evaluable document.
2. `templates.residual` removes every template of `config/review_templates.yaml` that matches at partial ratio 90 or more (FRB templates per Reserve Bank district). A section is flagged when the residual exceeds 120 characters, or no template reaches 85, or the residual contains a sentence that asserts a finding (a finding predicate without negation; third criterion added 2026-10-08 with template library version 2, 39 templates, `templates.py`); a section the locator did not find is flagged as a locator miss. The second corpus pass (`docs/corpus_pass_2.md`) flagged 62 sections before the manifest overrides (53 located, 9 locator misses). The census regenerated after the overrides (`results/census.csv`) flags 53 located sections plus 8 locator misses (FRB_785473_20220418 is now located), so the hand-reading queue is 61 plus the random 100.
3. Hand reading starts only when the flagged located sections plus the locator misses to hand-locate number at most 300 (200 under the checkpoint fallback); above the cap, template variants are added and the census rerun. The first run (2026-10-07) flagged 1,972 of 2,792 sections and missed 92 review sections, so the cap rule applied then; the regenerated census counts 53 + 8 = 61.
4. A simple random sample of 100 unflagged located sections is hand-read. Seed 20261009. It is drawn after draw-test (`census draw` refuses earlier unless `--before-test-draw`) from the unflagged located sections of split corpus (audit included; dev, test and excluded rows left out), without replacement, and the seed, date, pool size and hash and doc_ids are recorded in `config/census_sample.yaml` before any section is read.
5. Locator misses are listed and hand-located when fewer than 100 (8 in the regenerated census; 92 at the first run); otherwise a random 50 (same seed, recorded) are hand-located and the miss rate is a census row. A miss with no review section in the PDF is coded finding_present false with a note.
6. Flagged sections and locator misses that belong to test documents (2 of 161 in the 2026-10-08 sheet: OCC_3147_20230703, FRB_164975_20250818) are read only after test labeling ends, so nothing of a test document is seen before its blind labeling; the random sample never contains test documents. Every read section is labeled with the Group C codebook, blind to S0, S3 and S4 output. Positives get the full attribute set and are all read twice (outside coder if available, else the author after the delay of the reliability section); disagreements are resolved and logged.
7. Census truth: the hand label for read sections; unread unflagged sections are negative by template. Only census sheet rows are hand truth: the gold Group C labels of dev and test documents do not count as census reads, and unflagged dev and test sections are negative by template. distractor_laws_named of an unread unflagged section comes from the best-matching template only.

Reporting. Group C is reported as counts with exact binomial (Clopper-Pearson, `stats.clopper_pearson_ci`; scipy is absent, so the standard-library fallback runs) 95 percent CIs over the labeled census, and is excluded from Table 1 and from the audit.

- Positives (finding_present) and rating_lowered counts by regulator and method; prevalence with an exact CI.
- Miss bound: positives among the random 100 unflagged sections with the exact upper bound. No recall claim is made for the census beyond this bound.
- S0, S3 (corpus tier), S4 (on the documents it ran on, n stated) and S5 (scored as its own system `s5`, = S3 for Group C): precision and recall on finding_present and rating_lowered against census truth, exact CIs. Precision and recall are conditional on answering, with abstentions in their own row. The S0 false-positive rate uses every census negative as the denominator (FP / census negatives, so an S0 abstention on a locator miss is in it) and is reported for all regulators, the OCC, the FDIC and the FRB, with `n_negated_hits` per regulator from `results/baseline.csv`. distractor_laws_named is a reporting stratum for false positives.
- Among census positives: counts of finding_type, practice_basis and laws_cited, reported by regulator and by regulator x method; accuracy of rating_effect, rating_lowered_level, rating_before, rating_after and finding_source with exact CIs, where attribute accuracy = correct / (correct + wrong + fabricated_absence); element-wise precision and recall for laws_cited.

## Audit design

- Target: corpus-level error of the Group B values released by the corpus pass (S5, which for Group B is the S3 corpus tier). The audit uses system `s5` (the rows of `make s5-apply`).
- Unit: the document. All 11 Group B fields are labeled per audited document. Component ratings are not audited.
- Population: corpus documents with a `doc_risk` value, excluding dev and test banks, tuning-document banks and excluded rows. N_h counts this frame, so the audit estimates the error of the corpus minus those banks.
- Document risk: `doc_risk` = sum over the document's 11 Group B values of `n_flags` (next section) for S5, with the disagreement flag computed against S1; written to `results/risk_corpus.csv` (doc_id, doc_risk), with the per-value flags in `results/risk_values_corpus.csv`. The audit reads `n_flags` from that per-value file, so the tertiles, the error by flag count and the review curve share one flag input; the estimate refuses to run when a document's `n_flags` sum differs from its `doc_risk`. The S5 consistency flag lending_data_period_end <= exam_date reads S5's own exam_date, which comes from S1 when exam_date routes to S1.
- Strata: document-risk tertiles, the rank thirds of the population by doc_risk with ties broken at random by the draw's generator (strata.py:574-582); sizes differ by at most one document. Regulator is a post-stratification covariate: reported, not stratified.
- Allocation: 10 documents per tertile, 30 in total (fallback: 8 per tertile, 24), bank-exclusive.
- Design weights: w_h = N_h / n_h at document level (N_h tertile size, n_h documents drawn), recorded in strata.yaml; every value inherits its document's weight.
- Blinding by tertile: low tertile re-extracted blind from the PDF (no model value or quote shown); high tertile with the model status (value, absent, abstain), value and quote shown, confirmed or corrected; middle tertile by position in the sorted list of its drawn doc_ids, positions counted from 1 (the first sorted doc_id is blind), odd positions blind and even positions shown. Minutes per value are recorded in both modes.
- Scoring: each audited value gets a cell from `score_cell` against the audit label. Error indicator y = 1 for wrong, fabrication or fabricated_absence and 0 for correct or correct_null (unlike Table 1 accuracy, released correct nulls count as correct); abstentions are excluded and the design-weighted abstention rate is reported beside.
- Estimator: sum(w y) / sum(w) over answered values; 95 percent CI from `stats.stratified_weighted_mean`: 1,000 bootstrap resamples of documents with replacement within each tertile, percentile interval, seed 0. Every audited document is resampled (an abstained value has weight 0), so n_h is kept, also per field.
- No-error branches: overall, if the sample has no error the bootstrap interval is degenerate and one Wilson upper bound per tertile is reported instead; the per-tertile bounds are not combined into a corpus bound. Per field, a field with no error gets the point estimate with no CI and a note.
- Per field: design-weighted error with the same bootstrap, labeled wide; no per-field-per-tertile CIs. Per tertile: raw error pooled over fields with Wilson CIs, to show the risk gradient. By regulator: point estimates only. Point estimates only, too, for the design-weighted abstention rate and the error by flag count. Middle-tertile error by mode (blind vs shown) with Wilson CIs, descriptive. Reviewer minutes per value: unweighted, over every value, by mode.
- Gold metrics and audit estimates are always reported separately and never pooled.

## Risk score, calibration and review curve

Flags per value (`evaluate.risk_table`, evaluate.py:513-566), no fitted weights, `n_flags` from 0 to 4:

1. locator_miss: the field group's section was not located (whole-document fallback).
2. disagreement: another system answered the item with a value that does not match under the field's tolerance. The code compares with every other system in the predictions file; this plan restricts the input to the pair the design names (S1 for an LLM system, the S3 corpus tier for S1) by running `evaluate --systems` with those two systems (on test with `--replay-only`, outputs to a separate directory). On the corpus (`corpus_scoring.write_risk`) the reference of an S5 value is the system it did not come from: S1 for an S3-sourced value (every Group B value), S3 for an S1-sourced value.
3. groundedness_fail: any of the three grounding failures.
4. consistency_fail, on the system's own values: lending equals overall for OCC Small; lending_data_period_end on or before exam_date (the system's exam_date, else the doc_id date); n_full_scope at most n_assessment_areas; rating_after equals overall when the lowered level is overall. `matches_aa_section_count` never flags in the code.

Flags never read gold. They are computed for answered values only, so an abstained value has `n_flags` 0. `doc_risk` sums `n_flags` over Group B values of one system.

- Calibration (`evaluate.calibration_table`): answered Group B test values, one block per system (never pooled across systems), probability = n_flags / 4, outcome = cell not correct; error rate by flag count; ECE over 5 equal-width bins with a bootstrapped SE (1,000 resamples of values, seed 0). At most 330 values per system (30 documents x 11 fields) and 5 possible probabilities, so the ECE and its plot are labeled indicative. The calibration inset of Figure 1 comes from the pair run (`make eval-pair`, `results/pair/`) and its caption names the pair. Verification: design-weighted audit error by flag count. Group A flags are validated corpus-wide against index agreement (error by flag count, no sampling; `results/risk_validation_group_a.csv`): answered values with truth only; error = cell not correct (a fabrication in an ABS cell is an error); raw truth, and the triaged block only when the system is S1. The disagreement reference of an S1-sourced S5 value is S3. Test and audit are never pooled.
- Review curve: answered values ordered by `n_flags`, highest first; within a block of equal flag counts the expected residual error under random order is used (linear across the block). x = share of answered values reviewed; y = errors among unreviewed answered values / all answered values, assuming review corrects every reviewed value. Group A curve exact against the index corpus-wide (answered values with index truth, raw truth); Group B curve from the audit with design weights. The manual-review error rate from the reliability check is drawn as a reference line. Operating points: top 10, 25 and 50 percent of values; the cost table applies them to the answered Group B values of the released system.

## Expected CI widths

Recomputed with `stats.wilson_ci` (z = 1.96) and `stats.clopper_pearson_ci` in the project venv:

```
.venv/bin/python -c "
from cra_extract.stats import wilson_ci, clopper_pearson_ci
grid = [(25,25),(24,25),(20,25),(30,30),(29,30),(27,30),(24,30),(15,30),(50,50),(49,50),(45,50),(40,50),(25,50),(100,100),(99,100),(90,100),(80,100),(50,100),(200,200),(199,200),(180,200),(160,200),(100,200),(0,90),(1,90),(10,90),(0,110),(1,110),(10,110),(0,88),(1,88),(10,88)]
for k, n in grid: print('wilson', k, n, ' '.join('%.1f' % (100*x) for x in wilson_ci(k, n)))
for k, n in [(30,2700),(0,100)]: print('wilson', k, n, ' '.join('%.2f' % (100*x) for x in wilson_ci(k, n)), '| exact', ' '.join('%.2f' % (100*x) for x in clopper_pearson_ci(k, n)))
"
```

Accuracy-type proportions, percent:

| n | all correct | one error | 90 percent | 80 percent | 50 percent |
|---|---|---|---|---|---|
| 25 | 25/25 [86.7, 100.0] | 24/25 [80.5, 99.3] | n/a | 20/25 [60.9, 91.1] | n/a |
| 30 | 30/30 [88.6, 100.0] | 29/30 [83.3, 99.4] | 27/30 [74.4, 96.5] | 24/30 [62.7, 90.5] | 15/30 [33.2, 66.8] |
| 50 | 50/50 [92.9, 100.0] | 49/50 [89.5, 99.6] | 45/50 [78.6, 95.7] | 40/50 [67.0, 88.8] | 25/50 [36.6, 63.4] |
| 100 | 100/100 [96.3, 100.0] | 99/100 [94.6, 99.8] | 90/100 [82.6, 94.5] | 80/100 [71.1, 86.7] | 50/100 [40.4, 59.6] |
| 200 | 200/200 [98.1, 100.0] | 199/200 [97.2, 99.9] | 180/200 [85.1, 93.4] | 160/200 [73.9, 85.0] | 100/200 [43.1, 56.9] |

- Test (design 5.1): the n = 30 row reproduces the design's figures (30/30 [88.6, 100]; 29/30 [83.3, 99.4]; 24/30 [62.7, 90.5]); n = 25 applies under the fallback. Per field, n is the number of test documents in the metric's denominator, so fields with nulls have smaller n. A perfect Group A smoke test bounds accuracy at 88.6 percent (86.7 under the fallback), which is why Group A is evaluated corpus-wide. Fabrication probes have small denominators: the cell stratum alone gives at least 11 Small documents (cd_test_rating) and 7 FDIC or FRB Small documents (lending_test_rating); zero fabrications then bound the rate at 25.9 and 35.4 percent (8 and 5 documents under the fallback: 32.4 and 43.4 percent).
- Corpus cells (design 6.3): the 18 regulator x method x year cells hold 52 to 412 documents (the design expected 50 to 250); the n = 50 to 200 rows bracket the per-cell widths.
- Audit (design 5.3), error per tertile pooled over fields:

| values per tertile | 0 errors | 1 error | 10 errors |
|---|---|---|---|
| 90 (design: about 9 values x 10 documents) | [0.0, 4.1] | [0.2, 6.0] | [6.1, 19.3] |
| 110 (11 Group B fields x 10 documents) | [0.0, 3.4] | [0.2, 5.0] | [5.0, 15.9] |
| 88 (fallback: 11 fields x 8 documents) | [0.0, 4.2] | [0.2, 6.2] | [6.3, 19.7] |

- Census (design 5.2): 30 positives of 2,700 give Wilson [0.78, 1.58] percent (the design's figure) and exact [0.75, 1.58]; 0 positives in the random 100 give an upper bound of 3.70 percent (Wilson) or 3.62 percent (exact). This plan reports the exact figures.
- The design-weighted audit interval cannot be computed in advance; it depends on how errors spread across tertiles.

## Cost accounting

- Manual baseline per field: minutes from the timer column of the gold label sheets (dev and test) plus the blind-mode timers of the audit sheet. Manual cost per document = sum of a document's minutes / 60 x hourly wage, from the gold sheets only.
- TODO: state the hourly wage for the manual baseline (`manual_baseline.wage_usd_per_hour` in config/prices.yaml is null) and its source, here and in prices.yaml, before the freeze.
- LLM cost: input and output tokens from the usage fields of each trace times the prices in `config/prices.yaml` (Anthropic prices retrieved 2026-09-25, re-verified on run day with the date updated; Gemini prices null, so its traces carry cost 0.0 until filled; local models priced 0 on "Apple M5, 24 GB", electricity unpriced). The batch discount (0.5) applies only to calls actually sent through the Batch API; the pricing mode used is reported.
- Trace attribution: traces are attributed by `Trace.mode` (located, whole, closedbook; older traces without the field count as located, and closed-book traces are recognized by field group); the latest call per (document, field group, backend, model, mode, effort) counts; a whole-mode call that replays a located-fallback trace with the same prompt is not a new trace.
- Reported: tokens per call by mode, and per document under located and whole-document input; locator fallback rate (test, corpus); cost per document and per correct field (total cost / correct values over every scored field on test) by system and tier; the corpus cost actually spent; for tiers not run on the corpus, mean test cost per document times the evaluable corpus count; S4 wall clock and hardware.
- Projection without calls (`run --project`, `results/cost_projection_{split}.csv`): input tokens = characters / 4 of the system prompt, user prompt and schema, rounded up per call; output tokens from `--output-tokens-per-call`, else the mean of the stored traces.
- Review operating points (top 10, 25, 50 percent of the answered Group B values of the released system by `n_flags`): total cost = extraction cost + reviewer minutes per value from the shown-mode audit timers x values reviewed / 60 x wage, with the residual error at that point from the review curve. The corpus tier whose cost enters the operating points is named by `report --corpus-system`.

## Heterogeneity

- Group A: regulator x method x release year on the corpus (18 cells), Wilson CIs per cell.
- Group B: by regulator on test; intervals reported, no between-regulator test.
- Group C: census counts by regulator and method.
- Audit: regulator as a post-stratification covariate (point estimates).
- Scanned vs born-digital: dropped; no scanned document exists in the corpus (the one candidate, FRB_785473_20220418, was a PDF portfolio). The README says so.
- No other cut is confirmatory.

## Leakage and contamination controls

- Bank-level split: a bank (regulator, bank_id without leading zeros) sits in at most one of dev, test and audit (`strata.exclusivity_problems`, checked on every draw; `label.init_sheet` refuses a dev or test sheet with overlap). A bank that changed regulator keeps two keys; this is a stated limitation.
- Tuning: rules, prompts, templates and thresholds are tuned only on dev, the 100-document locator check and corpus-wide index agreement; never on test or audit documents.
- Tuning documents: corpus documents read while tuning before the test draw are tuning documents (config/tuning_documents.yaml, recorded by count and hash in strata.yaml); their banks are not eligible for test or audit (`strata.eligible_pool`). On 2026-10-08 the file lists 161 documents: 150 doc_ids cited in config/headings.yaml and config/review_templates.yaml, plus 11 read for the locate build without being cited. Before draw-test it must also list the documents read for the Group A triage of corpus-wide index agreement (config/group_a_triage.yaml: 31 entries on 30 documents, 3 of them already listed). `make tuning-docs` lists cited documents missing from the file, and the draws refuse while one is missing.
- Test guard as implemented (`evaluate.check_test_guard`, evaluate.py:624-667): `evaluate --split test` exits 2 unless the git tag `plan-v1.0` exists or `CRA_ALLOW_TEST_EVAL=1` (reserved for CI replay on traces and never set on the author's machine). Each accepted invocation is appended to `results/test_invocations.log` as run or replay. A scoring run after the first needs at least as many dated entries in `docs/deviations.md` (dated on or after the first run) as runs already logged. `--replay-only` regenerations are logged and never blocked. Refused invocations are not logged.
- Closed-book contamination baseline (`make closed-book`, prompt version cb1): the S3 corpus tier receives only the bank name and exam date of each test document (no PE text; the FDIC exam date is the file-id date) and is asked for overall_rating and finding_present. The contamination bound is the accuracy among answered items, compared with the test set's majority-class rate; the overall accuracy (abstentions as misses) is reported beside it. Reported in Limitations. Every closed-book value carries `quote_not_found`, and the variant is left out of Table 1.
- Run-to-run agreement on dev only: each S3 tier runs dev twice live. Replay would serve the first trace, so the second run (`make dev-rerun`) writes to its own directories, `--trace-dir data/traces/dev_rerun` and `--results-dir results/dev_rerun` (the rate-limit cache is shared), with `--allow-api`. Agreement (`run agreement`, `results/run_agreement_dev.csv`) per (system, field) = share of items with equal status and, when both are values, `values_match`; Wilson CI. Run A is the reference slot; items present in one run only are left out and counted (`n_only_a`, `n_only_b`).

## Reliability

- Draw (`cra reliability draw --branch outside|fallback --seed 20261017 --date <draw date>`): seed 20261017 for whichever branch runs; the documents, seed, date and pool hash go to `config/reliability_sample.yaml` before any second coding.
- Preferred (design 5.4), branch `outside`: an outside coder double-codes 8 test documents drawn at random after test labeling ends from the test documents other than the eight fixture samples (whose values the labeling guide quotes) on n_assessment_areas, n_full_scope, loan_products_evaluated, lending_data_period_start and lending_data_period_end, plus every census positive and 20 template-negative sections on the Group C fields (`census double`), following labeling guide section 7, blind to the author's labels and to system output. The template negatives are drawn from the read random unflagged sections, allocated by regulator in proportion to the positives (largest remainder); the fixture positives (FDIC_12299, FDIC_26816), whose values the labeling guide quotes, stay in the double read.
- Fallback, branch `fallback`: the author blind re-codes 6 dev documents drawn at random from all 10 dev documents, from a fresh `init` sheet at least 5 days after first coding (dev was coded 10-07 to 10-08, so not before 10-13; the compare refuses rows labeled before 2026-10-13), reported as intra-rater stability and labeled as the weaker check. The pool includes the dev fixture documents, which the author coded and the labeling guide quotes, so recall of those values can inflate the fallback agreement; the README states this caveat.
- TODO: state whether an outside coder exists (branch `outside`) or the fallback runs.
- Agreement: an item is a document (Group B, `reliability compare`) or a section (Group C, `census double --compare`); values are compared in canonical form (parsed dates, numbers, sorted sets, null codes as categories). Reported per field: Cohen's kappa where each coding uses two or more categories (null codes are categories), otherwise percent agreement with the majority-class prevalence; set fields are also compared element-wise (one binary item per document or section and allowed element), with an element-wise kappa (loan_products_evaluated in Group B); the coder's minutes beside the author's as the manual-baseline range. Disagreements are resolved by re-reading; a changed test label goes to `docs/deviations.md` with before and after metrics. The manual-review error rate drawn on the review curve is 1 - pooled document-level agreement over the five Group B fields (`results/reliability.csv`). The README states which branch ran.

## Descriptive analyses (exploratory)

Design section 7, produced by `report.descriptive`; exploratory and non-causal.

- NI and SN decomposition: an NI or SN document is compliance-driven when any census positive of that document has rating_effect substantive_lowered, otherwise performance-driven; counts by regulator and method with exact CIs.
- Publication lag (release_date minus exam_date): in months for every regulator, and in days for the OCC and the FRB only (FDIC release dates are months). Rating transitions, exam cycle length by prior rating and publication lag are computed over the whole manifest.
- Generated text is checked against the comparative-language word list `report.COMPARATIVE_WORDS` and carries none of those words.
- FRB ISB matrix-floor check: `report.ISB_OVERALL_FLOOR` encodes paragraph (d)(3) of Appendix A to 12 CFR part 345 (the same text as Appendix A to 12 CFR parts 25 and 228), 2023 annual edition of the CFR read on govinfo on 2026-10-08 (amendments through 75 FR 61045; the framework in force for the 2022 to 2025 exams): an ISB may receive an overall "satisfactory" only with at least "satisfactory" on both tests, may (not must) receive "outstanding" with "outstanding" on one test and at least "satisfactory" on the other, and otherwise receives "needs to improve" or "substantial noncompliance" depending on the degree of the failure. The check therefore flags only an overall NI or SN when both test ratings are at least "satisfactory" (not explained by the tests; candidate adjustment under 12 CFR 345.28(c)); with any test below "satisfactory" no floor between NI and SN follows from the text and nothing is flagged. Verified by the author's delegate on 2026-10-08; the author re-verifies only if an exam date falls under a later framework.

## Decisions that depend on dev results

1. S5 routing table. Rule: per field, S1 coverage and agreement against the raw index over the evaluable documents of split `corpus` after the test draw (dev, test, audit and excluded rows left out), S1 at the frozen RULES_VERSION; route to S1 when coverage is at least 95 percent and agreement at least 98 percent (inclusive), else to the S3 corpus tier ("Systems and versions"). `make s5-routing` writes the table below as `results/s5_routing.md` (values to one decimal, with the n of each) and the record `config/s5_routing.yaml` (date, RULES_VERSION, systems, thresholds, population counts); the table is pasted here before the freeze.

| field | n with index value | S1 coverage | S1 agreement | route |
|---|---|---|---|---|
| regulator | 2,751 | 100.0 (2,750 of 2,751) | 100.0 (2,750 of 2,750) | S1 |
| bank_name | 2,751 | 99.9 (2,748 of 2,751) | 97.6 (2,681 of 2,748) | S3 |
| bank_id | 2,751 | 99.9 (2,748 of 2,751) | 99.9 (2,745 of 2,748) | S1 |
| exam_date | 998 | 100.0 (998 of 998) | 98.6 (984 of 998) | S1 |
| exam_method | 2,751 | 99.9 (2,747 of 2,751) | 99.8 (2,742 of 2,747) | S1 |
| overall_rating | 2,751 | 99.9 (2,747 of 2,751) | 100.0 (2,746 of 2,747) | S1 |
| prior_evaluation_date | 2,477 | 91.6 (2,269 of 2,477) | 94.8 (2,150 of 2,269) | S3 |
| prior_rating | 2,477 | 81.7 (2,023 of 2,477) | 99.8 (2,019 of 2,023) | S3 |
| lending_test_rating (FRB/isb) | 196 | 75.0 (147 of 196) | 100.0 (147 of 147) | S3 |
| cd_test_rating (FRB/isb) | 196 | 76.0 (149 of 196) | 100.0 (149 of 149) | S3 |

Computed 2026-10-08 and recomputed 2026-10-14 with the same result after the test draw (seed 20261013) on 2,751 evaluable documents of split corpus with dev, test, audit, tuning-document banks and excluded rows left out; RULES_VERSION 0.2.4; record in `config/s5_routing.yaml`. exam_date is decided on the 998 OCC and FRB documents (FDIC exam_date has no index truth) and applies to FDIC documents.

2. Corpus tier. Rule: on dev, pooled Group B accuracy per tier = sum of n_correct / sum of (n_correct + n_wrong + n_fabricated_absence) over the 11 Group B rows of `table1_dev.csv`; the eligible tiers are those within the margin of the best tier; the corpus pass uses the eligible tier with the lowest mean `cost_usd` per dev document. Proposed margin: 3 percentage points. Dev holds about 110 Group B items per tier, so the margin is a cost rule, not a significance test.
   TODO: confirm the 3 percentage-point margin and record the chosen tier with its dev accuracy and cost per document.
3. Whole-document ablation (design 12, COULD 1). Rule: it runs on 20 test documents (the first 20 test doc_ids ordered by the sha256 hex digest of the UTF-8 doc_id, ascending; `run --hash-sample 20`) with the S3 corpus tier and the whole document as input for every field group, under its own system name, and its predictions enter the single test scoring run, if the projected LLM spend (spent so far + corpus pass estimate + ablation estimate from dev whole-document input tokens x prices) stays within the budget cap of design section 0. If it runs, the ablation table reports per field the accuracy of the located tier and of its `:whole` run on the documents of the `:whole` run, abstentions excluded, with the discordant counts over gold-value items both runs answered. The projected spend uses the projection rule of "Cost accounting".
   TODO: record yes or no for the ablation, with the projected spend and the dev token figures used.

The test size (30, or 25 under the 10-09 checkpoint rule) is not an open decision: the rule is stated under "Test split" and the outcome is recorded in strata.yaml.

## Deviations log and freeze

- `docs/deviations.md` (created at the first entry): one dated entry per change after the tag, with the ISO date in a heading or list item so `evaluate.deviation_dates` counts it; what changed and why; before and after metrics for any label correction after a scoring run. A second test scoring run needs its own entry.
- Freeze (`cra freeze`, freeze.py): this file must carry the headings "Scoring matrix", "Tolerances", "Audit design" and "Expected CI widths" (freeze.py:51) and no line containing the open-decision marker string (freeze.py:52, 186-203; a plain substring test, so the marker lines above, one per author decision still open, must be removed when filled); the working tree must be clean; the test split must be drawn and its doc_id hash present in `docs/hashes.md`. The freeze then appends a dated Freeze block to `docs/hashes.md` (RULES_VERSION, PROMPT_VERSION, CLOSED_BOOK_PROMPT_VERSION, the effort per backend, sha256 of `config/fields.yaml` and `config/headings.yaml`, test document count and doc_id hash), commits that file and creates the annotated tag `plan-v1.0`; an existing tag is moved only with `--force-retag`, never deleted. `cra freeze --check-only` rehearses.
- Order: exclusions set (`overrides apply`); tuning documents listed (`make tuning-docs`); `make scale`, so `results/sections_corpus.csv` carries the n_aa_sections count; test drawn and hashed; S5 routing computed and its table pasted; census sample drawn; corpus tier and ablation decisions filled; commit; freeze; test labeled blind; label hash committed; test run once (`make s5-test` before scoring); test scored once.

## Deviations from design v1

- bank_name: design 3 row 2 says token-set ratio; the code uses `token_sort_ratio` on punctuation-stripped names (evaluate.py:139-140, adopted in dev_notes 5).
- Accuracy denominator: design 6.2 lists correct null as a correct cell; `metrics_table` counts only gold-value items in accuracy (evaluate.py:386). The audit error includes nulls (Audit design).
- Overall rates: design 6.2 asks for error overall and conditional; the code gives `recall_overall` only for binary fields, without CI, and no overall accuracy column (evaluate.py:398). This plan derives them from table columns.
- Hallucination: NaN when grounding is partly known (evaluate.py:441-443), but finite when a quoting system has no known grounding at all (evaluate.py:431-438); the plan reports it only where `grounding_checked` is true.
- Disagreement flag: design 6.5 names S1 vs S3; the code compares with any other system in the file (evaluate.py:534-542). The plan restricts the input to the pair.
- Flags on abstentions: the code flags only answered values (evaluate.py:525, 531-533), so an abstained value never raises locator_miss and adds 0 to doc_risk.
- Risk groups: design 3 makes the component ratings Group B for OCC and FDIC; `RISK_GROUPS = ("B",)` (evaluate.py:60) leaves them out of doc_risk and calibration, and they are not audited.
- `matches_aa_section_count` (fields.yaml; labeling guide section 8 says evaluate checks it) never flags (evaluate.py:488-489, 510).
- lte_exam_date uses the system's own exam_date first and the doc_id date only as fallback (evaluate.py:545-549); dev_notes 5 says the doc_id date.
- Grounding scope: design 4.1(a) requires institution scope; the code also accepts pages with scope `unknown` (grounding.py:46, 256).
- Section scope: the section scope used by grounding comes from `scope_rules` of config/headings.yaml, not only from scope markers; the rules apply to all three regulators, and the review reset also overrides appendix scope.
- FDIC exam_date truth: design 3 row 4 makes the FDIC file-id date the index truth after gold verification; this plan gives FDIC exam_date no index truth and scores it on gold only, and `results/fdic_exam_date_gate.csv` is a reported diagnostic.
- Test and audit pools: design 5.1 and 5.3 remove dev and test banks; the code also removes the banks of the tuning documents (Leakage), so the audit frame is the corpus minus those banks.
- Group C boilerplate: design 4.1(c) says "not within edit distance k of a stored template"; the code tests whether the template residual asserts a finding, with no length threshold, and adds the predicates "cited" and "downgrade" (grounding.py:48, 467-499).
- Fabrication probes: the prompt states the applicability rule (prompts.py:165-169), so probes test rule following rather than unaided detection of absence.
- Dev composition: design 5.1 asks for 1 per cell plus 4 layout picks (FRB Kansas City, multi-state ISB, non-HMDA bank, OCC with Table O); the code draws the 8 fixtures, uncovered cells, one FRB PE outside the New York and Kansas City states and a random fill (strata.py:374-456).
- Dev draw record: draw-dev is not rerun; its recorded pools predate the exclusions.
- Test fallback: 25 documents with at least 3 per cell cannot be drawn next to the 5 + 4 fixed strata (strata.py:470-471); the plan uses `--per-cell 2` (Makefile:52).
- Multi-AA stratum: design says 3 or more assessment areas; the code counts distinct assessment-area units with one rule shared with the lone-AA scope rule (`locate.aa_units` through `run.count_aa_sections`; Test split, step 4) or, without the counts file, uses an assets proxy (strata.py:530-547). On the corpus the unified rule moved 10 documents across the n_aa_sections >= 3 line and changed no lone-AA decision.
- Audit tertiles: design says tertile cuts on the corpus distribution; the code takes rank thirds with random tie-breaking (strata.py:574-582), so documents with equal risk can fall in different strata.
- Audit size: design 5.3 assumes about 9 values per document (270); fields.yaml has 11 Group B fields (330 values).
- Exclusions: design 2 names no exclusion mechanism; `overrides apply` sets split `excluded` and the draws check it (`draw-test` and `draw-audit` refuse while a non-evaluable row is outside that split).
- Census intervals: design 5.2 step 5 says Wilson and its plan wording says exact; the plan uses exact, which `metrics_table` (Wilson) does not compute.
- Table 1: design 5.2 excludes Group C from the test table; `metrics_table` writes Group C rows into `table1_test.csv`; the report moves the Group C probe rows to the probe table and does not report the other Group C rows on test.
- Regeneration check: design 4.2 says `results/table1.csv` regenerates byte-identically; the check runs on `results/table1_{split}.csv`.
- S3 backend: design 4 and 4.3 say the Claude API with three tiers; on 2026-10-06 the author chose the Google AI Studio free tier as the primary hosted model, with the Claude tiers optional on gold (design 4.3 carries the note). The run stage offers `gemini`, `anthropic` and `openai_compat` (run.py:10) and the Makefile default system is `llm:gemini:gemini-3.8-flash` (Makefile:76).
- Test guard: design 6.1 says every test invocation is logged; refused invocations raise before the log is written (evaluate.py:644-647, 666), and `CRA_ALLOW_TEST_EVAL=1` bypasses the tag.
- Not implemented on 2026-10-08: a Batch API path (design 4.3). The other stages that the first version of this plan listed here were added on 2026-10-08 (S5 routing, Group A corpus scoring and the `results/risk_corpus.csv` writer, the census sample and scoring, the audit estimator and report modules, the whole-document mode, the closed-book baseline, the second live dev run), together with the Group B reliability module (`cra_extract.reliability`).
