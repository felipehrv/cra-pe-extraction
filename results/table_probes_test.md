# Fabrication probes (test split)

Percent; Wilson 95 percent intervals. Probe items: the applicability ABS cells of the component ratings (cd_test_rating on Small documents, lending_test_rating on FDIC and FRB Small documents), and rating_before, rating_after and laws_cited on every document whose gold finding_present is false. The other Group C fields are scored against the census, not on test.

| Section | Field | System | Probe items | Fabrication [95% CI] | Abstained probes | Basis |
|---|---|---|---|---|---|---|
| Component rating probe | lending_test_rating | baseline | 10 | 0.0 [0.0, 27.8] | 10 | applicability ABS cells |
| Component rating probe | lending_test_rating | llm:gemini:gemini-3.5-flash-lite | 10 | 10.0 [1.8, 40.4] | 0 | applicability ABS cells |
| Component rating probe | lending_test_rating | rules | 10 | 0.0 [0.0, 27.8] | 0 | applicability ABS cells |
| Component rating probe | lending_test_rating | s5 | 10 | 10.0 [1.8, 40.4] | 0 | applicability ABS cells |
| Component rating probe | cd_test_rating | baseline | 14 | 0.0 [0.0, 21.5] | 14 | applicability ABS cells |
| Component rating probe | cd_test_rating | llm:gemini:gemini-3.5-flash-lite | 14 | 0.0 [0.0, 21.5] | 0 | applicability ABS cells |
| Component rating probe | cd_test_rating | rules | 14 | 0.0 [0.0, 21.5] | 0 | applicability ABS cells |
| Component rating probe | cd_test_rating | s5 | 14 | 0.0 [0.0, 21.5] | 0 | applicability ABS cells |
| Group C (fabrication probe) | rating_before | baseline | 30 | 0.0 [0.0, 11.4] | 30 | documents with gold finding_present false |
| Group C (fabrication probe) | rating_before | llm:gemini:gemini-3.5-flash-lite | 30 | 0.0 [0.0, 11.4] | 0 | documents with gold finding_present false |
| Group C (fabrication probe) | rating_before | rules | 30 | 0.0 [0.0, 11.4] | 30 | documents with gold finding_present false |
| Group C (fabrication probe) | rating_before | s5 | 30 | 0.0 [0.0, 11.4] | 0 | documents with gold finding_present false |
| Group C (fabrication probe) | rating_after | baseline | 30 | 0.0 [0.0, 11.4] | 30 | documents with gold finding_present false |
| Group C (fabrication probe) | rating_after | llm:gemini:gemini-3.5-flash-lite | 30 | 0.0 [0.0, 11.4] | 0 | documents with gold finding_present false |
| Group C (fabrication probe) | rating_after | rules | 30 | 0.0 [0.0, 11.4] | 30 | documents with gold finding_present false |
| Group C (fabrication probe) | rating_after | s5 | 30 | 0.0 [0.0, 11.4] | 0 | documents with gold finding_present false |
| Group C (fabrication probe) | laws_cited | baseline | 30 | 0.0 [0.0, 11.4] | 30 | documents with gold finding_present false |
| Group C (fabrication probe) | laws_cited | llm:gemini:gemini-3.5-flash-lite | 30 | 0.0 [0.0, 11.4] | 0 | documents with gold finding_present false |
| Group C (fabrication probe) | laws_cited | rules | 30 | 0.0 [0.0, 11.4] | 30 | documents with gold finding_present false |
| Group C (fabrication probe) | laws_cited | s5 | 30 | 0.0 [0.0, 11.4] | 0 | documents with gold finding_present false |
