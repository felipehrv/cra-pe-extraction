# Plan gaps to resolve before the freeze

Open points of `docs/analysis_plan.md` found while building and reviewing the stages of
2026-10-08 (overrides, locate scope rules, census, routing and corpus scoring, audit, report,
the run and llm extras, reliability). For each item the code already takes a position, stated
under "Code now"; the author either writes the proposed wording into the plan (or into its
"Deviations from design v1" section) or changes the code. Items marked "needed before first
use" block a step that comes before the freeze. `cra freeze` does not read this file; delete
it, or keep it as a record, once every box is ticked.

## 1. Exclusions, manifest overrides and the population

- [ ] **Exclusions section** (plan lines 19 to 26). Code now: `config/manifest_overrides.yaml`
  excludes FRB_614948_20250902 (pdf_unavailable_http_500) and OCC_9183_20230213
  (wrong_pdf_link), makes FRB_785473_20220418 evaluable (PDF portfolio, embedded 42-page PE
  extracted and parsed) and renames FDIC_445_20280408 to FDIC_445_20240408. Proposed
  wording: "Exclusions are listed with reason, evidence and date in
  config/manifest_overrides.yaml and applied by `python -m cra_extract.overrides apply`, which
  sets split excluded. Criteria: no fetched PDF, text_extractable false, or a PDF link that
  points to another evaluation (wrong_pdf_link, evidence: the cover date of the linked PDF).
  As of 2026-10-08: FRB_614948_20250902 (HTTP 500, data/index/fetch_failures.csv) and
  OCC_9183_20230213 (the index links /apr19/9183.pdf, the December 3, 2018 PE).
  FRB_785473_20220418 is a PDF portfolio; its embedded PE was extracted and parsed, so it is
  evaluable. Evaluable documents: 2,791."
- [ ] **Heterogeneity, scanned vs born-digital** (plan line 263). Proposed wording: "Dropped;
  no scanned document exists in the corpus (the one candidate, FRB_785473_20220418, was a PDF
  portfolio)."
- [ ] **doc_id policy for a corrected key field.** Code now: an override of regulator, bank_id
  or exam_date regenerates the doc_id and keeps the original as alias
  (FDIC_445_20240408, alias FDIC_445_20280408). Proposed wording: "A corrected regulator,
  bank_id or exam_date regenerates the doc_id; the original id stays an alias in
  config/manifest_overrides.yaml. The manifest exam_date of FDIC rows is the file-id date
  (exam_date_source fdic_file_id) except where exam_date_source is override (the printed
  date)."
- [ ] **Protected splits.** Code now: overrides refuse to exclude or rename a dev, test or audit
  row (the plan names dev and test). Proposed wording: add "or audit".
- [ ] **Dev draw record.** config/strata.yaml records the dev pools before the exclusions
  (frb_other_district 282, random_fill 2,783); a redraw on today's manifest gives the same 10
  documents and hash with pools 281 and 2,781. Proposed deviation line: "draw-dev is not
  rerun; its recorded pools predate the exclusions."
- [ ] **Non-evaluable rows.** Code now enforces the last Exclusions bullet: `overrides apply`
  and the draw-test and draw-audit CLIs refuse while a row without sha256 or with
  text_extractable not true is outside split excluded. Proposed wording: replace the
  deviation "no code sets split excluded" with "overrides apply sets it and the draws check it".

## 2. Leakage: tuning documents (needed before draw-test)

- [ ] **Corpus documents read while tuning.** The scope rules (config/headings.yaml) and the
  template library v2 (config/review_templates.yaml) were tuned by reading corpus documents
  that the test draw could select. Code now: `config/tuning_documents.yaml` lists 161
  documents (150 doc_ids cited in those two files, plus 11 the locate build read without
  citing them); `strata.eligible_pool` removes their banks from the test and audit pools
  (163 documents on the current manifest; FRB/isb pool 202 to 159) and the draw records the
  count and hash. `make tuning-docs` lists cited documents missing from the file, and the
  draws refuse while one is missing. Proposed wording for Leakage: "Corpus documents read
  while tuning before the test draw are tuning documents (config/tuning_documents.yaml,
  recorded by count and hash in strata.yaml); their banks are not eligible for test or
  audit." Decide whether the documents read for corpus-wide index agreement
  (config/group_a_triage.yaml, 31 documents, allowed by the plan) should also be listed;
  the code does not list them.
