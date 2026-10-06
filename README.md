# cra-pe-extraction

Work in progress. Design: [docs/design.md](docs/design.md).

Bank regulators publish CRA ratings as data but leave component ratings, illegal-credit-practice findings, assessment-area structure, inside-assessment-area lending and loan-to-deposit ratios in PDF prose. This repository extracts those fields from every Small and Intermediate Small Bank Performance Evaluation released 2023 to 2025, measures how far each value can be trusted, and tells regulators what to publish instead.

## Setup

```bash
make env
cp .env.example .env   # fill in GEMINI_API_KEY (free tier) and optional keys
make test
```

Pipeline targets, in the order of the work rule (pilot, measure, fix, freeze, test once, scale, audit):

```
make index    # build config/corpus.csv from the OCC, FDIC and FRB sources
make fetch    # download PDFs with checksum verification
make parse    # page-wise text extraction
make locate   # section locator coverage report
```
