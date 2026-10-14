# Whole-document ablation (test split)

Per field: accuracy of the located system and of its whole-document run on the same documents (the first 20 test doc_ids by sha256 of the doc_id), over gold-value items with abstentions excluded, Wilson 95 percent intervals. Discordant items: gold-value items both runs answered where exactly one is correct.

| Field | Located system | Documents | Located accuracy | Whole-document accuracy | Both answered | Located only correct | Whole only correct |
|---|---|---|---|---|---|---|---|
| regulator | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| bank_name | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| bank_id | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| exam_date | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| exam_method | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| overall_rating | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| lending_test_rating | llm:gemini:gemini-3.5-flash-lite | 20 | 92.3 [66.7, 98.6] (12/13) | 92.3 [66.7, 98.6] (12/13) | 13 | 0 | 0 |
| cd_test_rating | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [72.2, 100.0] (10/10) | 100.0 [72.2, 100.0] (10/10) | 10 | 0 | 0 |
| prior_evaluation_date | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| prior_rating | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| total_assets_usd | llm:gemini:gemini-3.5-flash-lite | 20 | 95.0 [76.4, 99.1] (19/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 1 |
| total_assets_asof | llm:gemini:gemini-3.5-flash-lite | 20 | 95.0 [76.4, 99.1] (19/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 1 |
| lending_data_period_start | llm:gemini:gemini-3.5-flash-lite | 20 | 54.5 [28.0, 78.7] (6/11) | 81.8 [52.3, 94.9] (9/11) | 11 | 0 | 3 |
| lending_data_period_end | llm:gemini:gemini-3.5-flash-lite | 20 | 63.6 [35.4, 84.8] (7/11) | 90.9 [62.3, 98.4] (10/11) | 11 | 0 | 3 |
| n_assessment_areas | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| n_full_scope | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [70.1, 100.0] (9/9) | 100.0 [70.1, 100.0] (9/9) | 9 | 0 | 0 |
| loan_products_evaluated | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| primary_product | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| inside_aa_share_number_pct | llm:gemini:gemini-3.5-flash-lite | 20 | 80.0 [58.4, 91.9] (16/20) | 85.0 [64.0, 94.8] (17/20) | 20 | 0 | 1 |
| inside_aa_share_product | llm:gemini:gemini-3.5-flash-lite | 20 | 80.0 [58.4, 91.9] (16/20) | 80.0 [58.4, 91.9] (16/20) | 20 | 1 | 1 |
| avg_ltd_ratio_pct | llm:gemini:gemini-3.5-flash-lite | 20 | 95.0 [76.4, 99.1] (19/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 1 |
| finding_present | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
| rating_lowered | llm:gemini:gemini-3.5-flash-lite | 20 | 100.0 [83.9, 100.0] (20/20) | 100.0 [83.9, 100.0] (20/20) | 20 | 0 | 0 |
