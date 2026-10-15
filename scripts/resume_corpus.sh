#!/bin/zsh
# Resume the frozen Flash-Lite corpus pass on the free tier: move failed traces aside,
# drop their prediction rows, and rerun (the run halts cleanly at the daily cap).
set -e
cd "$(dirname "$0")/.."
.venv/bin/python - <<'EOF'
import json, pandas as pd
keep, failed = [], []
for line in open("data/traces/corpus/gemini.jsonl"):
    d = json.loads(line)
    (failed if d.get("error") else keep).append(line)
open("data/traces/corpus/gemini.jsonl", "w").writelines(keep)
if failed:
    open("results/corpus_failed_traces.jsonl", "a").writelines(failed)
    ids = {json.loads(l)["doc_id"] for l in failed}
    p = pd.read_csv("results/predictions_corpus.csv", dtype=str, keep_default_na=False)
    mask = (p.system == "llm:gemini:gemini-3.5-flash-lite") & (p.doc_id.isin(ids))
    p[~mask].to_csv("results/predictions_corpus.csv", index=False)
print(f"good traces kept {len(keep)}, failed moved aside {len(failed)}")
EOF
.venv/bin/python -m cra_extract.run --split corpus --systems llm:gemini:gemini-3.5-flash-lite --allow-api --parse-missing >> results/scale_llm.log 2>&1
tail -1 results/scale_llm.log
