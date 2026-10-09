"""Group A at corpus scale: rules (S1) against the agency index (design section 6.3).

Joins a rules output (``results/rules.csv``) to the manifest (``config/corpus.csv``) and
writes, per field and regulator, the agreement among answered documents (with a Wilson 95
percent interval) and the abstention share. With ``--before`` (an earlier rules output) both
versions are tabulated side by side and every disagreement of either version is triaged into
``our_error``, ``source_error`` or ``definitional``:

- automatically, when a disagreement of the earlier version is resolved to the index value by
  the later one (``our_error``, the later quote is the evidence), or when an FDIC exam_date
  differs from the date in the FDIC file id (``definitional``: the file id carries the date
  the file was keyed, not the printed evaluation date);
- by hand otherwise, from ``config/group_a_triage.yaml``, whose evidence quotes are checked
  against the page text when ``data/text`` is present.

Outputs (``--out-dir``, default ``results``): ``group_a_agreement.csv``,
``group_a_triage.csv``, ``group_a_triage_summary.csv`` (counts per field, regulator and
category), ``fdic_exam_date_lag.csv`` (file-id date minus printed date, FDIC only) and, with
``--before``, ``group_a_abstention_sample.csv``: a seeded sample of the earlier version's
abstentions per field with their cause (a rule gap the later version answers, or a locator
miss: section not found, found on a table-of-contents line, or a text layer of no-break
spaces). The causes depend on the locator in the working tree.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from cra_extract.config import CONFIG_DIR, CORPUS_PATH, RESULTS_DIR, TEXT_DIR
from cra_extract.stats import wilson_ci

# rules field -> manifest column
INDEX_COLUMNS = {
    "regulator": "regulator",
    "bank_id": "bank_id",
    "exam_date": "exam_date",
    "exam_method": "method",
    "overall_rating": "overall_rating",
}
TRIAGE_FIELDS = ("bank_id", "exam_date", "exam_method", "overall_rating")
REGULATORS = ("OCC", "FDIC", "FRB")
CATEGORIES = ("our_error", "source_error", "definitional")
TRIAGE_PATH = CONFIG_DIR / "group_a_triage.yaml"
LAG_BUCKETS = [(-10**6, -1, "negative"), (1, 7, "1-7"), (8, 31, "8-31"), (32, 90, "32-90"), (91, 10**6, ">90")]


def _norm_id(value: str) -> str:
    return value.strip().lstrip("0") or ("0" if value.strip() else "")


def join(rules: pd.DataFrame, manifest: pd.DataFrame) -> pd.DataFrame:
    """One row per (document, Group A field) with the rule and index values side by side."""
    columns = [c for c in INDEX_COLUMNS.values() if c != "regulator"]
    idx = manifest[["doc_id", "regulator", *columns, "source_file_id", "release_date"]]
    idx = idx.rename(columns={c: f"index_{c}" for c in idx.columns if c not in ("doc_id", "regulator")})
    r = rules[rules["field"].isin(INDEX_COLUMNS)][["doc_id", "field", "status", "value", "quote", "page", "notes"]]
    out = r.merge(idx, on="doc_id", how="inner").reset_index(drop=True)
    out["index_value"] = out["regulator"]
    for field, column in INDEX_COLUMNS.items():
        if field != "regulator":
            sel = out["field"] == field
            out.loc[sel, "index_value"] = out.loc[sel, f"index_{column}"]
    out["rule_value"] = out["value"]
    ids = out["field"] == "bank_id"
    out.loc[ids, "rule_value"] = out.loc[ids, "rule_value"].map(_norm_id)
    out.loc[ids, "index_value"] = out.loc[ids, "index_value"].map(_norm_id)
    out["answered"] = out["status"] == "value"
    out["agree"] = out["answered"] & (out["rule_value"] == out["index_value"])
    return out


def agreement_table(joined: pd.DataFrame, version: str) -> pd.DataFrame:
    """Agreement among answered documents and abstention share per field x regulator (+ all)."""
    rows = []
    for field in INDEX_COLUMNS:
        f = joined[joined["field"] == field]
        for reg in (*REGULATORS, "all"):
            s = f if reg == "all" else f[f["regulator"] == reg]
            n, answered, agree = len(s), int(s["answered"].sum()), int(s["agree"].sum())
            lo, hi = wilson_ci(agree, answered)
            rows.append({
                "rules_version": version, "field": field, "regulator": reg, "n": n,
                "answered": answered, "agree": agree, "disagree": answered - agree,
                "abstain": n - answered,
                "agreement": round(agree / answered, 4) if answered else float("nan"),
                "agreement_lo": round(lo, 4), "agreement_hi": round(hi, 4),
                "abstention": round((n - answered) / n, 4) if n else float("nan"),
            })
    return pd.DataFrame(rows)


def disagreements(joined: pd.DataFrame) -> pd.DataFrame:
    """Answered rows of the triaged fields whose value differs from the index."""
    d = joined[joined["answered"] & ~joined["agree"] & joined["field"].isin(TRIAGE_FIELDS)]
    return d.reset_index(drop=True)


def fdic_date_lag(joined: pd.DataFrame) -> pd.DataFrame:
    """FDIC file-id date minus printed cover date, in days, bucketed (answered rows only)."""
    f = joined[(joined["field"] == "exam_date") & (joined["regulator"] == "FDIC") & joined["answered"]].copy()
    lag = (pd.to_datetime(f["index_value"]) - pd.to_datetime(f["rule_value"])).dt.days
    first = pd.to_datetime(f["index_value"]).dt.day == 1
    rows = [{"bucket": "0 (same date)", "n": int((lag == 0).sum()), "file_date_first_of_month": int((first & (lag == 0)).sum())}]
    for lo, hi, label in LAG_BUCKETS:
        sel = (lag >= lo) & (lag <= hi)
        rows.append({"bucket": label, "n": int(sel.sum()), "file_date_first_of_month": int((first & sel).sum())})
    differ = lag[lag != 0]
    rows.append({"bucket": "total", "n": int(len(lag)), "file_date_first_of_month": int(first.sum())})
    out = pd.DataFrame(rows)
    out["share"] = (out["n"] / max(1, len(lag))).round(4)
    out.attrs["median_lag_days"] = float(differ.median()) if len(differ) else 0.0
    return out


# --------------------------------------------------------------------------- triage


def load_hand_triage(path: Path = TRIAGE_PATH) -> dict[tuple[str, str], dict[str, Any]]:
    """Hand entries keyed by (doc_id, field)."""
    with open(path, encoding="utf-8") as fh:
        entries = (yaml.safe_load(fh) or {}).get("entries", [])
    out = {}
    for e in entries:
        if e["category"] not in CATEGORIES:
            raise ValueError(f"{e['doc_id']} {e['field']}: unknown category {e['category']!r}")
        out[(e["doc_id"], e["field"])] = e
    return out


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _straight(text: str) -> str:
    return _ws(text).translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'}))


def quote_on_page(doc_id: str, quote: str, page: int | None, text_dir: Path = TEXT_DIR) -> bool | None:
    """True when the quote is on the page (or anywhere, page unknown); None without page text."""
    path = text_dir / f"{doc_id}.pages.json"
    if not quote or not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        pages = json.load(fh)
    texts = [p["text"] for p in pages]
    if page:
        texts = texts[int(page) - 1 : int(page)]
    needle = _straight(quote)
    return any(needle in _straight(t) for t in texts)


def auto_triage(row: pd.Series, after: pd.Series | None) -> dict[str, Any] | None:
    """Category for a disagreement without a hand entry, or None when it needs one."""
    if after is not None and bool(after["agree"]) and row["version"] != "after":
        return {
            "category": "our_error", "error_side": "rules", "correct_value": after["rule_value"],
            "evidence_quote": after["quote"], "page": after["page"], "note": "fixed in the later version",
        }
    if row["field"] == "exam_date" and row["regulator"] == "FDIC":
        try:
            lag = (date.fromisoformat(row["index_value"]) - date.fromisoformat(row["rule_value"])).days
        except ValueError:
            return None
        note = f"FDIC file-id date {row['index_value']} ({row['index_source_file_id']}) is {lag} days after the printed date"
        if isinstance(row.get("notes"), str) and row["notes"]:
            note += f"; {row['notes']}"
        return {
            "category": "definitional", "error_side": "none", "correct_value": row["rule_value"],
            "evidence_quote": row["quote"], "page": row["page"], "note": note,
        }
    return None


def triage(before: pd.DataFrame | None, after: pd.DataFrame, hand: dict[tuple[str, str], dict[str, Any]],
           text_dir: Path = TEXT_DIR) -> pd.DataFrame:
    """Every disagreement of either version, with category and evidence."""
    after_by = after.set_index(["doc_id", "field"])
    frames = []
    if before is not None:
        frames.append(disagreements(before).assign(version="before"))
    frames.append(disagreements(after).assign(version="after"))
    dis = pd.concat(frames, ignore_index=True).drop_duplicates(["doc_id", "field"], keep="first")
    rows = []
    for _, row in dis.iterrows():
        key = (row["doc_id"], row["field"])
        a = after_by.loc[key] if key in after_by.index else None
        entry = hand.get(key)
        source = "hand"
        if entry is None:
            entry = auto_triage(row, a)
            source = "auto"
        rec = {
            "doc_id": row["doc_id"], "regulator": row["regulator"], "field": row["field"],
            "rule_value": row["rule_value"], "index_value": row["index_value"],
            "category": entry["category"] if entry else "untriaged",
            "error_side": entry.get("error_side", "") if entry else "",
            "correct_value": str(entry.get("correct_value", "")) if entry else "",
            "evidence_quote": _ws(entry.get("evidence_quote") or row["quote"]) if entry else _ws(row["quote"]),
            "page": (entry.get("page") or row["page"]) if entry else row["page"],
            "note": _ws(entry.get("note", "")) if entry else "",
            "triage": source if entry else "",
            "flagged_in": row["version"],
            "status_after": a["status"] if a is not None else "",
            "value_after": a["rule_value"] if a is not None else "",
            "agree_after": bool(a["agree"]) if a is not None else False,
        }
        page = rec["page"]
        rec["page"] = int(float(page)) if str(page) not in ("", "nan", "None") else ""
        rec["quote_found"] = quote_on_page(rec["doc_id"], rec["evidence_quote"], rec["page"] or None, text_dir)
        rows.append(rec)
    return pd.DataFrame(rows)


def triage_summary(tri: pd.DataFrame) -> pd.DataFrame:
    """Counts per field, regulator and category (rows for every regulator, zeros kept)."""
    out = []
    for field in TRIAGE_FIELDS:
        for reg in REGULATORS:
            s = tri[(tri["field"] == field) & (tri["regulator"] == reg)]
            rec = {"field": field, "regulator": reg}
            for cat in (*CATEGORIES, "untriaged"):
                rec[cat] = int((s["category"] == cat).sum())
            rec["total"] = len(s)
            out.append(rec)
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- abstentions

# The section each field's rule reads first (exam_method also reads the rating page).
NEEDED_SECTION = {
    "bank_id": "cover",
    "exam_date": "cover",
    "exam_method": "scope_of_evaluation",
    "overall_rating": "rating",
}
TOC_HIT_CHARS = 400  # a located section shorter than this is a table-of-contents line


def _fixed_cause(field: str, after: pd.Series) -> str:
    """Sub-cause of an abstention the later rules answer, from the value's note and quote."""
    notes, quote = str(after.get("notes") or ""), str(after.get("quote") or "")
    if field == "bank_id":
        if "second cover" in notes:
            return "cover_layout: second cover page (FRB Richmond)"
        if re.fullmatch(r"#?\s*\d+", quote.strip()):
            return "cover_layout: bare id line under the name"
        return "unseen_phrasing: id label"
    if field == "exam_date":
        if "scope section" in notes and "no date on the cover" in notes:
            return "cover_layout: no date on the cover, scope date used"
        return "unseen_phrasing: cover date format"
    if field == "exam_method":
        if "inferred" in notes:
            return f"unseen_phrasing: {notes}"
        return "unseen_phrasing: scope procedures sentence"
    return "unseen_phrasing: institution rating sentence"


