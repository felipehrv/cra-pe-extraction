"""Reliability of the gold labels (plan "Reliability", protocol of 2026-10-14).

Gold labels are read by two independent model readers (A and B) and every disagreement goes
to a third reader. The measure is agreement between A and B before adjudication, per field,
over all gold documents: :func:`agreement` (CLI ``agreement``, ``make reliability``).

Inputs (``data/gold``):

- ``dev_reads/{doc_id}.a.csv`` and ``.b.csv``, ``test_reads/{doc_id}.a.csv`` and ``.b.csv``:
  one row per field (doc_id, field, value, null_reason, evidence_page, quote, notes);
- ``census_reads/batch{N}.a.csv`` and ``.b.csv``: one row per census section with the Group C
  columns (finding_present ... distractor_laws_named) plus evidence_page, quote and notes;
- ``dev_disputes.csv`` and ``test_disputes.csv`` (doc_id, field, ...) and
  ``census_disputes.csv`` (doc_id, fields joined with ``|``): the items sent to adjudication.

An item is a (document or section, field) pair read by both readers. Values are compared in
canonical form (:func:`canonical_value`): a gold null code, or ``NA`` / ``ABS`` in the value
cell, is a category of its own (``null:not_applicable``, ``null:absent_from_document``); sets
are sorted element lists; booleans are true/false; dates are ISO dates; numbers are parsed;
any other text is casefolded with whitespace collapsed. Only ``doc_id``, ``field``, ``value``
and ``null_reason`` (and the Group C value columns) are read; quotes and notes are not.

Per split (dev, test, census) and field: ``n`` items, ``agree`` items coded alike, ``pct``
(percent agreement, 0 to 100) with a Wilson 95 percent interval (``stats.wilson_ci``), and
Cohen's kappa (``stats.cohen_kappa``) when each reader uses at least two categories on the
field; otherwise kappa is blank and ``note`` gives the majority class and its prevalence. Set
fields also get an element-wise comparison in ``note`` (one binary item per item and element).
``note`` gives the items sent to adjudication and any mismatch between the disagreements and
the dispute list. Pooled rows (``field`` = ``pooled``) sum ``n`` and ``agree`` over the fields
of a split and carry the adjudication rate (disputed items / items); ``split`` = ``all`` rows
pool the three splits, per field and overall. Outputs: ``results/reliability.csv``
(:data:`AGREEMENT_COLUMNS`) and ``results/reliability.md`` (the table and one sentence per
split).

Withdrawn branches (kept for the record, not run): the design 5.4 check by a second blind
coding, ``outside`` (an outside coder double-codes 8 test documents) or ``fallback`` (the
author re-codes 6 dev documents), over ``RELIABILITY_FIELDS``. :func:`draw`,
:func:`build_sheet` and :func:`compare` implement it; ``compare`` now writes
``results/reliability_second_coding.csv`` so that it cannot overwrite the agreement table.

CLI::

    python -m cra_extract.reliability agreement [--out results/reliability.csv] [--gold-dir data/gold]
    python -m cra_extract.reliability draw --branch outside|fallback --seed SEED --date YYYY-MM-DD
    python -m cra_extract.reliability sheet [--out data/gold/reliability_sheet.csv]
    python -m cra_extract.reliability compare [--sheet data/gold/reliability_sheet.csv]
"""

from __future__ import annotations

import argparse
import hashlib
import math
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from cra_extract import grounding, label
from cra_extract.config import (
    CONFIG_DIR,
    CORPUS_PATH,
    GOLD_DIR,
    RESULTS_DIR,
    field_names,
    field_spec,
)
from cra_extract.models import DocRecord
from cra_extract.stats import cohen_kappa, wilson_ci
from cra_extract.strata import SAMPLE_DOC_IDS

