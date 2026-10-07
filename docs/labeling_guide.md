# Labeling guide (codebook) for cra-pe-extraction

Version 1, 2026-10-05, written against design v1 (`docs/design.md`, sections 3, 5.1, 5.2, 5.4) and
the data dictionary `config/fields.yaml`. The validator in `src/cra_extract/label.py` enforces the
formal rules below; this guide explains the judgment calls.

The reader is assumed to be a careful economist who has not read a CRA Performance Evaluation (PE)
before. Nothing here requires legal training. When this guide and `fields.yaml` disagree,
`fields.yaml` wins for formats and `docs/design.md` wins for meaning; report the disagreement in
`docs/deviations.md`.

## 0. How to use this guide

Every example below cites one of the eight sample PEs in `tests/fixtures/samples/` by a short key
and a line number of the `pdftotext -layout` output (the `.txt` file next to each PDF). Line numbers
are for finding the passage quickly; the `evidence_page` you record in the sheet is the PDF page
number as printed in the PDF viewer (1 = cover page), never a txt line.

| key | file | regulator | method | overall | why it is in the set |
|---|---|---|---|---|---|
| GN | `OCC_703395_GNBank_2023_NI.txt` | OCC | Small | NI | OCC Small layout; enforcement action mentioned but no finding |
| LM | `OCC_11715_LemontNB_2024_SN.txt` | OCC | Small | SN | OCC Small with separate analysis periods (Tables O and P) |
| FN | `OCC_13682_FirstNeighborBank_2024_ISB_O.txt` | OCC | ISB | O | OCC ISB; no prior evaluation sentence; assets without an as-of date |
| SS | `FDIC_12299_SouthStar_2022_ISB_NI_fairlending.txt` | FDIC | ISB | NI | five AAs; rating lowered for ECOA and FHA violations (redlining) |
| LB | `FDIC_26816_LibertyBank_2022_SM_SN.txt` | FDIC | Small | SN | UDAP finding that did not change the rating; zero loans inside the AA |
| OA | `FDIC_6078_OneAmericanBank_2024_ISB_NI.txt` | FDIC | ISB | NI | four products reviewed; years-only data windows |
| FP | `FRB_3664588_FieldPoint_2024_ISB_NI.txt` | FRB (New York) | ISB | NI | four AAs in four states; state-level ratings |
| SB | `FRB_663656_SmallBusinessBank_2024_SM_NI.txt` | FRB (Kansas City) | Small | NI | FRB Small layout; Cease and Desist mentioned but no finding; laws named inside negated boilerplate |

A citation such as `SS:599` means line 599 of the SouthStar txt file.

## 1. Vocabulary

Every (document, field) cell takes exactly one of three states:

- **value**: the PE prints the information. You copy it, cite the page, and quote the sentence or
  table row verbatim.
- **not_applicable** (NA): the field has no meaning for this document given what the document is.
  Example: `n_full_scope` when the bank has one assessment area; every Group C attribute when there
  is no finding. NA is decided by a rule, not by searching the document.
- **absent_from_document** (ABS): the field has meaning for this document but the PE does not print
  it. Example: `cd_test_rating` in a Small Bank PE (the procedure has no Community Development
  Test); `avg_ltd_ratio_pct` when the PE says only "reasonable" without a number. ABS is decided
  after searching the sections listed for the field in section 5 and finding nothing.

The difference matters for scoring (design 6.2): a system that prints a value where gold says NA or
ABS is counted as a **fabrication**, and the share of such cells is reported per field. Several
cells are deliberately kept in the sheet only to catch fabrications (the "probes" in section 4).

