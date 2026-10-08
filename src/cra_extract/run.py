"""Run stage: the extraction systems over one split, writing the predictions file (design 6.1).

For every manifest document of the requested split the stage loads the page text
(``parse.load_pages``; with ``--parse-missing`` a missing text file is parsed from
``data/pdfs``), tags page scopes and locates sections (``locate``), then runs the requested
systems and appends their ``FieldValue`` rows to ``results/predictions_{split}.csv``:

- ``rules``: system S1, ``rules.extract`` over the located sections.
- ``baseline``: system S0, ``baseline.detect`` over the review section.
- ``llm:<backend>:<model>``: S3 (``gemini``, ``anthropic``, ``openai_compat``) or S4
  (``ollama``), one ``llm.extract`` call per field group of ``fields.yaml`` with the group's
  located sections as input and the whole document as fallback when none was located
  (``locator_miss`` = 1). The backend is a ``llm.ReplayBackend`` over
  ``data/traces/{split}/{backend}.jsonl``; a cache miss is an error for that (document,
  system) unless ``--allow-api`` is set, in which case the live backend is called and the new
  trace stored. The gemini free-tier limits of ``config/prices.yaml`` feed
  ``llm.RateLimiter``; ollama reads ``OLLAMA_BASE_URL`` inside ``llm``.

Output files, all append-safe and resumable (a (doc_id, system) pair already in the
predictions file is skipped unless ``--force``, which drops and recomputes it):

- ``results/predictions_{split}.csv``: the ``FieldValue`` columns plus ``grounding_failure``
  (empty means grounded; computed with ``grounding.check`` against the tagged pages) and
  ``locator_miss`` (0 or 1), the contract that ``evaluate.read_predictions`` reads.
- ``results/sections_{split}.csv``: one row per (document, section of ``headings.yaml``)
  with ``found``, ``page_start``, ``page_end``, ``scope``, ``chars``, ``confidence``,
  ``heading`` and the per-document ``n_aa_sections`` (see :func:`count_aa_sections`), which
  feeds the locator coverage report and the ``matches_aa_section_count`` consistency rule.
- ``results/run_log_{split}.jsonl``: one line per (document, system) with seconds, tokens,
  cost and the error, if any. An error in one document is logged and the run continues;
  the exit status is 1 when any error was logged. ``llm.DailyLimitExceeded`` stops the run
  because every later call would fail the same way.

CLI::

    python -m cra_extract.run --split dev --systems rules,baseline,llm:gemini:gemini-3.8-flash
        [--allow-api] [--limit N] [--doc-id ID] [--force] [--parse-missing] [--root DIR]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cra_extract import baseline, grounding, llm, locate, rules
from cra_extract.config import (
    CACHE_DIR,
    CORPUS_PATH,
    PDF_DIR,
    RESULTS_DIR,
    TEXT_DIR,
    TRACE_DIR,
    field_spec,
    load_fields,
    load_prices,
)
from cra_extract.fetch import read_manifest, row_record, select_rows
from cra_extract.models import DocRecord, FieldValue, Page, Section, Trace
from cra_extract.parse import doc_text, load_pages, parse_pdf

log = logging.getLogger(__name__)

LLM_BACKENDS = ("gemini", "anthropic", "openai_compat", "ollama")
PREDICTION_COLUMNS = [*FieldValue.model_fields, "grounding_failure", "locator_miss"]
SECTION_COLUMNS = [
    "doc_id", "regulator", "section", "found", "page_start", "page_end", "scope", "chars",
    "confidence", "heading", "n_aa_sections",
]
# Scopes whose headings open one assessment-area unit (FDIC prints "X AA - Full-Scope Review",
# FRB prints one MSA or MD block per assessment area).
AA_UNIT_SCOPES = ("assessment_area", "msa")
_CODE_RE = re.compile(r"\b(\d{4,5})\b")
_AA_PREFIX_RE = re.compile(r"^description of (?:the )?institution'?s operations in (?:the )?", re.IGNORECASE)
_AA_SUFFIX_RE = re.compile(r"\s*(?:[-–:]\s*)?(?:full|limited)[- ]scope review\s*$", re.IGNORECASE)
_AA_WORD_RE = re.compile(r"\b(?:aa|assessment area)s?\b", re.IGNORECASE)


# ----------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Paths:
    """Input and output locations; ``under(root)`` rebases them for tests and ``--root``."""

    manifest: Path = CORPUS_PATH
    pdf_dir: Path = PDF_DIR
    text_dir: Path = TEXT_DIR
    trace_dir: Path = TRACE_DIR
    cache_dir: Path = CACHE_DIR
    results_dir: Path = RESULTS_DIR

    @classmethod
    def under(cls, root: Path) -> Paths:
        root = Path(root)
        return cls(
            manifest=root / "config" / "corpus.csv",
            pdf_dir=root / "data" / "pdfs",
            text_dir=root / "data" / "text",
            trace_dir=root / "data" / "traces",
            cache_dir=root / "data" / "cache",
            results_dir=root / "results",
        )

    def predictions(self, split: str) -> Path:
        return self.results_dir / f"predictions_{split}.csv"

    def sections(self, split: str) -> Path:
        return self.results_dir / f"sections_{split}.csv"

    def run_log(self, split: str) -> Path:
        return self.results_dir / f"run_log_{split}.jsonl"


@dataclass(frozen=True)
class SystemSpec:
    """One entry of ``--systems``: ``rules``, ``baseline`` or ``llm:<backend>:<model>``."""

    name: str
    kind: str
    backend: str | None = None
    model: str | None = None


def parse_systems(spec: str | Iterable[str]) -> list[SystemSpec]:
    """Parse the ``--systems`` list; ``name`` is what ``FieldValue.system`` will carry."""
    items = spec.split(",") if isinstance(spec, str) else list(spec)
    out: list[SystemSpec] = []
    for raw in items:
        item = raw.strip()
        if not item:
            continue
        if item in ("rules", "baseline"):
            out.append(SystemSpec(item, item))
            continue
        parts = item.split(":")
        if len(parts) != 3 or parts[0] != "llm" or not parts[1] or not parts[2]:
            raise ValueError(f"unknown system {item!r}; use rules, baseline or llm:<backend>:<model>")
        if parts[1] not in LLM_BACKENDS:
            raise ValueError(f"unknown LLM backend {parts[1]!r}; known: {', '.join(LLM_BACKENDS)}")
        out.append(SystemSpec(item, "llm", parts[1], parts[2]))
    if not out:
        raise ValueError("no systems requested")
    if len({s.name for s in out}) != len(out):
        raise ValueError("a system is listed twice")
    return out


def gemini_limiter(cache_dir: Path = CACHE_DIR) -> llm.RateLimiter:
    """Free-tier limiter from the ``gemini.free_tier`` block of ``config/prices.yaml``.

    Null limits (the current file) mean no limit; fill them in on run day.
    """
    tier = (load_prices().get("gemini") or {}).get("free_tier") or {}
    return llm.RateLimiter(
        "gemini",
        per_minute=tier.get("requests_per_minute"),
        per_day=tier.get("requests_per_day"),
        cache_dir=cache_dir,
    )


def build_llm_backend(spec: SystemSpec, split: str, allow_api: bool, paths: Paths) -> llm.Backend:
    """Replay backend over the split's traces, delegating to the live backend with ``--allow-api``."""
    assert spec.kind == "llm" and spec.backend and spec.model
    live: llm.Backend | None = None
    if allow_api:
        kwargs: dict[str, Any] = {}
        if spec.backend == "gemini":
            kwargs["limiter"] = gemini_limiter(paths.cache_dir)
        live = llm.make_backend(spec.backend, spec.model, **kwargs)
    return llm.ReplayBackend(
        spec.backend, split, spec.model, live=live, allow_api=allow_api, trace_dir=paths.trace_dir
    )


