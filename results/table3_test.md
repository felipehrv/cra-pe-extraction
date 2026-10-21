# Table 3. Cost (test split)

Tokens are the usage fields of the latest call per document and field group; cost per document is those tokens at the prices in config/prices.yaml (empty while a price is not set); cost spent is the sum recorded in every trace, superseded calls included. Locator fallback = whole-document calls over calls with a known input mode. Pricing mode: standard (no Batch API path). A free-tier call is billed 0; the paid-tier columns price the same tokens at the provider's list price (list_input and list_output in config/prices.yaml). Manual baseline: the page-rate convention of the plan (pages of each document at manual_baseline.pages_per_hour in config/prices.yaml, from config/corpus.csv); wage: 45.27 USD per hour. Reader time: the model readers' reported minutes (labels_dev.csv, labels_test.csv, audit_sheet.csv (blind mode, per field only)), not a human baseline. Corpus: 2791 evaluable documents.

Field-group mode used: llm:gemini:gemini-3.5-flash-lite: all; llm:gemini:gemini-3.5-flash-lite:whole: all. Field groups: all = one LLM call per document for every field (run --field-groups all, the default), split = one call per llm_field_groups entry of config/fields.yaml; closed book is one call per document in either mode. Tokens per document count each call once, so an all call is counted once for its document; no per-field-group token view is given, since one all call serves every group. A document with calls of both modes counts the mode of its last successful run-log entry (else that of its newest trace); the calls of the other mode enter cost spent only. Under all, the one call of a document sends the whole document when any field group had no located section, so locator fallback is a share of documents; under split it is a share of field-group calls.

| System | Field groups | Documents | Calls | Locator fallback | Input tokens per call, located | Input tokens per call, whole document | Tokens per document (in / out) | Price | Cost spent (USD) | Cost per document (USD) | Cost per document at paid-tier list price (USD) | Cost per correct field (USD) | Corpus cost (USD) | Corpus cost at paid-tier list price (USD) | Wall clock (s) | Minutes per document |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline |  | 30 |  |  |  |  |  | no model calls | 0.0000 | 0.0000 |  | 0.0000 | 0.00 (no model calls) |  | 0.0 |  |
| llm:gemini:gemini-3.5-flash-lite | all | 30 | 30 | 6.7 | 7676 | 15248 | 8181 / 1775 | free tier (billed 0); paid-tier list price 0.30 in / 2.50 out USD per M tokens | 0.0000 |  | 0.0069 |  | 0.00 (spent (corpus traces)) | 18.39 | 440.5 |  |
| llm:gemini:gemini-3.5-flash-lite:closedbook |  | 30 | 30 |  |  |  | 229 / 46 | free tier (billed 0); paid-tier list price 0.30 in / 2.50 out USD per M tokens | 0.0000 |  | 0.0002 |  |  |  | 107.9 |  |
| llm:gemini:gemini-3.5-flash-lite:whole | all | 20 | 19 | 100.0 |  | 23946 | 23946 / 1845 | free tier (billed 0); paid-tier list price 0.30 in / 2.50 out USD per M tokens | 0.0000 |  | 0.0118 |  |  |  | 168.4 |  |
| rules |  | 30 |  |  |  |  |  | no model calls | 0.0000 | 0.0000 |  | 0.0000 | 0.00 (no model calls) |  | 0.2 |  |
| manual (page rate) |  | 30 |  |  |  |  |  | 60 pages per hour at wage 45.27 USD per hour |  | 20.2709 |  |  | 54062.19 (page counts of the 2791 evaluable documents) |  |  | 26.9 |
| reader time (model readers, reported) |  | 40 |  |  |  |  |  | not a cost; reported reading minutes |  |  |  |  |  |  |  | 11.5 |

## Review operating points (corpus tier: llm:gemini:gemini-3.5-flash-lite)

Values on the base of the review curve (answered Group B values of the released system when results/risk_values_corpus.csv exists, see the note); residual error from the Group B review curve; reviewer minutes per value from the page-rate baseline spread over the 11 Group B fields (plan, Cost accounting), else from the audit timers (shown mode, else all modes).