**Scope.** Every quote has a scope, recorded in the `scope` column with one of `institution`,
`state`, `msa`, `assessment_area`, `appendix`, `toc`, `unknown`. All fields in this codebook are
institution-level. A sentence found under a state or assessment-area heading must not be used for
an institution-level field even when it looks identical (FP:594 "The Lending Test is rated: Needs to
Improve." is the State of New York rating; the institution rating is FP:99). Record `appendix`
when the evidence is an appendix table (for instance the assessment-area list in OCC Appendix A).

**Quote.** Copy the shortest span of text that contains the value, verbatim, including the number
or word you coded. Do not paraphrase, do not fix typos (FP:2156 says "FLORIA"), do not join text
from two places. If the value is a table cell, quote the row (label plus numbers) as it reads on the
page. A value without a quote fails validation.

## 2. Labeling procedure

1. **Blind.** Label with the PDF only. Do not open system outputs, traces, `results/`, or the
   rules module while labeling. Pre-filled Group A cells come from the agency index, not from any
   extraction system; confirming them against the cover page is part of the job.
2. **One sheet per split.** `python -m cra_extract.label init --split dev --out data/gold/labels_dev.csv`
   writes one row per (doc_id, field) with these columns:

   | column | meaning |
   |---|---|
   | `doc_id` | `{regulator}_{bank_id}_{YYYYMMDD}` from the manifest |
   | `field` | field name from `fields.yaml` |
   | `field_group` | A, A_or_B, B, C (informational) |
   | `pdf_path` | where the PDF is on disk |
   | `prefilled` | `true` when `value` or `null_reason` was filled by `init` and needs confirmation |
   | `value` | the value, or empty |
   | `null_reason` | `not_applicable` or `absent_from_document`, or empty |
   | `evidence_page` | PDF page (1-based) of the quote; required whenever `value` is set |
   | `quote` | verbatim text; required whenever `value` is set |
   | `scope` | scope of the quote (section 1) |
   | `minutes` | time spent on this cell, required on every row (see step 4) |
   | `labeler` | your initials |
   | `labeled_at` | ISO datetime when you finished the cell |
   | `notes` | anything a second reader should know; also `analysis_periods=...` (section 5, field 12) |

   Exactly one of `value` and `null_reason` is set on every row. Prefilled rows are not exempt: a
   prefilled value still needs `evidence_page`, `quote`, `minutes`; a prefilled null still needs
   `minutes` (you confirmed the null by looking).
3. **Order.** Label one document at a time, in the row order of the sheet (Group A, then B, then C).
   Do not go back to a finished document after seeing another one unless you record the change in
   `notes` with the reason.
4. **Timer.** Start a timer when you begin a cell and record elapsed minutes (decimals allowed,
   0.1 minimum effort means 0.1, do not write 0). The timer feeds the manual cost baseline (design
   6.9), so include the time spent searching for something that turned out to be absent. Group A
   confirmations are expected to take about a minute per document in total; record them per row
   anyway.
5. **Formats.** Dates are ISO `YYYY-MM-DD`. Ratings are `O`, `S`, `NI`, `SN`. Sets are elements
   joined by `|` (`home_mortgage|small_business`). Booleans are `true` or `false`. Percentages are
   numbers without the sign (`75.2`). Dollar amounts are integers in dollars (`1159094000`).
6. **Validate often.** `python -m cra_extract.label validate data/gold/labels_dev.csv` lists every
   formal problem by row; fix them before moving on. The sheet must validate cleanly before it is
   committed.
7. **Checkpoint.** After 10 dev documents, report the mean minutes per document. Above 35 minutes
   the fallback sizes in design 5.5 apply.
8. **Hashes.** Before test labeling starts: `python -m cra_extract.label hash --split test` records
   the sha256 of the sorted test doc_id list in `docs/hashes.md`. After test labeling ends:
   `python -m cra_extract.label hash --split test --labels data/gold/labels_test.csv` records the
   label file hash. Both are committed. Any later edit to a test label needs a `docs/deviations.md`
   entry with before and after metrics.

## 3. Where things are in a PE

All three agencies use the same interagency procedures, so the content is the same; the headings
and the order differ. Headings are stable within an agency and are the patterns in
`config/headings.yaml`.

**OCC** (GN, LM, FN). Cover page: "PUBLIC DISCLOSURE", date, bank name, "Charter Number" (LM:1-8).
Page 1: "Overall CRA Rating" with "Institution's CRA Rating: This institution is rated ..." and
"The lending test is rated: ..." (GN:51-54); ISB adds "The community development test is rated:
..." (FN:60). Then "Loan-to-Deposit Ratio" and "Lending in Assessment Area" with Table D
(FN:81-113), "Description of Institution" (FN:120), "Scope of the Evaluation" with "Evaluation
Period/Products Evaluated" and "Selection of Areas for Full-Scope Review" (GN:159-170), the
review section "Discriminatory or Other Illegal Credit Practices Review" (GN:185), then one "State
Rating" section per state (GN:209), then Appendix A "Scope of Examination" with the "List of
Assessment Areas and Type of Examination" (GN:407-412) and Appendix C tables. Every page carries
"Charter Number: NNN" in the header; ignore it when counting evidence.

**FDIC** (SS, LB, OA). Cover: "PUBLIC DISCLOSURE", date, name, "Certificate Number" (SS:1-14).
Table of contents with dot leaders (SS:42-52; the locator skips these lines, you should too).
"INSTITUTION RATING" with "INSTITUTION'S CRA RATING: This institution is rated ..." (SS:56),
"The Lending Test is rated ..." and "The Community Development Test is rated ..." for ISB
(SS:67, SS:85). "DESCRIPTION OF INSTITUTION" (prior evaluation, assets), "DESCRIPTION OF
ASSESSMENT AREAS" (SS:163), "SCOPE OF EVALUATION" with "General Information" and "Activities
Reviewed" (SS:202, SS:239), "CONCLUSIONS ON PERFORMANCE CRITERIA" with "Loan-to-Deposit Ratio"
and "Assessment Area Concentration" (SS:318, SS:349), the review section "DISCRIMINATORY OR OTHER
ILLEGAL CREDIT PRACTICES REVIEW" (SS:592), then one section per assessment area for multi-AA banks
("AUSTIN MSA AA - Full-Scope Review", SS:606), then appendices and glossary.

**FRB** (FP, SB). Cover: "PUBLIC DISCLOSURE", date, name, "RSSD No." or "RSSD#" (FP:11-12,
SB:10-11). "INSTITUTION'S CRA RATING" (FP:96-100) or, in the Kansas City district,
"INSTITUTION'S COMMUNITY REINVESTMENT ACT RATING" with the sentence "Small Business Bank (the
bank) is rated Needs to Improve." (SB:52-54). "SCOPE OF EXAMINATION" (SB:67), "DESCRIPTION OF
INSTITUTION" (SB:84), "DESCRIPTION OF ASSESSMENT AREA" (SB:135), "CONCLUSIONS WITH RESPECT TO
PERFORMANCE CRITERIA" with "Net Loan-to-Deposit Ratio" or "Loan-to-Deposit Ratio" and "Assessment
Area Concentration" (SB:373, SB:408; FP:371, FP:380), the review section "FAIR LENDING OR OTHER
ILLEGAL CREDIT PRACTICES REVIEW" (FP:579, SB:529), then for multi-state banks one "STATE OF ..."
section per state with its own ratings (FP:592-595), then appendices; FRB Appendix C "SCOPE OF
EVALUATION" repeats the time period, products and the assessment-area list (FP:4365-4406). Wording
differs by Reserve Bank district; the design pulls one PE per district to catalogue the variants.

