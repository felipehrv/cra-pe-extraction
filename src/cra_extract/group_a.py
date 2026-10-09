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

Shared index comparison. :func:`index_truth` turns the manifest into one truth row per
(document, Group A field or component rating) and :func:`compare_with_index` scores any
system's rows against it with ``evaluate.score_cell`` and the field's tolerance
(``evaluate.values_match``). :func:`join` uses the same tolerance for its ``agree`` column,
and ``cra_extract.routing`` (S5 routing) and ``cra_extract.corpus_scoring`` (Group A at
corpus scale, risk validation) build on these helpers, so one comparison serves all three.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from cra_extract.config import (
    CONFIG_DIR,
    CORPUS_PATH,
    GOLD_DIR,
    OVERRIDES_PATH,
    RESULTS_DIR,
    TEXT_DIR,
    applicability,
    field_spec,
)
from cra_extract.evaluate import score_cell, values_match
from cra_extract.models import FieldValue, LabelRow
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


def index_agrees(field: str, index_value: Any, value: Any) -> bool:
    """True when ``value`` matches the index value under the field's tolerance
    (``evaluate.values_match``; an empty string on either side never matches)."""
    i = None if index_value in ("", None) else index_value
    v = None if value in ("", None) else value
    return values_match(i, v, field_spec(field))


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
    out["agree"] = [
        bool(a) and index_agrees(f, i, r)
        for a, f, i, r in zip(out["answered"], out["field"], out["index_value"], out["rule_value"], strict=True)
    ]
    return out


# --------------------------------------------------------------------------- index truth

# Group A field (and component rating) -> manifest column that carries the index value.
INDEX_TRUTH_COLUMNS: dict[str, str] = {
    "regulator": "regulator",
    "bank_name": "bank_name",
    "bank_id": "bank_id",
    "exam_date": "exam_date",
    "exam_method": "method",
    "overall_rating": "overall_rating",
    "prior_evaluation_date": "prior_evaluation_date_index",
    "prior_rating": "prior_rating_index",
    "lending_test_rating": "lending_test_rating",
    "cd_test_rating": "cd_test_rating",
}
COMPONENT_FIELDS = ("lending_test_rating", "cd_test_rating")
# The only (regulator, method) cell whose index carries the component ratings (FRB CSV ISB main row).
COMPONENT_INDEX_CELL = ("FRB", "isb")
PRIOR_FIELDS = ("prior_evaluation_date", "prior_rating")
# Optional manifest column naming where exam_date came from; these sources are not truth.
EXAM_DATE_SOURCE_COLUMN = "exam_date_source"
FILE_ID_DATE_SOURCES = frozenset({"file_id", "fdic_file_id"})
TRUTH_COLUMNS = [
    "doc_id", "regulator", "method", "release_year", "split", "field", "index_value", "truth", "truth_reason",
]


def _truth_of(
    field: str, value: str, rec: dict[str, Any], columns: set[str], fdic_exam_date_truth: bool = False
) -> tuple[str, str, str]:
    """(truth, reason, index value) for one manifest row and field. ``truth`` is ``value``,
    a gold null code (``absent_from_document`` or ``not_applicable``, from the applicability
    block of ``fields.yaml``) or ``none`` (no index truth)."""
    reg, method = rec.get("regulator", ""), rec.get("method", "")
    if field in COMPONENT_FIELDS:
        app = applicability(field, reg, method)
        if app != "value":
            return app, f"applicability {reg}/{method}", ""
        if (reg, method) != COMPONENT_INDEX_CELL:
            return "none", "component rating not in the index for this cell", ""
    if field == "exam_date":
        source = str(rec.get(EXAM_DATE_SOURCE_COLUMN) or "").strip().casefold()
        if reg == "FDIC" and not fdic_exam_date_truth:
            # Plan, Group A bullet 3: the FDIC file-id date is truth only after the gate
            # (fdic_exam_date_gate) passes; until then no FDIC row has exam_date truth, an
            # override row included, so every FDIC cell is treated alike.
            return "none", f"FDIC exam_date: file-id gate not passed (exam_date_source {source or 'empty'})", ""
        if reg != "FDIC" and source in FILE_ID_DATE_SOURCES:
            return "none", f"exam_date_source {source}", ""
    if INDEX_TRUTH_COLUMNS[field] not in columns:
        return "none", f"manifest has no {INDEX_TRUTH_COLUMNS[field]} column", ""
    if not value:
        reason = "no previous index row" if field in PRIOR_FIELDS else "no index value"
        return "none", reason, ""
    return "value", "index", value