- [ ] **Audit frame.** With tuning banks out of the audit pool, N_h counts the corpus minus
  those banks, so the audit estimates the error of that frame. Proposed wording under Audit
  design, Population: "excluding dev and test banks, tuning-document banks and excluded rows".

## 3. Systems and versions

- [ ] **Effort level** (needed before the first pilot). Code now: llm.DEFAULT_EFFORT is low for
  gemini, anthropic and openai_compat and none for ollama; it is part of the replay key and
  trace_id, recorded in Trace.effort and in the Freeze block. Proposed row text: "effort: low
  (Gemini thinking_level LOW, Anthropic effort low); none for S4." Verify with one smoke call
  per tier that the models accept the parameter (a rejected parameter makes every field of
  the call abstain).
- [ ] **Closed-book prompt version.** prompts.CLOSED_BOOK_PROMPT_VERSION is cb1; the Freeze block
  now records it. Add it to the Systems table.
- [ ] **RULES_VERSION.** Bumped to 0.2.2 in this pass (no pattern change; S1 inputs changed with
  the scope rules and the manifest overrides; rules.py CHANGELOG). The plan says 0.2.1 "at
  the time of writing"; update the figure. results/rules.csv was regenerated at 0.2.2.
- [ ] **S5 in Table 1.** Code now: `make s5-test` appends S5 rows to predictions_test.csv before
  the single scoring run; S1-sourced S5 values carry grounding_failure na
  (evaluate.GROUNDING_NOT_GRADED), so groundedness covers only the S3-sourced values and a
  Table 1 row whose values are all S1-sourced shows the error rate without grounding.
  Proposed wording: "Table 1 reports S5 as a system; its S1-sourced values have no grounding
  component."
- [ ] **S5 without a value.** S5 takes the routed system's row as it is: an abstention of the
  routed system is an S5 abstention (no fallback), and S5 rows exist for documents that have
  S3 rows. State it under Systems.

## 4. Group A at corpus scale

