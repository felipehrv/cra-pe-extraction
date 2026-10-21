# cra-pe-extraction

Bank regulators publish each CRA exam's overall rating as data but leave component ratings, illegal-credit-practice findings and the downgrades they cause, assessment-area structure, inside-area lending and loan-to-deposit ratios in PDF prose; this repository extracts those fields from the 2,791 evaluable Small and Intermediate Small Bank (ISB) Performance Evaluations (PEs) that the OCC, FDIC and Federal Reserve released in 2023 to 2025, and measures how far each value can be trusted.

- **Index-verified fields (Group A), agreement of the released system S5 with the agency indexes on the whole corpus, by regulator x method, Wilson 95% CIs:** regulator 2,790 of 2,790, overall rating 2,786 of 2,787, bank id 2,785 of 2,788, exam method 2,782 of 2,787, exam date 1,007 of 1,021 (OCC and FRB; the FDIC feed has no evaluation date), FRB ISB lending-test and CD-test ratings 202 of 202 and 201 of 202; the weakest cells are OCC bank names (90.6% for Small Banks, Wilson lower bound 87.2) and prior evaluation dates (91.7% to 99.0%), where the index and the PE format the same fact differently (`results/group_a_corpus.csv`)
- **Fields read from prose and tables (Group B), on the 30-document test set and from the 30-document audit:** the corpus tier, Gemini 3.5 Flash-Lite, answers every Group B item and gets 262 of 289 right (90.7%, Wilson 86.7 to 93.5), from 66.7% on the start of the lending data period to 100% on the number of assessment areas and the products evaluated; the rules answer 182 of 330 and get 175 right (96.2%, 92.3 to 98.1); 16 of the model's 27 misses are values it declared absent that the PE states. On the corpus, the 30-document audit puts the design-weighted Group B error of the released system at 14.9% (bootstrap 95% CI 11.8 to 18.2), higher than on test; the errors sit in three fields where the codebook leaves a tie (below)
- **Cost per document, S5 against a page-rate manual baseline:** every model call ran on the Google AI Studio free tier at $0.00 in API fees, with 8,181 input and 1,775 output tokens per PE on test recorded from the usage fields; reading a PE by hand at 60 pages per hour and the BLS median wage for financial examiners costs $19.37 for the average 25.7-page PE. Corpus spend was $0.00; the same 2,751 calls would cost $18.39 at the paid-tier list price, against $54,062 for reading the 2,791 evaluable PEs at the page rate
- **One verified row of the data-quality table (Table 2):** the FDIC feed does publish ISB Lending Test ratings, under Large Bank labels the ISB PEs never print; on all 9 gold FDIC ISB PEs the feed's "High Satisfactory" is a printed Satisfactory, "Outstanding" is Outstanding and "Low Satisfactory" is a printed Needs to Improve, so the column is usable only with a mapping the feed does not document

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

The page-wise locator finds each section in the 2,792 parsed PEs as below (`results/locator_coverage.csv`, after corpus pass 2 in `docs/corpus_pass_2.md`). Found is not verified: on 100 random PEs read by the model readers (`results/locator_recall.csv`), section recall is 1.00 for the cover, the rating page, the description of the institution and the review section (95% CI 0.96 to 1.00 each), 0.96 for the assessment-area concentration section (0.90 to 0.98) and 0.94 for the loan-to-deposit section (0.88 to 0.97), with precision 1.00 for every section; the 10 missed sections are in OCC PEs (6, among 17 sampled) and FDIC PEs (4, among 64), none in FRB PEs.

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
- **S1, rules** (`locate.py`, `rules.py`, RULES_VERSION 0.2.4 with a changelog): a page-wise locator that tags every page institution, state, MSA, assessment area, appendix or table of contents, with per-regulator headings in `config/headings.yaml`; regular expressions and a table parser over the located sections.
- **S3, hosted LLM** (`llm.py`, `prompts.py`, PROMPT_VERSION v5): one structured-output call per document that covers every field group, each group's located sections passed under its heading, or the whole document for a group whose section the locator missed (fallback rate reported). Model: `gemini-3.5-flash-lite` on the Google AI Studio free tier (the reported model id is stored per trace) on dev, test and the corpus; Gemini 3.8 Flash and 3.6 Flash returned 503 on every free-tier attempt on the run days, so Table 1 has one hosted tier and the Claude tiers of the design were not run. Effort is fixed at low, no temperature is sent, and reproducibility is by trace replay. The free tier allows 500 requests a day for this model, so the corpus pass runs over several days (`docs/dev_notes.md`). The prompt states only the regulator and exam method, so for S3 those two fields are consistency checks, not extraction results.
- **S4, local open-weight model**: the backend (`llm.py`, OpenAI-compatible endpoint), prompts, schema, grounding and tests are in place for Llama 3.1 8B or Qwen2.5 7B instruct, 4-bit, through Ollama (`num_ctx` 32768 with a prompt-fit assertion); it was not run in this version because Ollama was not installed on the build machine, so no S4 row appears in any table.
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