def index_truth(
    manifest: pd.DataFrame,
    fields: list[str] | tuple[str, ...] | None = None,
    fdic_exam_date_truth: bool = False,
) -> pd.DataFrame:
    """One row per (manifest document, field) with the index truth (``TRUTH_COLUMNS``).

    Rules (``docs/analysis_plan.md``, "Field groups and truth sources" and "Group A at
    corpus scale"): the agency index value of the manifest; prior fields only where a
    previous index row exists; component ratings from the FRB CSV main row in FRB/isb and
    a gold-null code in the applicability ABS cells (a returned value there is a
    fabrication), no truth in the other cells. FDIC exam_date is the file-id date (or a
    hand override, ``exam_date_source`` override) and is truth only when
    ``fdic_exam_date_truth`` is True, which the caller sets from :func:`fdic_exam_date_gate`
    (the plan uses the file-id date only after it matches the printed cover date on every
    FDIC dev and test gold document). Without the gate no FDIC row has exam_date truth.
    """
    fields = list(fields or INDEX_TRUTH_COLUMNS)
    unknown = [f for f in fields if f not in INDEX_TRUTH_COLUMNS]
    if unknown:
        raise ValueError(f"no index truth for fields {unknown}")
    m = manifest.fillna("").astype(str)
    columns = set(m.columns)
    rows = []
    for rec in m.to_dict("records"):
        year = str(rec.get("release_date", ""))[:4]
        for field in fields:
            raw = str(rec.get(INDEX_TRUTH_COLUMNS[field], "")).strip()
            truth, reason, value = _truth_of(field, raw, rec, columns, fdic_exam_date_truth)
            rows.append({
                "doc_id": rec["doc_id"], "regulator": rec.get("regulator", ""), "method": rec.get("method", ""),
                "release_year": year, "split": rec.get("split", ""), "field": field,
                "index_value": value, "truth": truth, "truth_reason": reason,
            })
    return pd.DataFrame(rows, columns=TRUTH_COLUMNS)


GATE_COLUMNS = ["doc_id", "split", "manifest_exam_date", "gold_exam_date", "match"]
GATE_SPLITS = ("dev", "test")


def fdic_exam_date_gate(
    manifest: pd.DataFrame, gold_dir: Path = GOLD_DIR
) -> tuple[str, pd.DataFrame]:
    """The plan's condition for FDIC exam_date index truth (Group A at corpus scale, bullet 3).

    Compares the manifest exam_date of every FDIC document of the dev and test splits with
    the gold exam_date of ``labels_{split}.csv`` (``evaluate.values_match``). Returns the
    status and one row per document (``GATE_COLUMNS``): ``pass`` when both splits are drawn
    and labeled, every FDIC gold document has a gold exam_date and every one matches;
    ``fail`` when any labeled document differs (the mismatch is triaged and recorded before
    corpus scoring); ``pending`` otherwise (test not drawn or not labeled yet). Only the
    exam_date rows of FDIC gold documents are read.
    """
    m = manifest.fillna("").astype(str)
    rows: list[dict[str, Any]] = []
    complete = True
    for split in GATE_SPLITS:
        docs = m[(m["regulator"] == "FDIC") & (m["split"] == split)]
        path = Path(gold_dir) / f"labels_{split}.csv"
        if docs.empty or not path.exists():
            complete = False
            continue
        gold = pd.read_csv(path, dtype=str, keep_default_na=False)
        gold = gold[(gold["field"] == "exam_date") & gold["doc_id"].isin(set(docs["doc_id"]))]
        by_doc = dict(zip(gold["doc_id"], gold["value"], strict=True))
        for rec in docs.to_dict("records"):
            g = by_doc.get(rec["doc_id"], "")
            if not g:
                complete = False
            match = bool(g) and values_match(g, rec["exam_date"], field_spec("exam_date"))
            rows.append({"doc_id": rec["doc_id"], "split": split, "manifest_exam_date": rec["exam_date"],
                         "gold_exam_date": g, "match": match})
    table = pd.DataFrame(rows, columns=GATE_COLUMNS)
    if any(not r["match"] and r["gold_exam_date"] for r in rows):
        return "fail", table
    return ("pass" if complete and rows else "pending"), table


