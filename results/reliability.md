# Inter-reader agreement

Agreement between readers A and B before adjudication (plan "Reliability", protocol of 2026-10-14), written by `python -m cra_extract.reliability agreement`. Values are compared in canonical form; a null code (or NA / ABS) is a category of its own. pct is percent agreement with a Wilson 95 percent interval; kappa is Cohen's kappa where each reader uses at least two categories on the field. Pooled rows sum the items over fields.

- dev: readers A and B agreed on 319 of 320 items (99.7 percent, 95 percent CI 98.3 to 99.9) over 32 fields of 10 documents; 1 item was sent to adjudication (adjudication rate 0.3 percent).
- test: readers A and B agreed on 958 of 960 items (99.8 percent, 95 percent CI 99.2 to 99.9) over 32 fields of 30 documents; 2 items were sent to adjudication (adjudication rate 0.2 percent).
- audit: readers A and B agreed on 324 of 330 items (98.2 percent, 95 percent CI 96.1 to 99.2) over 11 fields of 30 documents; 6 items were sent to adjudication (adjudication rate 1.8 percent).
- census: readers A and B agreed on 1767 of 1771 items (99.8 percent, 95 percent CI 99.4 to 99.9) over 11 fields of 161 sections; 4 items were sent to adjudication (adjudication rate 0.2 percent).
- all: 3368 of 3381 items agree (99.6 percent, 95 percent CI 99.3 to 99.8); adjudication rate 0.4 percent.

