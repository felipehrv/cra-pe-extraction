# Deviations log

One dated entry per change after the plan tag.

## 2026-10-14 Applicability exception kept as coded

FDIC_16600_20230227 (FDIC, Small Bank) prints a Lending Test rating sentence; both readers read it. The gold keeps absent_from_document for lending_test_rating, as the pre-registered applicability table requires for FDIC/small, so a system that returns the printed value is scored as a fabrication on this item. Recorded before the test scoring run; the applicability table is left unchanged. A data-quality row notes the cell exception.

## 2026-10-14 Second test scoring run: S5 rows were missing from the first

The first `evaluate --split test` run (logged in results/test_invocations.log) scored S0, S1, S3, the whole-document ablation and the closed-book baseline but no S5 rows: `routing apply` looked for the S3 system named in config/s5_routing.yaml (gemini-3.8-flash, the Makefile default when the table was computed) and found none, so it appended nothing. The record now names the corpus tier fixed at the freeze (gemini-3.5-flash-lite); S5 rows were appended with `routing apply --s3 llm:gemini:gemini-3.5-flash-lite` from the same frozen predictions, and the test was scored a second time. No label, prediction, rule, prompt or threshold changed between the two runs; the second run adds the S5 rows only.
