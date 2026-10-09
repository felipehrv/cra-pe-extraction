"""Documented, versioned overrides and exclusions for the corpus manifest.

The manifest (``config/corpus.csv``) is built from the agency indexes, and a few of its
rows are wrong in ways only a reading of the PDF shows: an impossible exam date in an FDIC
file id, an index link that points to an older PE, a PDF the agency cannot serve, a PDF
portfolio whose text layer is only a reader notice. Design section 2 and the analysis plan
("Exclusions") require such rows to be corrected or excluded before the test draw, and
every change to be recorded. ``config/manifest_overrides.yaml`` is that record; this module
validates it and applies it to the manifest.

The YAML has three lists. Every entry carries a ``reason`` (a snake_case code), at least one
``evidence`` item and the ``date`` it was decided; ``note`` is optional free text.

- ``field_overrides``: ``doc_id``, ``field`` (a manifest column), ``old`` and ``new`` (the
  cell text before and after, quoted strings so YAML does not retype them; null is the empty
  cell). An override of ``regulator``, ``bank_id`` or ``exam_date`` changes the doc_id, which
  is rebuilt with :func:`cra_extract.models.make_doc_id`; the entry then states the result in
  ``new_doc_id`` and the original doc_id becomes an alias (:meth:`Overrides.aliases`).
- ``exclusions``: ``doc_id``; the row gets split ``excluded``, the only split that
  :func:`cra_extract.strata.eligible_pool` removes, so no gold or audit draw can select it.
- ``pdf_replacements``: a PDF portfolio whose embedded PE is extracted to
  ``data/pdfs/{doc_id}.pdf``; the downloaded file is kept as ``{doc_id}.portfolio.pdf``. The
  manifest cells that change with the file (``sha256``, ``pages``, ``chars_per_page``,
  ``text_extractable``) are listed as field overrides, so the manifest change stays in one
  list; a replacement requires the matching ``sha256`` override.

Evidence items are either a document page (``doc``, ``page``, ``quote``: the quote is
verbatim from ``data/text/{doc}.pages.json`` after whitespace is collapsed, and
:func:`check_evidence` verifies it) or a log (``log``, a repo path or description, and
``detail``).

Every ``doc_id`` in the YAML is the id the index stage builds, never the renamed one, so the
file stays valid against a freshly rebuilt manifest. Applying is idempotent: a cell that
already holds the ``new`` value is left alone, and a cell holding neither ``old`` nor
``new`` is a conflict that stops the run.

The manifest column ``exam_date_source`` records where each row's ``exam_date`` comes from:
``fdic_file_id`` (the YYMMDD of the FDIC ``SYS_EXTRN_FILE_ID``), ``index_exam_date`` (the
OCC and FRB index exam date) or ``override`` (an ``exam_date`` field override). Group A
scoring uses it to treat the FDIC file-id date as having no index truth.

Rows of a drawn gold split (dev, test, audit) are protected: an exclusion of such a row or
an override that changes its doc_id is refused; an override that keeps the doc_id is
allowed. A row with split ``excluded`` and no exclusion entry is refused too, so every
exclusion in the manifest is documented.

CLI::

    python -m cra_extract.overrides apply [--manifest config/corpus.csv] [--dry-run] [--no-files]
    python -m cra_extract.overrides check [--overrides config/manifest_overrides.yaml]

``apply`` validates the YAML, applies it to the manifest rows (pure,
:func:`apply_overrides`), renames the data files of a renamed doc_id
(``data/pdfs/{id}.pdf`` and ``.portfolio.pdf``, ``data/text/{id}.txt`` and ``.pages.json``),
extracts and parses the PDF replacements, and rewrites the manifest in place: every cell
that the overrides do not change is written back verbatim, with the layout of
:func:`cra_extract.strata.write_rows`. ``check`` validates the YAML and verifies every
document quote against the page text that is present on disk.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cra_extract.config import CORPUS_PATH, OVERRIDES_PATH, PDF_DIR, TEXT_DIR
from cra_extract.models import make_doc_id

EXAM_DATE_SOURCE_COLUMN = "exam_date_source"
ExamDateSource = Literal["index_exam_date", "fdic_file_id", "override"]
EXAM_DATE_SOURCES: tuple[str, ...] = ("index_exam_date", "fdic_file_id", "override")

# Columns the doc_id is built from; an override of one of them renames the document.
KEY_FIELDS = ("regulator", "bank_id", "exam_date")
# Columns this module manages itself and that an override may not set.
RESERVED_FIELDS = ("doc_id", "split", EXAM_DATE_SOURCE_COLUMN)
# Drawn samples: no exclusion and no doc_id change once a row is in one of them.
PROTECTED_SPLITS = ("dev", "test", "audit")
EXCLUDED = "excluded"

DOC_ID_RE = re.compile(r"^(OCC|FDIC|FRB)_([0-9A-Za-z]+)_(\d{8})$")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PORTFOLIO_SUFFIX = ".portfolio.pdf"
# Files of one document under (data/pdfs, data/text), renamed with the doc_id.
PDF_SUFFIXES = (".pdf", PORTFOLIO_SUFFIX)
TEXT_SUFFIXES = (".txt", ".pages.json")


class OverrideError(ValueError):
    """The overrides file is invalid or cannot be applied to this manifest."""


def default_exam_date_source(regulator: str) -> str:
    """Where the index stage takes a row's exam_date from: the FDIC file id or the index."""
    return "fdic_file_id" if regulator == "FDIC" else "index_exam_date"


