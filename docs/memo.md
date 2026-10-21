# Memo: fields the CRA Performance Evaluations print and the public feeds do not carry

To: the data offices of the OCC, the FDIC and the Federal Reserve
From: the author of `cra-pe-extraction`, an independent research project built on public records
Re: structured publication of Small Bank and ISB PE fields, 2023 to 2025 (draft of 2026-10-16)

## Summary

I built a pipeline that reads every Small Bank and Intermediate Small Bank (ISB) CRA
Performance Evaluation (PE) the three agencies released from January 2023 to December 2025:
2,793 documents in the agencies' own indexes (FDIC 1,770, OCC 605, Federal Reserve 418), of
which 2,791 are evaluable (one PDF returns HTTP 500 and one index link points to an older PE).
It extracts 31 fields per document from four parts of the PE (cover and rating, description of
the institution, lending tables, and the review of discriminatory and other illegal credit
practices), keeps a verbatim quote for every model-extracted value, and checks each field an
agency already publishes against that agency's feed. Extraction cost $0.00 per document in API
fees on the free tier of the hosted model (8,181 input and 1,775 output tokens per document);
reading a PE by hand at 60 pages per hour takes 25.7 minutes for the average PE, $19.37 at the
BLS median hourly wage for financial examiners.

The feeds are reliable for what they carry: the overall rating printed in the PE matches the
index on 2,785 of the 2,786 documents where the rules read it, and the one exception is an
index error (data-quality row 33). The problems are in what the feeds leave out or encode
loosely. ISB component ratings are absent from the OCC feed and, in the FDIC feed, are published
under Large Bank labels that the ISB PEs never print (on the 9 gold FDIC ISB PEs the labels map
one to one to the printed ISB scale, so the column is usable only with an undocumented mapping). The FDIC feed has no
evaluation date; the date inside its PDF file name differs from the printed date on 127 PEs.
No feed records whether examiners identified discriminatory or other illegal credit practices,
which laws were involved, or whether the rating was lowered, so finding those cases takes a
library of 39 boilerplate templates and a hand-reading queue of 161 review sections. The
data-quality table behind this memo has 37 rows: 23 source errors, 7 definitional differences
and 7 errors of my own, each fixed or documented. I recommend publishing, in this order, the
ISB component ratings, the FDIC evaluation date, a structured record of illegal-credit-practice
findings, the inside-assessment-area share and loan-to-deposit ratio, and last the share of
lending to low- and moderate-income borrowers and areas. Each is already printed in the PEs,
so publishing it means carrying into a column a value the examiner has already written.

## Fields to publish, in priority order

Row numbers refer to `results/data_quality_findings.csv` in file order. Counts come from
`results/corpus_counts.csv`, `docs/corpus_pass_2.md`, `results/s5_routing.md` and
`docs/analysis_plan.md`; feed details from `docs/design.md`. Costs are per document from the
cost table: $0.00 in API fees for the released system (free tier; 8,181 input and 1,775 output
tokens per document) and 25.7 minutes of manual reading at 60 pages per hour (a page-rate
convention, since there are no human timers), both for all fields; each item names the part it
relies on.

### 1. ISB component ratings (Lending Test and Community Development Test)

- Who prints it: every ISB PE, at all three agencies, on the rating page. The Federal Reserve
  also publishes it as data: the main row of its CSV carries both test ratings for ISBs.
- Discrepancy: the OCC search API has no component-rating field (it returns charter, bank
  name, overall rating, method, exam date, release date and PDF path), so the 233 OCC ISB PEs
  in the window carry their test ratings in prose only. The FDIC CRAPES feed has a lending
  rating field, but 752 of its 789 ISB rows say "High Satisfactory", 28 "Outstanding" and 8
  "Low Satisfactory" (row 1). "High Satisfactory" and "Low Satisfactory" are Large Bank
  lending-test values; the ISB scale is Outstanding, Satisfactory, Needs to Improve and
  Substantial Noncompliance. One American Bank's PE (FDIC_6078_20240805) prints "Needs to
  Improve" where the feed says "Low Satisfactory". Gold verification: on the 9 gold FDIC ISB PEs (dev and test, read by the model readers) the feed's
  value maps to the printed ISB rating in every case, High Satisfactory to Satisfactory (7),
  Outstanding to Outstanding (1) and Low Satisfactory to Needs to Improve (1), so the field is
  usable with that mapping, which the feed does not document.
