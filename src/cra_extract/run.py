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
  (``locator_miss`` = 1). The model may contain colons (``llama3.1:8b-instruct-q4_K_M``).
- ``llm:<backend>:<model>:whole`` (or ``--whole-document``): the whole-document ablation
  (analysis plan, "Decisions" item 3): the whole document text, pages joined, for every
  field group; ``locator_miss`` is ``na`` (not applicable).
- ``llm:<backend>:<model>:closedbook`` (or ``--closed-book``): the closed-book contamination
  baseline (analysis plan, "Leakage and contamination controls"): one ``llm.extract_closed_book``
  call per document with only the bank name and exam date, answering ``overall_rating`` and
  ``finding_present``. No page text is sent, or needed: a closed-book-only document is not
  loaded. Its values carry no quote, so ``grounding_failure`` is ``quote_not_found``.

LLM backends are a ``llm.ReplayBackend`` over ``{trace_dir}/{split}/{backend}.jsonl``; a cache
miss is an error for that (document, system) unless ``--allow-api`` is set, in which case the
live backend is called and the new trace stored. ``--effort`` is sent with every call and
recorded per trace (default ``llm.DEFAULT_EFFORT``: ``low`` for the hosted backends, none for
ollama); the replay key includes it. The gemini free-tier limits of ``config/prices.yaml``
feed ``llm.RateLimiter`` with the day boundary at midnight America/Los_Angeles; ollama reads
``OLLAMA_BASE_URL`` inside ``llm``.

Output files, all append-safe and resumable (a (doc_id, system) pair already in the
predictions file is skipped unless ``--force``, which drops and recomputes it):

- ``results/predictions_{split}.csv``: the ``FieldValue`` columns plus ``grounding_failure``
  (empty means grounded; computed with ``grounding.check`` against the tagged pages) and
  ``locator_miss`` (0, 1 or ``na``), the contract that ``evaluate.read_predictions`` reads.
- ``results/sections_{split}.csv``: one row per (document, section of ``headings.yaml``)
  with ``found``, ``page_start``, ``page_end``, ``scope``, ``chars``, ``confidence``,
  ``heading`` and the per-document ``n_aa_sections`` (see :func:`count_aa_sections`), which
  feeds the locator coverage report and the ``matches_aa_section_count`` consistency rule.
- ``results/run_log_{split}.jsonl``: one line per (document, system) with seconds, tokens,
  cost, mode, effort and the error, if any. An error in one document is logged and the run
  continues; the exit status is 1 when any error was logged. ``llm.DailyLimitExceeded``
  stops the run because every later call would fail the same way; rerunning the same
  command resumes it: written pairs are skipped and the stored traces of a half-done
  document are replayed, so no call is sent twice.

Second live run (run-to-run agreement, dev only): ``--trace-dir data/traces/dev_rerun
--results-dir results/dev_rerun --allow-api`` writes fresh traces and predictions without
replaying the first run; ``python -m cra_extract.run agreement A B`` then writes
``results/run_agreement_dev.csv`` (:func:`agreement`). ``--trace-dir`` is refused on test.

Cost projection: ``--project`` builds every prompt the run would send, counts the calls,
estimates input tokens as characters / 4, marks calls already in the traces and prices the
rest with ``config/prices.yaml``, without building a backend or calling anything
(:func:`project`); it writes ``results/cost_projection_{split}.csv``.

CLI::

    python -m cra_extract.run --split dev --systems rules,baseline,llm:gemini:gemini-3.8-flash
        [--allow-api] [--effort LEVEL] [--whole-document | --closed-book] [--hash-sample N]
        [--limit N] [--doc-id ID] [--force] [--parse-missing] [--root DIR]
        [--trace-dir DIR] [--results-dir DIR] [--project]
    python -m cra_extract.run agreement A B [--out results/run_agreement_dev.csv]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cra_extract import baseline, grounding, llm, locate, prompts, rules
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
from cra_extract.evaluate import read_predictions, values_match
from cra_extract.fetch import read_manifest, row_record, select_rows
from cra_extract.models import DocRecord, FieldValue, Page, Section, Trace
from cra_extract.parse import doc_text, load_pages, parse_pdf
from cra_extract.stats import wilson_ci

log = logging.getLogger(__name__)

