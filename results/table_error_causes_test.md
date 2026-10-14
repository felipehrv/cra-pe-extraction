# Error causes (test split)

Hand classification of the non-correct cells of errors_test.csv (design 6.6; analysis plan, Metrics, Error causes). The cause column holds a cause, or several separated by semicolons, from: parsing, locator_miss, wrong_scope, wrong_period, unit_or_format, boilerplate_confusion, reasoning, fabrication, label. The stage column holds one stage for every cause of the row, or one stage per cause in the same order, from: parse, locate, extract, label; parsing, locator_miss and label carry the parse, locate and label stages, and a blank stage takes that stage. A locator miss is a locate-stage cause shared by S1 and S3, not an LLM reasoning error; a gold error found here is a label-stage cause.

Values: non-correct cells with at least one mention, in percent of the system's non-correct cells; a cell with several causes counts once under each, so these shares can sum past 100 percent. Mentions: each (cell, cause) pair once, in percent of the system's mentions. A cell with a blank cause counts as unclassified; a cause with no stage given counts under stage unassigned. Whole-document and closed-book variant cells left out: 1255.

| System | Level | Cause | Stage | Values | Share of values | Mentions | Share of mentions |
|---|---|---|---|---|---|---|---|
| baseline | total | all | all | 872 | 100.0 | 872 | 100.0 |
| baseline | cause | parsing | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | locator_miss | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | wrong_scope | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | wrong_period | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | unit_or_format | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | boilerplate_confusion | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | reasoning | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | fabrication | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | label | all | 0 | 0.0 | 0 | 0.0 |
| baseline | cause | unclassified | all | 872 | 100.0 | 872 | 100.0 |
| baseline | stage | all | parse | 0 | 0.0 | 0 | 0.0 |
| baseline | stage | all | locate | 0 | 0.0 | 0 | 0.0 |
| baseline | stage | all | extract | 0 | 0.0 | 0 | 0.0 |
| baseline | stage | all | label | 0 | 0.0 | 0 | 0.0 |
| baseline | stage | all | unassigned | 872 | 100.0 | 872 | 100.0 |
| baseline | cause_stage | unclassified | unassigned | 872 | 100.0 | 872 | 100.0 |
| llm:gemini:gemini-3.5-flash-lite | total | all | all | 48 | 100.0 | 48 | 100.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | parsing | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | locator_miss | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | wrong_scope | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | wrong_period | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | unit_or_format | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | boilerplate_confusion | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | reasoning | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | fabrication | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | label | all | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | cause | unclassified | all | 48 | 100.0 | 48 | 100.0 |
| llm:gemini:gemini-3.5-flash-lite | stage | all | parse | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | stage | all | locate | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | stage | all | extract | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | stage | all | label | 0 | 0.0 | 0 | 0.0 |
| llm:gemini:gemini-3.5-flash-lite | stage | all | unassigned | 48 | 100.0 | 48 | 100.0 |
| llm:gemini:gemini-3.5-flash-lite | cause_stage | unclassified | unassigned | 48 | 100.0 | 48 | 100.0 |
| rules | total | all | all | 468 | 100.0 | 468 | 100.0 |
| rules | cause | parsing | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | locator_miss | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | wrong_scope | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | wrong_period | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | unit_or_format | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | boilerplate_confusion | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | reasoning | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | fabrication | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | label | all | 0 | 0.0 | 0 | 0.0 |
| rules | cause | unclassified | all | 468 | 100.0 | 468 | 100.0 |
| rules | stage | all | parse | 0 | 0.0 | 0 | 0.0 |
| rules | stage | all | locate | 0 | 0.0 | 0 | 0.0 |
| rules | stage | all | extract | 0 | 0.0 | 0 | 0.0 |
| rules | stage | all | label | 0 | 0.0 | 0 | 0.0 |
| rules | stage | all | unassigned | 468 | 100.0 | 468 | 100.0 |
| rules | cause_stage | unclassified | unassigned | 468 | 100.0 | 468 | 100.0 |
| s5 | total | all | all | 46 | 100.0 | 46 | 100.0 |
| s5 | cause | parsing | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | locator_miss | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | wrong_scope | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | wrong_period | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | unit_or_format | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | boilerplate_confusion | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | reasoning | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | fabrication | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | label | all | 0 | 0.0 | 0 | 0.0 |
| s5 | cause | unclassified | all | 46 | 100.0 | 46 | 100.0 |
| s5 | stage | all | parse | 0 | 0.0 | 0 | 0.0 |
| s5 | stage | all | locate | 0 | 0.0 | 0 | 0.0 |
| s5 | stage | all | extract | 0 | 0.0 | 0 | 0.0 |
| s5 | stage | all | label | 0 | 0.0 | 0 | 0.0 |
| s5 | stage | all | unassigned | 46 | 100.0 | 46 | 100.0 |
| s5 | cause_stage | unclassified | unassigned | 46 | 100.0 | 46 | 100.0 |