## 4. Applicability table

Applicability is a function of (regulator, exam method). A cell marked ABS is pre-filled by `init`
as `absent_from_document`; you confirm it by checking the rating page. If a pre-filled ABS cell
turns out to have a printed value, enter the value, clear `null_reason`, and write
`applicability_exception` in `notes`: this is exactly the kind of thing the dev pilot exists to
find.

| field | OCC Small | OCC ISB | FDIC Small | FDIC ISB | FRB Small | FRB ISB |
|---|---|---|---|---|---|---|
| lending_test_rating | value, equals overall | value | ABS | value | ABS (to verify) | value |
| evidence | GN:54, LM:50 | FN:59 | LB:51-76 has no Lending Test sentence | SS:67, OA:57 | SB:54-64 has no Lending Test sentence; one district only, verify on the 12-district pilot | FP:99 |
| cd_test_rating | ABS (probe) | value | ABS (probe) | value | ABS (probe, to verify) | value |
| evidence | GN:51-66, LM:47-62 have no CD sentence | FN:60 | LB:51-76 | SS:85, OA:73 | SB:54-64; verify on the 12-district pilot | FP:100 |

Every other field is "value" in every cell, with field-level null rules in section 5.

**Fabrication probes** kept in the sheet on purpose: `cd_test_rating` on every Small Bank
document; `lending_test_rating` on FDIC and FRB Small; `rating_before`, `rating_after`,
`laws_cited` on every document with `finding_present = false` (gold NA). Confirm these nulls with
the same care as a value: a Small Bank PE that did print a CD rating would be a design error we
need to know about.

## 5. Field codebook

Format for each field: definition; where to find it; typical wording per regulator with one
cited example; null rule; matching tolerance (how a system answer is scored against yours, from
`fields.yaml`); traps.

### Group A (index-verified; pre-filled from the manifest, confirm on the cover page)

**1. regulator** (enum OCC, FDIC, FRB). The agency named on the cover page: "Office of the
Comptroller of the Currency" (GN:15), "Federal Deposit Insurance Corporation" (SS:22), "Federal
Reserve Bank of New York" (FP:17). Never null. Exact match. Trap: the FDIC cover also names a
regional office; the regulator is still FDIC.

**2. bank_name** (string). The name printed on the cover page, as printed (GN:7 "GN Bank";
FN:8 "First Neighbor Bank, National Association"; FP:11 "Fieldpoint Private Bank & Trust"). Never
null. Tolerance: normalized token-set ratio at least 90, so punctuation and "N.A." versus
"National Association" do not matter, but a holding-company name instead of the bank name does.
Trap: the FRB CSV prints names in capitals with trailing spaces; confirm the cover, not the index.

**3. bank_id** (string). OCC "Charter Number 11715" (LM:8), FDIC "Certificate Number: 12299"
(SS:14), FRB "RSSD No. 3664588" (FP:12) or "RSSD# 663656" (SB:11). Never null. Exact after
stripping leading zeros. Trap: OCC charter numbers above 700000 (GN:8 "703395") are federal savings
associations converted to OCC supervision; they are still charter numbers.

**4. exam_date** (date). The date printed on the cover page under "PUBLIC DISCLOSURE": LM:2
"April 8, 2024", SS:4 "September 12, 2022", SB:3 "November 18, 2024". Never null. Exact. Traps:
the FDIC API has only a release month, so the pre-filled FDIC date comes from the file-id pattern
and your confirmation is what makes it trustworthy; the FRB page header repeats the date on every
page (FP:94), which is fine as a quote but cite page 1.