LLM_BACKENDS = ("gemini", "anthropic", "openai_compat", "ollama")
# LLM input modes and the system-name suffix of the two variants.
LOCATED, WHOLE, CLOSED_BOOK = "located", "whole", prompts.CLOSED_BOOK_GROUP
MODE_SUFFIXES = {WHOLE: ":whole", CLOSED_BOOK: f":{prompts.CLOSED_BOOK_GROUP}"}
EFFORT_CHOICES = ("minimal", "low", "medium", "high", "max", "none")
LOCATOR_MISS_NA = "na"
# Free-tier day boundary (llm.RateLimiter): Google AI Studio resets daily quotas at
# midnight Pacific time; prices.yaml gemini.free_tier.reset_timezone overrides it.
GEMINI_RESET_TZ = "America/Los_Angeles"
CHARS_PER_TOKEN_PROJECTION = 4.0
AGREEMENT_COLUMNS = [
    "system", "field", "n", "n_agree", "agreement", "ci_low", "ci_high",
    "n_status_mismatch", "n_value_mismatch", "n_only_a", "n_only_b",
]
PROJECTION_COLUMNS = [
    "system", "mode", "effort", "n_docs", "n_doc_errors", "n_calls", "n_calls_cached",
    "n_calls_new", "section_chars", "prompt_chars", "input_tokens_all", "input_tokens_new",
    "output_tokens_per_call", "output_tokens_source", "output_tokens_new",
    "price_input_per_mtok", "price_output_per_mtok", "priced", "cost_usd_new",
    "requests_per_day", "days_at_free_tier",
]
PREDICTION_COLUMNS = [*FieldValue.model_fields, "grounding_failure", "locator_miss"]
SECTION_COLUMNS = [
    "doc_id", "regulator", "section", "found", "page_start", "page_end", "scope", "chars",
    "confidence", "heading", "n_aa_sections",
]
# Scopes whose headings open one assessment-area unit (FDIC prints "X AA - Full-Scope Review",
# FRB prints one MSA or MD block per assessment area).
AA_UNIT_SCOPES = ("assessment_area", "msa")


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

    def cost_projection(self, split: str) -> Path:
        return self.results_dir / f"cost_projection_{split}.csv"


@dataclass(frozen=True)
class SystemSpec:
    """One entry of ``--systems``: ``rules``, ``baseline`` or ``llm:<backend>:<model>[:mode]``.

    ``mode`` is ``located`` (the S3 and S4 systems), ``whole`` (whole-document ablation) or
    ``closedbook`` (contamination baseline); ``effort`` is the level sent with every call of
    an LLM system (``None``: no effort parameter). The effort is not part of ``name``: the
    plan fixes one effort per tier, so it is recorded per trace rather than per system.
    """

    name: str
    kind: str
    backend: str | None = None
    model: str | None = None
    mode: str = LOCATED
    effort: str | None = None


def resolve_effort(backend: str, effort: str | None) -> str | None:
    """Effort for one backend: ``None`` gives ``llm.DEFAULT_EFFORT``, ``"none"`` sends none."""
    if effort is None:
        return llm.DEFAULT_EFFORT.get(backend)
    if effort == "none":
        return None
    if effort not in EFFORT_CHOICES:
        raise ValueError(f"unknown effort {effort!r}; use one of {', '.join(EFFORT_CHOICES)}")
    return effort


def llm_spec(backend: str, model: str, mode: str = LOCATED, effort: str | None = None) -> SystemSpec:
    """``SystemSpec`` of an LLM system; the name carries the mode suffix."""
    name = f"llm:{backend}:{model}{MODE_SUFFIXES.get(mode, '')}"
    return SystemSpec(name, "llm", backend, model, mode, resolve_effort(backend, effort))


def parse_systems(spec: str | Iterable[str], effort: str | None = None) -> list[SystemSpec]:
    """Parse the ``--systems`` list; ``name`` is what ``FieldValue.system`` will carry.

    An LLM entry is ``llm:<backend>:<model>`` with an optional ``:whole`` or ``:closedbook``
    suffix; everything between the backend and the suffix is the model id, so Ollama tags
    with a colon parse. ``effort`` applies to every LLM entry (see :func:`resolve_effort`).
    """
    items = spec.split(",") if isinstance(spec, str) else list(spec)
    out: list[SystemSpec] = []
    for raw in items:
        item = raw.strip()
        if not item:
            continue
        if item in ("rules", "baseline"):
            out.append(SystemSpec(item, item))
            continue
        parts = item.split(":", 2)
        if len(parts) != 3 or parts[0] != "llm" or not parts[1] or not parts[2]:
            raise ValueError(f"unknown system {item!r}; use rules, baseline or llm:<backend>:<model>")
        if parts[1] not in LLM_BACKENDS:
            raise ValueError(f"unknown LLM backend {parts[1]!r}; known: {', '.join(LLM_BACKENDS)}")
        model, mode = parts[2], LOCATED
        for m, suffix in MODE_SUFFIXES.items():
            if model.endswith(suffix):
                model, mode = model[: -len(suffix)], m
        if not model:
            raise ValueError(f"system {item!r} has no model")
        out.append(llm_spec(parts[1], model, mode, effort))
    if not out:
        raise ValueError("no systems requested")
    if len({s.name for s in out}) != len(out):
        raise ValueError("a system is listed twice")
    return out


