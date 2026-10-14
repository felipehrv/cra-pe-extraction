# Table 1 (test split)

Percent; intervals are Wilson 95 percent. n is the accuracy denominator (gold-value items, abstentions excluded). Group A rows are a smoke test; Group A is evaluated against the agency indexes on the whole corpus. Precision and recall of the set field are element-level. Hallucination rate is shown only where grounding was checked on every answered item; systems rules, baseline show the error rate with no grounding component. Group C and the component-rating fabrication probes are in the probe table.

| Section | Field | System | n | Accuracy [95% CI] | Precision [95% CI] | Recall [95% CI] | Abstention | Fabrication [95% CI] (denominator) | Grounded | Hallucination [95% CI] | Error rate, no grounding component [95% CI] |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Group A (smoke test) | regulator | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | regulator | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | regulator | rules | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | regulator | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | bank_name | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | bank_name | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | bank_name | rules | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | bank_name | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | bank_id | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | bank_id | llm:gemini:gemini-3.5-flash-lite | 30 | 93.3 [78.7, 98.2] |  |  | 0.0 | (0) | 100.0 | 6.7 [1.8, 21.3] |  |
| Group A (smoke test) | bank_id | rules | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | bank_id | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | exam_date | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | exam_date | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | exam_date | rules | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | exam_date | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | exam_method | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | exam_method | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 90.0 | 10.0 [3.5, 25.6] |  |
| Group A (smoke test) | exam_method | rules | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | exam_method | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | overall_rating | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | overall_rating | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | overall_rating | rules | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Group A (smoke test) | overall_rating | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) |  |  | 0.0 [0.0, 11.4] |
| Component ratings | lending_test_rating | baseline | 0 |  |  |  | 100.0 | see probe table |  |  |  |
| Component ratings | lending_test_rating | llm:gemini:gemini-3.5-flash-lite | 20 | 95.0 [76.4, 99.1] |  |  | 0.0 | see probe table | 100.0 | 6.7 [1.8, 21.3] |  |
| Component ratings | lending_test_rating | rules | 20 | 95.0 [76.4, 99.1] |  |  | 0.0 | see probe table |  |  | 3.3 [0.6, 16.7] |
| Component ratings | lending_test_rating | s5 | 20 | 95.0 [76.4, 99.1] |  |  | 0.0 | see probe table | 100.0 | 6.7 [1.8, 21.3] |  |
| Component ratings | cd_test_rating | baseline | 0 |  |  |  | 100.0 | see probe table |  |  |  |
| Component ratings | cd_test_rating | llm:gemini:gemini-3.5-flash-lite | 16 | 100.0 [80.6, 100.0] |  |  | 0.0 | see probe table | 100.0 | 0.0 [0.0, 11.4] |  |
| Component ratings | cd_test_rating | rules | 16 | 100.0 [80.6, 100.0] |  |  | 0.0 | see probe table |  |  | 0.0 [0.0, 11.4] |
| Component ratings | cd_test_rating | s5 | 16 | 100.0 [80.6, 100.0] |  |  | 0.0 | see probe table | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | prior_evaluation_date | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | prior_evaluation_date | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | prior_evaluation_date | rules | 28 | 89.3 [72.8, 96.3] |  |  | 6.7 | (0) |  |  | 10.7 [3.7, 27.2] |
| Group A (smoke test) | prior_evaluation_date | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | prior_rating | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group A (smoke test) | prior_rating | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group A (smoke test) | prior_rating | rules | 24 | 95.8 [79.8, 99.3] |  |  | 20.0 | (0) |  |  | 4.2 [0.7, 20.2] |
| Group A (smoke test) | prior_rating | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 100.0 | 0.0 [0.0, 11.4] |  |
| Group B | total_assets_usd | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | total_assets_usd | llm:gemini:gemini-3.5-flash-lite | 30 | 96.7 [83.3, 99.4] |  |  | 0.0 | (0) | 100.0 | 3.3 [0.6, 16.7] |  |
| Group B | total_assets_usd | rules | 25 | 100.0 [86.7, 100.0] |  |  | 16.7 | (0) |  |  | 0.0 [0.0, 13.3] |
| Group B | total_assets_usd | s5 | 30 | 96.7 [83.3, 99.4] |  |  | 0.0 | (0) | 100.0 | 3.3 [0.6, 16.7] |  |
| Group B | total_assets_asof | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | total_assets_asof | llm:gemini:gemini-3.5-flash-lite | 30 | 96.7 [83.3, 99.4] |  |  | 0.0 | (0) | 100.0 | 3.3 [0.6, 16.7] |  |
| Group B | total_assets_asof | rules | 25 | 100.0 [86.7, 100.0] |  |  | 16.7 | (0) |  |  | 0.0 [0.0, 13.3] |
| Group B | total_assets_asof | s5 | 30 | 96.7 [83.3, 99.4] |  |  | 0.0 | (0) | 100.0 | 3.3 [0.6, 16.7] |  |
| Group B | lending_data_period_start | baseline | 0 |  |  |  | 100.0 | 0.0 [0.0, 24.2] (12) |  |  |  |
| Group B | lending_data_period_start | llm:gemini:gemini-3.5-flash-lite | 18 | 66.7 [43.7, 83.7] |  |  | 0.0 | 25.0 [8.9, 53.2] (12) | 75.0 | 33.3 [19.2, 51.2] |  |
| Group B | lending_data_period_start | rules | 0 |  |  |  | 100.0 | 0.0 [0.0, 24.2] (12) |  |  |  |
| Group B | lending_data_period_start | s5 | 18 | 66.7 [43.7, 83.7] |  |  | 0.0 | 25.0 [8.9, 53.2] (12) | 75.0 | 33.3 [19.2, 51.2] |  |
| Group B | lending_data_period_end | baseline | 0 |  |  |  | 100.0 | 0.0 [0.0, 24.2] (12) |  |  |  |
| Group B | lending_data_period_end | llm:gemini:gemini-3.5-flash-lite | 18 | 72.2 [49.1, 87.5] |  |  | 0.0 | 25.0 [8.9, 53.2] (12) | 75.0 | 33.3 [19.2, 51.2] |  |
| Group B | lending_data_period_end | rules | 0 |  |  |  | 100.0 | 0.0 [0.0, 24.2] (12) |  |  |  |
| Group B | lending_data_period_end | s5 | 18 | 72.2 [49.1, 87.5] |  |  | 0.0 | 25.0 [8.9, 53.2] (12) | 75.0 | 33.3 [19.2, 51.2] |  |
| Group B | n_assessment_areas | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | n_assessment_areas | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 80.0 | 20.0 [9.5, 37.3] |  |
| Group B | n_assessment_areas | rules | 27 | 88.9 [71.9, 96.1] |  |  | 10.0 | (0) |  |  | 11.1 [3.9, 28.1] |
| Group B | n_assessment_areas | s5 | 30 | 100.0 [88.6, 100.0] |  |  | 0.0 | (0) | 80.0 | 20.0 [9.5, 37.3] |  |
| Group B | n_full_scope | baseline | 0 |  |  |  | 100.0 | 0.0 [0.0, 18.4] (17) |  |  |  |
| Group B | n_full_scope | llm:gemini:gemini-3.5-flash-lite | 13 | 92.3 [66.7, 98.6] |  |  | 0.0 | 0.0 [0.0, 18.4] (17) | 58.3 | 20.0 [9.5, 37.3] |  |
| Group B | n_full_scope | rules | 0 |  |  |  | 100.0 | 0.0 [0.0, 18.4] (17) |  |  |  |
| Group B | n_full_scope | s5 | 13 | 92.3 [66.7, 98.6] |  |  | 0.0 | 0.0 [0.0, 18.4] (17) | 58.3 | 20.0 [9.5, 37.3] |  |
| Group B | loan_products_evaluated | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | loan_products_evaluated | llm:gemini:gemini-3.5-flash-lite | 30 | 100.0 [88.6, 100.0] | 100.0 [93.8, 100.0] | 100.0 [93.8, 100.0] | 0.0 | (0) | 90.0 | 10.0 [3.5, 25.6] |  |
| Group B | loan_products_evaluated | rules | 29 | 93.1 [78.0, 98.1] | 100.0 [93.2, 100.0] | 96.4 [87.7, 99.0] | 3.3 | (0) |  |  | 6.9 [1.9, 22.0] |
| Group B | loan_products_evaluated | s5 | 30 | 100.0 [88.6, 100.0] | 100.0 [93.8, 100.0] | 100.0 [93.8, 100.0] | 0.0 | (0) | 90.0 | 10.0 [3.5, 25.6] |  |
| Group B | primary_product | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | primary_product | llm:gemini:gemini-3.5-flash-lite | 30 | 90.0 [74.4, 96.5] |  |  | 0.0 | (0) | 96.7 | 13.3 [5.3, 29.7] |  |
| Group B | primary_product | rules | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | primary_product | s5 | 30 | 90.0 [74.4, 96.5] |  |  | 0.0 | (0) | 96.7 | 13.3 [5.3, 29.7] |  |
| Group B | inside_aa_share_number_pct | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | inside_aa_share_number_pct | llm:gemini:gemini-3.5-flash-lite | 30 | 86.7 [70.3, 94.7] |  |  | 0.0 | (0) | 85.7 | 20.0 [9.5, 37.3] |  |
| Group B | inside_aa_share_number_pct | rules | 27 | 100.0 [87.5, 100.0] |  |  | 10.0 | (0) |  |  | 0.0 [0.0, 12.5] |
| Group B | inside_aa_share_number_pct | s5 | 30 | 86.7 [70.3, 94.7] |  |  | 0.0 | (0) | 85.7 | 20.0 [9.5, 37.3] |  |
| Group B | inside_aa_share_product | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | inside_aa_share_product | llm:gemini:gemini-3.5-flash-lite | 30 | 83.3 [66.4, 92.7] |  |  | 0.0 | (0) | 89.3 | 20.0 [9.5, 37.3] |  |
| Group B | inside_aa_share_product | rules | 27 | 100.0 [87.5, 100.0] |  |  | 10.0 | (0) |  |  | 0.0 [0.0, 12.5] |
| Group B | inside_aa_share_product | s5 | 30 | 83.3 [66.4, 92.7] |  |  | 0.0 | (0) | 89.3 | 20.0 [9.5, 37.3] |  |
| Group B | avg_ltd_ratio_pct | baseline | 0 |  |  |  | 100.0 | (0) |  |  |  |
| Group B | avg_ltd_ratio_pct | llm:gemini:gemini-3.5-flash-lite | 30 | 96.7 [83.3, 99.4] |  |  | 0.0 | (0) | 100.0 | 3.3 [0.6, 16.7] |  |
| Group B | avg_ltd_ratio_pct | rules | 22 | 90.9 [72.2, 97.5] |  |  | 26.7 | (0) |  |  | 9.1 [2.5, 27.8] |
| Group B | avg_ltd_ratio_pct | s5 | 30 | 96.7 [83.3, 99.4] |  |  | 0.0 | (0) | 100.0 | 3.3 [0.6, 16.7] |  |