def parse_doc_id(doc_id: str) -> tuple[str, str, date]:
    """``(regulator, bank_id, exam_date)`` of a doc_id. Raises ``OverrideError`` if malformed."""
    m = DOC_ID_RE.match(doc_id)
    if not m:
        raise OverrideError(f"malformed doc_id {doc_id!r} (expected REGULATOR_BANKID_YYYYMMDD)")
    try:
        exam = date(int(m.group(3)[:4]), int(m.group(3)[4:6]), int(m.group(3)[6:]))
    except ValueError as err:
        raise OverrideError(f"doc_id {doc_id!r} has an invalid date: {err}") from err
    return m.group(1), m.group(2), exam


def _collapse(text: str) -> str:
    return " ".join(text.split())


# ----------------------------------------------------------------------------- schema


class Evidence(BaseModel):
    """One piece of evidence: a document page with a verbatim quote, or a log entry."""

    model_config = ConfigDict(extra="forbid")

    doc: str | None = Field(default=None, description="doc_id whose page text holds the quote")
    page: int | None = Field(default=None, ge=1, description="1-based page in data/text/{doc}.pages.json")
    quote: str | None = Field(default=None, description="verbatim page text (whitespace collapsed)")
    log: str | None = Field(default=None, description="log file (repo path) or source of a non-page fact")
    detail: str | None = Field(default=None, description="what the log shows")

    @model_validator(mode="after")
    def _one_form(self) -> Evidence:
        is_doc = self.doc is not None
        is_log = self.log is not None
        if is_doc == is_log:
            raise ValueError("evidence needs exactly one of 'doc' (with page and quote) or 'log' (with detail)")
        if is_doc:
            if self.page is None or not (self.quote or "").strip():
                raise ValueError("document evidence needs 'page' and a non-empty 'quote'")
            if self.detail is not None:
                raise ValueError("document evidence takes no 'detail'")
            parse_doc_id(self.doc or "")
        else:
            if not (self.detail or "").strip():
                raise ValueError("log evidence needs a non-empty 'detail'")
            if self.page is not None or self.quote is not None:
                raise ValueError("log evidence takes no 'page' or 'quote'")
        return self