def with_mode(systems: Sequence[SystemSpec], mode: str) -> list[SystemSpec]:
    """Every located LLM system switched to ``mode`` (``--whole-document``, ``--closed-book``);
    rules, baseline and systems already carrying a mode suffix are kept as given."""
    out = [
        llm_spec(s.backend, s.model, mode, s.effort or "none")
        if s.kind == "llm" and s.mode == LOCATED and s.backend and s.model else s
        for s in systems
    ]
    if len({s.name for s in out}) != len(out):
        raise ValueError("a system is listed twice after applying the mode")
    return out


def gemini_free_tier() -> dict[str, Any]:
    """The ``gemini.free_tier`` block of ``config/prices.yaml`` (empty when absent)."""
    return (load_prices().get("gemini") or {}).get("free_tier") or {}


def gemini_limiter(cache_dir: Path = CACHE_DIR) -> llm.RateLimiter:
    """Free-tier limiter from the ``gemini.free_tier`` block of ``config/prices.yaml``.

    ``requests_per_minute`` and ``requests_per_day`` set the limits (null means no limit,
    logged as a warning; fill them in on run day); the daily count resets at midnight
    ``reset_timezone`` (default America/Los_Angeles, the Google AI Studio quota day) and is
    persisted under ``cache_dir``, so a halted run resumes against the same budget.
    """
    tier = gemini_free_tier()
    if tier.get("requests_per_day") is None or tier.get("requests_per_minute") is None:
        log.warning(
            "gemini free-tier requests_per_minute or requests_per_day is null in config/prices.yaml; "
            "that limit is not applied client-side"
        )
    return llm.RateLimiter(
        "gemini",
        per_minute=tier.get("requests_per_minute"),
        per_day=tier.get("requests_per_day"),
        cache_dir=cache_dir,
        reset_tz=ZoneInfo(tier.get("reset_timezone") or GEMINI_RESET_TZ),
    )


