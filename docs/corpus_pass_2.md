# Corpus pass 2 (2026-10-08)

Second corpus-wide run after the tuning of 2026-10-08 on the corpus and dev splits (design
6.1 and 6.8): FRB locator and `config/headings.yaml`, the review-template library
(`config/review_templates.yaml`, version 2, 39 templates) and the Group A rules (rules 0.2.0,
then 0.2.1). No test document was looked at; the test split is not drawn and the `split`
column was not touched.

"Before" is the first pass of 2026-10-07 (`results/locator_coverage.csv`, `census.csv`,
`baseline.csv` of that run, and `results/rules_0.1.0.csv`). "After" is this run, in order:

```
make locate      # results/locator_coverage.csv, log results/locate_full.log (70 s)
make templates   # results/census.csv,            log results/templates_full.log (46 s)
make rules       # results/rules.csv (0.2.1),     log results/rules_full.log (44 s)
make baseline    # results/baseline.csv,          log results/baseline_full.log (35 s)
make group-a     # results/group_a_*.csv (0.1.0 vs 0.2.1)
```

Denominators: 2,792 parsed documents (FDIC 1,770, FRB 417, OCC 605) of 2,793 manifest rows;
FRB_614948_20250902 has no PDF (HTTP 500). All counts include the 10 dev documents.

## Reconciliation edits

1. `tests/test_templates.py`: `test_template_library_is_well_formed` checked the old one-entry
   FDIC library; it now checks the seeded FDIC entry first, 17 FDIC templates, 30 corpus
   variants that each carry evidence and a note and cite their first evidence document as
   `source`. `test_each_template_matches_the_section_it_was_seeded_from` now skips the corpus
   variants (they are checked against their evidence in `tests/test_template_evidence.py`).
   New `test_no_template_evidence_comes_from_a_held_out_split`: every evidence doc_id is in
   split `corpus`.
2. `src/cra_extract/rules.py`: RULES_VERSION 0.2.0 -> 0.2.1 with a CHANGELOG entry. The
   regulator rule now matches the cover line after `locate.normalize_line`, because with the
   locator fix the 46 FRB Chicago covers (U+00A0 for every space) were located but the agency
   pattern still missed them (regulator abstention FRB 0.110 with rules 0.2.0 on the new
   locator). The entry also records that S1 inputs changed with the locator and headings
   tuning. `tests/test_rules.py`: version test updated (and checks the CHANGELOG is ordered
   and unique); new `test_regulator_rule_reads_no_break_space_cover`.
3. No conflict was found in `config/headings.yaml` (no duplicate keys or patterns) or in
   `locate.py`; both agents' additions are kept as they were.

## Locator coverage (share found, misses after)

| section | FDIC | FRB | OCC | all |
|---|---|---|---|---|
| cover | 0.999 -> 0.999 (1 miss) | 0.890 -> 0.998 (1 miss) | 1.000 -> 1.000 (0) | 0.983 -> 0.999 |
| rating | 1.000 -> 1.000 (0) | 0.820 -> 0.998 (1 miss) | 0.995 -> 0.995 (3 miss) | 0.972 -> 0.999 |
| description_of_institution | 1.000 -> 1.000 (0) | 0.827 -> 0.998 (1 miss) | 0.980 -> 0.980 (12 miss) | 0.970 -> 0.995 |
| description_of_assessment_area | 0.990 -> 0.990 (18 miss) | 0.441 -> 0.573 (178 miss) | 0.000 -> 0.000 (605, by layout) | 0.693 -> 0.713 |
| scope_of_evaluation | 0.999 -> 0.999 (1 miss) | 0.830 -> 0.998 (1 miss) | 0.998 -> 0.998 (1 miss) | 0.974 -> 0.999 |
| ltd_ratio | 0.960 -> 0.960 (71 miss) | 0.628 -> 0.995 (2 miss) | 0.954 -> 0.954 (28 miss) | 0.909 -> 0.964 |
| aa_concentration | 0.988 -> 0.988 (21 miss) | 0.839 -> 0.995 (2 miss) | 0.952 -> 0.952 (29 miss) | 0.958 -> 0.981 |
| illegal_credit_practices_review | 0.984 -> 0.998 (4 miss) | 0.849 -> 0.988 (5 miss) | 1.000 -> 1.000 (0) | 0.967 -> 0.997 |

