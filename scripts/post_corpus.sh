#!/bin/zsh
# The chain after the corpus Flash-Lite pass completes (2,751 good traces), in plan order.
#   ./scripts/post_corpus.sh before   # routing, Group A corpus scoring, risk, audit draw, audit sheet, readers' copy
#   (run scripts/audit_double_read.js through the Workflow tool, then)
#   ./scripts/post_corpus.sh after    # merge reads, reliability, audit estimate, census score, dev rerun, report
# Dry-run of every step on the partial corpus: docs/dev_notes.md, 2026-10-16.
set -e
cd "$(dirname "$0")/.."
S3=llm:gemini:gemini-3.5-flash-lite
case "$1" in
  before)
    n=$(.venv/bin/python -c "import json;print(sum(1 for l in open('data/traces/corpus/gemini.jsonl') if not json.loads(l).get('error')))")
    test "$n" -eq 2751 || { echo "corpus pass incomplete: $n of 2751 good traces"; exit 2; }
    make s5-apply S3_CORPUS=$S3
    make group-a-corpus S3_CORPUS=$S3
    make risk-corpus S3_CORPUS=$S3
    make draw-audit DRAW_DATE=$(date +%Y-%m-%d)
    make audit-sheet
    .venv/bin/python scripts/merge_reads.py readers
    .venv/bin/python - <<'EOF'
import yaml, pandas as pd, json
rec = yaml.safe_load(open("config/strata.yaml"))["splits"]["audit"]
m = pd.read_csv("config/corpus.csv", dtype=str, keep_default_na=False).set_index("doc_id")
ids = sorted(m[m.split == "audit"].index)
docs = [{"doc_id": d, "bank": m.loc[d, "bank_name"], "pages": int(m.loc[d, "pages"])} for d in ids]
json.dump({"root": str(__import__("pathlib").Path(".").resolve()), "docs": docs}, open("data/gold/audit_workflow_args.json", "w"), indent=1)
print(f"{len(docs)} audit documents; workflow args in data/gold/audit_workflow_args.json")
EOF
    ;;
  after)
    .venv/bin/python scripts/merge_reads.py merge
    .venv/bin/python -m cra_extract.audit validate data/gold/audit_sheet.csv
    make reliability
    make audit
    make census-score
    make dev-rerun S3_CORPUS=$S3 RUN_FLAGS=--allow-api
    make report CORPUS_SYSTEM=$S3
    make reproduce
    make test
    ;;
  *) echo "usage: $0 before|after"; exit 2 ;;
esac