def build_llm_backend(spec: SystemSpec, split: str, allow_api: bool, paths: Paths) -> llm.Backend:
    """Replay backend over the split's traces, delegating to the live backend with ``--allow-api``.

    Both carry ``spec.effort``; the replay key includes it.
    """
    assert spec.kind == "llm" and spec.backend and spec.model
    live: llm.Backend | None = None
    if allow_api:
        kwargs: dict[str, Any] = {"effort": spec.effort}
        if spec.backend == "gemini":
            kwargs["limiter"] = gemini_limiter(paths.cache_dir)
        live = llm.make_backend(spec.backend, spec.model, **kwargs)
    return llm.ReplayBackend(
        spec.backend, split, spec.model, live=live, allow_api=allow_api, trace_dir=paths.trace_dir,
        effort=spec.effort,
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
    """Normalize an assessment-area or MSA heading to the unit it opens
    (:func:`cra_extract.locate.aa_unit_name`; empty for a marker that names no area).

    FRB headings carry the MSA or MD code ("MD 35614 (NEW YORK-...) FULL SCOPE REVIEW" and
    "DESCRIPTION OF INSTITUTION'S OPERATIONS IN MD 35614 ..." both give ``35614``); FDIC
    headings are reduced to the area name ("AUSTIN MSA AA - Full-Scope Review" and
    "DESCRIPTION OF INSTITUTION'S OPERATIONS IN AUSTIN MSA AA" both give ``austin msa``).
    """
    return locate.aa_unit_name(heading)


def count_aa_sections(pages: list[Page], regulator: str) -> int:
    """Number of distinct assessment-area units opened by scope-marker headings.

    ``scope_spans`` merges consecutive assessment-area blocks into one span (SouthStar's five
    AAs, FDIC_12299 lines 606, 935, 1246, 1403 and 1561, give one span) and FRB prints one
    ``msa`` block per AA (FieldPoint, FRB_3664588 lines 647, 1219, 1741 and 2210), so
    page-scope changes to ``assessment_area`` give 0 there. This function counts the units of
    :func:`cra_extract.locate.aa_units` over ``assessment_area`` and ``msa`` markers, the same
    rule as the lone-AA scope rule: units are deduplicated by MSA or MD code or area name, a
    marker that names no area (the bare Dallas "DESCRIPTION OF THE INSTITUTION'S OPERATIONS",
    FRB_709143_20230130 pp12, 29 and 44) counts per occurrence, and markers read in the sticky
    appendix scope (glossary prose, FDIC_12303_20220921 p21) are ignored. A single-AA document
    without such a heading counts 0; the consistency rule should compare ``n_assessment_areas``
    with ``max(1, n_aa_sections)``.
    """
    lines, _ = locate._analyze(pages, regulator)
    units, _has_state = locate.aa_units(lines, AA_UNIT_SCOPES)
    return len(units)


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


def prediction_row(
    fv: FieldValue, located: Located | None, regulator: str, locator_miss: bool | None
) -> dict[str, Any]:
    """Predictions-file row: the ``FieldValue`` columns plus grounding and locator flags.

    ``locator_miss`` ``None`` (whole-document and closed-book modes) is written as ``na``,
    which ``evaluate`` reads as no flag. Without ``located`` (closed book: no text was
    loaded) an answered value has no quote to locate and is written as ``quote_not_found``.
    """
    row = fv.model_dump(mode="json")
    row["value"] = _display(fv.value)
    failure = ""
    if fv.status == "value":
        if located is None:
            failure = "quote_not_found"
        else:
            result = grounding.check(fv, located.pages, located.sections, field_spec(fv.field), regulator)
            failure = result.failure or ""
    row["grounding_failure"] = failure
    row["locator_miss"] = LOCATOR_MISS_NA if locator_miss is None else int(locator_miss)
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


def llm_inputs(doc: DocRecord, located: Located, mode: str = LOCATED) -> list[tuple[str, dict[str, Section], bool | None]]:
    """(field group, sections sent, locator_miss) for every field group of ``fields.yaml``.

    ``located``: the group's located sections, or the whole document when none was located
    (``locator_miss`` True). ``whole``: the whole document text (pages joined with form
    feeds, ``llm.sections_from_text``) for every group, ``locator_miss`` ``None``.
    """
    text = doc_text(located.pages)
    out: list[tuple[str, dict[str, Section], bool | None]] = []
    for group_name, group in load_fields()["llm_field_groups"].items():
        if mode == WHOLE:
            out.append((group_name, llm.sections_from_text(doc.doc_id, text), None))
            continue
        secs = {n: located.sections[n] for n in group["sections"] if n in located.sections}
        if secs:
            out.append((group_name, secs, False))
        else:
            out.append((group_name, llm.sections_from_text(doc.doc_id, text), True))
    return out


def run_llm(
    doc: DocRecord,
    located: Located,
    backend: llm.Backend,
    split: str,
    trace_dir: Path,
    mode: str = LOCATED,
    system: str | None = None,
) -> SystemOutput:
    """One ``llm.extract`` call per field group over the inputs of :func:`llm_inputs`.

    ``system`` defaults to ``llm:{backend}:{model}`` with the mode suffix. A
    ``TraceMissError`` or any other exception propagates so that the (document, system)
    pair is written atomically or not at all.
    """
    if mode not in (LOCATED, WHOLE):
        raise ValueError(f"run_llm handles the located and whole modes, not {mode!r}")
    system = system or llm.system_name(backend, WHOLE if mode == WHOLE else None)
    out = SystemOutput()
    for group_name, secs, miss in llm_inputs(doc, located, mode):
        if miss:
            out.n_locator_fallbacks += 1
        values, trace = llm.extract(doc, secs, group_name, backend, split, trace_dir, system=system, mode=mode)
        out.traces.append(trace)
        out.rows += [prediction_row(fv, located, doc.regulator, miss) for fv in values]
    return out


def run_closed_book(
    doc: DocRecord, backend: llm.Backend, split: str, trace_dir: Path, system: str | None = None
) -> SystemOutput:
    """The closed-book baseline for one document: one call, no PE text (``llm.extract_closed_book``)."""
    values, trace = llm.extract_closed_book(doc, backend, split, trace_dir, system=system)
    return SystemOutput(
        rows=[prediction_row(fv, None, doc.regulator, None) for fv in values], traces=[trace]
    )


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


def doc_id_hash(doc_id: str) -> str:
    """sha256 hex digest of the doc_id (UTF-8), the ablation's ordering key."""
    return hashlib.sha256(doc_id.encode("utf-8")).hexdigest()


def hash_sample(docs: Sequence[DocRecord], n: int) -> list[DocRecord]:
    """The first ``n`` documents ordered by :func:`doc_id_hash`.

    The whole-document ablation runs on "the first 20 test doc_ids ordered by the sha256 of
    the doc_id" (analysis plan, "Decisions" item 3): ``--hash-sample 20``.
    """
    return sorted(docs, key=lambda d: doc_id_hash(d.doc_id))[: max(0, n)]


def select_documents(
    paths: Paths, split: str, doc_id: str | None = None, n_hash_sample: int | None = None
) -> list[DocRecord]:
    """Manifest records of ``split`` (``all`` for every row), optionally one document, and
    optionally only the first ``n_hash_sample`` by :func:`doc_id_hash` (in that order)."""
    df = read_manifest(paths.manifest)
    idx = select_rows(df, split=split, doc_id=doc_id)
    docs = [row_record(df.loc[i]) for i in idx]
    return hash_sample(docs, n_hash_sample) if n_hash_sample is not None else docs


def _log_entry(
    doc_id: str,
    system: str | SystemSpec,
    seconds: float,
    out: SystemOutput | None,
    error: str | None,
) -> dict[str, Any]:
    spec = system if isinstance(system, SystemSpec) else None
    return {
        "doc_id": doc_id,
        "system": spec.name if spec else system,
        "mode": spec.mode if spec and spec.kind == "llm" else None,
        "effort": spec.effort if spec else None,
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
    n_hash_sample: int | None = None,
    backend_factory: BackendFactory | None = None,
) -> RunSummary:
    """Run ``systems`` over the documents of ``split`` and write the three result files.

    ``limit`` counts documents that had work to do (pairs not yet in the predictions file),
    as ``fetch --limit`` counts rows that needed network work. Backends are built once per
    run before any document is processed, so a missing API key fails fast. A document whose
    pending systems are all closed-book is not loaded or located (and gets no sections
    rows). ``backend_factory`` defaults to :func:`build_llm_backend`.
    """
    paths = paths or Paths()
    backend_factory = backend_factory or build_llm_backend
    docs = select_documents(paths, split, doc_id, n_hash_sample)
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
        located: Located | None = None
        if any(s.mode != CLOSED_BOOK for s in pending):
            try:
                pages = load_document(doc.doc_id, paths, parse_missing)
                located = locate_document(doc.doc_id, pages, doc.regulator)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error("%s: %s", doc.doc_id, error)
                for s in pending:
                    append_run_log(log_path, _log_entry(doc.doc_id, s, time.perf_counter() - t0, None, error))
                    summary.n_errors += 1
                continue
            if doc.doc_id not in sections_done:
                sections.append(section_rows(doc, located))
                sections_done.add(doc.doc_id)

        for s in pending:
            t1 = time.perf_counter()
            try:
                if s.kind == "rules":
                    assert located is not None
                    out = run_rules(doc, located)
                elif s.kind == "baseline":
                    assert located is not None
                    out = run_baseline(doc, located)
                elif s.mode == CLOSED_BOOK:
                    out = run_closed_book(doc, backends[s.name], split, paths.trace_dir, system=s.name)
                else:
                    assert located is not None
                    out = run_llm(
                        doc, located, backends[s.name], split, paths.trace_dir, mode=s.mode, system=s.name
                    )
            except llm.DailyLimitExceeded as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error(
                    "%s %s: %s; stopping the run (rerun the same command after the quota resets "
                    "to resume: written pairs are skipped and stored traces replayed)",
                    doc.doc_id, s.name, error,
                )
                append_run_log(log_path, _log_entry(doc.doc_id, s, time.perf_counter() - t1, None, error))
                summary.n_errors += 1
                summary.halted = True
                return summary
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error("%s %s: %s", doc.doc_id, s.name, error)
                append_run_log(log_path, _log_entry(doc.doc_id, s, time.perf_counter() - t1, None, error))
                summary.n_errors += 1
                continue
            predictions.append(out.rows)
            done.add((doc.doc_id, s.name))
            summary.n_pairs_written += 1
            entry = _log_entry(doc.doc_id, s, time.perf_counter() - t1, out, None)
            append_run_log(log_path, entry)
            log.info(
                "%s %s: %d values in %.2fs, %d tokens, $%.4f",
                doc.doc_id, s.name, entry["n_values"], entry["seconds"], entry["tokens"], entry["cost_usd"],
            )
    return summary


# ----------------------------------------------------------------------------- projection


def call_prompts(
    doc: DocRecord, located: Located | None, mode: str
) -> list[tuple[str, str, str, dict[str, Any], str, int]]:
    """Every call the run would send for one document and mode, without sending it.

    Each item is (field group, system prompt, user prompt, JSON schema, prompt version,
    characters of the sections sent), built exactly as ``llm.extract`` and
    ``llm.extract_closed_book`` build them, so the prompt hash matches the stored traces.
    """
    if mode == CLOSED_BOOK:
        return [(
            prompts.CLOSED_BOOK_GROUP, prompts.CLOSED_BOOK_SYSTEM_PROMPT,
            prompts.build_closed_book_prompt(doc), prompts.build_closed_book_schema(),
            prompts.CLOSED_BOOK_PROMPT_VERSION, 0,
        )]
    if located is None:
        raise ValueError(f"{doc.doc_id}: mode {mode!r} needs the located document")
    return [
        (
            group, prompts.SYSTEM_PROMPT, prompts.build_prompt(doc, secs, group),
            prompts.build_schema(group), prompts.PROMPT_VERSION, sum(len(x.text) for x in secs.values()),
        )
        for group, secs, _ in llm_inputs(doc, located, mode)
    ]


def _output_tokens_per_call(replay: llm.ReplayBackend, spec: SystemSpec) -> tuple[float, str]:
    """Mean output tokens of the stored traces of this model, effort and mode family."""
    closed = spec.mode == CLOSED_BOOK
    tokens = [
        t.output_tokens for t in replay.stored()
        if t.model == spec.model and t.output_tokens is not None
        and (t.field_group == prompts.CLOSED_BOOK_GROUP) == closed
    ]
    if not tokens:
        return 0.0, "none"
    return sum(tokens) / len(tokens), "traces"


def project(
    split: str,
    systems: Sequence[SystemSpec],
    *,
    paths: Paths | None = None,
    limit: int | None = None,
    doc_id: str | None = None,
    parse_missing: bool = False,
    n_hash_sample: int | None = None,
    output_tokens_per_call: float | None = None,
    write: bool = True,
) -> pd.DataFrame:
    """Dry-run cost projection for the LLM systems over a split (analysis plan, "Decisions"
    item 3 and "Cost accounting"); no backend is built and nothing is called.

    For every selected document (``limit`` takes the first N) the prompts the run would send
    are built (:func:`call_prompts`); input tokens are characters / 4 of the system prompt,
    the user prompt (which holds the located sections, or the whole document) and the JSON
    schema, rounded up per call. A call whose (model, effort, prompt hash) is already in
    ``{trace_dir}/{split}/{backend}.jsonl`` is counted as cached: replay serves it at no
    cost. Output tokens per call come from ``output_tokens_per_call``, else from the mean of
    the stored traces of that model, else 0 (``output_tokens_source`` says which). Cost uses
    ``config/prices.yaml`` (null prices count as 0 and set ``priced`` False, as in the
    traces). For gemini, ``days_at_free_tier`` is new calls / ``requests_per_day``, a lower
    bound because the limiter also counts retries. Writes
    ``results/cost_projection_{split}.csv`` when ``write``.
    """
    paths = paths or Paths()
    llm_systems = [s for s in systems if s.kind == "llm" and s.backend and s.model]
    if not llm_systems:
        raise ValueError("--project needs at least one llm:<backend>:<model> system")
    docs = select_documents(paths, split, doc_id, n_hash_sample)
    if limit is not None:
        docs = docs[:limit]
    replays = {
        s.name: llm.ReplayBackend(s.backend, split, s.model, trace_dir=paths.trace_dir, effort=s.effort)
        for s in llm_systems
    }
    counts = {
        s.name: dict.fromkeys(
            ("n_docs", "n_doc_errors", "n_calls", "n_calls_cached", "section_chars", "prompt_chars",
             "input_tokens_all", "input_tokens_new"), 0,
        )
        for s in llm_systems
    }
    need_text = any(s.mode != CLOSED_BOOK for s in llm_systems)
    for doc in docs:
        located: Located | None = None
        if need_text:
            try:
                located = locate_document(doc.doc_id, load_document(doc.doc_id, paths, parse_missing), doc.regulator)
            except Exception as exc:
                log.warning("%s: %s: %s", doc.doc_id, type(exc).__name__, exc)
        for s in llm_systems:
            c = counts[s.name]
            if s.mode != CLOSED_BOOK and located is None:
                c["n_doc_errors"] += 1
                continue
            try:
                calls = call_prompts(doc, located, s.mode)
            except ValueError as exc:
                log.warning("%s %s: %s", doc.doc_id, s.name, exc)
                c["n_doc_errors"] += 1
                continue
            c["n_docs"] += 1
            for _group, system_prompt, user_prompt, schema, version, section_chars in calls:
                chars = len(system_prompt) + len(user_prompt) + len(json.dumps(schema))
                tokens = math.ceil(chars / CHARS_PER_TOKEN_PROJECTION)
                cached = replays[s.name].has(s.model, llm.prompt_hash(system_prompt, user_prompt, schema, version))
                c["n_calls"] += 1
                c["n_calls_cached"] += int(cached)
                c["section_chars"] += section_chars
                c["prompt_chars"] += chars
                c["input_tokens_all"] += tokens
                c["input_tokens_new"] += 0 if cached else tokens

    rows = []
    for s in llm_systems:
        c = counts[s.name]
        n_new = c["n_calls"] - c["n_calls_cached"]
        if output_tokens_per_call is not None:
            out_per_call, source = float(output_tokens_per_call), "argument"
        else:
            out_per_call, source = _output_tokens_per_call(replays[s.name], s)
        price = llm.model_price(s.model) or {}
        p_in, p_out = price.get("input"), price.get("output")
        output_new = round(out_per_call * n_new)
        cost = (c["input_tokens_new"] * (p_in or 0.0) + output_new * (p_out or 0.0)) / 1e6
        per_day = gemini_free_tier().get("requests_per_day") if s.backend == "gemini" else None
        rows.append({
            "system": s.name, "mode": s.mode, "effort": s.effort or "", **c,
            "n_calls_new": n_new,
            "output_tokens_per_call": round(out_per_call, 1), "output_tokens_source": source,
            "output_tokens_new": output_new,
            "price_input_per_mtok": p_in, "price_output_per_mtok": p_out,
            "priced": p_in is not None and p_out is not None,
            "cost_usd_new": round(cost, 4),
            "requests_per_day": per_day,
            "days_at_free_tier": math.ceil(n_new / per_day) if per_day else None,
        })
    df = pd.DataFrame(rows, columns=PROJECTION_COLUMNS)
    if write:
        out = paths.cost_projection(split)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False)
    return df