def apply_triage(truth: pd.DataFrame, triage_rows: pd.DataFrame) -> pd.DataFrame:
    """Triaged truth (secondary): ``source_error`` rows take the printed value as truth
    (the triaged ``rule_value``, which the hand triage read on the page), ``definitional``
    rows lose their truth, ``our_error`` rows keep the index value. Only rows that have
    index truth are changed, so a field without index truth (FDIC exam_date) stays
    without it. The triage covers the S1 (rules) disagreements of ``TRIAGE_FIELDS``, so
    the triaged truth is S1-triaged truth. The input index is ignored (rows are matched
    by position after a reset)."""
    out = truth.reset_index(drop=True).copy()
    if triage_rows is None or triage_rows.empty:
        return out
    t = triage_rows.drop_duplicates(["doc_id", "field"], keep="last").set_index(["doc_id", "field"])
    keys = list(zip(out["doc_id"], out["field"], strict=True))
    for i, key in enumerate(keys):
        if key not in t.index or out.at[i, "truth"] != "value":
            continue
        entry = t.loc[key]
        if entry["category"] == "source_error":
            out.at[i, "index_value"] = str(entry["rule_value"])
            out.at[i, "truth_reason"] = "triage: source_error, printed value"
        elif entry["category"] == "definitional":
            out.at[i, "index_value"] = ""
            out.at[i, "truth"] = "none"
            out.at[i, "truth_reason"] = "triage: definitional"
    return out


@functools.cache
def index_cell(field: str, truth: str, index_value: str, status: str, value: str) -> str:
    """Scoring-matrix cell of one system value against the index truth
    (``evaluate.score_cell`` with the index as the reference); ``no_truth`` without truth."""
    if truth == "none":
        return "no_truth"
    ref = LabelRow(doc_id="", field=field, value=index_value if truth == "value" else None,
                   null_reason=None if truth == "value" else truth)
    pred = FieldValue(doc_id="", field=field, system="", status=status, value=value or None)
    return score_cell(ref, pred, field_spec(field))


def compare_with_index(values: pd.DataFrame, truth: pd.DataFrame, systems: list[str] | None = None) -> pd.DataFrame:
    """Every truth row crossed with each system: the system's status and value (a missing
    row is an abstention, as in ``evaluate``) and the scoring-matrix ``cell``.

    ``values`` needs ``doc_id``, ``field``, ``system``, ``status`` and ``value``; extra
    columns are ignored. Duplicate (doc_id, field, system) rows keep the last one.
    """
    cols = ["doc_id", "field", "system", "status", "value"]
    v = values[values["field"].isin(set(truth["field"]))][cols].fillna("").astype(str)
    systems = sorted(v["system"].unique()) if systems is None else list(systems)
    frames = []
    for system in systems:
        s = v[v["system"] == system].drop_duplicates(["doc_id", "field"], keep="last").drop(columns="system")
        j = truth.merge(s, on=["doc_id", "field"], how="left")
        j["system"] = system
        j["status"] = j["status"].fillna("abstain").replace("", "abstain")
        j["value"] = j["value"].fillna("")
        j["cell"] = [
            index_cell(f, t, str(i), st, str(val))
            for f, t, i, st, val in zip(j["field"], j["truth"], j["index_value"], j["status"], j["value"], strict=True)
        ]
        frames.append(j)
    if not frames:
        return pd.DataFrame(columns=[*TRUTH_COLUMNS, "status", "value", "system", "cell"])
    return pd.concat(frames, ignore_index=True)


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


def manifest_aliases(path: Path = OVERRIDES_PATH) -> dict[str, str]:
    """Original doc_id -> renamed doc_id from ``config/manifest_overrides.yaml`` (empty
    when the file does not exist)."""
    if not Path(path).exists():
        return {}
    from cra_extract.overrides import load_overrides

    return load_overrides(path).aliases()


def load_hand_triage(
    path: Path = TRIAGE_PATH, aliases: dict[str, str] | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """Hand entries keyed by (doc_id, field); a doc_id renamed by the manifest overrides
    (``aliases``, original -> current) is keyed by its current id."""
    aliases = aliases or {}
    with open(path, encoding="utf-8") as fh:
        entries = (yaml.safe_load(fh) or {}).get("entries", [])
    out = {}
    for e in entries:
        if e["category"] not in CATEGORIES:
            raise ValueError(f"{e['doc_id']} {e['field']}: unknown category {e['category']!r}")
        doc_id = aliases.get(e["doc_id"], e["doc_id"])
        out[(doc_id, e["field"])] = {**e, "doc_id": doc_id}
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
    tri = triage(before, after, load_hand_triage(args.triage, manifest_aliases()), args.text_dir)
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
