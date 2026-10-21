"""Audit sheet helpers for the model-reader protocol (analysis plan, "Field groups and truth
sources" and "Audit of Group B on the corpus").

    python scripts/merge_reads.py readers   # data/gold/audit_sheet_readers.csv: the sheet without model columns
    python scripts/merge_reads.py merge     # fill data/gold/audit_sheet.csv from data/gold/audit_reads/

``readers`` writes the copy the two readers receive: every row of ``audit_sheet.csv`` with the
``model_status``, ``model_value`` and ``model_quote`` columns removed, so no reader sees system
output in any tertile (the ``mode`` column is sheet layout, not a reading condition).

``merge`` reads ``audit_reads/{doc_id}.a.csv`` and ``.b.csv`` (header doc_id, field, value,
null_reason, evidence_page, quote, notes, reading_minutes optional) and, where the two readers
disagree on value or null_reason, ``audit_reads/{doc_id}.adj.csv`` (same header, disputed
fields only). It fills value, null_reason, evidence_page, quote, minutes (the mean of the two
readers' reading minutes spread over the 11 Group B fields), labeler (``claude:reader_a_agreed``
or ``claude:adjudicated``), labeled_at and notes of the matching rows of ``audit_sheet.csv``,
leaves the model columns untouched, and writes ``audit_disputes.csv`` (doc_id, field,
reader_a, reader_b, final, note) for ``cra_extract.reliability``. It refuses to run when a
disputed field has no adjudication row or a reader file is missing, so nothing half-merged is
written. Run ``python -m cra_extract.audit validate`` afterwards.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "data" / "gold"
SHEET = GOLD / "audit_sheet.csv"
READERS_SHEET = GOLD / "audit_sheet_readers.csv"
READS = GOLD / "audit_reads"
DISPUTES = GOLD / "audit_disputes.csv"
MODEL_COLUMNS = ("model_status", "model_value", "model_quote")
READ_COLUMNS = ["doc_id", "field", "value", "null_reason", "evidence_page", "quote", "notes"]


def read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k: (v or "").strip() for k, v in r.items() if k is not None and not isinstance(v, list)} for r in csv.DictReader(fh)]


def write_rows(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)


def norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def readers_copy() -> int:
    rows = read_rows(SHEET)
    columns = [c for c in rows[0].keys() if c not in MODEL_COLUMNS]
    write_rows(READERS_SHEET, rows, columns)
    print(f"wrote {READERS_SHEET} ({len(rows)} rows, model columns removed)")
    return 0


def reading_minutes(rows: list[dict[str, str]]) -> float | None:
    for r in rows:
        v = r.get("reading_minutes") or r.get("minutes")
        if v:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def merge() -> int:
    sheet = read_rows(SHEET)
    columns = list(sheet[0].keys())
    docs = sorted({r["doc_id"] for r in sheet})
    fields_of = {d: [r["field"] for r in sheet if r["doc_id"] == d] for d in docs}
    problems: list[str] = []
    final: dict[tuple[str, str], dict[str, str]] = {}
    disputes: list[dict[str, str]] = []
    stamp = datetime.now().isoformat(timespec="seconds")
    for d in docs:
        reads = {}
        for tag in ("a", "b"):
            p = READS / f"{d}.{tag}.csv"
            if not p.exists():
                problems.append(f"{d}: missing reader file {p.name}")
                continue
            reads[tag] = read_rows(p)
        if len(reads) < 2:
            continue
        A = {r["field"]: r for r in reads["a"]}
        B = {r["field"]: r for r in reads["b"]}
        adj_path = READS / f"{d}.adj.csv"
        ADJ = {r["field"]: r for r in read_rows(adj_path)} if adj_path.exists() else {}
        mins = [m for m in (reading_minutes(reads["a"]), reading_minutes(reads["b"])) if m is not None]
        per_row = (sum(mins) / len(mins) / len(fields_of[d])) if mins else None
        for f in fields_of[d]:
            a, b = A.get(f), B.get(f)
            if a is None or b is None:
                problems.append(f"{d} {f}: not read by both readers")
                continue
            agree = norm(a["value"]) == norm(b["value"]) and norm(a["null_reason"]) == norm(b["null_reason"])
            if agree:
                row, labeler, note = a, "claude:reader_a_agreed", ""
            elif f in ADJ:
                row, labeler, note = ADJ[f], "claude:adjudicated", ADJ[f].get("notes", "")
            else:
                problems.append(f"{d} {f}: readers disagree and {adj_path.name} has no row for it")
                continue
            if not agree:
                disputes.append({"doc_id": d, "field": f, "reader_a": a["value"] or a["null_reason"],
                                 "reader_b": b["value"] or b["null_reason"],
                                 "final": row["value"] or row["null_reason"], "note": note})
            final[(d, f)] = {
                "value": row["value"], "null_reason": row["null_reason"], "evidence_page": "" if (row["null_reason"] and row["evidence_page"] in ("", "0")) else row["evidence_page"],
                "quote": row["quote"], "minutes": "" if per_row is None else f"{per_row:.2f}",
                "labeler": labeler, "labeled_at": stamp, "notes": row.get("notes", ""),
            }
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print(f"error: {len(problems)} problem(s); nothing written", file=sys.stderr)
        return 1
    for r in sheet:
        r.update(final[(r["doc_id"], r["field"])])
    write_rows(SHEET, sheet, columns)
    write_rows(DISPUTES, disputes, ["doc_id", "field", "reader_a", "reader_b", "final", "note"])
    n_adj = sum(1 for v in final.values() if v["labeler"] == "claude:adjudicated")
    print(f"filled {len(final)} rows of {SHEET} ({n_adj} adjudicated); {len(disputes)} disputes in {DISPUTES}")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(readers_copy() if cmd == "readers" else merge() if cmd == "merge" else (print(__doc__) or 2))