BackendFactory = Callable[[SystemSpec, str, bool, Paths], llm.Backend]


# ----------------------------------------------------------------------------- documents


def load_document(doc_id: str, paths: Paths, parse_missing: bool = False) -> list[Page]:
    """Pages of one document from ``text_dir``; parse the PDF first when allowed and needed."""
    try:
        return load_pages(doc_id, paths.text_dir)
    except FileNotFoundError:
        pdf = paths.pdf_dir / f"{doc_id}.pdf"
        if not parse_missing:
            raise FileNotFoundError(
                f"{doc_id}: no page text in {paths.text_dir}; use --parse-missing to parse {pdf}"
            ) from None
        if not pdf.exists():
            raise FileNotFoundError(f"{doc_id}: no page text and no PDF at {pdf}") from None
        parse_pdf(pdf, doc_id, paths.text_dir)
        return load_pages(doc_id, paths.text_dir)


@dataclass
class Located:
    """Tagged pages, located sections and the per-document assessment-area count."""

    pages: list[Page]
    sections: dict[str, Section]
    missing: list[str]
    n_aa_sections: int


def aa_unit_key(heading: str) -> str:
    """Normalize an assessment-area or MSA heading to the unit it opens.

    FRB headings carry the MSA or MD code ("MD 35614 (NEW YORK-...) FULL SCOPE REVIEW" and
    "DESCRIPTION OF INSTITUTION'S OPERATIONS IN MD 35614 ..." both give ``35614``); FDIC
    headings are reduced to the area name ("AUSTIN MSA AA - Full-Scope Review" and
    "DESCRIPTION OF INSTITUTION'S OPERATIONS IN AUSTIN MSA AA" both give ``austin msa``).
    """
    text = locate.normalize_line(heading)
    m = _CODE_RE.search(text)
    if m:
        return m.group(1)
    text = _AA_PREFIX_RE.sub("", text)
    text = _AA_SUFFIX_RE.sub("", text)
    text = _AA_WORD_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -–:").casefold()