- **Work rule.** Pilot on dev, measure, fix versioned rules and prompts, freeze, have test labeled blind by model readers, score test once, scale to the corpus, audit.
- **Bank-level splits.** A bank (regulator, bank id) sits in at most one of dev, test and audit. The 334 corpus PEs read while tuning rules, locator scope, templates, the Group A triage or the locator check are listed in `config/tuning_documents.yaml`; the 339 PEs of their 332 banks left the test pool and stay out of the audit pool.
- **Gold labels from model readers.** Every gold PE (dev, test, the audit and the census sheets) is read independently by two Claude model readers, a model family different from the Gemini and local open-weight systems under evaluation, from the PDF pages with no access to any system output, fixture value or parsed text, following `docs/labeling_guide.md`; a third reader adjudicates every field where the two disagree, citing the page, and the label sheet records whether each value was agreed or adjudicated.
- **Test, scored once.** 30 PEs: 3 or 4 per regulator x method cell, 5 rated NI or SN, and 4 with three or more assessment areas; reported unweighted and never pooled with the audit. `evaluate --split test` refuses to run before the tag exists, logs every accepted invocation, and refuses a second scoring run without a dated deviations entry. A perfect 30 of 30 bounds accuracy only at 88.6% (Wilson), which is why Group A is scored on the corpus.
- **Group A on the corpus.** S1, S3 and S5 against the indexes on every evaluable PE, per regulator x method x year (18 cells), Wilson CIs, rows marked in-sample; each disagreement triaged as our error, source error or definitional, and accuracy reported against raw (primary) and triaged truth.
- **Group C census.** The review section of every PE is compared with 39 no-finding templates (per Reserve Bank district for the FRB). 61 sections are flagged (53 with residual text, a low match or an asserting sentence; 8 locator misses); those plus a random 100 of the 2,694 unflagged corpus-split sections (seed 20261009, `config/census_sample.yaml`) are read by hand, 161 in all, and every positive is read twice. Counts carry exact binomial CIs. The miss bound is the exact upper limit from the random 100 (0 of 100 gives 3.6%); no recall claim is made beyond it. S0, S3, S4 and S5 get precision and recall against the census.
- **Correctness x groundedness.** Per field and system: the 2x2 cells, the three failure categories, abstention, and the hallucination rate = (wrong + fabrication + fabricated absence + correct but misgrounded) / answered items, shown only where grounding was checked on every answered item. S0 and S1 get an error rate without a grounding component.
- **Audit of Group B on the corpus.** 30 PEs, 10 per tertile of document risk, where risk sums four flags over the 11 Group B values (locator miss, S1 and S3 disagreement, grounding failure, consistency failure; no fitted weights). Design weights N_h / n_h; bootstrap CI from 1,000 document resamples within tertile. Every audited PE is re-extracted blind by the model readers under the gold protocol; the design's shown mode, and its blind-versus-shown comparison, were withdrawn when the readers became models (`docs/analysis_plan.md`, Deviations from design v1).
- **Reliability.** Inter-reader agreement per field between the two model readers before adjudication, over all gold PEs (percent agreement, and Cohen's kappa where both classes occur), with the adjudication rate; there is no outside coder and no author re-coding. Agreement and adjudication rate: 3,044 of 3,051 items (99.8%, Wilson 99.5 to 99.9) over dev, test and the census sheets, 7 items adjudicated (0.2%); Cohen's kappa is 1.00 on every field except primary_product on dev (0.86), the lending data period dates on test (0.96 each) and practice_basis, finding_source and distractor_laws_named on the census (0.95 to 0.99) (`results/reliability.md`).
- **Closed-book bound.** The S3 corpus tier gets only the bank name and exam date of each test PE and is asked for the overall rating and finding_present; its accuracy is compared with the majority-class rate.
- **Run-to-run agreement.** Each S3 tier runs dev twice live (never test); agreement per field with Wilson CIs.

## Results

Each entry is filled from the results file named next to it, written by the stage that owns it (`docs/dev_notes.md`, section 1).

- **Table 1** (`results/table1_test.md`, from `results/table1_test.csv`): Flash-Lite answers all 330 Group B items (no abstentions) and gets 262 of the 289 with a gold value right (90.7%, Wilson 86.7 to 93.5): total assets and its date 29 of 30 each, number of assessment areas 30 of 30, products evaluated 30 of 30, loan-to-deposit ratio 29 of 30, primary product 27 of 30, inside-area share 26 of 30 and its product 25 of 30, full-scope count 12 of 13, lending data period start 12 of 18 and end 13 of 18. Of its 27 misses, 16 are values declared absent that the PE states (10 on the lending data period), 11 are wrong values; it also fabricated 6 values on items with no gold value. The rules answer 182 of 330 and get 175 right (96.2%, 92.3 to 98.1), abstaining on 148 (45%), never on the inside-area share and 5 times on total assets; S0 extracts no Group B field. Group A on test is a smoke test: S5 240 of 240, Flash-Lite 238 of 238 answered (2 bank ids abstained on a locator miss), the rules 228 of 232 answered. Component ratings: Flash-Lite 19 of 20 lending-test and 16 of 16 CD-test values; the one miss is a candidate label error left as coded (`docs/deviations.md`). Rows field x system; n, accuracy or precision and recall with CIs, abstention, fabrication with its denominator, grounded rate, hallucination rate.
- **Fabrication probes** (`results/table_probes_test.md`): on the applicability probes Flash-Lite filled 1 of 10 lending-test cells that the codebook marks absent (FDIC_16600, where the PE does print a rating; logged as a candidate label error) and 0 of 14 CD-test cells; the rules filled none. On the Group C probes (rating_before, rating_after, laws_cited on the 30 test PEs, none of which has a finding) every system fabricated 0 of 30. Outside the pre-registered probes, Flash-Lite answered `unstated` for finding_source on 11 of the 30 no-finding PEs, an enum choice where the field does not apply rather than an invented fact; Table 1 counts it as a fabrication and the error-cause table as a reasoning error. **Correctness x groundedness** (`results/table_grounding_test.csv`): of Flash-Lite's 279 answered Group B values, 249 carry a quote that passes both stages (89%); the 30 failures are 24 quotes that do not contain the value (mostly counts: the quote lists the assessment areas but not their number, 6 cases, and the full-scope count, 3) and 6 quotes not found on any page, with no out-of-scope quote. By cell, 241 correct and grounded, 21 correct but misgrounded, 6 wrong with a quote that supports the wrong value, 5 wrong and misgrounded, and 6 fabrications. Grounded rate per field runs from 0.58 (full-scope count) to 1.00 (total assets, loan-to-deposit ratio, finding_present).
- **Figure 1** (`results/figure1.png`): residual error against the share of values reviewed in risk order; Group A curve exact on the corpus, Group B curve from the audit, inter-reader disagreement line from the reliability check in place of a manual-review error rate, calibration inset labeled indicative. Reviewing values in flag order lowers the Group B residual error from 14.9% to 9.6% at 10% of values reviewed, 6.4% at 25% and 4.3% at 50%.
- **Group A on the corpus** (`results/group_a_corpus.csv`): see the first headline bullet; rows are marked in-sample, since the rules were tuned on this comparison.
- **Census** (`results/census_summary.csv`, `results/census_positives.csv`, `results/findings_registry.csv`): 23 of 2,791 PEs state a finding (0.82%, exact CI 0.52 to 1.23): OCC 1 of 604, FDIC 16 of 1,770, FRB 6 of 417; 6 had the rating lowered (0.21%, 0.08 to 0.47), 5 FDIC and 1 FRB, all at the overall level; 9 findings cite ECOA and Regulation B, 12 the FTC Act, 3 the Fair Housing Act. Miss bound: 0 of the 100 random unflagged sections was a finding, so the exact upper bound on misses among unflagged sections is 3.6%; the locator missed 8 review sections (0.3%), all read. By system, S0 finds 22 of the 23 (recall 95.7%) at precision 22 of 48 (45.8%), with 26 false positives, 14 of them in FRB PEs; S3 (and S5, which takes Group C from S3) finds all 21 census positives in the corpus split with no false positive (precision and recall 21 of 21, exact lower bound 83.9%) and all 5 lowered ratings among them; the other 2 positives are in dev and test.
- **Audit** (`results/audit_summary.csv`, `audit_by_tertile.csv`, `audit_by_field.csv`): 30 corpus PEs drawn 2026-10-20 after the corpus pass, 10 per risk tertile, read blind by two model readers (324 of 330 items agreed before adjudication, 98.2%). Design-weighted error of the released system's answered Group B values: 14.9% (bootstrap 95% CI 11.8 to 18.2), 9.1% in the low-risk tertile, 10.0% in the middle and 25.7% in the high. By field, the error is 36.7% on the inside-area share and its product, 30.0% on the lending data period dates and 20.0% on the primary product, 3.3% on assessment-area counts, full-scope counts and the loan-to-deposit ratio, and 0 on total assets and the products evaluated. The readers' notes show why: many audit PEs print the inside-area table by product with no Total row and weigh two products equally, so the codebook's rule (use the primary product's row) does not pick one, and the model and the readers choose different rows. Test (9.3% error on the same fields) was drawn to cover every regulator x method cell, and the audit by risk tertile, so the two answer different questions and are never pooled. The risk flags work as a review order: values with no flag are wrong 8.5% of the time, with one flag 50.0%, with two 75.0% (design-weighted).
- **Error causes** (cause column of `results/errors_test.csv`; parsing, locator miss, wrong scope, wrong period, unit or format, boilerplate confusion, reasoning, fabrication, each attributed to the stage that produced it): of Flash-Lite's 48 wrong values on test, 34 are reasoning errors (the located text states the value and the model gives another, 11 of them the `unstated` finding_source), 5 unit or format (lending data periods given as full-year dates where the PE states none or another span), 5 locator misses, 3 wrong period (a later quarter's total assets), 2 fabrications (inside-area shares that appear nowhere in the PE) and 2 candidate label errors; 41 arise at extraction, 5 at location, 2 at labeling, none at parsing (`results/table_error_causes_test.md`). The rules' 7 wrong values are 3 assessment-area counts, 2 product sets and 2 loan-to-deposit ratios.
- **Heterogeneity.** Group A by regulator x method x year (`results/group_a_corpus.csv`): S5 agreement is at least 99.1% in every cell for regulator, bank id, exam method and overall rating; the spread is in OCC bank names (90.6% Small Bank, 94.0% ISB) and OCC prior evaluation dates (91.7% ISB, 92.4% Small Bank). By regulator, the audit error is 16.9% on 22 FDIC PEs, 12.3% on 6 OCC PEs and 0 on 2 FRB PEs (design-weighted point estimates; regulator was not a stratum). Group B by regulator on test, Flash-Lite: OCC 78 of 84 (92.9%), FDIC 106 of 117 (90.6%), FRB 78 of 88 (88.6%), with 3 or 4 PEs per regulator x method cell, so the gaps sit inside the intervals (`results/table_group_b_by_regulator_test.md`). Census by regulator and method in `results/census_summary.csv`.
- **Contamination and stability.** Closed-book: given only the bank name and exam date, Flash-Lite abstained on 20 of 30 test PEs and got 6 of the 10 overall ratings it gave right (60%, Wilson 31 to 83) against a majority-class rate of 67%, and 10 of 10 on finding_present where the majority rate is 100%, no sign that the test ratings are memorized (`results/table_closed_book_test.md`). Run-to-run: two live runs of the same prompt on the 10 dev PEs agree on 299 of 310 field values (96.5%); 5 of the 11 differences are finding_source on PEs without a finding, the others one each on the lending data period dates, the inside-area product, laws_cited and practice_basis (`results/run_agreement_dev.csv`). Whole-document ablation: on the 20 test PEs with the lowest doc_id hash, the same model given the whole PE instead of the located sections got 181 of 191 Group B items (94.8%, 90.6 to 97.1) against 171 of 191 with the located sections (89.5%, 84.4 to 93.1), gaining 6 of the 10 on the lending data period dates, which the PE often states outside the located lending sections, at 2.9 times the input tokens (23,946 against 8,181 per PE); Group A and C were identical in both modes (`results/table_ablation_test.md`).
- **Exploratory, non-causal.** NI and SN ratings split into compliance-driven and performance-driven: of the 70 NI and SN ratings in the evaluable corpus, 6 are compliance-driven (the census codes a lowered rating; 8.6%, exact CI 3.2 to 17.7) and 64 performance-driven; FDIC 5 of 54, FRB 1 of 4, OCC 0 of 12 (`results/desc_low_rating_decomposition.csv`). FRB ISB matrix-floor check, rating transitions and publication lag: `results/descriptive.md`.