def classify_abstention(field: str, doc_id: str, regulator: str, after: pd.Series | None,
                        text_dir: Path = TEXT_DIR) -> str:
    """Why the earlier rules abstained: a rule gap the later rules fix, or a locator miss."""
    if after is not None and after["status"] == "value":
        return "rule_gap_fixed; " + _fixed_cause(field, after)
    path = text_dir / f"{doc_id}.pages.json"
    if not path.exists():
        return "no_text"
    from cra_extract.locate import load_pages_json, locate_all

    pages = load_pages_json(path)
    first = pages[0].text if pages else ""
    sections, _ = locate_all(doc_id, pages, regulator)
    sec = sections.get(NEEDED_SECTION[field])
    if sec is None:
        if first and first.count(" ") > 0.01 * len(first):
            return "locator_miss: no-break spaces in the text layer, no heading matched"
        return f"locator_miss: {NEEDED_SECTION[field]} not found"
    if sec.char_end - sec.char_start < TOC_HIT_CHARS:
        return f"locator_miss: {NEEDED_SECTION[field]} located on a table-of-contents line"
    return "unresolved: section located, no pattern matched"


def abstention_sample(before: pd.DataFrame, after: pd.DataFrame, n: int, seed: int,
                      text_dir: Path = TEXT_DIR) -> pd.DataFrame:
    """``n`` abstentions per triaged field of the earlier version, with cause and later value."""
    after_by = after.set_index(["doc_id", "field"])
    rows = []
    for field in TRIAGE_FIELDS:
        pool = before[(before["field"] == field) & ~before["answered"]]
        for _, row in pool.sample(n=min(n, len(pool)), random_state=seed).iterrows():
            key = (row["doc_id"], field)
            a = after_by.loc[key] if key in after_by.index else None
            rows.append({
                "field": field, "doc_id": row["doc_id"], "regulator": row["regulator"],
                "index_value": row["index_value"],
                "cause": classify_abstention(field, row["doc_id"], row["regulator"], a, text_dir),
                "status_after": a["status"] if a is not None else "",
                "value_after": a["rule_value"] if a is not None else "",
                "agree_after": bool(a["agree"]) if a is not None else False,
                "quote_after": _ws(a["quote"]) if a is not None else "",
                "page_after": a["page"] if a is not None else "",
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- CLI


def _read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _version(rules: pd.DataFrame, default: str) -> str:
    v = rules["rules_version"].iloc[0] if "rules_version" in rules and len(rules) else ""
    return v or default


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m cra_extract.group_a --rules results/rules.csv --before results/rules_0.1.0.csv``."""
    ap = argparse.ArgumentParser(description="Group A rules versus the agency index, corpus-wide (design 6.3).")
    ap.add_argument("--rules", type=Path, default=RESULTS_DIR / "rules.csv")
    ap.add_argument("--before", type=Path, help="an earlier rules output to compare and triage")
    ap.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    ap.add_argument("--triage", type=Path, default=TRIAGE_PATH)
    ap.add_argument("--text-dir", type=Path, default=TEXT_DIR)
    ap.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    ap.add_argument("--abstention-sample", type=int, default=30,
                    help="abstentions of the --before version sampled per field (0 to skip)")
    ap.add_argument("--seed", type=int, default=20261008)
    args = ap.parse_args(argv)

    manifest = _read(args.manifest)
    after_rules = _read(args.rules)
    after = join(after_rules, manifest)
    tables = [agreement_table(after, _version(after_rules, "after"))]
    before = None
    if args.before:
        before_rules = _read(args.before)
        before = join(before_rules, manifest)
        tables.insert(0, agreement_table(before, _version(before_rules, "before")))
    agreement = pd.concat(tables, ignore_index=True)
    tri = triage(before, after, load_hand_triage(args.triage), args.text_dir)
    summary = triage_summary(tri)
    lag = fdic_date_lag(after)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    agreement.to_csv(args.out_dir / "group_a_agreement.csv", index=False)
    tri.to_csv(args.out_dir / "group_a_triage.csv", index=False)
    summary.to_csv(args.out_dir / "group_a_triage_summary.csv", index=False)
    lag.to_csv(args.out_dir / "fdic_exam_date_lag.csv", index=False)
    if before is not None and args.abstention_sample > 0:
        sample = abstention_sample(before, after, args.abstention_sample, args.seed, args.text_dir)
        sample.to_csv(args.out_dir / "group_a_abstention_sample.csv", index=False)
        causes = sample.assign(cause=sample["cause"].str.split(";").str[0])
        print(causes.groupby(["field", "cause"]).size().to_string())
        print()

    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(agreement.to_string(index=False))
        print()
        print(summary.to_string(index=False))
        print()
        print(lag.to_string(index=False))
        print(f"median FDIC lag where the dates differ: {lag.attrs['median_lag_days']:.0f} days")
    missing_quotes = tri[tri["quote_found"] == False]  # noqa: E712 (None means no page text)
    if len(missing_quotes):
        print(f"{len(missing_quotes)} evidence quotes not found on their page:", file=sys.stderr)
        print(missing_quotes[["doc_id", "field", "page", "evidence_quote"]].to_string(index=False), file=sys.stderr)
    untriaged = tri[tri["category"] == "untriaged"]
    if len(untriaged):
        print(f"{len(untriaged)} disagreements need a hand entry in {args.triage}:", file=sys.stderr)
        print(untriaged[["doc_id", "field", "rule_value", "index_value"]].to_string(index=False), file=sys.stderr)
        return 1
    return 1 if len(missing_quotes) else 0


if __name__ == "__main__":
    sys.exit(main())