# ----------------------------------------------------------------------------- agreement


def _load_items(path: Path) -> tuple[dict[tuple[Any, ...], FieldValue], str]:
    """Items of one run, keyed for pairing with another run.

    A predictions CSV gives (system, doc_id, field). A traces JSONL file, or a directory
    searched recursively for ``*.jsonl``, gives (system, doc_id, field, effort,
    prompt_hash): two calls pair only when they sent the same prompt at the same effort,
    which keeps whole-document and located calls of one model apart. The last trace of a
    key wins (a later successful retry replaces an earlier failure).
    """
    path = Path(path)
    if path.is_dir() or path.suffix == ".jsonl":
        files = sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
        groups = set(load_fields()["llm_field_groups"]) | {prompts.CLOSED_BOOK_GROUP}
        items: dict[tuple[Any, ...], FieldValue] = {}
        for f in files:
            for tr in llm.read_traces(f):
                if tr.field_group not in groups:
                    continue
                for fv in llm.trace_values(tr):
                    items[(fv.system, fv.doc_id, fv.field, tr.effort, tr.prompt_hash)] = fv
        return items, "traces"
    preds, _ = read_predictions(path)
    return {(fv.system, fv.doc_id, fv.field): fv for fv in preds}, "predictions"


def _asof(items: dict[tuple[Any, ...], FieldValue], key: tuple[Any, ...]) -> Any:
    other = items.get((key[0], key[1], "total_assets_asof", *key[3:]))
    return other.value if other is not None and other.status == "value" else None