- The Federal Reserve format works: on its 202 ISB PEs the rules read a lending rating in 153
  and a CD rating in 155, and every one matches the Federal Reserve file.
- Cost of the PDF route: the ratings come from the cover-and-rating sections of the one model call per
  document ($0.00 in API fees on the free tier) and from the first pages of the 25.7 manual
  minutes per document. For OCC and FDIC ISBs
  there is no usable index to check against, so their accuracy rests on a hand-labeled sample.
- Ask: one column per test on the ISB scale, as the Federal Reserve already publishes.

### 2. Evaluation date in the FDIC feed

- Who prints it: every PE on its cover (FDIC PEs repeat it in the Scope sentence). The OCC
  (exam date field) and the Federal Reserve (exam date column) publish it as data.
- Discrepancy: CRAPES has no evaluation-date field. Besides a release month (row 2: month
  precision on all 1,770 FDIC rows), its only date is inside the PDF file id
  (`YYYY/cert_YYMMDD.PDF`). That date differs from the printed cover date on 127 PEs (row 27).
  Where they differ the file-id date is usually later, by a median of 5 days, by over 90 days
  in 15 cases, and earlier once; one file id carries an impossible 2028 date (row 4). The
  pattern is consistent with a processing date, not the evaluation date. Across the three
  feeds the rules match the index exam date on 2,647 of the 2,791 PEs where they read a date
  (94.8 percent); counting the FDIC file-id differences as agreements gives 99.4 percent.
  Related: 25 FDIC PEs print one date on the cover and another in the Scope sentence (row 28);
  13 OCC (row 29) and 2 Federal Reserve (row 31) index dates appear nowhere in the PE.
- Cost of the PDF route: the routing table sends the date to the rules, so it adds nothing to the
  model cost; the cost is human time, part of the 25.7 manual minutes per document plus triage of every FDIC disagreement against the PE, and FDIC dates can be scored
  only on hand-labeled PEs because the feed holds no evaluation date.
- Ask: the evaluation date printed on the cover, as its own field, and a day-level release date.

### 3. A structured record of illegal-credit-practice findings, laws and rating effect

- Who prints it: all three agencies, in the review section of each PE; the locator finds that
  section in 2,783 of the 2,792 PEs of the second corpus pass. No feed carries any of it.
- Discrepancy: absent from every feed, and the prose is a weak substitute. A keyword search
  returns 48 positives, 23 of them negated boilerplate such as "No evidence ... was identified"
  (row 26). Eight OCC PEs keep the optional sentence "The CRA performance rating was not lowered
  as a result of these findings" after stating that nothing was identified (row 20); one prints
  unfilled placeholders, "(preliminary rating)" and "(final rating)", next to "was not lowered"
  (row 21); one FDIC sentence has words missing (row 22); 4 Federal Reserve PEs have no review
  heading (row 12) and 3 FDIC PEs show no review section in the text (row 23, PDF check open).
- Census evidence: before the template library, a residual rule flagged 1,972 of 2,792 review
  sections. With 39 templates, 61 sections still need a human reader (53 flagged located
  sections and 8 the locator could not find), plus a random 100 unflagged sections to bound
  misses: 161 sections in all. 26 flagged sections contain a sentence that asserts a finding;
  they are candidates, not confirmed findings. Census result: 23 PEs with a finding (0.82 percent,
  exact CI 0.52 to 1.23), 6 of them with a lowered rating; 0 of 100 random unflagged sections
  held a finding, so misses among the unflagged sections are bounded at 3.6 percent.
- Cost of the PDF route: the review section of the one model call per document ($0.00 in API fees
  on the free tier) plus the reading of 161 sections, each by two model readers.
- Ask: per PE, whether the review was performed; the finding type (none, discrimination, other
  illegal credit practice, both); the laws cited from a fixed list (ECOA and Regulation B, Fair
  Housing Act, FTC Act section 5, CFPA UDAAP, HOEPA, RESPA section 8, TILA, SCRA, MLA, other);
  the rating effect (noted, considered and not lowered, lowered); and, if lowered, which rating
  and from what to what. The OCC template leftovers suggest the text is drafted from a fixed
  menu, so the choice a structured field would record is made when the PE is written.

