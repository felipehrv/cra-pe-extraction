# Committed hashes

Doc hash = sha256 of the sorted doc_id list of the split (one per line). Label hash = sha256 of the label CSV. The test doc hash is recorded before test labeling starts and the label hash after it ends (design 5.1).

| recorded_at | split | n_docs | doc_id list sha256 | label file | label sha256 |
|---|---|---|---|---|---|
| 2026-10-08T21:04:17 | test | 30 | 641232fa5594981252b28029f1611d1581ea783267079e7a72aefb3dda3c7459 | data/gold/labels_test.csv | 293845940ddd08cb8ae56c643b77c455f0730deb617c7557f140f5635e3a7291 |

## Freeze 2026-10-14 (plan-v1.0)

- rules_version: 0.2.4
- prompt_version: v5
- closed_book_prompt_version: cb1
- effort per backend: anthropic=low, gemini=low, ollama=none, openai_compat=low
- field_groups: all (--field-groups)
- fields.yaml sha256: 9100209bd5c682b9eace3799adf4874e25615e2e8cd44d86e699affcea779bb5
- headings.yaml sha256: 5409f5090e3fb9c12fb50c6a2594f718e699466357dbc935b7f4682441994384
- test split: 30 documents, doc_id list sha256 641232fa5594981252b28029f1611d1581ea783267079e7a72aefb3dda3c7459
| 2026-10-14T11:28:02 | test | 30 | 641232fa5594981252b28029f1611d1581ea783267079e7a72aefb3dda3c7459 | data/gold/labels_test.csv | 03b896eeed4db2c59e893959a2ba299d08a7d6f360cff40a77edb307d5686b84 |