## Data-quality findings (Table 2)

`results/data_quality_findings.csv` has 37 rows, each with source, field, PEs affected, an example, a root cause and a recommendation: 23 source errors (the index or the PE contradicts the printed record), 7 definitional differences (both right under different definitions) and 7 errors of our own, fixed and kept on record. `make report` writes it sorted as `results/table2.md`. Five rows:

| source | field | PEs | root cause | what was found |
|---|---|---|---|---|
| FDIC CRAPES file id | exam_date | 127 | definitional | the date in the file id is not the printed evaluation date; most are 1 to 7 days later (`results/fdic_exam_date_lag.csv`) |
| OCC index | exam_date | 13 | source error | the index date appears nowhere in the PE; in 3 it is one year after the cover date |
| FDIC index | overall_rating | 1 | source error | the index says Satisfactory; the PE's rating page and conclusions say Outstanding (FDIC_15701_20241118) |
| OCC PEs | review section | 9 | source error | 8 keep the optional "rating was not lowered" template sentence after a no-finding statement; 1 prints the template with its placeholders unfilled |
| FRB Chicago PEs | section headings | 45 | our error | no-break spaces in the text layer defeated every heading pattern; fixed in the locator and in rules 0.2.1 |

Other rows: the FDIC feed gives release dates to the month (all 1,770 rows), so publication lag is reported in days only for the OCC and the FRB; 559 OCC index rows have no release date; five index rows give an exam method that contradicts the PE. The row on the FDIC feed's ISB lending ratings was held back until checked against gold: on the 9 gold FDIC ISB PEs the feed's Large Bank labels map one to one to the printed ISB scale (High Satisfactory to Satisfactory 7 of 7, Outstanding 1 of 1, Low Satisfactory to Needs to Improve 1 of 1), so it is now the headline row above.