| Share reviewed | Values reviewed | Residual error | Reviewer minutes per value | Review cost (USD) | Extraction cost (USD) | Total (USD) | Note |
|---|---|---|---|---|---|---|---|
| 10 | 2498 of 24983 | 9.6 | 2.33 | 4398.79 | 0.00 | 4398.79 | n_values: answered Group B values (risk_values_corpus.csv); reviewer minutes: page rate (60 pages per hour over 11 Group B fields) |
| 25 | 6246 of 24983 | 6.4 | 2.33 | 10998.74 | 0.00 | 10998.74 | n_values: answered Group B values (risk_values_corpus.csv); reviewer minutes: page rate (60 pages per hour over 11 Group B fields) |
| 50 | 12492 of 24983 | 4.3 | 2.33 | 21997.49 | 0.00 | 21997.49 | n_values: answered Group B values (risk_values_corpus.csv); reviewer minutes: page rate (60 pages per hour over 11 Group B fields) |

## Reader minutes per field (model readers, reported)

| Field | Source | Timed rows | Mean minutes |
|---|---|---|---|
| regulator | gold sheets (dev, test) | 40 | 0.36 |
| bank_name | gold sheets (dev, test) | 40 | 0.36 |
| bank_id | gold sheets (dev, test) | 40 | 0.36 |
| exam_date | gold sheets (dev, test) | 40 | 0.36 |
| exam_method | gold sheets (dev, test) | 40 | 0.36 |
| overall_rating | gold sheets (dev, test) | 40 | 0.36 |
| lending_test_rating | gold sheets (dev, test) | 40 | 0.36 |
| cd_test_rating | gold sheets (dev, test) | 40 | 0.36 |
| prior_evaluation_date | gold sheets (dev, test) | 40 | 0.36 |
| prior_rating | gold sheets (dev, test) | 40 | 0.36 |
| total_assets_usd | gold sheets (dev, test) | 40 | 0.36 |
| total_assets_asof | gold sheets (dev, test) | 40 | 0.36 |
| lending_data_period_start | gold sheets (dev, test) | 40 | 0.36 |
| lending_data_period_end | gold sheets (dev, test) | 40 | 0.36 |
| n_assessment_areas | gold sheets (dev, test) | 40 | 0.36 |
| n_full_scope | gold sheets (dev, test) | 40 | 0.36 |
| loan_products_evaluated | gold sheets (dev, test) | 40 | 0.36 |
| primary_product | gold sheets (dev, test) | 40 | 0.36 |
| inside_aa_share_number_pct | gold sheets (dev, test) | 40 | 0.36 |
| inside_aa_share_product | gold sheets (dev, test) | 40 | 0.36 |
| avg_ltd_ratio_pct | gold sheets (dev, test) | 40 | 0.36 |
| finding_present | gold sheets (dev, test) | 40 | 0.36 |
| finding_type | gold sheets (dev, test) | 40 | 0.36 |
| rating_effect | gold sheets (dev, test) | 40 | 0.36 |
| rating_lowered | gold sheets (dev, test) | 40 | 0.36 |
| rating_lowered_level | gold sheets (dev, test) | 40 | 0.36 |
| rating_before | gold sheets (dev, test) | 40 | 0.36 |
| rating_after | gold sheets (dev, test) | 40 | 0.36 |
| laws_cited | gold sheets (dev, test) | 40 | 0.36 |
| practice_basis | gold sheets (dev, test) | 40 | 0.36 |
| finding_source | gold sheets (dev, test) | 40 | 0.36 |
| distractor_laws_named | gold sheets (dev, test) | 40 | 0.36 |
| total_assets_usd | audit sheet, blind mode | 15 | 0.57 |
| total_assets_asof | audit sheet, blind mode | 15 | 0.57 |
| lending_data_period_start | audit sheet, blind mode | 15 | 0.57 |
| lending_data_period_end | audit sheet, blind mode | 15 | 0.57 |
| n_assessment_areas | audit sheet, blind mode | 15 | 0.57 |
| n_full_scope | audit sheet, blind mode | 15 | 0.57 |
| loan_products_evaluated | audit sheet, blind mode | 15 | 0.57 |
| primary_product | audit sheet, blind mode | 15 | 0.57 |
| inside_aa_share_number_pct | audit sheet, blind mode | 15 | 0.57 |
| inside_aa_share_product | audit sheet, blind mode | 15 | 0.57 |
| avg_ltd_ratio_pct | audit sheet, blind mode | 15 | 0.57 |
