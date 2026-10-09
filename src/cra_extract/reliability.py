"""Group B reliability check: a second, blind coding of gold documents (design 5.4).

Implements the Group B part of ``docs/analysis_plan.md`` "Reliability" (the Group C part,
every census positive plus template negatives, is ``census double``). Two branches:

- ``outside`` (preferred): an outside coder double-codes ``N_OUTSIDE`` (8) test documents
  drawn at random after test labeling ends, never one of the eight fixture samples whose
  values the labeling guide quotes (``strata.SAMPLE_DOC_IDS``).
- ``fallback``: the author blind re-codes ``N_FALLBACK`` (6) dev documents drawn at random,
  at least five days after the first coding (dev was coded 2026-10-07 to 2026-10-08, so not
  before ``FALLBACK_EARLIEST``, 2026-10-13); reported as intra-rater stability.

Both code ``RELIABILITY_FIELDS``: n_assessment_areas, n_full_scope, loan_products_evaluated,
lending_data_period_start and lending_data_period_end.

Steps:

1. :func:`draw` draws the documents with ``numpy.random.default_rng(seed)`` from the sorted
   pool and :func:`save_record` writes the seed, date, branch, pool hash and doc_ids to
   ``config/reliability_sample.yaml`` before any second coding.
2. :func:`build_sheet` writes the blind second sheet (``data/gold/reliability_sheet.csv``):
   the rows of ``label.init_sheet`` for the drawn documents and the five fields, so the coder
   sees no first-coding value and no system output (applicability nulls are pre-filled as in
   every fresh sheet).
3. :func:`compare` scores agreement per field between the first coding
   (``labels_{split}.csv``) and the second sheet: an item is one document; values are
   compared in canonical form (dates parsed, integers as numbers, sets as sorted element
   sets, a null code as a category of its own). Cohen's kappa (``stats.cohen_kappa``) is
   reported where both classes occur in both codings (for an enum, date or integer field:
   each coding uses at least two categories), percent agreement with the majority-class
   prevalence otherwise; sets are also compared element-wise (one binary item per document
   and allowed element) with an element-level kappa. Minutes per item of each coding are
   reported side by side (the manual-baseline range).

``results/reliability.csv`` holds one row per field with ``n`` (documents) and ``n_agree``
(documents coded alike): ``report.manual_review_error`` pools ``1 - sum(n_agree) / sum(n)``
over the Group B rows as the manual-review error line of the review curve.

CLI::

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
from cra_extract.config import CONFIG_DIR, CORPUS_PATH, GOLD_DIR, RESULTS_DIR, field_spec
from cra_extract.models import DocRecord
from cra_extract.stats import cohen_kappa
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


def canonical(field: str, row: Mapping[str, str]) -> str:
    """Canonical category of one coded cell: ``null:<code>`` for a gold null, sorted elements
    for a set, ISO date for a date, the number for an integer, the casefolded text otherwise;
    empty for an uncoded cell."""
    null = (row.get("null_reason") or "").strip()
    if null:
        return f"null:{null}"
    raw = (row.get("value") or "").strip()
    if not raw:
        return ""
    kind = field_spec(field).get("type")
    if kind == "set":
        return label.SET_SEPARATOR.join(sorted(grounding.parse_set(raw)))
    if kind == "date":
        d = grounding.parse_date(raw)
        return d.isoformat() if d else raw.casefold()
    if kind in ("int", "float"):
        try:
            x = float(raw.replace(",", "").replace("$", "").replace("%", ""))
        except ValueError:
            return raw.casefold()
        return str(int(x)) if x.is_integer() else str(x)
    return " ".join(raw.split()).casefold()


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
                    ea.append(e in sa)
                    eb.append(e in sb)
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


# ----------------------------------------------------------------------------- CLI


def _iso_date(s: str) -> str:
    return date.fromisoformat(s).isoformat()


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.reliability", description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    p.add_argument("--sample-file", type=Path, default=SAMPLE_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("draw", help="draw the documents to code twice and record the draw")
    s.add_argument("--branch", choices=sorted(BRANCHES), required=True)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--date", type=_iso_date, required=True, help="drawn-at date (ISO), recorded as given")
    s.add_argument("--n", type=int, default=None)
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("sheet", help="write the blind second-coding sheet")
    s.add_argument("--out", type=Path, default=SHEET_PATH)
    s.add_argument("--force", action="store_true", help="overwrite an existing sheet (destroys labels)")
    s = sub.add_parser("compare", help="agreement per field; writes results/reliability.csv")
    s.add_argument("--sheet", type=Path, default=SHEET_PATH)
    s.add_argument("--first", type=Path, default=None, help="first coding (default data/gold/labels_{split}.csv)")
    s.add_argument("--out", type=Path, default=OUT_PATH)
    s.add_argument("--allow-problems", action="store_true")
    a = p.parse_args(argv)
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