**5. exam_method** (enum small, isb). Which interagency procedure was used. OCC: "We assessed the
bank under the small bank CRA evaluation procedures" (LM:142-143) or "using intermediate small bank
evaluation procedures" (FN:154-155). FDIC: "Interagency Small Institution Examination Procedures"
(LB:225-226) or "Interagency Intermediate Small Institution Examination Procedures" (SS:203-204).
FRB: "Interagency Examination Procedures for Small Institutions" (SB:69-70) or "FFIEC Interagency
Intermediate Small Institution Examination Procedures" (FP:253-254). Never null. Exact. Trap: a
bank can change procedure between exams (OA:262-264 "became subject to the Interagency
Intermediate Small Institution Examination Procedures effective January 1, 2024"); code the
procedure used in this PE, which is also what the index says.

**6. overall_rating** (enum). "This institution is rated Needs to Improve." (GN:52, SS:56),
"Small Business Bank (the bank) is rated Needs to Improve." (SB:54). Map words to codes:
Outstanding = O, Satisfactory = S, Needs to Improve = NI, Substantial Noncompliance = SN
(also spelled "Substantial Non-Compliance", LM:134). Never null. Exact. Trap: multi-state FRB and
OCC PEs print a rating per state further down (FP:593 "CRA RATING FOR NEW YORK: NEEDS TO
IMPROVE."); the institution rating is the one on the rating page (FP:98).

**7. lending_test_rating** (enum, applicability table). OCC: "The lending test is rated: Needs to
Improve." (GN:54). FDIC: "The Lending Test is rated Satisfactory." (SS:67). FRB: "The Lending Test
is rated: Needs to Improve." (FP:99). Null rule: ABS for FDIC Small and FRB Small (section 4);
value elsewhere. Exact; for OCC Small it must equal `overall_rating` (the validator checks).
Traps: FRB and OCC state sections repeat the sentence at state scope (FP:594, GN:211), do not use
them; OCC Small PEs print a lending test rating even though the overall rating is by construction
the same (LM:48-50).

**8. cd_test_rating** (enum, applicability table). OCC: "The community development test is rated:
Outstanding." (FN:60). FDIC: "The Community Development Test is rated Satisfactory." (SS:85).
FRB: "The Community Development Test is rated: Satisfactory." (FP:100). Null rule: ABS for every
Small Bank PE (there is no CD test under Small Bank procedures). Exact. Trap: Small Bank PEs still
discuss community development qualitatively in places (LB:288 "The bank made two investments");
that is not a rating.

**9. prior_evaluation_date** (date). The date of the previous CRA evaluation as the PE states it,
usually in the Description of Institution. OCC: "in its last CRA evaluation dated October 1, 2018"
(GN:150), "at the preceding CRA evaluation, dated June 19, 2021" (LM:134-135). FDIC: "previous
... Performance Evaluation, dated July 15, 2019" (SS:106-107), "At the previous evaluation, dated
December 3, 2018" (OA:88-89). FRB: "prior CRA Performance Evaluation dated February 12, 2018"
(FP:201-202), "at its March 23, 2020 performance evaluation" (SB:125-126). Null rule: ABS when
the PE states there was no prior evaluation (a de novo bank) and also when the PE simply never
states the date: FN has no such sentence anywhere (grep "prior", "previous", "preceding", "last
CRA" finds only a glossary line, FN:568), so FN is ABS for fields 9 and 10 even though the OCC
index has a previous row. Exact. Trap: the Scope section restates the date as the start of the
evaluation period (SS:202 "from the prior evaluation dated July 15, 2019"); either sentence is a
valid quote, but prefer the Description of Institution because it also carries the rating.

**10. prior_rating** (enum). The rating of that previous evaluation: "assigned GN Bank a
'Satisfactory' rating" (GN:150), "received a Substantial Non-Compliance rating at the preceding
CRA evaluation" (LM:134), "received a Satisfactory rating at its previous ... Performance
Evaluation" (SS:106), "assigned a rating of 'Satisfactory' at the prior CRA Performance
Evaluation" (FP:201), "was rated Satisfactory under the CRA at its March 23, 2020 performance
evaluation" (SB:125). Null rule: ABS as for field 9. Exact. Trap: an FRB ISB PE may also recall
prior component ratings; code only the overall.

### Group B (gold-scored)

**11. total_assets_usd** (integer dollars) and **11a. total_assets_asof** (date). The total assets
figure the PE gives for the institution and the date it refers to. Where: Description of
Institution; in FDIC PEs also the "Total Assets as of" column of the LTD comparison table. OCC:
"As of December 31, 2021, GN Bank reported total assets of $84.7 million" (GN:121-122). FDIC: "As
of March 31, 2024, the institution reported total assets of $463 million" (OA:104); LB has no
figure in the description paragraph but the LTD table prints "Total Assets as of 6/30/2022
($000s)" with "Liberty Bank, Inc. 12,614" (LB:297-300), so value 12614000, as-of 2022-06-30, scope
institution. FRB: "Based on the September 30, 2023 ... Call Report, the bank had total assets of
$1.7 billion" (FP:150-151); "The bank has total assets of $94.2 million as of June 30, 2024."
(SB:91). Conversion: "$84.7 million" is 84700000; "$1.7 billion" is 1700000000; a table in
"$(000s)" is multiplied by 1000. Null rule: ABS when no figure is printed; `total_assets_asof` is
ABS when the figure is printed without a date that is attached to it. Attached means in the same
sentence or the same paragraph: FN:121 "is a $593.7 million (total assets) intrastate financial
institution" carries no date, and the "As of December 31, 2023" in FN:135-136 belongs to a different
paragraph about the loan portfolio, so FN is value 593700000 with `total_assets_asof` ABS (note
this case for the author; the arithmetic 462.1 / 593.7 = 77.8 percent suggests the same date but
inference is not quoting). Tolerance: within 1 percent at the same as-of quarter, which is why the
rounding of the source matters. Trap: when the PE prints both an approximate figure and an exact
table figure for the same date, take the exact one and note the other: SS:123 "assets totaled
approximately $1.2 billion" versus the LTD table SS:340 "1,159,094" ($000s, as of 06/30/2022), so
value 1159094000, as-of 2022-06-30, with `approx_1.2bn_in_text` in notes. Do not use peer banks'
rows or the "Asset Size" column of an FRB comparison table for other banks.

**12. lending_data_period_start, lending_data_period_end** (dates). The window of loan data the
Lending Test analysed, as stated. Where: OCC "Evaluation Period/Products Evaluated" (GN:161 "The
evaluation period for the Lending Test was January 1, 2019 through December 31, 2021."); FDIC
"Activities Reviewed" (SS:272-273 "small business loans originated during the period of January 1,
2021, to December 31, 2021") or the source line of the inside/outside table (LB:326 "Evaluation
Period: 1/1/2021 - 6/30/2022 Bank Data"); FRB "Evaluation Period" (FP:266-267 "home mortgage loans
and small business loans from January 1, 2017, to December 31, 2022") or the Scope bullet (SB:80-81
"originated between March 24, 2020, and December 31, 2023"). Null rule: ABS when the PE gives only
years; then write `analysis_periods=2021-2023` (or whatever the PE says) in `notes` of the start
row. "Calendar years 2021 through 2023" (LM:142) names a start and an end unambiguously and is
coded 2021-01-01 to 2023-12-31; a bare list of data years ("2021, 2022, and 2023 HMDA LARs",
OA:300-301; "originated, renewed, or extended in 2022 and 2023", OA:289-290) is years only and is
ABS. When products have different windows, code the window of the primary product (field 14a) and
list the others in notes: SS home mortgage is 2019 to 2021 by year (SS:263-266) and small business
is dated (SS:272-273); home mortgage is primary (SS:288), so SS is ABS with
`analysis_periods=HMDA 2019-2021; SB 2021-01-01..2021-12-31`. Exact; end must not be after
`exam_date` (validator). Traps: "This evaluation covers the period from the prior evaluation
dated ... to the current evaluation dated ..." (SS:202-203, LB:224-225, OA:261-262) is the
evaluation period, not the data window; the Community Development window (FP:267-269) is not the
lending window; the LTD averaging window (GN:70, SS:322-323) is not the lending window.

**13. n_assessment_areas** (integer, at least 1). Number of assessment areas the bank had
designated and that the PE evaluates. OCC: "The bank has only one AA" (GN:338), "First Neighbor
has one AA in the state of Illinois" (FN:177), and Appendix A "List of Assessment Areas and Type
of Examination" (GN:407-412). FDIC: "designated five assessment areas" (SS:165), "designated a
single assessment area" (OA:142), "has defined its AA as all of Salt Lake County" (LB:128-129).
FRB: "The CRA performance evaluation included the following four bank delineated assessment
areas" (FP:221), "The bank's delineated AA is comprised of the Kansas City, Missouri-Kansas
Multistate ... MSA AA" (SB:137-138). Never null. Exact; the evaluation module also checks the
count against the number of AA sections the locator finds. Traps: count what the PE evaluates,
not what the bank once had: FP:213-219 describes a fifth AA (Miami) that examiners excluded, so FP
is 4 (note `excluded_aa=MD 33124` in notes); OCC combines bank-delineated areas within one MSA into
one AA (LM:161-164), count the combined AAs the PE rates; a multi-state MSA (SB) is still one AA.

**13a. n_full_scope** (integer, 1 to n_assessment_areas). Number of AAs reviewed with full-scope
procedures. FDIC: SS:210-213 "applied full-scope procedures to ... the Austin MSA AA ... also
applied full-scope procedures to the Killeen MSA AA ... limited-scope procedures to the College
Station MSA AA, Houston MSA AA, and Non-MSA AA", so 2 of 5; the table of contents and the AA
section headings carry the same information (SS:46-50). FRB: "Examiners used full-scope
examination procedures to evaluate the bank's performance for all included assessment areas"
(FP:258-259), so 4, confirmed by Appendix C (FP:4391-4406). OCC: Appendix A "Type of Exam"
column (LM:389-394). Null rule: NA when `n_assessment_areas` is 1 (the validator enforces both
directions). Exact. Trap: OCC boilerplate about selecting full-scope areas (GN:167-170) appears
even for one-AA banks; it is not evidence of a count.

**14. loan_products_evaluated** (set of home_mortgage, small_business, small_farm, consumer,
other). The products the Lending Test analysed. OCC: "the primary loan products were consumer and
small business loans" (GN:162-163); "primary lending products are consumer and business loans ...
we sampled consumer and business loans" (FN:163-167). FDIC: "major product lines include home
mortgage and small business lending" (SS:239-240); "examiners reviewed consumer, small business,
small farm, and home mortgage loans" (OA:286-287). FRB: "loans reportable under the Home Mortgage
Disclosure Act (HMDA) as well as small business loans were analyzed" (FP:273-274); "A statistical
sample of 169 small business loans" (SB:80). Never empty. Tolerance: per-element precision and
recall. Mapping: "business", "commercial", "small loans to businesses" are small_business;
"residential", "HMDA", "home purchase, refinance, improvement, multifamily" are home_mortgage;
"agricultural" and "farm" are small_farm; "consumer", "Tiny Home Loans" (LB:89), student and auto
loans are consumer; anything else (for instance a credit card program analysed on its own) is
other. Traps: include a product the PE reviewed even when it carried little weight (LB:245-249
reviewed 2 small business loans next to the consumer product, so LB is consumer|small_business);
do not include products the PE says it excluded (FP:277-278 "Construction, consumer, and small
farm loans were excluded"); do not include products that appear only in the loan portfolio table
(SB:112-121 lists farmland loans, but the test used small business loans only).

**14a. primary_product** (enum, same plus unstated). The product the PE calls primary, major, or
gives the most weight. "home mortgage loans represent the bank's primary lending product" (LM:144);
"home mortgage loans received heavier weighting when arriving at overall conclusions" (SS:288);
"examiners placed more weight on home mortgage loans" (FP:276-277); "consumer loans received the
most weight, followed by home mortgage, small business, and small farm loans" (OA:334-335). Never
null: when only one product was evaluated, it is primary (SB: small_business, quote SB:80); when
several were evaluated and the PE neither names one as primary nor orders them by weight, code
`unstated` (GN:162-163 names two products as "the primary loan products" and gives no order, so
GN is unstated). Exact. Trap: "primary lending focus" in the Description of Institution describes
the portfolio (SB:94-95 agricultural, OA:271 consumer); use the Scope or Conclusions sentence
about the test when there is one, and fall back to the portfolio sentence only if the PE itself
uses it to justify the product choice (OA:271-272 does; code consumer).

**15. inside_aa_share_number_pct** (percent, 0 to 100) and **15a. inside_aa_share_product**
(enum). The share of loans by number originated inside the assessment area(s), as printed, and
which product set it refers to. Where: OCC "Lending in Assessment Area" (LM:99 "originated and
purchased 54.5 percent of its total loans inside the bank's AA"; FN:102 "87.5 percent of its total
loans sampled inside the AA"; GN:78-79 "3.4 percent"); FDIC "Assessment Area Concentration"
table (OA:394 "Total 82 0.9"; LB:325 "Total 0 0.0"); FRB "Assessment Area Concentration" (FP:382-383
"lending inside the assessment area was 40.8% by number and 43.6% by dollar volume"; SB:412-413
"only 21.3 percent of the bank's lending by number"). Product: `all_reviewed_combined` when the
figure is the Total row or the sentence says "total loans" (LM, FN, GN, OA, LB, FP, SB); otherwise
the product the figure belongs to. SS prints subtotals per product and no Total row (SS:363-368),
so SS is the primary product's pooled subtotal: 75.2 with `home_mortgage` (SS:367 "Subtotal 713
75.2"). Null rule: ABS when the PE says only "a majority" or "substantial majority" without a
number; 15a is NA when 15 is ABS. Tolerance: within 0.1 percentage point. Traps: number, not
dollars (FP:383 prints both; take 40.8); the bank-level figure, not an AA-level one in a multi-AA
section; zero is a value (LB), not ABS; a single-year row is not the pooled figure (SS:364-366 are
per year, SS:367 is the subtotal).

**16. avg_ltd_ratio_pct** (percent, 0 to 300). The average (net) loan-to-deposit ratio over the
period the PE states. OCC: "quarterly average LTD ratio ... was 54.32 percent" (GN:69-70),
"average LTD for the 16-quarter period since the preceding CRA evaluation was 10.6 percent"
(LM:69-70), "quarterly LTD ratio averaged 85.5 percent" (FN:85). FDIC: "averaged 100.7 percent
over the past 12 calendar quarters" (SS:322-323), "averaged 64.9 percent over the past five
calendar quarters" (LB:281-282), "average net loan-to-deposit ratio is 128.1 percent over the past
22 calendar quarters" (OA:349-350). FRB: "average LTD ratio for the 24-quarters ... was 91.1%"
(FP:374-375), "19-quarter average NLTD ratio of 85.9 percent" (SB:382-383). Null rule: ABS when
the PE says only "reasonable" and prints no average. Tolerance: within 0.1 pp; copy the decimals
as printed (54.32 stays 54.32). Traps: the headline average, not the range ("ranged from a low of
79.5 percent", SS:323-324), not the peer average (GN:72-74), not the prior-exam average (SS:324
"107.9 percent ... at the prior evaluation"), not an adjusted figure the PE computes afterwards
(LB:289 "After the investments ... 65.5 percent"; code 64.9).

### Group C (census of the review section; also labeled on every gold document)

Read section 6 before coding these. All Group C fields come from the review section (OCC
"Discriminatory or Other Illegal Credit Practices Review", FDIC the same in capitals, FRB "Fair
Lending or Other Illegal Credit Practices Review") plus, when a finding exists, the rating page
paragraph that restates it (SS:63-65, LB:74-76).

**17. finding_present** (bool, derived). True only under the codebook rule in section 6. Never
null. Scored as precision and recall with true as the positive class. Positive examples: SS:599-601,
LB:352-354. Negative examples: GN:194-196, FN:205-206, OA:658-659, FP:581-583, SB:531-535.

**17a. finding_type** (enum discrimination, other_illegal_credit_practice, both). Discrimination
when the PE names a prohibited-basis practice or a fair lending law violation (ECOA, Regulation B,
Fair Housing Act): SS:599-602 "discriminatory lending practices ... violations of the Equal Credit
Opportunity Act and Fair Housing Act ... on the basis of redlining". Other when the practice is a
consumer-protection violation without a prohibited basis: LB:352-354 UDAP under FTC Act Section 5.
Both when the section reports one of each. NA when 17 is false.

**17b. rating_effect** (enum). What the PE says the finding did to the rating:
`substantive_lowered` when the rating was lowered (SS:599 "The FDIC lowered the CRA rating from
Satisfactory to Needs to Improve"); `substantive_not_lowered` when the PE considered the violation
substantive but states the rating was not changed (LB:358-360 "Although the bank's practices
harmed consumers, considering the bank's already substantially deficient performance, this illegal
credit practice did not affect the CRA rating"); `technical_only_noted` when the PE calls the
violations technical, isolated, or says they did not warrant consideration; `affected_unspecified`
when the PE says the finding was considered or affected the rating without saying whether the
rating moved. NA when 17 is false. Exact.

**17c. rating_lowered** (bool, derived): true exactly when 17b is `substantive_lowered`. Never null.

**17d. rating_lowered_level** (enum overall, lending_test, cd_test, state_or_msa, unspecified).
Which rating was lowered. SS lowered the institution rating (SS:63-65), so `overall`. Use
`state_or_msa` when only a state or MSA rating in a multi-state PE moved, `lending_test` or
`cd_test` when a component moved and the PE says so, `unspecified` when the PE says "lowered" and
names no rating. NA unless 17c.

**17e. rating_before, rating_after** (enum). The from and to ratings when 17c: SS:599 "from
Satisfactory to Needs to Improve" gives S and NI. NA unless 17c; ABS when the PE says the rating
was lowered but gives no from/to pair. When the level is `overall`, `rating_after` must equal
`overall_rating` (validator).

**17f. laws_cited** (set). Laws the finding cites: ECOA and Regulation B are one element
`ECOA_RegB`; "Fair Housing Act" is `FHA`; "Federal Trade Commission Act, Section 5" is
`FTC_s5_UDAP` (LB:352-354); "Consumer Financial Protection Act" or "UDAAP" is `CFPA_UDAAP`; HOEPA,
RESPA Section 8, TILA, SCRA, MLA as named; anything else is `other` with the law in notes. SS is
`ECOA_RegB|FHA` (SS:600-601). NA when 17 is false (probe); ABS when a finding names no law.
Per-element precision and recall. Trap: laws named in negated boilerplate (SB:532-533 names ECOA
and the Fair Housing Act in a sentence that reports no finding) are not cited laws; they set field
17i instead.

**17g. practice_basis** (set redlining, pricing, underwriting, marketing, servicing, UDAP_product,
other). What the practice was: SS:601-602 "on the basis of redlining" gives `redlining`.
`UDAP_product` only when the PE identifies a specific product, fee or term as the unfair or
deceptive practice; LB:354 says only that "The UDAP violation caused substantial harm to a majority
of the bank's mortgage loan borrowers" and names no practice, so LB is ABS. NA when 17 is false;
ABS when a finding names no practice. Reported as counts.

**17h. finding_source** (enum). Where the finding came from according to the PE: `this_CRA_exam`
when the PE attributes it to examiners during the evaluation or review period without naming a
separate examination (SS:600 "Examiners identified violations", LB:352 "The FDIC identified
illegal credit practices during the evaluation period"); `concurrent_compliance_exam` when the PE
names a compliance or consumer examination; `prior_exam_referenced` when the finding was made at
an earlier examination and is carried into this rating; `enforcement_action_referenced` when the
PE bases the finding on a consent order or similar action; `DOJ_HUD_referral` when the PE says the
matter was referred to or came from DOJ or HUD; `unstated` otherwise. NA when 17 is false.

**17i. distractor_laws_named** (bool, gold flag, not scored). True when the review section names a
law inside a sentence that reports no finding: SB:531-535 "Compliance with the substantive
provisions of antidiscrimination and other consumer protection laws and regulations, including the
Equal Credit Opportunity Act and the Fair Housing Act, was considered ... No evidence ... was
identified." False for FP:581-583 (no law named), OA:658-659, GN:194-196. Also true for a positive
document whose section names a law in the boilerplate part. Never null. It defines a reporting
stratum for the census.

## 6. Group C rules

### 6.1 The codebook rule (verbatim from design section 3)

"A finding exists only if the review section of this PE states that discriminatory or other illegal
credit practices were identified and considered in this rating. Findings described in a prior PE,
enforcement actions mentioned in the Description of Institution, and the OCC forward-looking
boilerplate are not findings. A UDAP violation is finding_type = other_illegal_credit_practice, not
discrimination."

### 6.2 A finding versus a reference to a prior finding or an enforcement action

A **finding** is a statement, in the review section of this PE, that examiners (or the agency)
identified a violation or practice, and a statement of how it was considered in this rating. Both
parts are present in SS:599-604 and in LB:352-360 (where the consideration is that it "did not
affect the CRA rating": still a finding, with 17b = `substantive_not_lowered`).

A **reference** is any mention of violations, orders, or compliance problems that does not meet
that test:

- An enforcement action in the Description of Institution: GN:150-156 "The bank has been operating
  under a formal enforcement action due to the financial condition of the bank"; SB:126-128 "the
  bank's directorate entered into a Cease and Desist Order (C&D) with the Board of Governors". Both
  review sections are negative (GN:194-196, SB:533-535). These are not findings. Record the
  reference in `notes` as `reference=enforcement_action_description` so the findings registry
  can list it as context.
- A prior PE's finding recalled in the narrative ("the rating was lowered at the previous
  evaluation because ...") without a statement that it is considered in this rating. Not a finding;
  note `reference=prior_finding`.
- The OCC forward-looking boilerplate: "The OCC will consider any information that this institution
  engaged in discriminatory or other illegal credit practices, identified by or provided to the OCC
  before the end of the institution's next performance evaluation" (GN:198-204, LM:196-199,
  FN:208-211). Present in every OCC PE, always a non-finding. A keyword search for "illegal credit
  practices" hits it several times per OCC document; that is the false-positive stratum design 4
  mentions.
- Community-contact comments about discrimination in the market (FP:351 "appraisal
  discrimination") are about the area, not the bank.

If the review section itself says the agency "considered" a prior exam's finding or an
enforcement action in this rating, that is a finding with 17h = `prior_exam_referenced` or
`enforcement_action_referenced`.

### 6.3 UDAP versus discrimination

Discrimination means treatment on a prohibited basis (race, national origin, sex, age, and the
other bases in ECOA and the Fair Housing Act) including redlining, pricing or underwriting
disparities, and marketing exclusion. UDAP (Section 5 of the FTC Act) and UDAAP (the Consumer
Financial Protection Act) are unfair, deceptive, or abusive acts toward any customers, with no
prohibited basis: fees, add-on products, misleading terms, servicing errors. A UDAP or UDAAP
violation is `finding_type = other_illegal_credit_practice` even when the harmed borrowers happen
to be a protected group, unless the PE itself says the practice was on a prohibited basis, in which
case it is `both`. Violations of TILA, RESPA, HOEPA, SCRA, MLA and similar consumer laws are also
`other_illegal_credit_practice`.

## 7. Reliability procedure

Preferred (design 5.4): an outside coder double-codes 8 documents on the hard Group B fields
(`n_assessment_areas`, `n_full_scope`, `loan_products_evaluated`, `lending_data_period_start`,
`lending_data_period_end`) plus every census positive and 20 matched template-negative review
sections on all Group C fields. Procedure:

1. Give the coder this guide, the 8 PDFs (or the review sections for the census part) and an
   `init` sheet filtered to the fields above. No system output, no author labels.
2. The coder records value or null, page, quote and minutes exactly as in section 2.
3. Agreement is computed per field: Cohen's kappa where both classes occur in both coders' labels,
   percent agreement with the prevalence of the majority class otherwise. Set fields are compared
   element-wise.
4. Every disagreement is resolved by re-reading the PE together; the resolution and the reason go
   into `docs/deviations.md` if a gold label changes, and into this guide as a new trap if the
   cause was an ambiguous rule.
5. The coder's minutes per field are reported next to the author's as the manual baseline range.

Fallback: the author blind re-codes 6 dev documents at least 5 days after the first coding, on the
same fields, from a fresh `init` sheet, and reports intra-rater agreement labeled as the weaker
check. Dev documents only; test labels are never re-coded outside a deviations entry.

## 8. Validation rules the tool enforces

`python -m cra_extract.label validate PATH` reports, with the row number and the rule name:

| rule | meaning |
|---|---|
| `columns` | the file lacks a required column |
| `unknown_field` | `field` is not in `fields.yaml` |
| `duplicate_row` | the same (doc_id, field) appears twice |
| `bad_value` | enum not in the allowed list, set element not allowed or empty, bool not true/false, number not parseable or out of range |
| `bad_date` | a date field is not ISO `YYYY-MM-DD` |
| `bad_null_reason` | `null_reason` is not `not_applicable` or `absent_from_document` |
| `value_and_null` | both set |
| `empty` | neither set |
| `not_nullable` | a null on a field that is never null |
| `missing_evidence_page`, `missing_quote` | a value without its page or quote |
| `bad_evidence_page`, `bad_scope`, `bad_labeled_at` | malformed cells |
| `missing_minutes` | minutes empty or negative on any row |
| `lte_exam_date` | `lending_data_period_end` after `exam_date` |
| `lte_n_assessment_areas` | `n_full_scope` larger than `n_assessment_areas` |
| `na_when_single_aa` | `n_full_scope` must be NA exactly when `n_assessment_areas` is 1 |
| `equals_overall_for_OCC_small` | OCC Small lending test rating differs from the overall rating |
| `equals_overall_when_level_overall` | `rating_after` differs from `overall_rating` when the lowered level is `overall` |

The `matches_aa_section_count` rule in `fields.yaml` is checked by the evaluation module against
the located sections, not by the sheet validator.