## Cost (Table 3)

Prices are in `config/prices.yaml` with retrieval dates; tokens come from each API's usage fields, not estimates. Free-tier Gemini calls carry no API fee and the local model none beyond hardware; Claude tiers are priced at the listed rates. The human baseline is 60 pages per hour, the convention of Surani et al. (2025) for deed review, applied to each PE's page count at the BLS median hourly wage for Financial Examiners, $45.27 (Occupational Outlook Handbook, May 2025 data); the model readers' reported reading time is shown as reader time, not as a human baseline. `results/table3_test.md`:

| quantity | value |
|---|---|
| manual minutes per PE (page rate) and hourly wage with its source | no human timers exist; 25.7 pages per evaluable PE (26.9 on test) at 60 pages per hour gives 25.7 minutes, $19.37 per PE at the BLS median hourly wage for financial examiners, $45.27 (May 2025 data, retrieved 2026-10-14) |
| tokens per PE, located sections against whole document | test, Flash-Lite: 8,181 input and 1,775 output tokens per PE with located sections; 23,946 and 1,845 with the whole document (ablation, 19 PEs with usage recorded); 255 and 51 closed book |
| locator fallback rate, test and corpus | test 2 of 30 PEs (6.7%); corpus 36 of 2,751 PEs (1.3%) |
| cost per PE and per correct field, by system and tier | Flash-Lite on the free tier: $0.00 per PE and per correct field in API fees on test and the corpus; S0 and S1 run locally in well under a second per PE; no Claude tier was run; paid-tier equivalent at the Gemini Standard list price ($0.30 and $2.50 per million input and output tokens, retrieved 2026-10-16): $0.0069 per PE, $18.39 for the corpus |
| corpus spend, and extrapolation for tiers not run on the corpus | $0.00 spent on the free tier; $18.39 at the paid-tier list price; no other tier ran |
| S4 wall clock and hardware | not run (Ollama not installed on the build machine) |
| total cost and residual error when the top 10, 25 and 50% of values by risk are reviewed (`results/table3_test_review.csv`) | reviewing the top 10, 25 and 50% of the 24,983 answered Group B corpus values by flag count leaves 9.6, 6.4 and 4.3% residual error, at $4,399, $10,999 and $21,997 of reviewer time (2.33 minutes per value at the page rate) |