| split | field | n | agree | pct | ci_low | ci_high | kappa | note |
|---|---|---:|---:|---:|---:|---:|---:|---|
| dev | regulator | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | bank_name | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | bank_id | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | exam_date | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | exam_method | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | overall_rating | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | lending_test_rating | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | cd_test_rating | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | prior_evaluation_date | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | prior_rating | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | total_assets_usd | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | total_assets_asof | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | lending_data_period_start | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | lending_data_period_end | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | n_assessment_areas | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | n_full_scope | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | loan_products_evaluated | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 | element-wise 50/50 agree over 5 elements, kappa 1.000 |
| dev | primary_product | 10 | 9 | 90.0 | 59.6 | 98.2 | 0.865 | adjudicated 1 of 10 |
| dev | inside_aa_share_number_pct | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | inside_aa_share_product | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | avg_ltd_ratio_pct | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | finding_present | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | finding_type | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | rating_effect | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | rating_lowered | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | rating_lowered_level | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | rating_before | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | rating_after | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | laws_cited | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 | element-wise 100/100 agree over 10 elements, kappa 1.000 |
| dev | practice_basis | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 | element-wise 70/70 agree over 7 elements, kappa 1.000 |
| dev | finding_source | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | distractor_laws_named | 10 | 10 | 100.0 | 72.2 | 100.0 | 1.000 |  |
| dev | pooled | 320 | 319 | 99.7 | 98.3 | 99.9 |  | 32 fields; kappa not pooled across fields; Wilson CI treats items as independent (items cluster within documents); adjudication rate 1/320 = 0.3% |
| test | regulator | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | bank_name | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | bank_id | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | exam_date | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | exam_method | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | overall_rating | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | lending_test_rating | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | cd_test_rating | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | prior_evaluation_date | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | prior_rating | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | total_assets_usd | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | total_assets_asof | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | lending_data_period_start | 30 | 29 | 96.7 | 83.3 | 99.4 | 0.956 | adjudicated 1 of 30 |
| test | lending_data_period_end | 30 | 29 | 96.7 | 83.3 | 99.4 | 0.956 | adjudicated 1 of 30 |
| test | n_assessment_areas | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | n_full_scope | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | loan_products_evaluated | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 | element-wise 150/150 agree over 5 elements, kappa 1.000 |
| test | primary_product | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | inside_aa_share_number_pct | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | inside_aa_share_product | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | avg_ltd_ratio_pct | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | finding_present | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'false', prevalence 1.000 |
| test | finding_type | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000 |
| test | rating_effect | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000 |
| test | rating_lowered | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'false', prevalence 1.000 |
| test | rating_lowered_level | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000 |
| test | rating_before | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000 |
| test | rating_after | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000 |
| test | laws_cited | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000; element-wise 300/300 agree over 10 elements |
| test | practice_basis | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000; element-wise 210/210 agree over 7 elements |
| test | finding_source | 30 | 30 | 100.0 | 88.6 | 100.0 |  | kappa not computed: reader A uses 1 and reader B 1 category; majority class 'null:not_applicable', prevalence 1.000 |
| test | distractor_laws_named | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| test | pooled | 960 | 958 | 99.8 | 99.2 | 99.9 |  | 32 fields; kappa not pooled across fields; Wilson CI treats items as independent (items cluster within documents); adjudication rate 2/960 = 0.2% |
| audit | total_assets_usd | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| audit | total_assets_asof | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| audit | lending_data_period_start | 30 | 29 | 96.7 | 83.3 | 99.4 | 0.951 | adjudicated 1 of 30 |
| audit | lending_data_period_end | 30 | 29 | 96.7 | 83.3 | 99.4 | 0.949 | adjudicated 1 of 30 |
| audit | n_assessment_areas | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| audit | n_full_scope | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| audit | loan_products_evaluated | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 | element-wise 150/150 agree over 5 elements, kappa 1.000 |
| audit | primary_product | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| audit | inside_aa_share_number_pct | 30 | 28 | 93.3 | 78.7 | 98.2 | 0.931 | adjudicated 2 of 30 |
| audit | inside_aa_share_product | 30 | 28 | 93.3 | 78.7 | 98.2 | 0.873 | adjudicated 2 of 30 |
| audit | avg_ltd_ratio_pct | 30 | 30 | 100.0 | 88.6 | 100.0 | 1.000 |  |
| audit | pooled | 330 | 324 | 98.2 | 96.1 | 99.2 |  | 11 fields; kappa not pooled across fields; Wilson CI treats items as independent (items cluster within documents); adjudication rate 6/330 = 1.8% |
| census | finding_present | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 |  |
| census | finding_type | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 |  |
| census | rating_effect | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 |  |
| census | rating_lowered | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 |  |
| census | rating_lowered_level | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 |  |
| census | rating_before | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 |  |
| census | rating_after | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 |  |
| census | laws_cited | 161 | 161 | 100.0 | 97.7 | 100.0 | 1.000 | element-wise 1610/1610 agree over 10 elements, kappa 1.000 |
| census | practice_basis | 161 | 159 | 98.8 | 95.6 | 99.7 | 0.952 | element-wise 1125/1127 agree over 7 elements, kappa 0.899; adjudicated 2 of 161 |
| census | finding_source | 161 | 160 | 99.4 | 96.6 | 99.9 | 0.976 | adjudicated 1 of 161 |
| census | distractor_laws_named | 161 | 160 | 99.4 | 96.6 | 99.9 | 0.988 | adjudicated 1 of 161 |
| census | pooled | 1771 | 1767 | 99.8 | 99.4 | 99.9 |  | 11 fields; kappa not pooled across fields; Wilson CI treats items as independent (items cluster within documents); adjudication rate 4/1771 = 0.2% |
| all | regulator | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | bank_name | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | bank_id | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | exam_date | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | exam_method | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | overall_rating | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | lending_test_rating | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | cd_test_rating | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | prior_evaluation_date | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | prior_rating | 40 | 40 | 100.0 | 91.2 | 100.0 | 1.000 | splits dev, test |
| all | total_assets_usd | 70 | 70 | 100.0 | 94.8 | 100.0 | 1.000 | splits audit, dev, test |
| all | total_assets_asof | 70 | 70 | 100.0 | 94.8 | 100.0 | 1.000 | splits audit, dev, test |
| all | lending_data_period_start | 70 | 68 | 97.1 | 90.2 | 99.2 | 0.961 | splits audit, dev, test; adjudicated 2 of 70 |
| all | lending_data_period_end | 70 | 68 | 97.1 | 90.2 | 99.2 | 0.960 | splits audit, dev, test; adjudicated 2 of 70 |
| all | n_assessment_areas | 70 | 70 | 100.0 | 94.8 | 100.0 | 1.000 | splits audit, dev, test |
| all | n_full_scope | 70 | 70 | 100.0 | 94.8 | 100.0 | 1.000 | splits audit, dev, test |
| all | loan_products_evaluated | 70 | 70 | 100.0 | 94.8 | 100.0 | 1.000 | splits audit, dev, test; element-wise 350/350 agree over 5 elements, kappa 1.000 |
| all | primary_product | 70 | 69 | 98.6 | 92.3 | 99.7 | 0.981 | splits audit, dev, test; adjudicated 1 of 70 |
| all | inside_aa_share_number_pct | 70 | 68 | 97.1 | 90.2 | 99.2 | 0.971 | splits audit, dev, test; adjudicated 2 of 70 |
| all | inside_aa_share_product | 70 | 68 | 97.1 | 90.2 | 99.2 | 0.940 | splits audit, dev, test; adjudicated 2 of 70 |
| all | avg_ltd_ratio_pct | 70 | 70 | 100.0 | 94.8 | 100.0 | 1.000 | splits audit, dev, test |
| all | finding_present | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test |
| all | finding_type | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test |
| all | rating_effect | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test |
| all | rating_lowered | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test |
| all | rating_lowered_level | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test |
| all | rating_before | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test |
| all | rating_after | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test |
| all | laws_cited | 201 | 201 | 100.0 | 98.1 | 100.0 | 1.000 | splits census, dev, test; element-wise 2010/2010 agree over 10 elements, kappa 1.000 |
| all | practice_basis | 201 | 199 | 99.0 | 96.4 | 99.7 | 0.956 | splits census, dev, test; element-wise 1405/1407 agree over 7 elements, kappa 0.908; adjudicated 2 of 201 |
| all | finding_source | 201 | 200 | 99.5 | 97.2 | 99.9 | 0.978 | splits census, dev, test; adjudicated 1 of 201 |
| all | distractor_laws_named | 201 | 200 | 99.5 | 97.2 | 99.9 | 0.990 | splits census, dev, test; adjudicated 1 of 201 |
| all | pooled | 3381 | 3368 | 99.6 | 99.3 | 99.8 |  | 32 fields; kappa not pooled across fields; Wilson CI treats items as independent (items cluster within documents); adjudication rate 13/3381 = 0.4%; 5 document(s) read in more than one split count once per split |
