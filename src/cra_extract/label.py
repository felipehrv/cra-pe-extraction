"""Gold label sheets: initialise, validate, load, and hash.

The gold set is a CSV with one row per (doc_id, field). This module

- builds the sheet for a split from the manifest (``init_sheet``), pre-filling
  Group A values that the agency index already knows and the null defaults that
  the applicability table in ``config/fields.yaml`` implies;
- checks a filled sheet against the data dictionary and the consistency rules
  (``validate``), returning a list of problems instead of raising;
- loads a validated sheet as ``models.LabelRow`` records (``load_labels``);
- hashes the sorted doc_id list of a split and the label file itself so both can
  be committed to ``docs/hashes.md`` before and after test labeling (design 5.1);
- checks that no bank appears in both dev and test (design 6.8).

CLI::

    python -m cra_extract.label init --split dev --out data/gold/labels_dev.csv
    python -m cra_extract.label validate data/gold/labels_dev.csv
    python -m cra_extract.label hash --split test [--labels data/gold/labels_test.csv]
    python -m cra_extract.label overlap

The codebook the labeler follows is ``docs/labeling_guide.md``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, get_args

from cra_extract.config import CORPUS_PATH, DOCS_DIR, ROOT, applicability, load_fields
from cra_extract.models import DocRecord, LabelRow, Scope

SHEET_COLUMNS = [
    "doc_id",
    "field",
    "field_group",
    "pdf_path",
    "prefilled",
    "value",
    "null_reason",
    "evidence_page",
    "quote",
    "scope",
    "minutes",
    "labeler",
    "labeled_at",
    "notes",
]

# Separator between elements of a set-typed value inside one CSV cell.
SET_SEPARATOR = "|"

# Gold field -> manifest attribute that pre-fills it (value to confirm on the cover page).
GROUP_A_FROM_MANIFEST = {
    "regulator": "regulator",
    "bank_name": "bank_name",
    "bank_id": "bank_id",
    "exam_date": "exam_date",
    "exam_method": "method",
    "overall_rating": "overall_rating",
    "lending_test_rating": "lending_test_rating",
    "cd_test_rating": "cd_test_rating",
}

# Numeric ranges from design section 3 (min, max); None means unbounded.
RANGES: dict[str, tuple[float | None, float | None]] = {
    "total_assets_usd": (1, None),
    "n_assessment_areas": (1, None),
    "n_full_scope": (1, None),
    "inside_aa_share_number_pct": (0, 100),
    "avg_ltd_ratio_pct": (0, 300),
}

HASHES_PATH = DOCS_DIR / "hashes.md"
SCOPES = set(get_args(Scope))


@dataclass(frozen=True)
class Problem:
    """One validation failure. ``row`` is the 1-based data row in the CSV (0 = file level)."""

    row: int
    doc_id: str
    field: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"row {self.row} {self.doc_id} {self.field}: [{self.rule}] {self.message}"


# ----------------------------------------------------------------------------- manifest


def read_manifest(path: Path = CORPUS_PATH) -> list[DocRecord]:
    """Read ``config/corpus.csv`` into ``DocRecord`` rows; empty cells become None."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return [DocRecord(**{k: (v if v not in ("", None) else None) for k, v in r.items()}) for r in rows]


def split_docs(manifest: list[DocRecord], split: str) -> list[DocRecord]:
    """Documents of one split, sorted by doc_id."""
    return sorted((d for d in manifest if d.split == split), key=lambda d: d.doc_id)


def bank_overlap(
    manifest: list[DocRecord], splits: tuple[str, str] = ("dev", "test")
) -> list[tuple[str, str]]:
    """(regulator, bank_id) pairs present in both splits. Must be empty (design 6.8)."""
    keys = [{(d.regulator, d.bank_id.lstrip("0")) for d in split_docs(manifest, s)} for s in splits]
    return sorted(keys[0] & keys[1])


def pdf_path_for(doc_id: str) -> str:
    """Repo-relative path where ``fetch`` stores the PDF of a document."""
    return f"data/pdfs/{doc_id}.pdf"


# ----------------------------------------------------------------------------- init