## What this borrows and what is new

Borrowed. Scoring hallucination as a wrong value or a quote that does not support it (correctness x groundedness) follows "Hallucination-Free? Assessing the Reliability of Leading AI Legal Research Tools" (Magesh et al., 2024). The closed-book probe, asking a model about a record it is not shown, follows "Large Legal Fictions: Profiling Legal Hallucinations in Large Language Models" (Dahl et al., 2024). Running an open-weight model locally beside hosted models, and pricing the pipeline against manual review, follows "AI for Scaling Legal Reform: Mapping and Redacting Racial Covenants in Santa Clara County" (Surani et al., 2025). A pre-registered plan, unit-level (bank) splits and a stratified audit with design weights are standard evaluation and survey practice.

New here: grounding that checks scope, so a quote from a state or assessment-area section cannot support an institution-level value in a multi-state or multi-area PE; an applicability codebook per (regulator, method) that turns structurally absent fields into fabrication probes; a template-residual census of a rare event with an exact miss bound; a root-cause table that separates our errors from source errors and definitional differences across three feeds; and a memo that turns the extraction work into reporting requirements.

## Limitations

| source | affected metric | direction | mitigation |
|---|---|---|---|
| test oversamples NI and SN ratings and multi-area PEs | Group B test error | overstates corpus error | test reported unweighted and apart; corpus error from the audit with design weights |
| LLM metrics are conditional on the locator finding the section | end-to-end error | understated | locator coverage and reader-checked recall reported first; fallback rate; the whole-document ablation shows the located input costs 10 of 191 Group B items on 20 PEs |
| the audit is 30 PEs, read blind by the model readers, with no shown-mode comparison | audit error | wide interval | design-weighted bootstrap CI over 1,000 document resamples within tertile; the audit is never pooled with test |
| the census sees only what the agency chose to state | finding prevalence | lower bound | stated; the OCC says it will consider later information at the next evaluation |
| rules and prompts tuned against the index they are scored on | Group A corpus agreement | overstated | rows marked in-sample; test-only block reported beside them |
| the prompt states the applicability rule | fabrication rate on probes | understated against unaided detection | stated; the probes measure rule following |
| gold, census and audit labels come from model readers, whose label noise may be shared with the evaluated models and correlate with their errors on hard fields | all gold, census and audit metrics | overstates accuracy | readers from a different model family (Claude readers, Gemini and local systems), a third reader adjudicating every disagreement, and the disputed-field list committed with the labels; inter-reader agreement 99.8%, 7 of 3,051 items adjudicated; two candidate label errors found after scoring are logged and left as coded |
| public PEs may be in model pretraining data | S3 and S4 accuracy | overstated | closed-book probe: 6 of 10 answered overall ratings right against a 67% majority class, 20 of 30 abstentions |
| born-digital PEs only | generality to scanned records | unknown | no scanned PE in the corpus; no OCR |

The primary hosted model runs on the Google AI Studio free tier, whose terms allow Google to retain and use the submitted text. That is acceptable here only because PEs are public records. For non-public records (exam workpapers, complaint narratives) the local model S4 is the option, and its accuracy against S3 is measured in Table 1.

## Memo to regulators

[`docs/memo.md`](docs/memo.md), two pages, lists the fields to publish in structured form, each with the regulator that already prints it, the discrepancy seen in the current feed, and the cost of extracting it from PDFs. The first three:

- ISB Lending Test and Community Development Test ratings as index fields for every regulator. The FRB CSV carries them on the ISB main row; the OCC index carries none.
- The printed evaluation date in the FDIC feed, which now gives a release month and a file-id date that differs from the printed date on 127 PEs.
- A structured record of illegal-credit-practice findings: whether one was identified, the laws, the source, and whether and which rating was lowered, from what to what. Today it is one review paragraph per PE (23 positives in the census, 6 with a lowered rating).

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