- [ ] **FDIC exam_date truth.** Code now implements the plan's conditional rule:
  `group_a.fdic_exam_date_gate` compares the manifest exam_date of every FDIC dev and test
  gold document with the gold exam_date (results/fdic_exam_date_gate.csv); FDIC rows have
  exam_date index truth only when the gate passes, and an override row is treated like every
  other FDIC row. Dev passes (4 of 4); the gate stays pending until test is labeled, so the
  routing (computed before the freeze) has no FDIC exam_date truth and rests on 1,015 OCC and
  FRB documents. Corpus-wide the file-id date differs from the printed date on 127 FDIC
  documents (definitional), which the raw-index accuracy counts as errors if the gate passes.
  Decide: keep the conditional rule (state "routing is computed before the gate and uses no
  FDIC exam_date truth") or adopt "FDIC exam_date has no index truth and is scored on gold
  only" (then pass fdic_exam_date_truth False always).
- [ ] **Triaged truth.** Proposed wording: "Triaged truth: a source_error disagreement takes the
  printed value read in the triage (rule_value), a definitional one has no truth, an
  our_error one keeps the index value. The triage covers the S1 disagreements on bank_id,
  exam_date, exam_method and overall_rating; the triaged block is reported for S1 only."
  Code now does exactly this (corpus_scoring note column says "S1-triaged truth"). Extending
  the triage to S3 and S5 disagreements needs hand reading after the corpus pass.
- [ ] **Denominators.** Abstention rate over items with truth (index value or ABS-cell null);
  fabrication rate over every ABS-cell item, abstentions included (as metrics_table).
- [ ] **Scope and labels.** Every evaluable document of every split (dev, test, audit) is in the
  corpus figures, plus a test-only block; every row carries in_sample true. consistency_check is
  true for regulator and exam_method on S3, and on S5 only when S5 takes the field from S3.
- [ ] **Component ratings outside FRB/isb** take S3 in S5 and have no index truth (OCC Small
  lending equals overall but is not in the index; no truth is derived).
- [ ] **Group A flag validation.** Answered values with truth only; error = cell not correct (a
  fabrication in an ABS cell is an error); raw truth, triaged block only when the system is S1.
  The disagreement reference of an S1-sourced S5 value is S3.

## 5. S5 routing

- [ ] Thresholds are inclusive (integer comparison at exactly 95 and 98 percent).
- [ ] A field with no index value or no S1 value routes to S3.
- [ ] The exam_date route is decided on OCC and FRB documents and applies to FDIC documents.
- [ ] The routing population is split corpus and evaluable, so audit documents are left out
  once drawn (the plan names dev and test). Agreement is against the raw index.
- [ ] Code now refuses `routing compute` before the test draw unless --provisional, which writes
  under results/provisional/ and never to config/; `routing apply` refuses a provisional table.

## 6. Locator scope rules and the multi-AA count (needed before draw-test)

- [ ] **Grounding Stage 2(a)** (plan Metrics). Proposed sentence: "A located section's scope
  follows the scope rules of config/headings.yaml (scope_rules): the fair lending or other
  illegal credit practices review section is institution scope wherever its heading is
  printed, and in a document with exactly one assessment-area unit (locate.lone_aa_unit) the
  LTD ratio and AA concentration sections found in that unit's scope are institution scope;
  these rules set the section scope only, not the page scope." Add a deviation line: the
  section scope used by grounding comes from scope_rules, not only from scope markers. The
  rules apply to all three regulators and the review reset also overrides appendix scope.
  Matching sentence for design 4.1(a): "The fair lending or other illegal credit practices
  review is institution-wide in all three formats and is institution scope wherever the PE
  prints it (FRB_930442_20240715 p56), and in a PE with a single assessment area the LTD ratio
  and AA concentration sections printed after the assessment-area heading are institution
  scope."
- [ ] **How an assessment-area unit is counted** (Test split step 4). Code now: one rule,
  `locate.aa_units`, for the lone-AA scope rule and the strata count `run.count_aa_sections`:
  units are scope markers that opened an assessment_area or msa scope, deduplicated by MSA or
  MD code or by area name; a marker naming no area (the bare Dallas "DESCRIPTION OF THE
  INSTITUTION'S OPERATIONS") counts per occurrence; markers in the sticky appendix scope are
  ignored. On the corpus this moves 10 documents across the n_aa_sections >= 3 line
  (FRB_709143_20230130 goes from 1 to 3; 9 FDIC and OCC documents drop below 3 because
  glossary prose markers no longer count) and changes no lone-AA decision. Proposed wording
  for step 4: "n_aa_sections counts distinct assessment-area units (locate.aa_units): markers
  deduplicated by MSA or MD code or area name, a marker without a name counted per occurrence,
  markers in appendix scope ignored." Rerun `make scale` so results/sections_corpus.csv carries
  the new count before draw-test.
- [ ] **False markers still feed the count and the grounding scope.** FDIC "State of X" table
  cells (case-insensitive FDIC state pattern; FDIC_10077_20250218 p6) keep 282 of 1,699 FDIC
  LTD sections out of institution scope, and prose lines open false msa markers
  (FDIC_24540_20221114 p5). Decide before draw-test whether to make the FDIC state pattern
  case-sensitive (29 multistate FDIC PEs with mixed-case headings need reading first).

## 7. Group C census

- [ ] **Census seed** (needed before first use). The plan names none; `make census-draw`
  requires CENSUS_SEED and CENSUS_DATE. Proposed: fix a seed in step 4.
- [ ] **Timing and pool.** Code now refuses the draw before draw-test unless --before-test-draw;
  the pool leaves out dev, test and excluded rows and keeps audit rows. Proposed wording for
  step 4: "drawn after draw-test from unflagged located sections of split corpus (audit
  included)". If the census must be read before the test draw (design timeline 10-10 to
  10-12), state that and that later test documents can be in the sample.