### 4. Inside-assessment-area share and loan-to-deposit ratio

- Who prints it: all three agencies, in the lending criteria of Small Bank and ISB PEs, in a
  sentence and usually a table. No feed carries either figure.
- Discrepancy: with no feed there is nothing to compare, but the documents show why a single
  published value helps. Lemont National Bank's PE prints 54.5 percent inside the assessment
  area in the sentence and 54.4 in the table (row 10). One Federal Reserve PE prints no
  performance-criteria conclusions at all (row 11). 32 Federal Reserve PEs print the
  institution-level conclusions after an assessment-area heading (row 19), so a reader of the
  text needs a layout rule to know the figure covers the whole bank.
- Cost of the PDF route: the lending sections of the one model call per document ($0.00 in API
  fees on the free tier; on test the model read the inside share right on 26 of 30 PEs and the
  loan-to-deposit ratio on 29 of 30) and part of the 25.7 manual minutes per document. The locator could not find the loan-to-deposit
  section in 71 FDIC, 28 OCC and 2 Federal Reserve PEs, nor the assessment-area concentration
  section in 21, 29 and 2; those need whole-document input or a person.
- Ask: the average net loan-to-deposit ratio with the quarters averaged, and the inside share
  by number and by dollar amount with the loan product and period it covers. Call Reports allow
  an approximate rebuild of the ratio, not of the inside share, which depends on the loans the
  examiner reviewed.

### 5. Share of lending to low- and moderate-income borrowers and areas (last)

- Who prints it: the PEs of all three agencies, in the borrower-profile and geographic
  distribution analyses, usually per assessment area.
- Discrepancy: not measured; per-assessment-area tables are a stretch item, with no table row.
- Cost of the PDF route: not measured; the per-document cost and minutes above exclude it.
- Why last: for home mortgages the agencies can compute these shares from HMDA. A published
  figure would matter mainly for products outside HMDA and for the examiner's own sample.

## Feed maintenance issues found while building the index

- FFIEC combined CRA ratings file: the download is CAPTCHA-gated, the file is stale as of
  mid-2024 and its FDIC dates are release months (row 3). I did not use it.
- FDIC CRAPES API: at most 500 rows per query, so a complete pull has to be split by month and
  method and each query checked for truncation; release dates come as months only (row 2).
- OCC API: 559 index rows have no release date (row 5), three of them 2023 exams that may
  belong in the 2023 to 2025 window; a query for 2023 to 2025 also returns 2026 rows.
- Federal Reserve CSV: an exam appears on a main row and again on MSA and State rows with the
  same RSSD and exam date; only a blank MSA and State marks the main row. 14 older rows (exams
  2002 to 2017) carry an exam date after the public date (row 6).
- Wrong PDF link: the OCC row for the 2023-02-13 exam of charter 9183 links the PE of the
  December 3, 2018 exam (row 30); the 2023 PE cannot be reached from the index.
- PDF portfolio: one Federal Reserve PE is served as a PDF portfolio whose text layer is only a
  reader notice, with the 42-page PE embedded inside (row 9).
- Transient 404s: five FDIC PDFs returned 404 after 502 and 503 retries and were fetched on a
  later run (row 8).
- HTTP 500: one Federal Reserve PE returned HTTP 500 on 6 attempts in each of two runs on
  2026-10-08 (row 7).
- Index values the PE contradicts: an overall rating (row 33), five exam methods (row 34) and
  the exam dates listed under item 2.

A link check, a row-type column and a release date on every row would remove most of these.

## What this work does not do, and how the data were used

The pipeline measures what the documents say. It is not an enforcement scorecard: it does not
rank banks, judge examiners' conclusions or compare the agencies, and counts appear by agency
only because each agency runs its own feed. Every analysis is descriptive and none is causal.
Where a feed and a PE disagree, I report the printed value and classify the cause as a source
error, a definitional difference or my own error. Only public records were used: the PEs and
the agencies' public indexes. The hosted model runs on a free tier whose terms let the provider
keep and reuse submitted text, which is acceptable only because PEs are public. Non-public
records, such as draft PEs or examination workpapers, would need a paid tier with zero data
retention or a local open-weight model, which the pipeline already supports.
