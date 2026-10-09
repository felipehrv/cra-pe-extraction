# cra-pe-extraction

Bank regulators publish each CRA exam's overall rating as data but leave component ratings, illegal-credit-practice findings and the downgrades they cause, assessment-area structure, inside-area lending and loan-to-deposit ratios in PDF prose; this repository extracts those fields from the 2,791 evaluable Small and Intermediate Small Bank (ISB) Performance Evaluations (PEs) that the OCC, FDIC and Federal Reserve released in 2023 to 2025, and measures how far each value can be trusted.

- **Index-verified fields (Group A), agreement of the released system S5 with the agency indexes on the whole corpus, by regulator x method, Wilson 95% CIs:** {{HEADLINE_GROUP_A}}
- **Fields read from prose and tables (Group B), accuracy range on the 30-document test set and design-weighted corpus error from the 30-document audit:** {{HEADLINE_GROUP_B}}
- **Cost per document, S5 against the author's timed manual baseline:** {{HEADLINE_COST}}
- **One verified row of the data-quality table (Table 2):** {{HOOK_FINDING}}

[Memo to regulators](docs/memo.md) · [Pre-registered plan](docs/analysis_plan.md), frozen at tag [`plan-v1.0`](../../tree/plan-v1.0) · [Table 1](results/table1_test.csv) · [`make reproduce`](#reproduce) rebuilds Table 1 from stored traces without an API key.

## What the documents contain and why it matters
| | OCC index | FDIC index | FRB index | the PE |
|---|---|---|---|---|
| overall rating, exam method, bank id | yes | yes | yes | cover and rating page |
| evaluation date | yes | no: a release month, and a date inside the PDF file id | yes | cover |
| total assets | no | yes | yes | description of institution |
| ISB Lending Test and Community Development Test ratings | no | a lending field, not used until checked against gold PEs | ISB main row | rating page |
| illegal-credit-practice finding, laws cited, rating downgrade | no | no | no | review section |
| number of assessment areas, full-scope count, loan products reviewed | no | no | no | scope and description sections |
| share of loans inside the assessment areas, average loan-to-deposit ratio | no | no | no | performance conclusions |

Two dev-split PEs show what the feeds miss. SouthStar Bank (FDIC, ISB, 2022 exam, five assessment areas) is rated Satisfactory on both the Lending Test and the Community Development Test, yet its overall rating is Needs to Improve. The reason is a one-paragraph review section: examiners identified Equal Credit Opportunity Act and Fair Housing Act violations on the basis of redlining, and the FDIC lowered the rating from Satisfactory to Needs to Improve. The FDIC feed records only the final rating. Liberty Bank (FDIC, Small Bank, 2022 exam) is rated Substantial Noncompliance and reports 0.0 percent of its loans inside its single assessment area; its review section reports an unfair or deceptive practice under Section 5 of the FTC Act that harmed a majority of its mortgage borrowers and states that the finding did not change the rating, given performance that was already substantially deficient. A finding that did not move the rating leaves no trace in any feed.

CRA ratings are taken into account when a bank applies to open a branch, merge or acquire, and anyone who wants to know how often a low rating reflects compliance findings rather than lending performance has to read PDFs. This project measures what the documents say; it is not an enforcement scorecard. Results are reported by regulator without comparative language.

## Corpus and index

- **Sources.** The OCC CRA search API; the FDIC CRAPES API, pulled by month and exam method with an assertion that no query reaches the 500-row cap; the Federal Reserve CRA CSV, deduplicated to one main row per bank and exam with state and MSA rows stored apart. The FFIEC combined ratings file is not used (CAPTCHA-gated download, stale as of mid-2024, FDIC dates are release months).
- **Manifest** `config/corpus.csv`: 2,793 PEs examined under Small Bank or ISB procedures and released 2023-01-01 to 2025-12-31. FDIC 1,770 (981 Small, 789 ISB), OCC 605 (372, 233), FRB 418 (216, 202); 52 to 412 PEs per regulator x method x release-year cell (`results/corpus_counts.csv`). Overall ratings: 2,470 Satisfactory, 253 Outstanding, 65 Needs to Improve, 5 Substantial Noncompliance.
- **Exclusions**, each with evidence in `config/manifest_overrides.yaml`: FRB_614948_20250902 (HTTP 500 on every attempt in two runs) and OCC_9183_20230213 (the index links the 2018 PE). 2,791 PEs are evaluable. All are born-digital; the one apparent scan was a PDF portfolio whose embedded PE was extracted, so there is no OCR and no scanned versus born-digital cut.
- **Splits, by bank:** dev 10 (seed 20261007), test 30 (seed 20261013, drawn 2026-10-08, doc_id hash in `docs/hashes.md`), corpus 2,751, excluded 2. The audit sample of 30 is drawn from the corpus split after the corpus pass.

The page-wise locator finds each section in the 2,792 parsed PEs as below (`results/locator_coverage.csv`, after corpus pass 2 in `docs/corpus_pass_2.md`). Found is not verified: recall on 100 hand-checked random PEs is {{LOCATOR_RECALL}}.

| section | FDIC misses (of 1,770) | FRB misses (of 417) | OCC misses (of 605) | all misses (share found) |
|---|---|---|---|---|
| cover | 1 | 0 | 0 | 1 (99.96%) |
| rating | 0 | 0 | 3 | 3 (99.9%) |
| description of institution | 0 | 0 | 12 | 12 (99.6%) |
| scope of evaluation | 1 | 0 | 1 | 2 (99.9%) |
| loan-to-deposit ratio | 71 | 1 | 28 | 100 (96.4%) |
| assessment-area concentration | 21 | 1 | 29 | 51 (98.2%) |
| illegal-credit-practices review | 4 | 4 | 0 | 8 (99.7%) |
| assessment-area description | 18 | 178 | 605 | 801 (71.3%); OCC and many FRB PEs describe areas only under per-area headings |

S1 (rules 0.2.2) against the indexes, among answered PEs of the 2,792 parsed (`results/group_a_agreement.csv`): regulator 2,791 of 2,791, bank_id 2,786 of 2,789, overall_rating 2,786 of 2,787, exam_method 2,783 of 2,788, exam_date 2,649 of 2,792. These figures are in-sample, since the rules were tuned against this comparison. Every disagreement of either rules version was read against the PE (`results/group_a_triage_summary.csv`): 40 our errors (all exam_method, all fixed), 25 source errors, 129 definitional differences, 127 of them FDIC dates.

## Fields and codebook

Data dictionary: `config/fields.yaml`. Codebook: `docs/labeling_guide.md`, where every judgment call cites a line of one of the eight sample PEs in `tests/fixtures/samples/`. Fields are grouped by how they can be scored, not by difficulty.

| group | fields | truth | scored on |
|---|---|---|---|
| A (8) | regulator, bank_name, bank_id, exam_date, exam_method, overall_rating, prior_evaluation_date, prior_rating | agency index; prior fields from the bank's previous index row; FDIC exam_date has no index truth | whole corpus; test as a smoke test |
| component ratings (2) | lending_test_rating, cd_test_rating | FRB CSV in the FRB ISB cell, gold elsewhere | corpus (FRB ISB and probes); test |
| B (11) | total_assets_usd and its as-of date, lending data period start and end, n_assessment_areas, n_full_scope, loan_products_evaluated, primary_product, inside_aa_share_number_pct and its product, avg_ltd_ratio_pct | gold labels | test (Table 1); audit for corpus error |
| C (10, plus 1 flag) | finding_present, finding_type, rating_effect, rating_lowered, rating_lowered_level, rating_before, rating_after, laws_cited, practice_basis, finding_source; distractor_laws_named is a reporting flag | hand-read census of review sections | census; on test only as fabrication probes |
| D (stretch) | per-assessment-area home-mortgage distribution rows | gold, FDIC and OCC | exploratory, on a go decision |

Group C rule, verbatim in the codebook: "A finding exists only if the review section of this PE states that discriminatory or other illegal credit practices were identified and considered in this rating." A finding recalled from a prior PE, an enforcement action mentioned in the description of the institution, and the OCC forward-looking boilerplate are not findings; a UDAP violation is other_illegal_credit_practice, not discrimination.

Null vocabularies. Gold: `value`, `not_applicable` (NA, set by rule), `absent_from_document` (ABS, set after searching the listed sections). System: `value`, `absent`, `abstain`. Applicability is a function of (regulator, method), and each cell cites its evidence in the codebook:

| field | OCC Small | OCC ISB | FDIC Small | FDIC ISB | FRB Small | FRB ISB |
|---|---|---|---|---|---|---|
| lending_test_rating | value (equals overall) | value | ABS | value | ABS | value |
| cd_test_rating | ABS | value | ABS | value | ABS | value |

The ABS cells are fabrication probes, as are rating_before, rating_after and laws_cited on every document without a finding. The FRB Small cells rest on one Kansas City PE until checked in other districts.

Scoring matrix (`evaluate.score_cell`):

| gold \ system | value | absent | abstain |
|---|---|---|---|
| value | correct or wrong under the field's tolerance | fabricated absence (wrong) | abstention |
| NA or ABS | fabrication | correct null | abstention |

Accuracy = correct / (correct + wrong + fabricated absence) over gold-value items. Fabrication rate = fabrications / gold NA and ABS items of the field, with that denominator in its own column. Abstentions are left out of conditional metrics and reported as a rate. Tolerances: exact for enums, dates and counts; 0.1 percentage points for shares and ratios; 1 percent and the same quarter for total assets; token-sort ratio of at least 90 for bank names.

## Systems

- **S0, keyword baseline** (`baseline.py`): sentence-level triggers with negation over the located review section, the search an analyst would run first. On corpus pass 2 it marked 48 PEs positive; 23 of them are negated boilerplate.
- **S1, rules** (`locate.py`, `rules.py`, RULES_VERSION 0.2.2 with a changelog): a page-wise locator that tags every page institution, state, MSA, assessment area, appendix or table of contents, with per-regulator headings in `config/headings.yaml`; regular expressions and a table parser over the located sections.
- **S3, hosted LLM** (`llm.py`, `prompts.py`, PROMPT_VERSION v2): one structured-output call per field group over that group's located sections, or the whole document when the locator misses (fallback rate reported). Primary model: Gemini Flash on the Google AI Studio free tier (`gemini-3.8-flash`, the reported model id stored per trace) on dev, test and the corpus. Optional tiers on gold only, run if the Anthropic console credit is available: Claude Haiku 4.5, Sonnet 5.5 and Opus 5.5, each its own row. Effort is fixed at low, no temperature is sent, and reproducibility is by trace replay. The prompt states only the regulator and exam method, so for S3 those two fields are consistency checks, not extraction results.
- **S4, local open-weight model**: Llama 3.1 8B or Qwen2.5 7B instruct, 4-bit, through Ollama (`num_ctx` 32768 with a prompt-fit assertion) on an Apple M5 with 24 GB; same prompts, schema and grounding as S3; all gold documents plus a 100-document corpus sample, wall clock recorded.
- **S5, routed composite** (`routing.py`): for each Group A field, S1 when its coverage is at least 95% and its agreement with the raw index at least 98% on the corpus split; otherwise the S3 corpus tier, with an S1 disagreement raised as a risk flag. Group B and C fields always take S3. Computed 2026-10-08 after the test draw, on 2,751 corpus-split PEs (`results/s5_routing.md`, record in `config/s5_routing.yaml`):

| field | n with index value | S1 coverage | S1 agreement | route |
|---|---|---|---|---|
| regulator | 2,751 | 100.0 (2,750 of 2,751) | 100.0 (2,750 of 2,750) | S1 |
| bank_name | 2,751 | 99.9 (2,748 of 2,751) | 97.6 (2,681 of 2,748) | S3 |
| bank_id | 2,751 | 99.9 (2,748 of 2,751) | 99.9 (2,745 of 2,748) | S1 |
| exam_date (OCC and FRB only) | 998 | 100.0 (998 of 998) | 98.6 (984 of 998) | S1 |
| exam_method | 2,751 | 99.9 (2,747 of 2,751) | 99.8 (2,742 of 2,747) | S1 |
| overall_rating | 2,751 | 99.9 (2,747 of 2,751) | 100.0 (2,746 of 2,747) | S1 |
| prior_evaluation_date | 2,477 | 91.6 (2,269 of 2,477) | 94.8 (2,150 of 2,269) | S3 |
| prior_rating | 2,477 | 81.7 (2,023 of 2,477) | 99.8 (2,019 of 2,023) | S3 |
| lending_test_rating (FRB ISB) | 196 | 75.0 (147 of 196) | 100.0 (147 of 147) | S3 |
| cd_test_rating (FRB ISB) | 196 | 76.0 (149 of 196) | 100.0 (149 of 149) | S3 |

**Grounding** (`grounding.py`) is rule-based, in two stages, with no LLM judge. Every S3 and S4 value carries a verbatim quote. Stage 1 finds the quote on a page (rapidfuzz partial ratio of at least 90 after normalization). Stage 2 checks scope (an institution-level field needs an institution-scope quote, so a state's CD rating cannot support the bank's), value presence (the value, date or number within tolerance appears in the quote), and for Group C that the quote asserts a finding and is not template boilerplate. Failures fall in three categories: `quote_not_found`, `quote_out_of_scope`, `quote_does_not_support`. No judge model is needed because every field is an enum, a date, a number, a set or an index-verified string. Every call is stored as a trace (model requested and reported, prompt hash, effort, input mode, tokens from the usage fields, cost, latency, date); the replay client raises on a missing trace unless `--allow-api` is passed.

## Evaluation design

Pre-registered in `docs/analysis_plan.md` and frozen as tag `plan-v1.0` before test labeling; any later change is a dated entry in `docs/deviations.md`, with before and after metrics for a label correction.

- **Work rule.** Pilot on dev, measure, fix versioned rules and prompts, freeze, label test blind, score test once, scale to the corpus, audit.
- **Bank-level splits.** A bank (regulator, bank id) sits in at most one of dev, test and audit. The 234 corpus PEs read while tuning rules, locator scope, templates or the Group A triage are listed in `config/tuning_documents.yaml`; the 235 PEs of their banks left the test pool and stay out of the audit pool.
- **Test, scored once.** 30 PEs: 3 or 4 per regulator x method cell, 5 rated NI or SN, and 4 with three or more assessment areas; reported unweighted and never pooled with the audit. `evaluate --split test` refuses to run before the tag exists, logs every accepted invocation, and refuses a second scoring run without a dated deviations entry. A perfect 30 of 30 bounds accuracy only at 88.6% (Wilson), which is why Group A is scored on the corpus.
- **Group A on the corpus.** S1, S3 and S5 against the indexes on every evaluable PE, per regulator x method x year (18 cells), Wilson CIs, rows marked in-sample; each disagreement triaged as our error, source error or definitional, and accuracy reported against raw (primary) and triaged truth.
- **Group C census.** The review section of every PE is compared with 39 no-finding templates (per Reserve Bank district for the FRB). 61 sections are flagged (53 with residual text, a low match or an asserting sentence; 8 locator misses); those plus a random 100 of the 2,694 unflagged corpus-split sections (seed 20261009, `config/census_sample.yaml`) are read by hand, 161 in all, and every positive is read twice. Counts carry exact binomial CIs. The miss bound is the exact upper limit from the random 100 (0 of 100 gives 3.6%); no recall claim is made beyond it. S0, S3, S4 and S5 get precision and recall against the census.
- **Correctness x groundedness.** Per field and system: the 2x2 cells, the three failure categories, abstention, and the hallucination rate = (wrong + fabrication + fabricated absence + correct but misgrounded) / answered items, shown only where grounding was checked on every answered item. S0 and S1 get an error rate without a grounding component.
- **Audit of Group B on the corpus.** 30 PEs, 10 per tertile of document risk, where risk sums four flags over the 11 Group B values (locator miss, S1 and S3 disagreement, grounding failure, consistency failure; no fitted weights). Design weights N_h / n_h; bootstrap CI from 1,000 document resamples within tertile. Low tertile re-extracted blind, high tertile with model output shown, middle tertile alternating; minutes per value recorded in both modes.
- **Reliability.** Preferred: an outside coder double-codes 8 test PEs on the hard Group B fields, every census positive and 20 template negatives (Cohen's kappa). Fallback, labeled the weaker check: the author re-codes 6 dev PEs at least 5 days later. Branch run: {{RELIABILITY_BRANCH}}.
- **Closed-book bound.** The S3 corpus tier gets only the bank name and exam date of each test PE and is asked for the overall rating and finding_present; its accuracy is compared with the majority-class rate.
- **Run-to-run agreement.** Each S3 tier runs dev twice live (never test); agreement per field with Wilson CIs.

## Results

Each entry is filled from the results file named next to it, written by the stage that owns it (`docs/dev_notes.md`, section 1).

- **Table 1** (`results/table1_test.md`, from `results/table1_test.csv`): {{TABLE1}}. Rows field x system; n, accuracy or precision and recall with CIs, abstention, fabrication with its denominator, grounded rate, hallucination rate.
- **Fabrication probes** (`results/table_probes_test.md`): {{PROBES}}. **Correctness x groundedness** (`results/table_grounding_test.csv`): {{GROUNDING_CELLS}}.
- **Figure 1** (`results/figure1.png`): residual error against the share of values reviewed in risk order; Group A curve exact on the corpus, Group B curve from the audit, manual-review error line from the reliability check, calibration inset labeled indicative. {{FIGURE1_NOTE}}
- **Group A on the corpus** (`results/group_a_corpus.csv`): {{GROUP_A_CORPUS}}.
- **Census** (`results/census_summary.csv`, `results/census_positives.csv`): {{CENSUS_POSITIVES}} positives, {{CENSUS_LOWERED}} with the rating lowered, miss bound {{CENSUS_MISS_BOUND}}; precision and recall by system {{CENSUS_SYSTEM_PR}}.
- **Audit** (`results/audit_summary.csv`, `audit_by_tertile.csv`, `audit_by_field.csv`): {{AUDIT_ERROR}}.
- **Error causes** (cause column of `results/errors_test.csv`; parsing, locator miss, wrong scope, wrong period, unit or format, boilerplate confusion, reasoning, fabrication, each attributed to the stage that produced it): {{ERROR_CAUSES}}.
- **Heterogeneity.** Group A by regulator x method x year: {{HETEROGENEITY_GROUP_A}} (`results/group_a_corpus.csv`). Group B by regulator on test: {{HETEROGENEITY_GROUP_B}} (`results/scored_test.csv`). Census by regulator and method in `results/census_summary.csv`.
- **Contamination and stability.** Closed-book: {{CLOSED_BOOK_BOUND}} (`results/table_closed_book_test.md`). Run-to-run: {{RUN_AGREEMENT}} (`results/run_agreement_dev.csv`). Whole-document ablation, if run: {{ABLATION}} (`results/table_ablation_test.md`).
- **Exploratory, non-causal.** NI and SN ratings split into compliance-driven and performance-driven: {{NI_SN_DECOMPOSITION}} (`results/desc_low_rating_decomposition.csv`). FRB ISB matrix-floor check, rating transitions and publication lag: `results/descriptive.md`.

## Data-quality findings (Table 2)

`results/data_quality_findings.csv` has 37 rows, each with source, field, PEs affected, an example, a root cause and a recommendation: 23 source errors (the index or the PE contradicts the printed record), 7 definitional differences (both right under different definitions) and 7 errors of our own, fixed and kept on record. `make report` writes it sorted as `results/table2.md`. Five rows:

| source | field | PEs | root cause | what was found |
|---|---|---|---|---|
| FDIC CRAPES file id | exam_date | 127 | definitional | the date in the file id is not the printed evaluation date; most are 1 to 7 days later (`results/fdic_exam_date_lag.csv`) |
| OCC index | exam_date | 13 | source error | the index date appears nowhere in the PE; in 3 it is one year after the cover date |
| FDIC index | overall_rating | 1 | source error | the index says Satisfactory; the PE's rating page and conclusions say Outstanding (FDIC_15701_20241118) |
| OCC PEs | review section | 9 | source error | 8 keep the optional "rating was not lowered" template sentence after a no-finding statement; 1 prints the template with its placeholders unfilled |
| FRB Chicago PEs | section headings | 45 | our error | no-break spaces in the text layer defeated every heading pattern; fixed in the locator and in rules 0.2.1 |

Other rows: the FDIC feed gives release dates to the month (all 1,770 rows), so publication lag is reported in days only for the OCC and the FRB; 559 OCC index rows have no release date; five index rows give an exam method that contradicts the PE. One seed row, on the component ratings in the FDIC feed, stays out of this README until it is checked against at least three gold PEs.

## Cost (Table 3)

Prices are in `config/prices.yaml` with retrieval dates; tokens come from each API's usage fields, not estimates. Free-tier Gemini calls carry no API fee and the local model none beyond hardware; Claude tiers are priced at the listed rates. The manual baseline is the author's own per-field timer during gold labeling at a stated wage, not an assumed pages-per-hour rate. `results/table3_test.md`:

| quantity | value |
|---|---|
| manual minutes per PE (gold timers) and hourly wage with its source | {{MANUAL_BASELINE}} |
| tokens per PE, located sections against whole document | {{TOKENS_PER_DOC}} |
| locator fallback rate, test and corpus | {{FALLBACK_RATE}} |
| cost per PE and per correct field, by system and tier | {{COST_BY_SYSTEM}} |
| corpus spend, and extrapolation for tiers not run on the corpus | {{CORPUS_COST}} |
| S4 wall clock and hardware | {{S4_WALL_CLOCK}} |
| total cost and residual error when the top 10, 25 and 50% of values by risk are reviewed (`results/table3_test_review.csv`) | {{REVIEW_OPERATING_POINTS}} |

## What this borrows and what is new

Borrowed. Scoring hallucination as a wrong value or a quote that does not support it (correctness x groundedness) follows "Hallucination-Free? Assessing the Reliability of Leading AI Legal Research Tools" (Magesh et al., 2024). The closed-book probe, asking a model about a record it is not shown, follows "Large Legal Fictions: Profiling Legal Hallucinations in Large Language Models" (Dahl et al., 2024). Running an open-weight model locally beside hosted models, and pricing the pipeline against manual review, follows "AI for Scaling Legal Reform: Mapping and Redacting Racial Covenants in Santa Clara County" (Surani et al., 2025). A pre-registered plan, unit-level (bank) splits and a stratified audit with design weights are standard evaluation and survey practice.

New here: grounding that checks scope, so a quote from a state or assessment-area section cannot support an institution-level value in a multi-state or multi-area PE; an applicability codebook per (regulator, method) that turns structurally absent fields into fabrication probes; a template-residual census of a rare event with an exact miss bound; a root-cause table that separates our errors from source errors and definitional differences across three feeds; and a memo that turns the extraction work into reporting requirements.

## Limitations

| source | affected metric | direction | mitigation |
|---|---|---|---|
| test oversamples NI and SN ratings and multi-area PEs | Group B test error | overstates corpus error | test reported unweighted and apart; corpus error from the audit with design weights |
| LLM metrics are conditional on the locator finding the section | end-to-end error | understated | locator coverage and hand-checked recall reported first; fallback rate; whole-document ablation if run |
| audit reviewers shown model output accept it more often | audit error | understated in the high tertile | blind re-extraction in the low tertile; blind and shown compared in the middle tertile |
| the census sees only what the agency chose to state | finding prevalence | lower bound | stated; the OCC says it will consider later information at the next evaluation |
| rules and prompts tuned against the index they are scored on | Group A corpus agreement | overstated | rows marked in-sample; test-only block reported beside them |
| the prompt states the applicability rule | fabrication rate on probes | understated against unaided detection | stated; the probes measure rule following |
| one labeler (the author) for gold, census and audit | all gold, census and audit metrics | unknown | reliability check, branch {{RELIABILITY_BRANCH}}; the fallback is the weaker check, and its pool includes dev PEs the codebook quotes |
| public PEs may be in model pretraining data | S3 and S4 accuracy | overstated | closed-book bound {{CLOSED_BOOK_BOUND}} |
| born-digital PEs only | generality to scanned records | unknown | no scanned PE in the corpus; no OCR |

The primary hosted model runs on the Google AI Studio free tier, whose terms allow Google to retain and use the submitted text. That is acceptable here only because PEs are public records. For non-public records (exam workpapers, complaint narratives) the local model S4 is the option, and its accuracy against S3 is measured in Table 1.

## Memo to regulators

[`docs/memo.md`](docs/memo.md), two pages, lists the fields to publish in structured form, each with the regulator that already prints it, the discrepancy seen in the current feed, and the cost of extracting it from PDFs. The first three:

- ISB Lending Test and Community Development Test ratings as index fields for every regulator. The FRB CSV carries them on the ISB main row; the OCC index carries none.
- The printed evaluation date in the FDIC feed, which now gives a release month and a file-id date that differs from the printed date on 127 PEs.
- A structured record of illegal-credit-practice findings: whether one was identified, the laws, the source, and whether and which rating was lowered, from what to what. Today it is one review paragraph per PE ({{CENSUS_POSITIVES}} positives in the census).

## Reproduce

```bash
make env               # native Python 3.11+; on Apple silicon it must be arm64 (the Makefile has the conda line for x86_64 Anaconda)
cp .env.example .env   # GEMINI_API_KEY (free tier); ANTHROPIC_API_KEY and OLLAMA_BASE_URL are optional
make test              # pytest -m "not network": no network, no API key
make reproduce         # rescore test from the committed predictions, regenerate every table and Figure 1, fail on any byte difference
```

`make reproduce` needs no API key. The test predictions come from the stored traces in `data/traces/test/`: `cra run --split test` without `--allow-api` rebuilds them and raises on any missing trace. Each stage also runs as `cra <stage>` or `python -m cra_extract.<stage>` (`docs/dev_notes.md`, section 1). Stage order before the freeze: `index` (or `index-replay` from the saved raw pull), `fetch`, `parse`, `overrides`, `locate`, `templates`, `rules`, `baseline`, `group-a`, `tuning-docs`, `scale`, `draw-test`, `s5-routing`, `census-draw`, `freeze`. After it: label test blind, `cra run --split test` once with `ablation`, `closed-book` and `s5-test`, then `eval-test`, `eval-pair`, `s5-apply`, `group-a-corpus`, `risk-corpus`, `draw-audit`, `audit-sheet`, `audit`, the census sheet and `census-score`, the reliability targets, and `report`. Live LLM calls need `RUN_FLAGS=--allow-api`.

CI (`.github/workflows/ci.yml`) runs `ruff check` and `pytest -m "not network"` on Python 3.12 on every push and pull request, with no API key. The byte-identical check of `make reproduce` joins CI once the test predictions and traces are committed.

## Repo map

```
src/cra_extract/
  index.py           manifest and per-agency index files from the OCC, FDIC and FRB sources
  fetch.py           PDF download with rate limit, retries, resume and sha256 check
  parse.py           page-wise text extraction (pymupdf)
  overrides.py       documented manifest corrections and exclusions
  locate.py          page-wise, scope-aware section locator
  rules.py           S1 rule extractors over located sections, versioned with a changelog
  baseline.py        S0 keyword baseline for the illegal-credit-practices finding
  templates.py       template-residual screen of every review section
  census.py          Group C census: random draw, reading sheet, validation, scoring
  group_a.py         S1 against the agency indexes, with disagreement triage
  strata.py          dev, test and audit draws, bank exclusivity, tuning-document pools
  label.py           gold label sheets: initialise, validate, load, hash
  prompts.py         prompts and JSON schemas built from config/fields.yaml
  llm.py             S3 and S4 clients behind one interface, traces, replay client
  run.py             runs the systems over a split; predictions, sections, cost projection
  grounding.py       two-stage, rule-based grounding check
  routing.py         S5 routing table and S5 rows
  evaluate.py        scoring matrix, tolerances, metrics table, risk flags, test guard
  corpus_scoring.py  Group A on the corpus and the corpus risk file
  audit.py           stratified audit sheet and design-weighted estimator
  reliability.py     second blind coding of gold documents
  stats.py           Wilson, Clopper-Pearson and stratified bootstrap intervals
  report.py          every table and figure from results/, byte-identical regeneration check
  freeze.py          plan freeze: checks, freeze record, tag plan-v1.0
  models.py          shared data contracts
  config.py          paths and configuration loading
  cli.py             the `cra <stage>` dispatcher
config/              fields.yaml (data dictionary, applicability), corpus.csv (manifest), headings.yaml, review_templates.yaml,
                     manifest_overrides.yaml, strata.yaml, tuning_documents.yaml, group_a_triage.yaml, s5_routing.yaml,
                     census_sample.yaml, prices.yaml
docs/                design.md, analysis_plan.md, labeling_guide.md, dev_notes.md, corpus_pass_2.md, plan_gaps.md, hashes.md, memo.md
data/                index/ (agency pulls), gold/ (label, census and audit sheets), traces/ (LLM calls); pdfs/ and text/ fetched on demand
results/             committed tables, figures and data-quality findings
tests/               tests for every stage module; fixtures/samples/ holds the eight sample PEs and their text
.github/workflows/   CI
```

License: MIT. PEs are US government works.