def _as_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def init_sheet(
    manifest: list[DocRecord], split: str, out_path: Path | None = None
) -> list[dict[str, str]]:
    """One row per (doc_id, field) for the documents of ``split``.

    Group A fields known to the manifest are pre-filled (``prefilled=true``) as values
    for the labeler to confirm. Fields whose applicability cell for the document's
    (regulator, method) is a null get that null pre-filled in ``null_reason``. Every
    other cell is empty. Refuses to build a dev or test sheet while a bank sits in
    both splits.
    """
    if split in ("dev", "test"):
        overlap = bank_overlap(manifest)
        if overlap:
            raise ValueError(f"bank_id in both dev and test: {overlap}")
    fields = load_fields()["fields"]
    rows: list[dict[str, str]] = []
    for doc in split_docs(manifest, split):
        for field, spec in fields.items():
            row = dict.fromkeys(SHEET_COLUMNS, "")
            row.update(
                doc_id=doc.doc_id,
                field=field,
                field_group=str(spec.get("group", "")),
                pdf_path=pdf_path_for(doc.doc_id),
                prefilled="false",
            )
            null = applicability(field, doc.regulator, doc.method)
            if null != "value":
                row["null_reason"] = null
                row["prefilled"] = "true"
            elif field in GROUP_A_FROM_MANIFEST:
                v = getattr(doc, GROUP_A_FROM_MANIFEST[field])
                if v is not None:
                    row["value"] = _as_cell(v)
                    row["prefilled"] = "true"
            rows.append(row)
    if out_path is not None:
        write_sheet(rows, out_path)
    return rows


def write_sheet(rows: list[dict[str, str]], path: Path) -> None:
    """Write sheet rows with the canonical column order."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in SHEET_COLUMNS})


def read_sheet(path: Path) -> list[dict[str, str]]:
    """Read a label CSV as a list of string dicts (no coercion)."""
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]


# ----------------------------------------------------------------------------- coercion


def coerce_value(field: str, raw: str) -> Any:
    """Parse a CSV cell into the field's Python type. Raises ValueError with a reason."""
    spec = load_fields()["fields"][field]
    kind = spec["type"]
    if kind == "enum":
        if raw not in spec["allowed"]:
            raise ValueError(f"{raw!r} not in allowed {spec['allowed']}")
        return raw
    if kind == "set":
        items = [x.strip() for x in raw.split(SET_SEPARATOR)]
        if any(x == "" for x in items):
            raise ValueError("empty element in set")
        bad = [x for x in items if x not in spec["allowed"]]
        if bad:
            raise ValueError(f"{bad} not in allowed {spec['allowed']}")
        return sorted(set(items))
    if kind == "bool":
        low = raw.lower()
        if low not in ("true", "false"):
            raise ValueError(f"{raw!r} is not true/false")
        return low == "true"
    if kind == "date":
        try:
            return date.fromisoformat(raw)
        except ValueError as e:
            raise ValueError(f"{raw!r} is not an ISO date") from e
    if kind == "int":
        try:
            n = int(raw.replace(",", ""))
        except ValueError as e:
            raise ValueError(f"{raw!r} is not an integer") from e
        _check_range(field, n)
        return n
    if kind == "float":
        try:
            x = float(raw)
        except ValueError as e:
            raise ValueError(f"{raw!r} is not a number") from e
        _check_range(field, x)
        return x
    return raw  # str


def _check_range(field: str, x: float) -> None:
    lo, hi = RANGES.get(field, (None, None))
    if lo is not None and x < lo:
        raise ValueError(f"{x} below minimum {lo}")
    if hi is not None and x > hi:
        raise ValueError(f"{x} above maximum {hi}")


def _doc_id_parts(doc_id: str) -> tuple[str | None, date | None]:
    """(regulator, exam_date) encoded in ``{regulator}_{bank_id}_{YYYYMMDD}``."""
    parts = doc_id.split("_")
    if len(parts) != 3:
        return None, None
    try:
        return parts[0], datetime.strptime(parts[2], "%Y%m%d").date()
    except ValueError:
        return parts[0], None


# ----------------------------------------------------------------------------- validate