class _Entry(BaseModel):
    """Fields shared by every entry: the document, why, the evidence and the decision date."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    reason: str
    evidence: list[Evidence] = Field(min_length=1)
    date: date
    note: str | None = None

    @field_validator("doc_id")
    @classmethod
    def _doc_id(cls, v: str) -> str:
        parse_doc_id(v)
        return v

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        if not REASON_RE.match(v):
            raise ValueError(f"reason must be a snake_case code, got {v!r}")
        return v


class FieldOverride(_Entry):
    """One manifest cell changed from ``old`` to ``new`` (cell text; None is the empty cell)."""

    field: str
    old: str | None
    new: str | None
    new_doc_id: str | None = None

    @field_validator("field")
    @classmethod
    def _field(cls, v: str) -> str:
        if v in RESERVED_FIELDS:
            raise ValueError(f"field {v!r} is managed by the overrides module and cannot be overridden")
        return v

    @field_validator("new_doc_id")
    @classmethod
    def _new_doc_id(cls, v: str | None) -> str | None:
        if v is not None:
            parse_doc_id(v)
        return v

    @model_validator(mode="after")
    def _changes_something(self) -> FieldOverride:
        if (self.old or "") == (self.new or ""):
            raise ValueError(f"{self.doc_id} {self.field}: old and new are equal")
        if self.field == "exam_date" and self.new is not None:
            try:
                date.fromisoformat(self.new)
            except ValueError as err:
                raise ValueError(f"{self.doc_id} exam_date: new value {self.new!r} is not an ISO date") from err
        return self

    @property
    def old_cell(self) -> str:
        return self.old or ""

    @property
    def new_cell(self) -> str:
        return self.new or ""


class Exclusion(_Entry):
    """One document removed from every draw and evaluation (split ``excluded``)."""


class PdfReplacement(_Entry):
    """A PDF portfolio replaced by its embedded PE; the download is kept beside it."""

    method: Literal["portfolio_embedded_file"]
    embedded_file: str = Field(min_length=1, description="name of the embedded file to extract")
    original_sha256: str
    sha256: str

    @field_validator("original_sha256", "sha256")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not SHA256_RE.match(v):
            raise ValueError(f"not a lowercase hex sha256: {v!r}")
        return v


class Overrides(BaseModel):
    """The parsed ``config/manifest_overrides.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    updated: date
    field_overrides: list[FieldOverride] = Field(default_factory=list)
    exclusions: list[Exclusion] = Field(default_factory=list)
    pdf_replacements: list[PdfReplacement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Overrides:
        problems: list[str] = []
        seen: set[tuple[str, str]] = set()
        for o in self.field_overrides:
            if (o.doc_id, o.field) in seen:
                problems.append(f"two overrides of {o.doc_id} {o.field}")
            seen.add((o.doc_id, o.field))
        problems += _doc_id_problems(self.field_overrides)
        excl = [e.doc_id for e in self.exclusions]
        problems += [f"{d} excluded twice" for d in sorted({d for d in excl if excl.count(d) > 1})]
        repl = [r.doc_id for r in self.pdf_replacements]
        problems += [f"{d} has two pdf_replacements" for d in sorted({d for d in repl if repl.count(d) > 1})]
        sha = {o.doc_id: o for o in self.field_overrides if o.field == "sha256"}
        for r in self.pdf_replacements:
            if r.doc_id in excl:
                problems.append(f"{r.doc_id} is both excluded and given a replacement PDF")
            o = sha.get(r.doc_id)
            if o is None or o.old != r.original_sha256 or o.new != r.sha256:
                problems.append(
                    f"{r.doc_id}: a pdf_replacement needs a sha256 field override from "
                    "original_sha256 to sha256"
                )
        new_ids = {o.new_doc_id for o in self.field_overrides if o.new_doc_id}
        for d in [o.doc_id for o in self.field_overrides] + excl + repl:
            if d in new_ids:
                problems.append(f"{d} is a renamed doc_id; entries use the original doc_id")
        if problems:
            raise ValueError("; ".join(sorted(set(problems))))
        return self

    def aliases(self) -> dict[str, str]:
        """Original doc_id -> doc_id after the overrides, for every renamed document."""
        return {o.doc_id: o.new_doc_id for o in self.field_overrides if o.new_doc_id and o.new_doc_id != o.doc_id}

    def resolve(self, doc_id: str) -> str:
        """The current doc_id of a document named by its original or its current id."""
        return self.aliases().get(doc_id, doc_id)

    def entries_for(self, doc_id: str) -> list[_Entry]:
        """Every entry about one document (named by its original or current id)."""
        back = {v: k for k, v in self.aliases().items()}
        original = back.get(doc_id, doc_id)
        return [
            e
            for e in (*self.field_overrides, *self.exclusions, *self.pdf_replacements)
            if e.doc_id == original
        ]


def _doc_id_problems(overrides: list[FieldOverride]) -> list[str]:
    """Check ``new_doc_id`` on key-field overrides against the rebuilt doc_id."""
    problems: list[str] = []
    by_doc: dict[str, list[FieldOverride]] = {}
    for o in overrides:
        by_doc.setdefault(o.doc_id, []).append(o)
    for doc_id, items in by_doc.items():
        regulator, bank_id, exam = parse_doc_id(doc_id)
        parts: dict[str, Any] = {"regulator": regulator, "bank_id": bank_id, "exam_date": exam}
        keys = [o for o in items if o.field in KEY_FIELDS]
        for o in keys:
            if o.new is None:
                problems.append(f"{doc_id} {o.field}: a doc_id field cannot be emptied")
                continue
            parts[o.field] = date.fromisoformat(o.new) if o.field == "exam_date" else o.new
        try:
            rebuilt = make_doc_id(parts["regulator"], parts["bank_id"], parts["exam_date"])
        except (TypeError, ValueError) as err:
            problems.append(f"{doc_id}: cannot rebuild the doc_id ({err})")
            continue
        for o in items:
            if o.field in KEY_FIELDS:
                if rebuilt == doc_id and o.new_doc_id is not None:
                    problems.append(f"{doc_id} {o.field}: new_doc_id given but the doc_id does not change")
                elif rebuilt != doc_id and o.new_doc_id != rebuilt:
                    problems.append(f"{doc_id} {o.field}: new_doc_id must be {rebuilt} (got {o.new_doc_id})")
            elif o.new_doc_id is not None:
                problems.append(f"{doc_id} {o.field}: new_doc_id only goes on regulator, bank_id or exam_date")
    return problems


def parse_overrides(data: Any) -> Overrides:
    """Validate parsed YAML. Raises ``OverrideError`` with every schema problem."""
    if not isinstance(data, dict):
        raise OverrideError("manifest overrides: the YAML top level must be a mapping")
    try:
        return Overrides.model_validate(data)
    except ValidationError as err:
        lines = []
        for e in err.errors():
            loc = ".".join(str(x) for x in e["loc"])
            lines.append(f"{loc}: {e['msg']}" if loc else e["msg"])
        raise OverrideError("manifest overrides: " + "; ".join(lines)) from err


def load_overrides(path: Path = OVERRIDES_PATH) -> Overrides:
    """Read and validate ``config/manifest_overrides.yaml``."""
    with open(path, encoding="utf-8") as fh:
        return parse_overrides(yaml.safe_load(fh))


# ----------------------------------------------------------------------------- pure apply


@dataclass(frozen=True)
class CellChange:
    """One manifest cell that :func:`apply_overrides` changed (row named by its new doc_id)."""

    doc_id: str
    column: str
    old: str
    new: str


def apply_overrides(manifest_rows: list[dict[str, str]], overrides: Overrides) -> list[dict[str, str]]:
    """Return a copy of the manifest rows with the overrides applied; the input is not changed.

    Rows keep their order. Field overrides are applied first (a cell holding ``old`` gets
    ``new``; a cell already holding ``new`` is left alone; anything else raises), then the
    doc_id of a row whose key fields changed is rebuilt, then ``exam_date_source`` is filled
    on every row (appended as the last column when absent), then exclusions set split
    ``excluded``. Raises ``OverrideError`` for an unknown doc_id or column, a conflicting cell,
    a doc_id collision, a protected gold row, an undocumented ``excluded`` row, or a
    non-evaluable row (no sha256, or ``text_extractable`` not true) left outside ``excluded``
    (plan, Exclusions: every non-evaluable row is excluded before draw-test).
    """
    rows = [dict(r) for r in manifest_rows]
    by_id: dict[str, dict[str, str]] = {}
    for r in rows:
        if r["doc_id"] in by_id:
            raise OverrideError(f"duplicate doc_id in manifest: {r['doc_id']}")
        by_id[r["doc_id"]] = r
    columns = list(rows[0]) if rows else []
    aliases = overrides.aliases()

    def locate(doc_id: str) -> dict[str, str]:
        row = by_id.get(doc_id) or by_id.get(aliases.get(doc_id, ""))
        if row is None:
            raise OverrideError(f"{doc_id}: not in the manifest (neither the original nor the renamed id)")
        return row

    by_doc: dict[str, list[FieldOverride]] = {}
    for o in overrides.field_overrides:
        by_doc.setdefault(o.doc_id, []).append(o)
    overridden_exam_date: set[int] = set()

    for doc_id, items in by_doc.items():
        row = locate(doc_id)
        for o in items:
            if o.field not in row:
                raise OverrideError(f"{doc_id}: no manifest column {o.field!r}")
            cur = row[o.field]
            if cur == o.old_cell:
                row[o.field] = o.new_cell
            elif cur != o.new_cell:
                raise OverrideError(
                    f"{doc_id} {o.field}: manifest has {cur!r}, the override expects {o.old_cell!r} "
                    f"(or {o.new_cell!r} when already applied)"
                )
            if o.field == "exam_date":
                overridden_exam_date.add(id(row))
        try:
            new_id = make_doc_id(row["regulator"], row["bank_id"], date.fromisoformat(row["exam_date"]))
        except (KeyError, ValueError) as err:
            raise OverrideError(f"{doc_id}: cannot rebuild the doc_id after the overrides ({err})") from err
        if new_id != row["doc_id"]:
            if row.get("split") in PROTECTED_SPLITS:
                raise OverrideError(
                    f"{row['doc_id']} is in the {row['split']} split; an override that changes its "
                    "doc_id is refused"
                )
            if new_id in by_id:
                raise OverrideError(f"{row['doc_id']}: the new doc_id {new_id} is already in the manifest")
            del by_id[row["doc_id"]]
            row["doc_id"] = new_id
            by_id[new_id] = row

    if EXAM_DATE_SOURCE_COLUMN not in columns:
        columns.append(EXAM_DATE_SOURCE_COLUMN)
    for r in rows:
        r[EXAM_DATE_SOURCE_COLUMN] = (
            "override" if id(r) in overridden_exam_date else default_exam_date_source(r["regulator"])
        )

    documented: set[int] = set()
    for e in overrides.exclusions:
        row = locate(e.doc_id)
        documented.add(id(row))
        if row["split"] == EXCLUDED:
            continue
        if row["split"] in PROTECTED_SPLITS:
            raise OverrideError(f"{row['doc_id']} is in the {row['split']} split; its exclusion is refused")
        row["split"] = EXCLUDED
    undocumented = [r["doc_id"] for r in rows if r.get("split") == EXCLUDED and id(r) not in documented]
    if undocumented:
        raise OverrideError(
            "rows with split excluded and no exclusion entry in the overrides file: " + ", ".join(undocumented)
        )
    unexcluded = non_evaluable_unexcluded(rows)
    if unexcluded:
        raise OverrideError(
            "non-evaluable rows (no sha256, or text_extractable not true) that are not excluded; "
            "the plan (Exclusions) sets every such row to split excluded before draw-test: add an "
            "exclusion entry, a pdf_replacement, or fetch and parse the document again: "
            + ", ".join(unexcluded)
        )
    return [{c: r[c] for c in columns} for r in rows]


def is_evaluable_row(row: dict[str, str]) -> bool:
    """True when a manifest row has a fetched PDF (a sha256) and ``text_extractable`` true."""
    return bool((row.get("sha256") or "").strip()) and (row.get("text_extractable") or "").strip().lower() == "true"


def non_evaluable_unexcluded(rows: list[dict[str, str]]) -> list[str]:
    """doc_ids of rows that are not evaluable (:func:`is_evaluable_row`) and whose split is not
    ``excluded``. The plan (Exclusions, last bullet) requires this list to be empty before
    ``draw-test``; :func:`apply_overrides` and :func:`cra_extract.strata.draw_test` refuse
    otherwise."""
    return [r["doc_id"] for r in rows if r.get("split") != EXCLUDED and not is_evaluable_row(r)]


def changed_cells(before: list[dict[str, str]], after: list[dict[str, str]]) -> list[CellChange]:
    """Cells that differ between two row lists of the same length and order (as written by
    :func:`apply_overrides`); a column present only in ``after`` counts from the empty cell."""
    if len(before) != len(after):
        raise OverrideError("changed_cells needs row lists of equal length")
    out = []
    for b, a in zip(before, after, strict=True):
        for col, new in a.items():
            old = b.get(col, "")
            if old != new:
                out.append(CellChange(a["doc_id"], col, old, new))
    return out


# ----------------------------------------------------------------------------- files


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def doc_files(doc_id: str, pdf_dir: Path = PDF_DIR, text_dir: Path = TEXT_DIR) -> list[Path]:
    """Every data file named after a doc_id (PDF, kept portfolio, text, pages)."""
    return [pdf_dir / f"{doc_id}{s}" for s in PDF_SUFFIXES] + [text_dir / f"{doc_id}{s}" for s in TEXT_SUFFIXES]


def rename_doc_files(
    old_id: str, new_id: str, pdf_dir: Path = PDF_DIR, text_dir: Path = TEXT_DIR, dry_run: bool = False
) -> list[tuple[Path, Path]]:
    """Rename the data files of ``old_id`` to ``new_id``; return the ``(src, dst)`` pairs moved.

    A file already under the new name is left alone; a file present under both names is a
    conflict (``OverrideError``) unless the two are byte-identical, in which case the old one
    is the stale copy and is left for the author to remove.
    """
    moves = []
    for src, dst in zip(doc_files(old_id, pdf_dir, text_dir), doc_files(new_id, pdf_dir, text_dir), strict=True):
        if not src.exists():
            continue
        if dst.exists():
            if src.read_bytes() != dst.read_bytes():
                raise OverrideError(f"both {src} and {dst} exist and differ; resolve by hand")
            continue
        moves.append((src, dst))
    if not dry_run:
        for src, dst in moves:
            src.rename(dst)
    return moves


def embedded_pdf(path: Path, name: str) -> bytes:
    """The bytes of the embedded file ``name`` of a PDF portfolio (pymupdf)."""
    import pymupdf

    with pymupdf.open(path) as doc:
        if doc.embfile_count() == 0:
            raise OverrideError(f"{path} has no embedded files")
        names = doc.embfile_names()
        if name not in names:
            raise OverrideError(f"{path}: no embedded file {name!r} (has {names})")
        return doc.embfile_get(name)


def replace_portfolio(
    rep: PdfReplacement, doc_id: str, pdf_dir: Path = PDF_DIR, text_dir: Path = TEXT_DIR, dry_run: bool = False
) -> tuple[str, Any]:
    """Make ``pdf_dir/{doc_id}.pdf`` the embedded PE of the portfolio and parse it.

    Returns ``(status, stats)``: status ``missing`` (no PDF on disk; nothing done),
    ``extracted`` (the portfolio was moved to ``{doc_id}.portfolio.pdf`` and its embedded file
    written in its place) or ``present`` (already done); stats is the
    :class:`cra_extract.parse.TextStats` of the PE, or None when missing or on a dry run.
    The text is re-extracted whenever its page count differs from the PDF's.
    """
    import pymupdf

    from cra_extract.fetch import write_atomic
    from cra_extract.parse import load_pages, parse_pdf

    pdf = pdf_dir / f"{doc_id}.pdf"
    kept = pdf_dir / f"{doc_id}{PORTFOLIO_SUFFIX}"
    if not pdf.exists():
        return "missing", None
    actual = sha256_file(pdf)
    if actual == rep.sha256:
        status = "present"
    elif actual == rep.original_sha256:
        data = embedded_pdf(pdf, rep.embedded_file)
        got = hashlib.sha256(data).hexdigest()
        if got != rep.sha256:
            raise OverrideError(f"{doc_id}: embedded file sha256 {got} differs from the recorded {rep.sha256}")
        if not data.startswith(b"%PDF"):
            raise OverrideError(f"{doc_id}: the embedded file is not a PDF")
        if dry_run:
            return "extracted", None
        if kept.exists() and sha256_file(kept) != rep.original_sha256:
            raise OverrideError(f"{kept} exists and is not the recorded portfolio")
        pdf.replace(kept)
        write_atomic(pdf, data)
        status = "extracted"
    else:
        raise OverrideError(
            f"{doc_id}: {pdf} has sha256 {actual}, neither the portfolio ({rep.original_sha256}) "
            f"nor its embedded PE ({rep.sha256})"
        )
    if dry_run:
        return status, None
    with pymupdf.open(pdf) as doc:
        n_pages = doc.page_count
    try:
        force = status == "extracted" or len(load_pages(doc_id, text_dir)) != n_pages
    except FileNotFoundError:
        force = True
    return status, parse_pdf(pdf, doc_id, text_dir, force=force)


def check_replacement_stats(row: dict[str, str], stats: Any) -> list[str]:
    """Differences between the manifest text columns of a row and the parsed statistics."""
    want = {
        "pages": str(stats.pages),
        "chars_per_page": f"{stats.chars_per_page:.1f}",
        "text_extractable": str(stats.text_extractable),
    }
    return [
        f"{row['doc_id']} {col}: manifest {row.get(col, '')!r}, parsed {val!r}"
        for col, val in want.items()
        if row.get(col, "") != val
    ]


# ----------------------------------------------------------------------------- evidence


def check_evidence(overrides: Overrides, text_dir: Path = TEXT_DIR) -> tuple[list[str], list[str]]:
    """Verify every document quote against its page text.

    Returns ``(problems, skipped)``: a problem is a quote not found on its page or a page
    beyond the document; skipped lists document evidence whose text file is absent (the
    text is gitignored). The text is looked up under the current doc_id first, then under
    the original one, so the check works before and after the files are renamed.
    """
    problems: list[str] = []
    skipped: list[str] = []
    for e in (*overrides.field_overrides, *overrides.exclusions, *overrides.pdf_replacements):
        for ev in e.evidence:
            if ev.doc is None:
                continue
            path = next(
                (p for p in (text_dir / f"{d}.pages.json" for d in (overrides.resolve(ev.doc), ev.doc)) if p.exists()),
                None,
            )
            label = f"{e.doc_id} evidence {ev.doc} p{ev.page}"
            if path is None:
                skipped.append(f"{label}: no page text on disk")
                continue
            with open(path, encoding="utf-8") as fh:
                pages = json.load(fh)
            if ev.page is None or ev.page > len(pages):
                problems.append(f"{label}: the document has {len(pages)} pages")
                continue
            if _collapse(ev.quote or "") not in _collapse(pages[ev.page - 1]["text"]):
                problems.append(f"{label}: quote {ev.quote!r} not on the page")
    # Entries may share one evidence list (a YAML anchor); report each finding once.
    return list(dict.fromkeys(problems)), list(dict.fromkeys(skipped))


# ----------------------------------------------------------------------------- CLI


def run_apply(
    manifest: Path = CORPUS_PATH,
    overrides_path: Path = OVERRIDES_PATH,
    pdf_dir: Path = PDF_DIR,
    text_dir: Path = TEXT_DIR,
    dry_run: bool = False,
    files: bool = True,
) -> list[CellChange]:
    """Apply the overrides file to the manifest and the data files; return the changed cells.

    Nothing is written when the pure apply or a file step fails. The manifest is rewritten
    with :func:`cra_extract.strata.write_rows`, which reproduces every unchanged cell.
    """
    from cra_extract.strata import read_rows, write_rows

    ov = load_overrides(overrides_path)
    _columns, rows = read_rows(manifest)
    after = apply_overrides(rows, ov)
    changes = changed_cells(rows, after)
    if files:
        for old_id, new_id in ov.aliases().items():
            for src, dst in rename_doc_files(old_id, new_id, pdf_dir, text_dir, dry_run=dry_run):
                print(f"{'would rename' if dry_run else 'renamed'} {src} -> {dst}")
        by_id = {r["doc_id"]: r for r in after}
        for rep in ov.pdf_replacements:
            doc_id = ov.resolve(rep.doc_id)
            status, stats = replace_portfolio(rep, doc_id, pdf_dir, text_dir, dry_run=dry_run)
            print(f"{doc_id}: replacement PDF {status}")
            if stats is not None:
                bad = check_replacement_stats(by_id[doc_id], stats)
                if bad:
                    raise OverrideError("parsed replacement disagrees with the overrides: " + "; ".join(bad))
    if not dry_run and changes:
        write_rows(list(after[0]), after, manifest)
    return changes


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.overrides", description=__doc__.split("\n\n")[0])
    p.add_argument("--overrides", type=Path, default=OVERRIDES_PATH, help="the overrides YAML")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("apply", help="apply the overrides to the manifest and the data files")
    s.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    s.add_argument("--pdf-dir", type=Path, default=PDF_DIR)
    s.add_argument("--text-dir", type=Path, default=TEXT_DIR)
    s.add_argument("--dry-run", action="store_true", help="report the changes, write nothing")
    s.add_argument("--no-files", action="store_true", help="change the manifest only, not data/pdfs or data/text")
    s = sub.add_parser("check", help="validate the YAML and verify the document quotes")
    s.add_argument("--text-dir", type=Path, default=TEXT_DIR)
    a = p.parse_args(argv)

    try:
        if a.cmd == "check":
            ov = load_overrides(a.overrides)
            problems, skipped = check_evidence(ov, a.text_dir)
            for line in skipped:
                print(f"skipped: {line}")
            for line in problems:
                print(f"problem: {line}", file=sys.stderr)
            n = len(ov.field_overrides), len(ov.exclusions), len(ov.pdf_replacements)
            print(f"{n[0]} field overrides, {n[1]} exclusions, {n[2]} pdf replacements; "
                  f"{len(problems)} evidence problems, {len(skipped)} skipped")
            return 1 if problems else 0
        changes = run_apply(a.manifest, a.overrides, a.pdf_dir, a.text_dir, dry_run=a.dry_run, files=not a.no_files)
    except OverrideError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    source_fills = [c for c in changes if c.column == EXAM_DATE_SOURCE_COLUMN and c.new != "override"]
    for c in changes:
        if c not in source_fills:
            print(f"{c.doc_id} {c.column}: {c.old!r} -> {c.new!r}")
    if source_fills:
        print(f"{EXAM_DATE_SOURCE_COLUMN}: filled on {len(source_fills)} rows from the regulator")
    verb = "would change" if a.dry_run else "changed"
    print(f"{verb} {len(changes)} cells in {a.manifest}" if changes else f"{a.manifest} already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