def count_aa_sections(pages: list[Page], regulator: str) -> int:
    """Number of distinct assessment-area units opened by scope-marker headings.

    ``locate`` exposes no public list of heading events, and its public views do not
    carry the count: ``scope_spans`` merges consecutive assessment-area blocks into one span
    (SouthStar's five AAs, FDIC_12299 lines 606, 935, 1246, 1403 and 1561, give one span) and
    FRB prints one ``msa`` block per AA (FieldPoint, FRB_3664588 lines 647, 1219, 1741 and
    2210), so page-scope changes to ``assessment_area`` give 0 there. This function therefore
    reads the marker events of ``locate._analyze`` and counts distinct :func:`aa_unit_key`
    values over ``assessment_area`` and ``msa`` markers. A single-AA document has no such
    heading and counts 0; the consistency rule should compare ``n_assessment_areas`` with
    ``max(1, n_aa_sections)``. Verified on the FDIC and FRB samples only; OCC multi-AA
    layouts and FRB non-metropolitan areas (tagged ``state`` by the locator) are to be
    checked on the 100-document locator run (design 6.10).
    """
    lines, _ = locate._analyze(pages, regulator)
    keys = {
        aa_unit_key(ln.text)
        for ln in lines
        if ln.event is not None and ln.event[0] == "marker" and ln.event[1] in AA_UNIT_SCOPES
    }
    return len(keys)


def locate_document(doc_id: str, pages: list[Page], regulator: str) -> Located:
    """Tag page scopes, locate every section and count the assessment-area units."""
    tagged = locate.tag_pages(pages, regulator)
    sections, missing = locate.locate_all(doc_id, tagged, regulator)
    return Located(tagged, sections, missing, count_aa_sections(pages, regulator))


def section_rows(doc: DocRecord, located: Located) -> list[dict[str, Any]]:
    """One ``sections_{split}.csv`` row per section name of ``headings.yaml``."""
    rows = []
    for name in locate.section_names():
        sec = located.sections.get(name)
        rows.append({
            "doc_id": doc.doc_id,
            "regulator": doc.regulator,
            "section": name,
            "found": sec is not None,
            "page_start": sec.page_start if sec else "",
            "page_end": sec.page_end if sec else "",
            "scope": sec.scope if sec else "",
            "chars": len(sec.text) if sec else 0,
            "confidence": sec.confidence if sec else "",
            "heading": sec.heading_matched or "" if sec else "",
            "n_aa_sections": located.n_aa_sections,
        })
    return rows