def validate(label_path: Path) -> list[Problem]:
    """Check a label sheet. Returns an empty list when the sheet is clean."""
    rows = read_sheet(label_path)
    problems: list[Problem] = []
    fields = load_fields()["fields"]
    gold_nulls = set(load_fields()["null_vocabulary"]["gold"])
    required = {"doc_id", "field", "value", "null_reason", "evidence_page", "quote", "minutes"}
    present = set(rows[0].keys()) if rows else set()
    missing = required - present
    if missing or not rows:
        problems.append(Problem(0, "", "", "columns", f"missing columns {sorted(missing)}"))
        return problems

    values: dict[tuple[str, str], Any] = {}
    seen: set[tuple[str, str]] = set()
    for i, r in enumerate(rows, start=1):
        doc_id, field = r["doc_id"], r["field"]

        def bad(rule: str, msg: str, i: int = i, doc_id: str = doc_id, field: str = field) -> None:
            problems.append(Problem(i, doc_id, field, rule, msg))

        if field not in fields:
            bad("unknown_field", f"{field!r} is not in fields.yaml")
            continue
        if (doc_id, field) in seen:
            bad("duplicate_row", "second row for this (doc_id, field)")
        seen.add((doc_id, field))
        spec = fields[field]
        raw, null = r["value"], r["null_reason"]

        if null and null not in gold_nulls:
            bad("bad_null_reason", f"{null!r} not in {sorted(gold_nulls)}")
            null = ""
        if raw and null:
            bad("value_and_null", "both value and null_reason are set")
        if not raw and not null:
            bad("empty", "neither value nor null_reason is set")
        if null and not spec.get("nullable", True):
            bad("not_nullable", f"{field} is never null")

        if raw:
            try:
                values[(doc_id, field)] = coerce_value(field, raw)
            except ValueError as e:
                rule = "bad_date" if spec["type"] == "date" else "bad_value"
                bad(rule, str(e))
            if not r.get("evidence_page"):
                bad("missing_evidence_page", "a value needs the page it was read from")
            if not r.get("quote"):
                bad("missing_quote", "a value needs a verbatim quote")

        page = r.get("evidence_page", "")
        if page and not (page.isdigit() and int(page) >= 1):
            bad("bad_evidence_page", f"{page!r} is not a positive integer")
        scope = r.get("scope", "")
        if scope and scope not in SCOPES:
            bad("bad_scope", f"{scope!r} not in {sorted(SCOPES)}")
        mins = r.get("minutes", "")
        try:
            if mins == "" or float(mins) < 0:
                raise ValueError
        except ValueError:
            bad("missing_minutes", "minutes must be a non-negative number on every row")
        stamp = r.get("labeled_at", "")
        if stamp:
            try:
                datetime.fromisoformat(stamp)
            except ValueError:
                bad("bad_labeled_at", f"{stamp!r} is not an ISO datetime")

    problems.extend(_consistency(rows, values, fields))
    return problems


def _consistency(
    rows: list[dict[str, str]], values: dict[tuple[str, str], Any], fields: dict[str, Any]
) -> list[Problem]:
    """Cross-field rules named by the ``consistency`` key in fields.yaml, per document."""
    problems: list[Problem] = []
    row_of = {(r["doc_id"], r["field"]): i for i, r in enumerate(rows, start=1)}
    docs = sorted({r["doc_id"] for r in rows})
    rules = {f: s["consistency"] for f, s in fields.items() if "consistency" in s}

    for doc_id in docs:
        v = {f: values.get((doc_id, f)) for f in fields}
        reg, id_date = _doc_id_parts(doc_id)
        regulator = v["regulator"] or reg
        exam_date = v["exam_date"] or id_date

        def bad(field: str, rule: str, msg: str, doc_id: str = doc_id) -> None:
            problems.append(Problem(row_of.get((doc_id, field), 0), doc_id, field, rule, msg))

        for field, rule in rules.items():
            x = v[field]
            if x is None:
                continue
            if rule == "lte_exam_date":
                if exam_date is not None and x > exam_date:
                    bad(field, rule, f"{x} is after exam_date {exam_date}")
            elif rule == "lte_n_assessment_areas":
                n = v["n_assessment_areas"]
                if n is not None and x > n:
                    bad(field, rule, f"{x} exceeds n_assessment_areas {n}")
            elif rule == "equals_overall_for_OCC_small":
                if regulator == "OCC" and v["exam_method"] == "small":
                    o = v["overall_rating"]
                    if o is not None and x != o:
                        bad(field, rule, f"{x} differs from overall_rating {o} (OCC Small)")
            elif rule == "equals_overall_when_level_overall":
                if v["rating_lowered_level"] == "overall":
                    o = v["overall_rating"]
                    if o is not None and x != o:
                        bad(field, rule, f"{x} differs from overall_rating {o}")
            # matches_aa_section_count is checked against located sections, not here.

        # n_full_scope is not_applicable exactly when the bank has one assessment area.
        n = v["n_assessment_areas"]
        fs_null = next(
            (r["null_reason"] for r in rows if (r["doc_id"], r["field"]) == (doc_id, "n_full_scope")),
            "",
        )
        if n == 1 and v["n_full_scope"] is not None:
            bad("n_full_scope", "na_when_single_aa", "must be not_applicable when n_assessment_areas is 1")
        if n is not None and n > 1 and fs_null == "not_applicable":
            bad("n_full_scope", "na_when_single_aa", f"not_applicable but n_assessment_areas is {n}")
    return problems


# ----------------------------------------------------------------------------- load


