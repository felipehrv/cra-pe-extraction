# Deviations log

One dated entry per change after the plan tag.

## 2026-10-14 Applicability exception kept as coded

FDIC_16600_20230227 (FDIC, Small Bank) prints a Lending Test rating sentence; both readers read it. The gold keeps absent_from_document for lending_test_rating, as the pre-registered applicability table requires for FDIC/small, so a system that returns the printed value is scored as a fabrication on this item. Recorded before the test scoring run; the applicability table is left unchanged. A data-quality row notes the cell exception.

## 2026-10-14 Second test scoring run: S5 rows were missing from the first

The first `evaluate --split test` run (logged in results/test_invocations.log) scored S0, S1, S3, the whole-document ablation and the closed-book baseline but no S5 rows: `routing apply` looked for the S3 system named in config/s5_routing.yaml (gemini-3.8-flash, the Makefile default when the table was computed) and found none, so it appended nothing. The record now names the corpus tier fixed at the freeze (gemini-3.5-flash-lite); S5 rows were appended with `routing apply --s3 llm:gemini:gemini-3.5-flash-lite` from the same frozen predictions, and the test was scored a second time. No label, prediction, rule, prompt or threshold changed between the two runs; the second run adds the S5 rows only.

## 2026-10-14 Error-cause classification; two candidate label errors left unchanged

The cause and stage columns of results/errors_test.csv were filled by a model reader from the cited pages after the scoring run (classification does not change any score). Two gold rows were flagged as candidate label errors and are left as coded, so the metrics stand: OCC_12141_20230601 lending_test_rating (gold S by the codebook rule that an OCC Small Bank lending rating equals the overall rating; the rating page prints Outstanding and Appendix B prints Satisfactory, so the document contradicts itself; systems that return Outstanding are scored wrong on this item) and FDIC_16600_20230227 lending_test_rating (the applicability exception already logged above; systems that return the printed value are scored as fabrications). Abstention rows for fields a system never extracts (baseline, rules, closed-book, the 10 documents outside the ablation sample) carry no cause and appear as unclassified in results/table_error_causes_test.csv.
