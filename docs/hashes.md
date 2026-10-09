# Committed hashes

Doc hash = sha256 of the sorted doc_id list of the split (one per line). Label hash = sha256 of the label CSV. The test doc hash is recorded before test labeling starts and the label hash after it ends (design 5.1).

| recorded_at | split | n_docs | doc_id list sha256 | label file | label sha256 |
|---|---|---|---|---|---|
| 2026-10-08T21:04:17 | test | 30 | 641232fa5594981252b28029f1611d1581ea783267079e7a72aefb3dda3c7459 | data/gold/labels_test.csv | 293845940ddd08cb8ae56c643b77c455f0730deb617c7557f140f5635e3a7291 |