Every FRB section reaches 0.95 except the AA description, whose 178 misses are layouts with
per-AA headings and no institution-level section (`results/frb_locator_misses.csv`). The
FDIC LTD (71) and OCC LTD, AA concentration and description misses were not part of this
pass. Found does not mean found in institution scope: 22 FRB LTD or AA concentration
sections and 32 FRB review sections are located at confidence 0.5 in AA scope (author
decision, see below).

## Template-residual census (Group C, design 5.2)

| regulator | n | review located | flagged | not located (flagged) | flagged with an asserting sentence | median best score |
|---|---|---|---|---|---|---|
| FDIC | 1770 | 1741 -> 1766 | 1575 -> 34 | 29 -> 4 | 17 | 66.9 -> 100.0 |
| FRB | 417 | 354 -> 412 | 368 -> 16 | 63 -> 5 | 7 | 74.0 -> 100.0 |
| OCC | 605 | 605 -> 605 | 29 -> 12 | 0 -> 0 | 2 | 100.0 -> 100.0 |
| all | 2792 | 2700 -> 2783 | 1972 -> 62 | 92 -> 9 | 26 | 70.3 -> 100.0 |

The asserting-sentence column did not exist before (third flag criterion, 2026-10-08).

Best template score over located review sections:

| score | before | after |
|---|---|---|
| [0, 60] | 50 | 8 |
| (60, 85] | 1565 | 22 |
| (85, 90] | 10 | 6 |
| (90, 95] | 69 | 78 |
| (95, 99.9] | 111 | 700 |
| (99.9, 100] | 895 | 1969 |
| mean | 79.6 | 99.1 |

Flag reasons after (not exclusive): 9 not located, 30 best score below 85, 53 residual over
120 characters, 26 asserting sentence (none flagged by that criterion alone).

Hand-reading queue (design 5.2, step 3): 62 flagged sections (53 located, 9 to hand-locate:
FDIC_12969_20230221, FDIC_14769_20231218, FDIC_29227_20231016, FDIC_57301_20230222,
FRB_164975_20250818, FRB_654300_20230710, FRB_785473_20220418, FRB_816304_20220912,
FRB_956376_20230515) plus the random 100 of the 2,722 unflagged located corpus-split
sections, so 162 sections. 2 of the 62 are dev documents (the SouthStar and Liberty
positives). 62 is under the design's expected 100 to 300 and under the 300 cap.

## Group A rules versus the index (design 6.3)

Rules 0.1.0 on the first-pass locator versus rules 0.2.1 on the pass-2 locator, against
`config/corpus.csv`. Agreement is among answered documents.

| field | regulator | agreement | agree/answered after | Wilson 95% CI after | abstention |
|---|---|---|---|---|---|
| regulator | OCC | 1.0000 -> 1.0000 | 605/605 | [0.9937, 1.0000] | 0.0000 -> 0.0000 |
| regulator | FDIC | 1.0000 -> 1.0000 | 1769/1769 | [0.9978, 1.0000] | 0.0006 -> 0.0006 |
| regulator | FRB | 1.0000 -> 1.0000 | 416/416 | [0.9908, 1.0000] | 0.1103 -> 0.0024 |
| regulator | all | 1.0000 -> 1.0000 | 2790/2790 | [0.9986, 1.0000] | 0.0168 -> 0.0007 |
| bank_id | OCC | 1.0000 -> 1.0000 | 605/605 | [0.9937, 1.0000] | 0.0711 -> 0.0000 |
| bank_id | FDIC | 0.9989 -> 0.9989 | 1767/1769 | [0.9959, 0.9997] | 0.0006 -> 0.0006 |
| bank_id | FRB | 0.9968 -> 0.9976 | 413/414 | [0.9864, 0.9996] | 0.2470 -> 0.0072 |
| bank_id | all | 0.9989 -> 0.9989 | 2785/2788 | [0.9968, 0.9996] | 0.0527 -> 0.0014 |
| exam_date | OCC | 0.9784 -> 0.9785 | 592/605 | [0.9636, 0.9874] | 0.0033 -> 0.0000 |
| exam_date | FDIC | 0.9268 -> 0.9271 | 1641/1770 | [0.9141, 0.9383] | 0.0040 -> 0.0000 |
| exam_date | FRB | 0.9946 -> 0.9952 | 414/416 | [0.9826, 0.9987] | 0.1127 -> 0.0024 |
| exam_date | all | 0.9474 -> 0.9484 | 2647/2791 | [0.9396, 0.9560] | 0.0201 -> 0.0004 |
| exam_method | OCC | 0.9702 -> 0.9933 | 597/601 | [0.9830, 0.9974] | 0.2231 -> 0.0066 |
| exam_method | FDIC | 0.9881 -> 0.9994 | 1769/1770 | [0.9968, 0.9999] | 0.0028 -> 0.0000 |
| exam_method | FRB | 0.9722 -> 1.0000 | 416/416 | [0.9908, 1.0000] | 0.1367 -> 0.0024 |
| exam_method | all | 0.9827 -> 0.9982 | 2782/2787 | [0.9958, 0.9992] | 0.0706 -> 0.0018 |
| overall_rating | OCC | 1.0000 -> 1.0000 | 601/601 | [0.9936, 1.0000] | 0.0446 -> 0.0066 |
| overall_rating | FDIC | 0.9994 -> 0.9994 | 1769/1770 | [0.9968, 0.9999] | 0.0045 -> 0.0000 |
| overall_rating | FRB | 1.0000 -> 1.0000 | 415/415 | [0.9908, 1.0000] | 0.2374 -> 0.0048 |
| overall_rating | all | 0.9996 -> 0.9996 | 2785/2786 | [0.9980, 0.9999] | 0.0480 -> 0.0021 |