RELIABILITY_FIELDS = (
    "n_assessment_areas",
    "n_full_scope",
    "loan_products_evaluated",
    "lending_data_period_start",
    "lending_data_period_end",
)
BRANCHES = {"outside": ("test", 8), "fallback": ("dev", 6)}
N_OUTSIDE = BRANCHES["outside"][1]
N_FALLBACK = BRANCHES["fallback"][1]
FALLBACK_EARLIEST = date(2026, 10, 13)
SAMPLE_PATH = CONFIG_DIR / "reliability_sample.yaml"
SHEET_PATH = GOLD_DIR / "reliability_sheet.csv"
OUT_PATH = RESULTS_DIR / "reliability.csv"
SECOND_CODING_OUT_PATH = RESULTS_DIR / "reliability_second_coding.csv"
COLUMNS = [
    "field", "branch", "n", "n_agree", "percent_agreement", "statistic", "kappa",
    "majority_class", "majority_prevalence", "n_elements", "n_elements_agree", "kappa_elements",
    "minutes_first_mean", "minutes_second_mean",
]


class ReliabilityError(ValueError):
    """The reliability inputs are missing or inconsistent with the recorded draw."""


def ids_hash(doc_ids: Iterable[str]) -> str:
    """sha256 of the sorted doc_id list, one id per line (``label.split_hash`` convention)."""
    ids = sorted(doc_ids)
    return hashlib.sha256(("\n".join(ids) + "\n").encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- draw


def pool(manifest: Sequence[DocRecord], branch: str) -> list[str]:
    """Sorted doc_ids a draw of ``branch`` may use: test documents other than the fixture
    samples (``outside``), or every dev document (``fallback``)."""
    if branch not in BRANCHES:
        raise ReliabilityError(f"unknown branch {branch!r}; expected one of {sorted(BRANCHES)}")
    split = BRANCHES[branch][0]
    ids = [d.doc_id for d in manifest if d.split == split]
    if branch == "outside":
        ids = [d for d in ids if d not in set(SAMPLE_DOC_IDS)]
    return sorted(ids)


def draw(manifest: Sequence[DocRecord], branch: str, seed: int, drawn_at: str, n: int | None = None) -> dict[str, Any]:
    """Simple random sample without replacement of the branch's documents (8 test or 6 dev
    by default), ``numpy.random.default_rng(seed).choice`` over :func:`pool`."""
    date.fromisoformat(drawn_at)
    n = BRANCHES[branch][1] if n is None and branch in BRANCHES else n
    ids = pool(manifest, branch)
    if n is None or n < 1:
        raise ReliabilityError("n must be at least 1")
    if len(ids) < n:
        raise ReliabilityError(f"{branch}: {n} documents wanted, {len(ids)} in the pool")
    picks = np.random.default_rng(seed).choice(len(ids), size=n, replace=False)
    doc_ids = sorted(ids[int(i)] for i in picks)
    return {
        "branch": branch,
        "split": BRANCHES[branch][0],
        "seed": int(seed),
        "drawn_at": drawn_at,
        "n": n,
        "fields": list(RELIABILITY_FIELDS),
        "pool_rule": (
            "test documents other than the eight fixture samples" if branch == "outside"
            else "dev documents (author re-codes blind, intra-rater stability)"
        ) + "; sorted by doc_id; numpy default_rng(seed).choice without replacement",
        "pool": len(ids),
        "pool_doc_id_hash": ids_hash(ids),
        "doc_ids": doc_ids,
        "doc_id_hash": ids_hash(doc_ids),
    }


def load_record(path: Path = SAMPLE_PATH) -> dict[str, Any] | None:
    """The recorded draw, or None before ``draw``."""
    if not Path(path).exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or None


def save_record(record: Mapping[str, Any], path: Path = SAMPLE_PATH, force: bool = False) -> None:
    """Write the draw record; an existing record is replaced only with ``force``."""
    path = Path(path)
    if path.exists() and not force:
        raise ReliabilityError(f"{path} already holds a draw; pass force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Written by python -m cra_extract.reliability; do not edit by hand.\n")
        yaml.safe_dump(dict(record), fh, sort_keys=False, allow_unicode=True, width=100)


# ----------------------------------------------------------------------------- sheet


def build_sheet(manifest: Sequence[DocRecord], record: Mapping[str, Any]) -> list[dict[str, str]]:
    """Blind second-coding rows: ``label.init_sheet`` rows of the drawn documents and the
    reliability fields (no first-coding value, no system output)."""
    wanted = set(record["doc_ids"])
    split = str(record["split"])
    docs = [d for d in manifest if d.doc_id in wanted]
    missing = sorted(wanted - {d.doc_id for d in docs})
    if missing:
        raise ReliabilityError(f"drawn documents not in the manifest: {missing}")
    wrong = sorted(d.doc_id for d in docs if d.split != split)
    if wrong:
        raise ReliabilityError(f"drawn documents no longer in split {split}: {wrong}")
    rows = label.init_sheet(list(docs), split)
    return [r for r in rows if r["field"] in RELIABILITY_FIELDS]


# ----------------------------------------------------------------------------- compare


NULL_CODES = ("not_applicable", "absent_from_document")
NULL_ALIASES = {"na": "not_applicable", "abs": "absent_from_document"}


def _field_type(field: str) -> str | None:
    try:
        return field_spec(field).get("type")
    except KeyError:
        return None


def _null_category(text: str) -> str | None:
    """``null:<code>`` when ``text`` is a gold null code or its short form NA / ABS."""
    key = text.strip().casefold()
    code = NULL_ALIASES.get(key, key)
    return f"null:{code}" if code in NULL_CODES else None


def canonical_value(field: str, value: str | None, null_reason: str | None = "") -> str:
    """Canonical category of one cell: ``null:<code>`` for a gold null (or NA / ABS in the
    value cell), sorted casefolded elements for a set, ``true``/``false`` for a boolean, the
    ISO date for a date, the number for an integer or float, and otherwise the casefolded text
    with whitespace collapsed; empty for an uncoded cell."""
    null = (null_reason or "").strip()
    if null:
        return _null_category(null) or f"null:{' '.join(null.split()).casefold()}"
    raw = (value or "").strip()
    if not raw:
        return ""
    as_null = _null_category(raw)
    if as_null:
        return as_null
    kind = _field_type(field)
    if kind == "set":
        elements = {" ".join(e.split()).casefold() for e in grounding.parse_set(raw)}
        return label.SET_SEPARATOR.join(sorted(e for e in elements if e))
    if kind == "bool":
        b = grounding.parse_bool(raw)
        if b is not None:
            return "true" if b else "false"
    if kind == "date":
        d = grounding.parse_date(raw)
        if d:
            return d.isoformat()
    if kind in ("int", "float"):
        try:
            x = float(raw.replace(",", "").replace("$", "").replace("%", ""))
        except ValueError:
            pass
        else:
            return str(int(x)) if x.is_integer() else str(x)
    return " ".join(raw.split()).casefold()


def canonical(field: str, row: Mapping[str, str]) -> str:
    """:func:`canonical_value` of a sheet row (``value`` and ``null_reason`` cells)."""
    return canonical_value(field, row.get("value"), row.get("null_reason"))


def _minutes(rows: Iterable[Mapping[str, str]]) -> float:
    vals = []
    for r in rows:
        try:
            vals.append(float(r.get("minutes", "")))
        except (TypeError, ValueError):
            continue
    return float(np.mean(vals)) if vals else math.nan


def compare(
    first: Sequence[Mapping[str, str]], second: Sequence[Mapping[str, str]], branch: str = ""
) -> pd.DataFrame:
    """Agreement per reliability field over the documents coded in both sheets (``COLUMNS``)."""
    a = {(r["doc_id"], r["field"]): r for r in first if r.get("field") in RELIABILITY_FIELDS}
    b = {(r["doc_id"], r["field"]): r for r in second if r.get("field") in RELIABILITY_FIELDS}
    out = []
    for field in RELIABILITY_FIELDS:
        keys = sorted(k for k in set(a) & set(b) if k[1] == field
                      and canonical(field, a[k]) and canonical(field, b[k]))
        xa = [canonical(field, a[k]) for k in keys]
        xb = [canonical(field, b[k]) for k in keys]
        n = len(keys)
        agree = sum(x == y for x, y in zip(xa, xb, strict=True))
        both = len(set(xa)) >= 2 and len(set(xb)) >= 2
        pooled = Counter(xa + xb)
        majority, count = pooled.most_common(1)[0] if pooled else ("", 0)
        row: dict[str, Any] = {
            "field": field, "branch": branch, "n": n, "n_agree": agree,
            "percent_agreement": agree / n if n else math.nan,
            "statistic": "kappa" if both else "percent_agreement",
            "kappa": cohen_kappa(xa, xb) if both else math.nan,
            "majority_class": majority, "majority_prevalence": count / (2 * n) if n else math.nan,
            "n_elements": math.nan, "n_elements_agree": math.nan, "kappa_elements": math.nan,
            "minutes_first_mean": _minutes(a[k] for k in keys),
            "minutes_second_mean": _minutes(b[k] for k in keys),
        }
        spec = field_spec(field)
        if spec.get("type") == "set":
            ea, eb = [], []
            for x, y in zip(xa, xb, strict=True):
                sa = set() if x.startswith("null:") else set(x.split(label.SET_SEPARATOR))
                sb = set() if y.startswith("null:") else set(y.split(label.SET_SEPARATOR))
                for e in spec["allowed"]:
                    ea.append(e.casefold() in sa)
                    eb.append(e.casefold() in sb)
            row["n_elements"] = len(ea)
            row["n_elements_agree"] = sum(x == y for x, y in zip(ea, eb, strict=True))
            if len(set(ea)) >= 2 and len(set(eb)) >= 2:
                row["kappa_elements"] = cohen_kappa(ea, eb)
        out.append(row)
    return pd.DataFrame(out, columns=COLUMNS)


def manual_review_error(table: pd.DataFrame) -> float:
    """One minus pooled agreement over the fields (the review curve's reference line)."""
    n = float(table["n"].sum())
    return math.nan if n <= 0 else 1.0 - float(table["n_agree"].sum()) / n


# ----------------------------------------------------------------------------- agreement

SPLITS = ("dev", "test", "audit", "census")
READER_SUFFIXES = ("a", "b")
AGREEMENT_COLUMNS = ["split", "field", "n", "agree", "pct", "ci_low", "ci_high", "kappa", "note"]
POOLED = "pooled"
ALL_SPLITS = "all"

Key = tuple[str, str]  # (doc_id, field); a census doc_id names one section


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.to_dict("records")


def read_document_reads(folder: Path, reader: str) -> dict[Key, str]:
    """Canonical values of one reader over ``{doc_id}.{reader}.csv`` files (one row per
    field). Only doc_id, field, value and null_reason are read."""
    out: dict[Key, str] = {}
    for path in sorted(Path(folder).glob(f"*.{reader}.csv")):
        for r in _read_csv_rows(path):
            doc_id = (r.get("doc_id") or "").strip() or path.name[: -len(f".{reader}.csv")]
            field = (r.get("field") or "").strip()
            if not field:
                continue
            key = (doc_id, field)
            if key in out:
                raise ReliabilityError(f"{path}: {doc_id} {field} read twice by reader {reader}")
            out[key] = canonical_value(field, r.get("value"), r.get("null_reason"))
    return out


def read_census_reads(folder: Path, reader: str) -> dict[Key, str]:
    """Canonical values of one reader over ``batch{N}.{reader}.csv`` files (one row per
    section, one column per Group C field)."""
    fields = field_names("C")
    out: dict[Key, str] = {}
    for path in sorted(Path(folder).glob(f"*.{reader}.csv")):
        for r in _read_csv_rows(path):
            doc_id = (r.get("doc_id") or "").strip()
            if not doc_id:
                continue
            for field in fields:
                if field not in r:
                    continue
                key = (doc_id, field)
                if key in out:
                    raise ReliabilityError(f"{path}: {doc_id} {field} read twice by reader {reader}")
                out[key] = canonical_value(field, r.get(field))
    return out


def read_disputes(path: Path) -> set[Key]:
    """Items sent to adjudication: rows (doc_id, field) or (doc_id, fields joined with ``|``)."""
    path = Path(path)
    if not path.exists():
        return set()
    out: set[Key] = set()
    for r in _read_csv_rows(path):
        doc_id = (r.get("doc_id") or "").strip()
        cell = r.get("field") if "field" in r else r.get("fields")
        for f in (cell or "").split(label.SET_SEPARATOR):
            if doc_id and f.strip():
                out.add((doc_id, f.strip()))
    return out


def load_split(split: str, gold_dir: Path = GOLD_DIR) -> tuple[dict[Key, str], dict[Key, str], set[Key]] | None:
    """Reader A values, reader B values and disputed items of one split; None when the
    split's reads folder is missing."""
    folder = Path(gold_dir) / f"{split}_reads"
    if not folder.is_dir():
        return None
    reader = read_census_reads if split == "census" else read_document_reads
    a, b = (reader(folder, x) for x in READER_SUFFIXES)
    return a, b, read_disputes(Path(gold_dir) / f"{split}_disputes.csv")


def _fmt_pct(x: float) -> str:
    return "" if x is None or math.isnan(x) else f"{100 * x:.1f}%"


def _elementwise(xa: Sequence[str], xb: Sequence[str], field: str) -> str:
    """Element-wise agreement of a set field (null = empty set) as a note fragment."""
    def elems(x: str) -> set[str]:
        return set() if not x or x.startswith("null:") else set(x.split(label.SET_SEPARATOR))
    try:
        allowed = {str(e).casefold() for e in field_spec(field).get("allowed", [])}
    except KeyError:
        allowed = set()
    universe = sorted(allowed | {e for x in (*xa, *xb) for e in elems(x)})
    ea, eb = [], []
    for x, y in zip(xa, xb, strict=True):
        sa, sb = elems(x), elems(y)
        ea += [e in sa for e in universe]
        eb += [e in sb for e in universe]
    if not ea:
        return ""
    agree = sum(p == q for p, q in zip(ea, eb, strict=True))
    text = f"element-wise {agree}/{len(ea)} agree over {len(universe)} elements"
    if len(set(ea)) >= 2 and len(set(eb)) >= 2:
        text += f", kappa {cohen_kappa(ea, eb):.3f}"
    return text


def field_row(split: str, field: str, xa: Sequence[str], xb: Sequence[str],
              keys: Sequence[tuple[str, ...]] = (), disputed: set[tuple[str, ...]] | None = None) -> dict[str, Any]:
    """One agreement row from the paired canonical values of the two readers."""
    n = len(xa)
    agree = sum(x == y for x, y in zip(xa, xb, strict=True))
    lo, hi = wilson_ci(agree, n)
    notes: list[str] = []
    kappa = math.nan
    cats_a, cats_b = len(set(xa)), len(set(xb))
    if n and cats_a >= 2 and cats_b >= 2:
        kappa = cohen_kappa(list(xa), list(xb))
    elif n:
        majority, count = Counter([*xa, *xb]).most_common(1)[0]
        notes.append(
            f"kappa not computed: reader A uses {cats_a} and reader B {cats_b} "
            f"categor{'y' if cats_b == 1 else 'ies'}; majority class "
            f"{majority!r}, prevalence {count / (2 * n):.3f}"
        )
    if _field_type(field) == "set" and n:
        notes.append(_elementwise(xa, xb, field))
    if disputed is not None and keys:
        sent = [k for k in keys if k in disputed]
        differ = {k for k, x, y in zip(keys, xa, xb, strict=True) if x != y}
        if sent:
            notes.append(f"adjudicated {len(sent)} of {n}")
        unlisted = len(differ - disputed)
        if unlisted:
            notes.append(f"{unlisted} disagreement(s) not in the dispute list")
        agreed_sent = sum(1 for k in sent if k not in differ)
        if agreed_sent:
            notes.append(f"{agreed_sent} disputed item(s) agree in canonical form")
    return {
        "split": split, "field": field, "n": n, "agree": agree,
        "pct": 100 * agree / n if n else math.nan,
        "ci_low": 100 * lo if n else math.nan, "ci_high": 100 * hi if n else math.nan,
        "kappa": kappa, "note": "; ".join(x for x in notes if x),
    }


def pooled_row(split: str, rows: Sequence[Mapping[str, Any]], n_disputed: int | None,
               extra: Sequence[str] = ()) -> dict[str, Any]:
    """Pooled agreement over field rows (sums of n and agree); the note gives the
    adjudication rate (disputed items / items)."""
    n = int(sum(r["n"] for r in rows))
    agree = int(sum(r["agree"] for r in rows))
    lo, hi = wilson_ci(agree, n)
    notes = [f"{len(rows)} fields", "kappa not pooled across fields",
             "Wilson CI treats items as independent (items cluster within documents)"]
    if n_disputed is not None and n:
        notes.append(f"adjudication rate {n_disputed}/{n} = {_fmt_pct(n_disputed / n)}")
    notes += list(extra)
    return {
        "split": split, "field": POOLED, "n": n, "agree": agree,
        "pct": 100 * agree / n if n else math.nan,
        "ci_low": 100 * lo if n else math.nan, "ci_high": 100 * hi if n else math.nan,
        "kappa": math.nan, "note": "; ".join(notes),
    }


def _field_order(fields: Iterable[str]) -> list[str]:
    known = field_names()
    present = set(fields)
    return [f for f in known if f in present] + sorted(present - set(known))


def split_rows(split: str, a: Mapping[Key, str], b: Mapping[Key, str],
               disputed: set[Key]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Field rows and the pooled row of one split, plus a summary (documents, unpaired items,
    disputed items) for the markdown sentence."""
    paired = sorted(k for k in set(a) & set(b) if a[k] and b[k])
    unpaired = len((set(a) | set(b)) - set(paired))
    rows = []
    for field in _field_order(k[1] for k in paired):
        keys = [k for k in paired if k[1] == field]
        rows.append(field_row(split, field, [a[k] for k in keys], [b[k] for k in keys], keys, disputed))
    n_disputed = len(disputed & set(paired))
    extra = []
    if unpaired:
        extra.append(f"{unpaired} item(s) read by one reader only or left blank, not compared")
    outside = len(disputed - set(paired))
    if outside:
        extra.append(f"{outside} disputed item(s) not among the compared items")
    summary = {
        "documents": len({k[0] for k in paired}), "fields": len(rows), "n_disputed": n_disputed,
        "unpaired": unpaired,
        "n_disagree": sum(1 for k in paired if a[k] != b[k]),
        "unlisted": sum(1 for k in paired if a[k] != b[k] and k not in disputed),
    }
    return rows, pooled_row(split, rows, n_disputed, extra), summary


def agreement(gold_dir: Path = GOLD_DIR, splits: Sequence[str] = SPLITS) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    """Inter-reader agreement per split and field, pooled per split, and over all splits
    (per field and pooled). Returns the table (:data:`AGREEMENT_COLUMNS`) and a summary per
    split; a split whose reads folder is missing is reported in the summary and skipped."""
    out: list[dict[str, Any]] = []
    summary: dict[str, dict[str, Any]] = {}
    all_a: dict[tuple[str, str, str], str] = {}
    all_b: dict[tuple[str, str, str], str] = {}
    all_disputed: set[tuple[str, ...]] = set()
    docs_by_split: dict[str, set[str]] = {}
    for split in splits:
        loaded = load_split(split, gold_dir)
        if loaded is None:
            summary[split] = {"missing": True}
            continue
        a, b, disputed = loaded
        rows, pooled, info = split_rows(split, a, b, disputed)
        out += rows
        out.append(pooled)
        summary[split] = {**info, "pooled": pooled}
        all_a.update({(split, *k): v for k, v in a.items()})
        all_b.update({(split, *k): v for k, v in b.items()})
        all_disputed |= {(split, *k) for k in disputed}
        docs_by_split[split] = {k[0] for k in a} | {k[0] for k in b}
    if len(docs_by_split) > 1:
        seen: Counter[str] = Counter(d for ids in docs_by_split.values() for d in ids)
        shared = sum(1 for c in seen.values() if c > 1)
        overlap = [f"{shared} document(s) read in more than one split count once per split"] if shared else []
        paired = sorted(k for k in set(all_a) & set(all_b) if all_a[k] and all_b[k])
        rows = []
        for field in _field_order(k[2] for k in paired):
            keys = [k for k in paired if k[2] == field]
            row = field_row(ALL_SPLITS, field, [all_a[k] for k in keys], [all_b[k] for k in keys],
                            keys, all_disputed)
            used = sorted({k[0] for k in keys})
            row["note"] = "; ".join(x for x in [f"splits {', '.join(used)}", row["note"]] if x)
            rows.append(row)
        out += rows
        n_disputed = len(all_disputed & set(paired))
        overall = pooled_row(ALL_SPLITS, rows, n_disputed, overlap)
        out.append(overall)
        summary[ALL_SPLITS] = {"pooled": overall, "n_disputed": n_disputed}
    return pd.DataFrame(out, columns=AGREEMENT_COLUMNS), summary


def _num(x: Any, nd: int) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return "" if math.isnan(v) else f"{v:.{nd}f}"


def split_sentence(split: str, info: Mapping[str, Any]) -> str:
    """One sentence on a split's pooled agreement and adjudication rate."""
    if info.get("missing"):
        return f"{split}: no reader files found, not compared."
    p = info["pooled"]
    n, agree = int(p["n"]), int(p["agree"])
    if not n:
        return f"{split}: no item was read by both readers."
    unit = "sections" if split == "census" else "documents"
    text = (
        f"{split}: readers A and B agreed on {agree} of {n} items ({_num(p['pct'], 1)} percent, "
        f"95 percent CI {_num(p['ci_low'], 1)} to {_num(p['ci_high'], 1)}) over {info['fields']} fields "
        f"of {info['documents']} {unit}; {info['n_disputed']} "
        f"{'item was' if info['n_disputed'] == 1 else 'items were'} sent to adjudication "
        f"(adjudication rate {_num(100 * info['n_disputed'] / n, 1)} percent)"
    )
    if info.get("unlisted"):
        text += f", and {info['unlisted']} of the {info['n_disagree']} disagreements are not in the dispute list"
    return text + "."


def agreement_markdown(table: pd.DataFrame, summary: Mapping[str, Mapping[str, Any]]) -> str:
    """``results/reliability.md``: one sentence per split and the table."""
    lines = [
        "# Inter-reader agreement",
        "",
        "Agreement between readers A and B before adjudication (plan \"Reliability\", protocol of "
        "2026-10-14), written by `python -m cra_extract.reliability agreement`. Values are compared "
        "in canonical form; a null code (or NA / ABS) is a category of its own. pct is percent "
        "agreement with a Wilson 95 percent interval; kappa is Cohen's kappa where each reader uses "
        "at least two categories on the field. Pooled rows sum the items over fields.",
        "",
    ]
    for split in [*SPLITS, *[s for s in summary if s not in SPLITS and s != ALL_SPLITS]]:
        if split in summary:
            lines.append(f"- {split_sentence(split, summary[split])}")
    if ALL_SPLITS in summary:
        p = summary[ALL_SPLITS]["pooled"]
        n = int(p["n"])
        if n:
            lines.append(
                f"- all: {int(p['agree'])} of {n} items agree ({_num(p['pct'], 1)} percent, 95 percent CI "
                f"{_num(p['ci_low'], 1)} to {_num(p['ci_high'], 1)}); adjudication rate "
                f"{_num(100 * summary[ALL_SPLITS]['n_disputed'] / n, 1)} percent."
            )
    lines += ["", "| split | field | n | agree | pct | ci_low | ci_high | kappa | note |",
              "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in table.to_dict("records"):
        note = str(r["note"]).replace("|", "/")
        lines.append(
            f"| {r['split']} | {r['field']} | {int(r['n'])} | {int(r['agree'])} | {_num(r['pct'], 1)} "
            f"| {_num(r['ci_low'], 1)} | {_num(r['ci_high'], 1)} | {_num(r['kappa'], 3)} | {note} |"
        )
    return "\n".join(lines) + "\n"


def write_agreement(table: pd.DataFrame, summary: Mapping[str, Mapping[str, Any]], out: Path = OUT_PATH) -> Path:
    """Write the CSV to ``out`` and the markdown next to it (same stem, ``.md``)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    t = table.copy()
    for col, nd in (("pct", 2), ("ci_low", 2), ("ci_high", 2), ("kappa", 4)):
        t[col] = [_num(v, nd) for v in t[col]]
    t.to_csv(out, index=False)
    md = out.with_suffix(".md")
    md.write_text(agreement_markdown(table, summary), encoding="utf-8")
    return md


# ----------------------------------------------------------------------------- CLI


def _iso_date(s: str) -> str:
    return date.fromisoformat(s).isoformat()


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.reliability", description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    p.add_argument("--sample-file", type=Path, default=SAMPLE_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("agreement", help="inter-reader agreement (A vs B) per split and field")
    s.add_argument("--out", type=Path, default=OUT_PATH, help="CSV path; the markdown goes next to it")
    s.add_argument("--gold-dir", type=Path, default=GOLD_DIR)
    s = sub.add_parser("draw", help="draw the documents to code twice and record the draw")
    s.add_argument("--branch", choices=sorted(BRANCHES), required=True)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--date", type=_iso_date, required=True, help="drawn-at date (ISO), recorded as given")
    s.add_argument("--n", type=int, default=None)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("sheet", help="write the blind second-coding sheet")
    s.add_argument("--out", type=Path, default=SHEET_PATH)
    s.add_argument("--force", action="store_true", help="overwrite an existing sheet (destroys labels)")
    s = sub.add_parser("compare", help="withdrawn second-coding check; writes results/reliability_second_coding.csv")
    s.add_argument("--sheet", type=Path, default=SHEET_PATH)
    s.add_argument("--first", type=Path, default=None, help="first coding (default data/gold/labels_{split}.csv)")
    s.add_argument("--out", type=Path, default=SECOND_CODING_OUT_PATH)
    s.add_argument("--allow-problems", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "agreement":
        try:
            table, summary = agreement(a.gold_dir)
        except ReliabilityError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if table.empty:
            print(f"error: no reader files under {a.gold_dir}", file=sys.stderr)
            return 1
        md = write_agreement(table, summary, a.out)
        for split in summary:
            if split != ALL_SPLITS:
                print(split_sentence(split, summary[split]))
        print(f"wrote {a.out} and {md}")
        return 0
    manifest = label.read_manifest(a.manifest)
    try:
        if a.cmd == "draw":
            record = draw(manifest, a.branch, a.seed, a.date, a.n)
            save_record(record, a.sample_file, force=a.force)
            print(f"{a.branch}: {record['n']} of {record['pool']} documents, seed {a.seed}, hash {record['doc_id_hash']}")
            return 0
        record = load_record(a.sample_file)
        if record is None:
            print(f"error: no draw in {a.sample_file}; run draw first", file=sys.stderr)
            return 1
        if a.cmd == "sheet":
            if a.out.exists() and not a.force:
                print(f"error: {a.out} exists; pass --force to overwrite it", file=sys.stderr)
                return 1
            rows = build_sheet(manifest, record)
            label.write_sheet(rows, a.out)
            print(f"wrote {len(rows)} rows for {len(record['doc_ids'])} documents to {a.out}")
            return 0
        problems = label.validate(a.sheet)
        if problems and not a.allow_problems:
            for pr in problems:
                print(pr, file=sys.stderr)
            print(f"error: {len(problems)} problem(s) in {a.sheet}", file=sys.stderr)
            return 1
        second = [r for r in label.read_sheet(a.sheet) if r["doc_id"] in set(record["doc_ids"])]
        if record["branch"] == "fallback":
            early = sorted({r["doc_id"] for r in second
                            if (r.get("labeled_at") or "")[:10] and (r.get("labeled_at") or "")[:10] < FALLBACK_EARLIEST.isoformat()})
            if early:
                print(f"error: re-coded before {FALLBACK_EARLIEST.isoformat()}: {early}", file=sys.stderr)
                return 1
        first_path = a.first or GOLD_DIR / f"labels_{record['split']}.csv"
        table = compare(label.read_sheet(first_path), second, record["branch"])
        a.out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(a.out, index=False, float_format="%.5f")
        print(table.to_string(index=False))
        print(f"manual-review error (1 - pooled agreement): {manual_review_error(table):.4f}; wrote {a.out}")
        return 0
    except ReliabilityError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