- [ ] **Reading cap and misses.** Code now: the cap of 300 (200 under the fallback) counts the
  flagged located sections plus the locator misses to hand-locate; with 100 or more misses a
  random 50 (same seed, recorded) are hand-located and the miss rate is a census row. Today:
  53 flagged plus 8 misses after the regenerated census (FRB_785473 is now located). Step 3
  and step 5 still cite the first run (1,972 flagged, 92 misses); update the figures.
- [ ] **Gold Group C labels of dev and test** do not count as census reads; only census sheet
  rows are hand truth, and unflagged dev and test sections are negative by template.
- [ ] **A miss with no review section in the PDF** is coded finding_present false with a note.
- [ ] **Reliability negatives** are drawn from read random sections (largest-remainder
  allocation by regulator); fixture positives (FDIC_12299, FDIC_26816), whose values the
  labeling guide quotes, stay in the double read.
- [ ] **Kappa rule for enums and sets.** Kappa when each coding uses at least two categories
  (null codes are categories); sets element-wise.
- [ ] **Precision and recall** are conditional on answering, abstentions in their own row;
  attribute accuracy among positives is correct / (correct + wrong + fabricated_absence).
- [ ] **S0 false-positive rate.** Code now uses every census negative as the denominator (plan
  wording), so an S0 abstention on a locator miss is in it; reported for all, OCC, FDIC and FRB
  (the plan names OCC). S5 is scored as its own system s5 (= S3 for Group C).
- [ ] distractor_laws_named of an unread unflagged section comes from the best-matching
  template only. Counts are reported by regulator and by regulator x method.

## 8. Audit design and the review curve

- [ ] **Middle tertile positions** count from 1 (first sorted doc_id blind).
- [ ] **Shown rows** carry model_status (value, absent, abstain) besides the value and quote.
- [ ] **Released system and flags.** Code now: the audit uses system s5 (`make s5-apply` rows)
  and reads n_flags from results/risk_values_corpus.csv, the file behind risk_corpus.csv, so
  the tertiles, error by flag count and review curve share one flag input; estimate refuses
  when a document's n_flags sum differs from doc_risk. The S5 consistency flag
  lending_data_period_end <= exam_date reads S5's own exam_date, which comes from S1 when
  exam_date routes to S1. State both.
- [ ] **Bootstrap.** Code now resamples every audited document (an abstained value has weight
  0), so n_h is kept as the plan says, also per field.
- [ ] **No-error branches.** Per field: point estimate with no CI and a note. Overall: one
  Wilson upper bound per tertile; the plan does not say how to combine them into a corpus
  bound.
- [ ] **Point estimates only** for the abstention rate and the error by flag count; middle
  tertile by mode with Wilson (descriptive); minutes per value unweighted over every value.
- [ ] **Review curve denominator.** Code now: "all values" means answered values, for the
  Group B curve (design-weighted) and the Group A curve (answered values with index truth,
  raw truth); the cost table applies the operating points to the answered Group B values of the
  released system. Proposed wording: "y = errors among unreviewed answered values / all
  answered values".

## 9. Reliability (Group B)

- [ ] **Seed and timing** (needed before first use). Code now: `cra reliability draw --branch
  outside|fallback --seed S --date D` draws 8 test documents other than the eight fixtures, or
  6 dev documents, into config/reliability_sample.yaml; the fallback compare refuses rows
  labeled before 2026-10-13. Record the seed in the plan.
- [ ] **Fallback pool** includes the dev fixture documents (the author coded them and the
  labeling guide quotes them); exclude them if the weaker check should avoid recall.
- [ ] **Agreement.** An item is a document; values compared in canonical form (parsed dates,
  numbers, sorted sets, null codes as categories); kappa where each coding uses two or more
  categories, element-wise kappa for loan_products_evaluated; the manual-review error line is
  1 - pooled document-level agreement over the five fields (results/reliability.csv).

