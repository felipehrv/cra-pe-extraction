# Closed-book baseline (test split)

The S3 corpus tier received only the bank name and exam date of each document. Accuracy among answered items and overall (abstentions as misses), Wilson 95 percent intervals, next to the majority-class rate of the split. Reported in Limitations as a contamination bound.

| Field | System | Gold items | Accuracy, answered | Accuracy, overall | Abstained | Majority class (rate) |
|---|---|---|---|---|---|---|
| overall_rating | llm:gemini:gemini-3.5-flash-lite:closedbook | 30 | 60.0 [31.3, 83.2] | 20.0 [9.5, 37.3] | 20 | s (66.7) |
| finding_present | llm:gemini:gemini-3.5-flash-lite:closedbook | 30 | 100.0 [72.2, 100.0] | 33.3 [19.2, 51.2] | 20 | false (100.0) |