def agreement(a: Path, b: Path, out: Path | None = None) -> pd.DataFrame:
    """Run-to-run agreement per (system, field) between two runs (analysis plan, "Leakage
    and contamination controls": each S3 tier runs dev twice live).

    ``a`` and ``b`` are both predictions CSVs or both trace files or directories (see
    :func:`_load_items`). An item agrees when both runs give the same status and, when both
    are values, ``evaluate.values_match`` holds with run ``a`` in the reference slot (for
    ``total_assets_usd`` the two runs' ``total_assets_asof`` values are passed as the as-of
    dates). Items present in only one run are counted in ``n_only_a`` / ``n_only_b`` and
    left out of ``n``. ``agreement`` = n_agree / n with a Wilson 95 percent interval. Writes
    the table to ``out`` when given (``results/run_agreement_dev.csv`` from the CLI).
    """
    items_a, kind_a = _load_items(a)
    items_b, kind_b = _load_items(b)
    if kind_a != kind_b:
        raise ValueError(f"cannot compare {kind_a} ({a}) with {kind_b} ({b})")
    specs = load_fields()["fields"]
    order = {name: i for i, name in enumerate(specs)}
    tally: dict[tuple[str, str], dict[str, int]] = {}
    for key in set(items_a) | set(items_b):
        t = tally.setdefault(
            (key[0], key[2]),
            dict.fromkeys(("n", "n_agree", "n_status_mismatch", "n_value_mismatch", "n_only_a", "n_only_b"), 0),
        )
        fa, fb = items_a.get(key), items_b.get(key)
        if fb is None:
            t["n_only_a"] += 1
            continue
        if fa is None:
            t["n_only_b"] += 1
            continue
        t["n"] += 1
        if fa.status != fb.status:
            t["n_status_mismatch"] += 1
        elif fa.status == "value" and not values_match(
            fa.value, fb.value, specs.get(key[2], {}), _asof(items_a, key), _asof(items_b, key)
        ):
            t["n_value_mismatch"] += 1
        else:
            t["n_agree"] += 1
    rows = []
    for (system, fld), t in sorted(tally.items(), key=lambda kv: (kv[0][0], order.get(kv[0][1], len(order)), kv[0][1])):
        lo, hi = wilson_ci(t["n_agree"], t["n"])
        rows.append({
            "system": system, "field": fld, "n": t["n"], "n_agree": t["n_agree"],
            "agreement": t["n_agree"] / t["n"] if t["n"] else math.nan, "ci_low": lo, "ci_high": hi,
            "n_status_mismatch": t["n_status_mismatch"], "n_value_mismatch": t["n_value_mismatch"],
            "n_only_a": t["n_only_a"], "n_only_b": t["n_only_b"],
        })
    df = pd.DataFrame(rows, columns=AGREEMENT_COLUMNS)
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False, float_format="%.5f")
    return df