FRB ISB component ratings with an index value (202 rows): lending_test_rating 117/117 ->
153/153 agree, cd_test_rating 117/117 -> 155/155.

Triage (`results/group_a_triage_summary.csv`, every disagreement of either version, none
untriaged): our_error 40 (all exam_method, all fixed), source_error 25, definitional 129
(127 of them the FDIC file-id exam dates). The one new disagreement on the pass-2 locator
(OCC_15748, exam_method) was already in the hand triage. exam_date fails the S5 98 percent
rule only because of the definitional FDIC rows; if they are counted as agreeing, it is
0.9939. The S5 routing table itself is computed after the test draw on split `corpus` only.

## S0 keyword baseline

| regulator | review located | finding_present | rating_lowered | hits (negated) |
|---|---|---|---|---|
| FDIC | 1741 -> 1766 | 26 -> 26 | 2 -> 2 | 56 (11) -> 57 (12) |
| FRB | 354 -> 412 | 12 -> 20 | 0 -> 0 | 361 (342) -> 420 (393) |
| OCC | 605 -> 605 | 2 -> 2 | 1 -> 1 | 610 (607) -> 615 (612) |
| all | 2700 -> 2783 | 40 -> 48 | 3 -> 3 | 1027 (960) -> 1092 (1017) |

Baseline 0.1.0 did not change; the 8 new FRB positives come from the newly located FRB
review sections. 25 of the 48 S0 positives are census-flagged; the other 23 (FDIC 10, FRB
13) were read and are all negated boilerplate ("No substantive violations ... were
identified", "found no violations of the substantive provisions", "Examiners identified no
evidence"), so they are S0 false positives (data-quality row "S0 keyword baseline").

## Data quality

`results/data_quality_findings.csv` has 37 rows (source_error 23, definitional 7, our_error
7): the three tuning reports' findings plus the known rows. Merged duplicates: the scanned
PDF and the portfolio PDF (FRB_785473_20220418) are one row; FDIC_445_20280408 is one row;
the two FRB review-heading rows are one row. The S0 negation row carries the pass-2 count
(23).

## For the author to verify

- FRB_785473_20220418 is a PDF portfolio, not a scan: extract the embedded PDF or drop it.
- FDIC exam_date definition (printed cover date as manifest exam_date, file-id date kept as
  provenance); FDIC_445_20280408 hand-correction to 2024-04-08.
- OCC_9183_20230213 links the 2018 PE: re-point or exclude before the test draw.
- FRB_614948_20250902: retry the download before the test draw, else log it as excluded.
- Single-AA scope decision for 32 FRB review and 22 LTD/AA concentration sections located
  in AA scope at confidence 0.5 (grounding would flag their quotes).
- Transition PEs (Small Bank to ISB): a `transition` value or a pre-registered tie rule.
- FDIC CRAPES ISB lending ratings on the Large Bank scale: confirm on at least three gold PEs.
- The third census flag criterion (asserting sentence) belongs in design 5.2 and
  `docs/analysis_plan.md` before the freeze; `docs/analysis_plan.md` still cites
  RULES_VERSION 0.1.0.
- The FRB marker changes alter `run.count_aa_sections`, an input of the test-split strata.
- Hand-read the 62 flagged sections and the random 100 unflagged.
