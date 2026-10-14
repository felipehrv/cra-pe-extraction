# Group B by regulator (test split)

Heterogeneity cut of the analysis plan (Group B by regulator) on the test split, from scored_test.csv. Per field, system and regulator: accuracy over gold-value items with abstentions excluded (n, as in Table 1), element-level precision and recall for the set field, precision and recall for binary fields, Wilson 95 percent intervals in percent, and the abstention rate over all gold items. Each regulator is reported on its own with its interval; no between-regulator test is made. Whole-document and closed-book variants are left out; the regulator is the first part of the doc_id.

| Field | System | Regulator | Documents | n | Accuracy [95% CI] | Precision [95% CI] | Recall [95% CI] | Abstention |
|---|---|---|---|---|---|---|---|---|
| total_assets_usd | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| total_assets_usd | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| total_assets_usd | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| total_assets_usd | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| total_assets_usd | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| total_assets_usd | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 88.9 [56.5, 98.0] |  |  | 0.0 |
| total_assets_usd | rules | OCC | 8 | 6 | 100.0 [61.0, 100.0] |  |  | 25.0 |
| total_assets_usd | rules | FDIC | 13 | 12 | 100.0 [75.7, 100.0] |  |  | 7.7 |
| total_assets_usd | rules | FRB | 9 | 7 | 100.0 [64.6, 100.0] |  |  | 22.2 |
| total_assets_usd | s5 | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| total_assets_usd | s5 | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| total_assets_usd | s5 | FRB | 9 | 9 | 88.9 [56.5, 98.0] |  |  | 0.0 |
| total_assets_asof | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| total_assets_asof | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| total_assets_asof | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| total_assets_asof | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| total_assets_asof | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| total_assets_asof | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 88.9 [56.5, 98.0] |  |  | 0.0 |
| total_assets_asof | rules | OCC | 8 | 6 | 100.0 [61.0, 100.0] |  |  | 25.0 |
| total_assets_asof | rules | FDIC | 13 | 12 | 100.0 [75.7, 100.0] |  |  | 7.7 |
| total_assets_asof | rules | FRB | 9 | 7 | 100.0 [64.6, 100.0] |  |  | 22.2 |
| total_assets_asof | s5 | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| total_assets_asof | s5 | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| total_assets_asof | s5 | FRB | 9 | 9 | 88.9 [56.5, 98.0] |  |  | 0.0 |
| lending_data_period_start | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| lending_data_period_start | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| lending_data_period_start | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| lending_data_period_start | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 75.0 [40.9, 92.9] |  |  | 0.0 |
| lending_data_period_start | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 3 | 66.7 [20.8, 93.9] |  |  | 0.0 |
| lending_data_period_start | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 7 | 57.1 [25.0, 84.2] |  |  | 0.0 |
| lending_data_period_start | rules | OCC | 8 | 0 |  |  |  | 100.0 |
| lending_data_period_start | rules | FDIC | 13 | 0 |  |  |  | 100.0 |
| lending_data_period_start | rules | FRB | 9 | 0 |  |  |  | 100.0 |
| lending_data_period_start | s5 | OCC | 8 | 8 | 75.0 [40.9, 92.9] |  |  | 0.0 |
| lending_data_period_start | s5 | FDIC | 13 | 3 | 66.7 [20.8, 93.9] |  |  | 0.0 |
| lending_data_period_start | s5 | FRB | 9 | 7 | 57.1 [25.0, 84.2] |  |  | 0.0 |
| lending_data_period_end | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| lending_data_period_end | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| lending_data_period_end | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| lending_data_period_end | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 75.0 [40.9, 92.9] |  |  | 0.0 |
| lending_data_period_end | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 3 | 66.7 [20.8, 93.9] |  |  | 0.0 |
| lending_data_period_end | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 7 | 71.4 [35.9, 91.8] |  |  | 0.0 |
| lending_data_period_end | rules | OCC | 8 | 0 |  |  |  | 100.0 |
| lending_data_period_end | rules | FDIC | 13 | 0 |  |  |  | 100.0 |
| lending_data_period_end | rules | FRB | 9 | 0 |  |  |  | 100.0 |
| lending_data_period_end | s5 | OCC | 8 | 8 | 75.0 [40.9, 92.9] |  |  | 0.0 |
| lending_data_period_end | s5 | FDIC | 13 | 3 | 66.7 [20.8, 93.9] |  |  | 0.0 |
| lending_data_period_end | s5 | FRB | 9 | 7 | 71.4 [35.9, 91.8] |  |  | 0.0 |
| n_assessment_areas | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| n_assessment_areas | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| n_assessment_areas | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| n_assessment_areas | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| n_assessment_areas | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| n_assessment_areas | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 100.0 [70.1, 100.0] |  |  | 0.0 |
| n_assessment_areas | rules | OCC | 8 | 7 | 85.7 [48.7, 97.4] |  |  | 12.5 |
| n_assessment_areas | rules | FDIC | 13 | 12 | 91.7 [64.6, 98.5] |  |  | 7.7 |
| n_assessment_areas | rules | FRB | 9 | 8 | 87.5 [52.9, 97.8] |  |  | 11.1 |
| n_assessment_areas | s5 | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| n_assessment_areas | s5 | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| n_assessment_areas | s5 | FRB | 9 | 9 | 100.0 [70.1, 100.0] |  |  | 0.0 |
| n_full_scope | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| n_full_scope | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| n_full_scope | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| n_full_scope | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 4 | 75.0 [30.1, 95.4] |  |  | 0.0 |
| n_full_scope | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 7 | 100.0 [64.6, 100.0] |  |  | 0.0 |
| n_full_scope | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 2 | 100.0 [34.2, 100.0] |  |  | 0.0 |
| n_full_scope | rules | OCC | 8 | 0 |  |  |  | 100.0 |
| n_full_scope | rules | FDIC | 13 | 0 |  |  |  | 100.0 |
| n_full_scope | rules | FRB | 9 | 0 |  |  |  | 100.0 |
| n_full_scope | s5 | OCC | 8 | 4 | 75.0 [30.1, 95.4] |  |  | 0.0 |
| n_full_scope | s5 | FDIC | 13 | 7 | 100.0 [64.6, 100.0] |  |  | 0.0 |
| n_full_scope | s5 | FRB | 9 | 2 | 100.0 [34.2, 100.0] |  |  | 0.0 |
| loan_products_evaluated | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| loan_products_evaluated | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| loan_products_evaluated | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| loan_products_evaluated | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 100.0 [67.6, 100.0] | 100.0 [77.2, 100.0] | 100.0 [77.2, 100.0] | 0.0 |
| loan_products_evaluated | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 100.0 [77.2, 100.0] | 100.0 [87.9, 100.0] | 100.0 [87.9, 100.0] | 0.0 |
| loan_products_evaluated | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 100.0 [70.1, 100.0] | 100.0 [81.6, 100.0] | 100.0 [81.6, 100.0] | 0.0 |
| loan_products_evaluated | rules | OCC | 8 | 7 | 100.0 [64.6, 100.0] | 100.0 [72.2, 100.0] | 100.0 [72.2, 100.0] | 12.5 |
| loan_products_evaluated | rules | FDIC | 13 | 13 | 100.0 [77.2, 100.0] | 100.0 [87.9, 100.0] | 100.0 [87.9, 100.0] | 0.0 |
| loan_products_evaluated | rules | FRB | 9 | 9 | 77.8 [45.3, 93.7] | 100.0 [79.6, 100.0] | 88.2 [65.7, 96.7] | 0.0 |
| loan_products_evaluated | s5 | OCC | 8 | 8 | 100.0 [67.6, 100.0] | 100.0 [77.2, 100.0] | 100.0 [77.2, 100.0] | 0.0 |
| loan_products_evaluated | s5 | FDIC | 13 | 13 | 100.0 [77.2, 100.0] | 100.0 [87.9, 100.0] | 100.0 [87.9, 100.0] | 0.0 |
| loan_products_evaluated | s5 | FRB | 9 | 9 | 100.0 [70.1, 100.0] | 100.0 [81.6, 100.0] | 100.0 [81.6, 100.0] | 0.0 |
| primary_product | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| primary_product | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| primary_product | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| primary_product | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 87.5 [52.9, 97.8] |  |  | 0.0 |
| primary_product | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| primary_product | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 77.8 [45.3, 93.7] |  |  | 0.0 |
| primary_product | rules | OCC | 8 | 0 |  |  |  | 100.0 |
| primary_product | rules | FDIC | 13 | 0 |  |  |  | 100.0 |
| primary_product | rules | FRB | 9 | 0 |  |  |  | 100.0 |
| primary_product | s5 | OCC | 8 | 8 | 87.5 [52.9, 97.8] |  |  | 0.0 |
| primary_product | s5 | FDIC | 13 | 13 | 100.0 [77.2, 100.0] |  |  | 0.0 |
| primary_product | s5 | FRB | 9 | 9 | 77.8 [45.3, 93.7] |  |  | 0.0 |
| inside_aa_share_number_pct | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| inside_aa_share_number_pct | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| inside_aa_share_number_pct | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| inside_aa_share_number_pct | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| inside_aa_share_number_pct | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 69.2 [42.4, 87.3] |  |  | 0.0 |
| inside_aa_share_number_pct | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 100.0 [70.1, 100.0] |  |  | 0.0 |
| inside_aa_share_number_pct | rules | OCC | 8 | 7 | 100.0 [64.6, 100.0] |  |  | 12.5 |
| inside_aa_share_number_pct | rules | FDIC | 13 | 12 | 100.0 [75.7, 100.0] |  |  | 7.7 |
| inside_aa_share_number_pct | rules | FRB | 9 | 8 | 100.0 [67.6, 100.0] |  |  | 11.1 |
| inside_aa_share_number_pct | s5 | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| inside_aa_share_number_pct | s5 | FDIC | 13 | 13 | 69.2 [42.4, 87.3] |  |  | 0.0 |
| inside_aa_share_number_pct | s5 | FRB | 9 | 9 | 100.0 [70.1, 100.0] |  |  | 0.0 |
| inside_aa_share_product | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| inside_aa_share_product | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| inside_aa_share_product | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| inside_aa_share_product | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| inside_aa_share_product | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 69.2 [42.4, 87.3] |  |  | 0.0 |
| inside_aa_share_product | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 88.9 [56.5, 98.0] |  |  | 0.0 |
| inside_aa_share_product | rules | OCC | 8 | 7 | 100.0 [64.6, 100.0] |  |  | 12.5 |
| inside_aa_share_product | rules | FDIC | 13 | 12 | 100.0 [75.7, 100.0] |  |  | 7.7 |
| inside_aa_share_product | rules | FRB | 9 | 8 | 100.0 [67.6, 100.0] |  |  | 11.1 |
| inside_aa_share_product | s5 | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| inside_aa_share_product | s5 | FDIC | 13 | 13 | 69.2 [42.4, 87.3] |  |  | 0.0 |
| inside_aa_share_product | s5 | FRB | 9 | 9 | 88.9 [56.5, 98.0] |  |  | 0.0 |
| avg_ltd_ratio_pct | baseline | OCC | 8 | 0 |  |  |  | 100.0 |
| avg_ltd_ratio_pct | baseline | FDIC | 13 | 0 |  |  |  | 100.0 |
| avg_ltd_ratio_pct | baseline | FRB | 9 | 0 |  |  |  | 100.0 |
| avg_ltd_ratio_pct | llm:gemini:gemini-3.5-flash-lite | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| avg_ltd_ratio_pct | llm:gemini:gemini-3.5-flash-lite | FDIC | 13 | 13 | 92.3 [66.7, 98.6] |  |  | 0.0 |
| avg_ltd_ratio_pct | llm:gemini:gemini-3.5-flash-lite | FRB | 9 | 9 | 100.0 [70.1, 100.0] |  |  | 0.0 |
| avg_ltd_ratio_pct | rules | OCC | 8 | 7 | 100.0 [64.6, 100.0] |  |  | 12.5 |
| avg_ltd_ratio_pct | rules | FDIC | 13 | 9 | 77.8 [45.3, 93.7] |  |  | 30.8 |
| avg_ltd_ratio_pct | rules | FRB | 9 | 6 | 100.0 [61.0, 100.0] |  |  | 33.3 |
| avg_ltd_ratio_pct | s5 | OCC | 8 | 8 | 100.0 [67.6, 100.0] |  |  | 0.0 |
| avg_ltd_ratio_pct | s5 | FDIC | 13 | 13 | 92.3 [66.7, 98.6] |  |  | 0.0 |
| avg_ltd_ratio_pct | s5 | FRB | 9 | 9 | 100.0 [70.1, 100.0] |  |  | 0.0 |