# ----------------------------------------------------------------------------- systems


def field_sections() -> dict[str, list[str]]:
    """Field name to the sections its LLM field group reads (``fields.yaml``)."""
    out: dict[str, list[str]] = {}
    for group in load_fields()["llm_field_groups"].values():
        for name in group["fields"]:
            out[name] = list(group["sections"])
    return out


def _display(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(v) for v in value)
    return "" if value is None else value


def prediction_row(fv: FieldValue, located: Located, regulator: str, locator_miss: bool) -> dict[str, Any]:
    """Predictions-file row: the ``FieldValue`` columns plus grounding and locator flags."""
    row = fv.model_dump(mode="json")
    row["value"] = _display(fv.value)
    failure = ""
    if fv.status == "value":
        result = grounding.check(fv, located.pages, located.sections, field_spec(fv.field), regulator)
        failure = result.failure or ""
    row["grounding_failure"] = failure
    row["locator_miss"] = int(locator_miss)
    return {col: ("" if row.get(col) is None else row.get(col)) for col in PREDICTION_COLUMNS}


@dataclass
class SystemOutput:
    """Rows and accounting for one (document, system) run."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    traces: list[Trace] = field(default_factory=list)
    n_locator_fallbacks: int = 0

    @property
    def input_tokens(self) -> int:
        return sum(t.input_tokens or 0 for t in self.traces)

    @property
    def output_tokens(self) -> int:
        return sum(t.output_tokens or 0 for t in self.traces)

    @property
    def cost_usd(self) -> float:
        return round(sum(t.cost_usd or 0.0 for t in self.traces), 6)


def _rows_for(values: list[FieldValue], located: Located, regulator: str) -> list[dict[str, Any]]:
    secs = field_sections()
    rows = []
    for fv in values:
        wanted = secs.get(fv.field, [])
        miss = bool(wanted) and not any(n in located.sections for n in wanted)
        rows.append(prediction_row(fv, located, regulator, miss))
    return rows


def run_rules(doc: DocRecord, located: Located) -> SystemOutput:
    """System S1 over the located sections."""
    values = rules.extract(doc.doc_id, located.pages, located.sections, doc.regulator)
    return SystemOutput(rows=_rows_for(values, located, doc.regulator))


def run_baseline(doc: DocRecord, located: Located) -> SystemOutput:
    """System S0 over the review section (abstains when it was not located)."""
    review = located.sections.get("illegal_credit_practices_review")
    values = baseline.detect(doc.doc_id, review, located.pages, doc.regulator)
    return SystemOutput(rows=_rows_for(values, located, doc.regulator))


def run_llm(
    doc: DocRecord, located: Located, backend: llm.Backend, split: str, trace_dir: Path
) -> SystemOutput:
    """One ``llm.extract`` call per field group; whole document when no section was located.

    A ``TraceMissError`` or any other exception propagates so that the (document, system)
    pair is written atomically or not at all.
    """
    out = SystemOutput()
    text = doc_text(located.pages)
    for group_name, group in load_fields()["llm_field_groups"].items():
        secs = {n: located.sections[n] for n in group["sections"] if n in located.sections}
        fallback = not secs
        if fallback:
            secs = llm.sections_from_text(doc.doc_id, text)
            out.n_locator_fallbacks += 1
        values, trace = llm.extract(doc, secs, group_name, backend, split, trace_dir)
        out.traces.append(trace)
        out.rows += [prediction_row(fv, located, doc.regulator, fallback) for fv in values]
    return out


# ----------------------------------------------------------------------------- CSV tables


class CsvTable:
    """Append-safe CSV with a fixed set of columns.

    An existing file's header fixes the column order; a header with a different column
    set is refused so that two contracts never share one file.
    """

    def __init__(self, path: Path, columns: Sequence[str]) -> None:
        self.path = Path(path)
        self.columns = list(columns)
        if self.path.exists() and self.path.stat().st_size > 0:
            with open(self.path, encoding="utf-8", newline="") as fh:
                header = next(csv.reader(fh), [])
            if set(header) != set(self.columns):
                raise RuntimeError(
                    f"{self.path}: columns {header} differ from {self.columns}; move the file aside"
                )
            self.columns = header

    def rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def keys(self, cols: Sequence[str]) -> set[tuple[str, ...]]:
        """Distinct tuples of ``cols`` present in the file."""
        return {tuple(r[c] for c in cols) for r in self.rows()}

    def drop(self, cols: Sequence[str], keys: set[tuple[str, ...]]) -> int:
        """Rewrite the file without the rows whose ``cols`` tuple is in ``keys``."""
        if not keys or not self.path.exists():
            return 0
        kept = [r for r in self.rows() if tuple(r[c] for c in cols) not in keys]
        dropped = len(self.rows()) - len(kept)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.columns)
            writer.writeheader()
            writer.writerows(kept)
        return dropped

    def append(self, rows: Iterable[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.columns, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerows(rows)


def append_run_log(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line to ``run_log_{split}.jsonl``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_run_log(path: Path) -> list[dict[str, Any]]:
    """Every entry of a run log (missing file gives an empty list)."""
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ----------------------------------------------------------------------------- driver


@dataclass
class RunSummary:
    """Counts returned by :func:`run_split`."""

    n_selected: int = 0
    n_processed: int = 0
    n_skipped: int = 0
    n_pairs_written: int = 0
    n_errors: int = 0
    halted: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.n_errors or self.n_selected == 0 else 0


def select_documents(
    paths: Paths, split: str, doc_id: str | None = None
) -> list[DocRecord]:
    """Manifest records of ``split`` (``all`` for every row), optionally one document."""
    df = read_manifest(paths.manifest)
    idx = select_rows(df, split=split, doc_id=doc_id)
    return [row_record(df.loc[i]) for i in idx]


def _log_entry(doc_id: str, system: str, seconds: float, out: SystemOutput | None, error: str | None) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "system": system,
        "seconds": round(seconds, 3),
        "tokens": out.input_tokens + out.output_tokens if out else 0,
        "input_tokens": out.input_tokens if out else 0,
        "output_tokens": out.output_tokens if out else 0,
        "cost_usd": out.cost_usd if out else 0.0,
        "n_values": len(out.rows) if out else 0,
        "n_locator_fallbacks": out.n_locator_fallbacks if out else 0,
        "error": error,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def run_split(
    split: str,
    systems: Sequence[SystemSpec],
    *,
    paths: Paths | None = None,
    allow_api: bool = False,
    limit: int | None = None,
    doc_id: str | None = None,
    force: bool = False,
    parse_missing: bool = False,
    backend_factory: BackendFactory = build_llm_backend,
) -> RunSummary:
    """Run ``systems`` over the documents of ``split`` and write the three result files.

    ``limit`` counts documents that had work to do (pairs not yet in the predictions file),
    as ``fetch --limit`` counts rows that needed network work. Backends are built once per
    run before any document is processed, so a missing API key fails fast.
    """
    paths = paths or Paths()
    docs = select_documents(paths, split, doc_id)
    summary = RunSummary(n_selected=len(docs))
    if not docs:
        log.error("no documents selected for split=%s doc_id=%s", split, doc_id)
        return summary

    backends = {
        s.name: backend_factory(s, split, allow_api, paths) for s in systems if s.kind == "llm"
    }
    predictions = CsvTable(paths.predictions(split), PREDICTION_COLUMNS)
    sections = CsvTable(paths.sections(split), SECTION_COLUMNS)
    log_path = paths.run_log(split)

    if force:
        targets = docs[:limit] if limit else docs
        pairs = {(d.doc_id, s.name) for d in targets for s in systems}
        n = predictions.drop(("doc_id", "system"), pairs)
        sections.drop(("doc_id",), {(d.doc_id,) for d in targets})
        log.info("--force: dropped %d prediction rows for %d documents", n, len(targets))
    done = predictions.keys(("doc_id", "system"))
    sections_done = {k[0] for k in sections.keys(("doc_id",))}

    for doc in docs:
        pending = [s for s in systems if (doc.doc_id, s.name) not in done]
        if not pending:
            summary.n_skipped += 1
            continue
        if limit is not None and summary.n_processed >= limit:
            break
        summary.n_processed += 1
        t0 = time.perf_counter()
        try:
            pages = load_document(doc.doc_id, paths, parse_missing)
            located = locate_document(doc.doc_id, pages, doc.regulator)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.error("%s: %s", doc.doc_id, error)
            for s in pending:
                append_run_log(log_path, _log_entry(doc.doc_id, s.name, time.perf_counter() - t0, None, error))
                summary.n_errors += 1
            continue
        if doc.doc_id not in sections_done:
            sections.append(section_rows(doc, located))
            sections_done.add(doc.doc_id)

        for s in pending:
            t1 = time.perf_counter()
            try:
                if s.kind == "rules":
                    out = run_rules(doc, located)
                elif s.kind == "baseline":
                    out = run_baseline(doc, located)
                else:
                    out = run_llm(doc, located, backends[s.name], split, paths.trace_dir)
            except llm.DailyLimitExceeded as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error("%s %s: %s; stopping the run", doc.doc_id, s.name, error)
                append_run_log(log_path, _log_entry(doc.doc_id, s.name, time.perf_counter() - t1, None, error))
                summary.n_errors += 1
                summary.halted = True
                return summary
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error("%s %s: %s", doc.doc_id, s.name, error)
                append_run_log(log_path, _log_entry(doc.doc_id, s.name, time.perf_counter() - t1, None, error))
                summary.n_errors += 1
                continue
            predictions.append(out.rows)
            done.add((doc.doc_id, s.name))
            summary.n_pairs_written += 1
            entry = _log_entry(doc.doc_id, s.name, time.perf_counter() - t1, out, None)
            append_run_log(log_path, entry)
            log.info(
                "%s %s: %d values in %.2fs, %d tokens, $%.4f",
                doc.doc_id, s.name, entry["n_values"], entry["seconds"], entry["tokens"], entry["cost_usd"],
            )
    return summary


# ----------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; exit 1 when any (document, system) pair failed, 2 on a usage error."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--split", required=True, help="manifest split: dev, test, corpus, audit or all")
    ap.add_argument(
        "--systems", required=True,
        help=f"comma list of rules, baseline, llm:<backend>:<model> (backends: {', '.join(LLM_BACKENDS)})",
    )
    ap.add_argument("--allow-api", action="store_true", help="call the live backend on a trace miss")
    ap.add_argument("--limit", type=int, help="process at most N documents that still have work")
    ap.add_argument("--doc-id", help="run only this document")
    ap.add_argument("--force", action="store_true", help="recompute pairs already in the predictions file")
    ap.add_argument("--parse-missing", action="store_true", help="parse data/pdfs when the page text is missing")
    ap.add_argument("--root", type=Path, help="repo root to read and write under (default: the package's)")
    ap.add_argument("--manifest", type=Path, help="manifest path (default config/corpus.csv under the root)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    paths = Paths.under(args.root) if args.root else Paths()
    if args.manifest:
        paths = Paths(**{**paths.__dict__, "manifest": args.manifest})
    try:
        systems = parse_systems(args.systems)
    except ValueError as exc:
        ap.error(str(exc))
    try:
        summary = run_split(
            args.split, systems, paths=paths, allow_api=args.allow_api, limit=args.limit,
            doc_id=args.doc_id, force=args.force, parse_missing=args.parse_missing,
        )
    except (llm.LLMError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"split={args.split}: {summary.n_selected} documents selected, {summary.n_processed} processed, "
        f"{summary.n_skipped} already done, {summary.n_pairs_written} (doc, system) pairs written, "
        f"{summary.n_errors} errors{' (halted)' if summary.halted else ''}; "
        f"predictions in {paths.predictions(args.split)}"
    )
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