def agreement_main(argv: Sequence[str]) -> int:
    """``python -m cra_extract.run agreement A B [--out PATH]``."""
    ap = argparse.ArgumentParser(
        prog="cra run agreement",
        description="Run-to-run agreement of two runs (two predictions CSVs, or two trace files or directories).",
    )
    ap.add_argument("a", type=Path, help="first run: predictions CSV, traces JSONL or traces directory")
    ap.add_argument("b", type=Path, help="second run, same kind as the first")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "run_agreement_dev.csv")
    args = ap.parse_args(argv)
    try:
        df = agreement(args.a, args.b, args.out)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for r in df.itertuples():
        print(f"{r.system:45s} {r.field:28s} n={r.n:4d} agree={r.agreement:.3f} [{r.ci_low:.3f}, {r.ci_high:.3f}]")
    print(f"agreement table in {args.out}")
    return 0


# ----------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; exit 1 when any (document, system) pair failed, 2 on a usage error."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "agreement":
        return agreement_main(argv[1:])
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--split", required=True, help="manifest split: dev, test, corpus, audit or all")
    ap.add_argument(
        "--systems", required=True,
        help=f"comma list of rules, baseline, llm:<backend>:<model>[:whole|:closedbook] "
        f"(backends: {', '.join(LLM_BACKENDS)})",
    )
    ap.add_argument("--allow-api", action="store_true", help="call the live backend on a trace miss")
    ap.add_argument(
        "--effort", choices=EFFORT_CHOICES,
        help="effort sent with every LLM call and recorded per trace (default: low for hosted "
        "backends, none for ollama; 'none' sends no effort parameter)",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--whole-document", action="store_true",
        help="send the whole document for every field group (system name suffix :whole)",
    )
    mode.add_argument(
        "--closed-book", action="store_true",
        help="closed-book baseline: bank name and exam date only (system name suffix :closedbook)",
    )
    ap.add_argument("--hash-sample", type=int, metavar="N", help="only the first N documents ordered by sha256(doc_id)")
    ap.add_argument("--limit", type=int, help="process at most N documents that still have work")
    ap.add_argument("--doc-id", help="run only this document")
    ap.add_argument("--force", action="store_true", help="recompute pairs already in the predictions file")
    ap.add_argument("--parse-missing", action="store_true", help="parse data/pdfs when the page text is missing")
    ap.add_argument("--root", type=Path, help="repo root to read and write under (default: the package's)")
    ap.add_argument("--manifest", type=Path, help="manifest path (default config/corpus.csv under the root)")
    ap.add_argument("--trace-dir", type=Path, help="traces directory (default data/traces under the root); not allowed on test")
    ap.add_argument("--results-dir", type=Path, help="results directory (default results under the root)")
    ap.add_argument("--project", action="store_true", help="dry-run cost projection; calls nothing")
    ap.add_argument("--output-tokens-per-call", type=float, help="--project: output tokens per call (default: mean of stored traces)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    if args.trace_dir and args.split == "test":
        ap.error("--trace-dir is refused on test: test documents are called live once and replayed after")
    paths = Paths.under(args.root) if args.root else Paths()
    overrides = {"manifest": args.manifest, "trace_dir": args.trace_dir, "results_dir": args.results_dir}
    paths = replace(paths, **{k: v for k, v in overrides.items() if v is not None})
    try:
        systems = parse_systems(args.systems, args.effort)
        if args.whole_document:
            systems = with_mode(systems, WHOLE)
        elif args.closed_book:
            systems = with_mode(systems, CLOSED_BOOK)
    except ValueError as exc:
        ap.error(str(exc))
    if args.project:
        try:
            df = project(
                args.split, systems, paths=paths, limit=args.limit, doc_id=args.doc_id,
                parse_missing=args.parse_missing, n_hash_sample=args.hash_sample,
                output_tokens_per_call=args.output_tokens_per_call,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for r in df.itertuples():
            print(
                f"{r.system}: {r.n_docs} documents ({r.n_doc_errors} not loadable), {r.n_calls} calls "
                f"({r.n_calls_cached} in traces), {r.input_tokens_new} new input tokens, "
                f"{r.output_tokens_new} new output tokens ({r.output_tokens_source}), "
                f"projected ${r.cost_usd_new:.4f}{'' if r.priced else ' (unpriced in prices.yaml)'}"
                + (f", {r.days_at_free_tier} free-tier days" if pd.notna(r.days_at_free_tier) else "")
            )
        print(f"projection in {paths.cost_projection(args.split)}")
        return 0
    try:
        summary = run_split(
            args.split, systems, paths=paths, allow_api=args.allow_api, limit=args.limit,
            doc_id=args.doc_id, force=args.force, parse_missing=args.parse_missing,
            n_hash_sample=args.hash_sample,
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