## 10. Report

- [ ] **S0 and S1 quotes.** The code gives S0 and S1 quotes and grounding outcomes; the report
  follows the plan and shows them without a grounding component.
- [ ] **Component probe cells** are the applicability ABS cells (fields.yaml); Group C probes are
  rating_before, rating_after and laws_cited on documents whose gold finding_present is false
  (code now); the other Group C rows are not reported on test.
- [ ] **Appendix and grounding file names**: table1_{split}_appendix.csv,
  table_grounding_{split}.csv. Byte-identical check on results/table1_{split}.csv (design 4.2
  says results/table1.csv).
- [ ] **Calibration inset** comes from the pair run (`make eval-pair`, results/pair/); caption
  names the pair.
- [ ] **Ablation table**: accuracy per field for the located tier and its :whole run on the
  documents of the :whole run, abstentions excluded; discordant counts over gold-value items
  both runs answered.
- [ ] **Closed-book** (needed before the closed-book run): both accuracies are reported, among
  answered items and overall with abstentions as misses, next to the majority-class rate;
  decide which is the contamination bound. Every closed-book value carries quote_not_found and
  the variant is left out of Table 1. The FDIC exam date in the prompt is the file-id date.
- [ ] **Cost.** Traces are attributed by Trace.mode (located, whole, closedbook; older traces
  without the field count as located, closed-book by field group); latest call per (document,
  field group, backend, model, mode, effort); a whole-mode call that replays a located-fallback
  trace with the same prompt is not a new trace. Tokens per call by mode; cost per correct
  field over every scored field; reviewer minutes from the shown mode; the manual baseline per
  field adds the blind-mode audit timers (per-document manual cost from the gold sheets only);
  corpus tier for the operating points from --corpus-system.
- [ ] **Descriptive rules**: NI and SN compliance-driven when any census positive has
  rating_effect substantive_lowered; publication lag in months for every regulator and days
  for OCC and FRB only; transitions, cycle length and lag over the whole manifest; the
  comparative-language word list.
- [ ] **ISB_OVERALL_FLOOR** rests on recollection of Appendix A to 12 CFR 228 and 345 and
  228.28(c) / 345.28(c). Someone must verify it against the regulation text in force on each
  exam date before any matrix-floor result is quoted.

## 11. Run stage

- [ ] **Second dev run.** Code now: `make dev-rerun` uses --trace-dir data/traces/dev_rerun and
  --results-dir results/dev_rerun (shared rate-limit cache), not `run --root`. Proposed wording
  for Leakage, run-to-run agreement: replace the --root sentence.
- [ ] **Agreement**: run A is the reference slot; items present in one run only are left out and
  counted (n_only_a, n_only_b); rows per (system, field).
- [ ] **Hash sample**: sha256 hex of the UTF-8 doc_id, ascending.
- [ ] **Projection**: input tokens = chars / 4 of system prompt, user prompt and schema, rounded
  up per call; output tokens from --output-tokens-per-call, else the trace mean.
- [ ] **Free-tier reset timezone**: America/Los_Angeles (config/prices.yaml
  gemini.free_tier.reset_timezone); the daily limiter counts retries.
- [ ] **openai_compat effort** low by default; use --effort none for providers that reject it.

## 12. Deviations section to update

- [ ] Remove from "Not implemented on 2026-10-08": S5 routing, Group A corpus scoring, the
  risk_corpus.csv writer, the census sample and scoring, the audit estimator and report
  modules, the whole-document mode, the closed-book baseline and the second live dev run. Add
  the Group B reliability module (cra_extract.reliability). The Batch API path remains.
- [ ] Remove the Effort bullet (run passes it, Trace.effort records it, the freeze pins it).
- [ ] Replace the Exclusions bullet ("no code sets split excluded") as in section 1.
- [ ] Update the Multi-AA bullet to the unified unit rule of section 6.