def load_labels(path: Path, strict: bool = True) -> list[LabelRow]:
    """Load a label sheet as typed ``LabelRow`` records.

    With ``strict`` (default) the sheet must validate cleanly; otherwise a ValueError
    lists the problems. With ``strict=False`` rows that fail coercion keep the raw
    string value.
    """
    problems = validate(path)
    if strict and problems:
        raise ValueError("label sheet has problems:\n" + "\n".join(map(str, problems)))
    out: list[LabelRow] = []
    for r in read_sheet(path):
        if r["field"] not in load_fields()["fields"]:
            continue
        value: Any = None
        if r["value"]:
            try:
                value = coerce_value(r["field"], r["value"])
            except ValueError:
                value = r["value"]
        out.append(
            LabelRow(
                doc_id=r["doc_id"],
                field=r["field"],
                value=value,
                null_reason=r["null_reason"] or None,
                evidence_page=int(r["evidence_page"]) if r.get("evidence_page", "").isdigit() else None,
                quote=r.get("quote") or None,
                scope=r.get("scope") or None,
                minutes=float(r["minutes"]) if r.get("minutes") else None,
                labeler=r.get("labeler") or None,
                labeled_at=datetime.fromisoformat(r["labeled_at"]) if r.get("labeled_at") else None,
                notes=r.get("notes") or None,
            )
        )
    return out


# ----------------------------------------------------------------------------- hashes


def split_hash(manifest: list[DocRecord], split: str) -> str:
    """sha256 of the sorted doc_id list of a split, one id per line."""
    ids = sorted(d.doc_id for d in manifest if d.split == split)
    return hashlib.sha256(("\n".join(ids) + "\n").encode("utf-8")).hexdigest()


def labels_hash(path: Path) -> str:
    """sha256 of the label file bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_hashes(
    split: str,
    n_docs: int,
    doc_hash: str,
    label_file: str | None,
    label_hash: str | None,
    path: Path = HASHES_PATH,
    now: datetime | None = None,
) -> None:
    """Append one row to ``docs/hashes.md`` (created with a header when missing)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# Committed hashes\n\n"
            "Doc hash = sha256 of the sorted doc_id list of the split (one per line). "
            "Label hash = sha256 of the label CSV. The test doc hash is recorded before test "
            "labeling starts and the label hash after it ends (design 5.1).\n\n"
            "| recorded_at | split | n_docs | doc_id list sha256 | label file | label sha256 |\n"
            "|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    stamp = (now or datetime.now()).replace(microsecond=0).isoformat()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(
            f"| {stamp} | {split} | {n_docs} | {doc_hash} | {label_file or ''} | {label_hash or ''} |\n"
        )


# ----------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.label", description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write an empty label sheet for a split")
    s.add_argument("--split", required=True, choices=["dev", "test", "audit", "corpus"])
    s.add_argument("--out", type=Path, required=True)

    s = sub.add_parser("validate", help="check a label sheet; exit 1 on problems")
    s.add_argument("path", type=Path)

    s = sub.add_parser("hash", help="record the split doc list hash (and label file hash)")
    s.add_argument("--split", required=True)
    s.add_argument("--labels", type=Path, default=None, help="label CSV to hash as well")
    s.add_argument("--out", type=Path, default=HASHES_PATH)

    sub.add_parser("overlap", help="list banks present in both dev and test")

    a = p.parse_args(argv)
    manifest = read_manifest(a.manifest) if a.cmd != "validate" else []

    if a.cmd == "init":
        rows = init_sheet(manifest, a.split, a.out)
        n_docs = len({r["doc_id"] for r in rows})
        print(f"wrote {len(rows)} rows for {n_docs} {a.split} documents to {a.out}")
        return 0
    if a.cmd == "validate":
        problems = validate(a.path)
        for pr in problems:
            print(pr)
        print(f"{len(problems)} problem(s) in {a.path}")
        return 1 if problems else 0
    if a.cmd == "hash":
        n_docs = len(split_docs(manifest, a.split))
        doc_hash = split_hash(manifest, a.split)
        lh = labels_hash(a.labels) if a.labels else None
        label_file = str(Path(a.labels).resolve().relative_to(ROOT)) if a.labels else None
        record_hashes(a.split, n_docs, doc_hash, label_file, lh, a.out)
        print(f"{a.split}: {n_docs} docs, doc hash {doc_hash}" + (f", labels {lh}" if lh else ""))
        return 0
    if a.cmd == "overlap":
        overlap = bank_overlap(manifest)
        for reg, bid in overlap:
            print(f"{reg} {bid} is in both dev and test")
        print(f"{len(overlap)} overlapping bank(s)")
        return 1 if overlap else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
