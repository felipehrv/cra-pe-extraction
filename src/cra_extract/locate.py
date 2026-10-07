"""Page-wise, scope-aware section locator (design section 4, system S1, and 6.10).

The locator takes the per-page text of one Performance Evaluation and the regulator and
returns the sections listed in ``config/headings.yaml`` as :class:`~cra_extract.models.Section`
records with page and character offsets. It works line by line:

1. Table-of-contents rows are tagged with ``toc_line_patterns``; a page whose non-blank lines
   are mostly ToC rows is marked ``is_toc`` and everything on it up to its last ToC row is
   ignored (wrapped ToC entries carry no dot leader).
2. Every line is assigned a scope. The document starts in ``institution`` scope; a line
   matching a ``scope_markers`` heading switches the scope for all following lines. Scope
   never returns to institution and ``appendix`` is sticky (see the comment in the YAML).
   A page carries the scope in effect at its first line.
3. A section starts at the first candidate line matching one of its patterns, preferring a
   line whose scope equals the section's declared scope (confidence 1.0; otherwise 0.5). It
   ends at the next heading of a different section, the next scope marker or the next
   ``boundary_patterns`` heading, capped by ``max_pages``.

Character offsets index the document text defined as the pages joined by a form feed,
which is what ``parse.doc_text`` produces; :func:`doc_text` reimplements that join here so
this module does not depend on ``parse``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pandas as pd

from cra_extract.config import CORPUS_PATH, RESULTS_DIR, TEXT_DIR, load_headings
from cra_extract.models import Page, Scope, Section

FORM_FEED = "\f"
MAX_HEADING_CHARS = 100
_COLUMN_GAP = re.compile(r"\S {3,}\S")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def doc_text(pages: list[Page]) -> str:
    """Document text as pages joined by a form feed (same join as ``parse.doc_text``)."""
    return FORM_FEED.join(p.text for p in pages)


def pages_from_text(text: str) -> list[Page]:
    """Split ``pdftotext`` output on form feeds into :class:`Page` records.

    ``pdftotext`` ends every page with a form feed, so a trailing empty element is dropped.
    """
    parts = text.split(FORM_FEED)
    if len(parts) > 1 and parts[-1].strip() == "":
        parts = parts[:-1]
    return [Page(number=i + 1, text=t, chars=len(t)) for i, t in enumerate(parts)]


def normalize_line(line: str) -> str:
    """Strip a line and replace curly quotes by straight ones before pattern matching."""
    return line.strip().translate(_QUOTES)


# --------------------------------------------------------------------------- patterns


@dataclass(frozen=True)
class _Patterns:
    """Compiled heading configuration for one regulator."""

    toc: list[re.Pattern[str]]
    sections: dict[str, list[re.Pattern[str]]]
    section_scope: dict[str, str]
    max_pages: dict[str, int | None]
    boundaries: list[re.Pattern[str]]
    markers: list[tuple[str, list[re.Pattern[str]]]]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


@cache
def _patterns(regulator: str) -> _Patterns:
    cfg = load_headings()
    sections = cfg["sections"]
    return _Patterns(
        toc=_compile(cfg["toc_line_patterns"]),
        sections={name: _compile(spec.get(regulator, [])) for name, spec in sections.items()},
        section_scope={name: spec.get("scope", "institution") for name, spec in sections.items()},
        max_pages={name: spec.get("max_pages") for name, spec in sections.items()},
        boundaries=_compile(cfg.get("boundary_patterns", {}).get(regulator, [])),
        markers=[
            (scope, _compile(spec.get(regulator, [])))
            for scope, spec in cfg.get("scope_markers", {}).items()
        ],
    )


def section_names() -> list[str]:
    """Section names in the order they appear in ``config/headings.yaml``."""
    return list(load_headings()["sections"])


# --------------------------------------------------------------------------- line model


@dataclass
class _Line:
    page: int  # 1-based
    offset: int  # start offset in the document text
    text: str  # normalized (stripped, straight quotes)
    is_toc: bool = False
    skip: bool = False  # inside the ToC block of a mostly-ToC page
    scope: str = "institution"
    event: tuple[str, str] | None = None  # ("section"|"boundary"|"marker", name)


def _split_lines(pages: list[Page]) -> list[_Line]:
    lines: list[_Line] = []
    base = 0
    for page in pages:
        offset = base
        for raw in page.text.split("\n"):
            lines.append(_Line(page.number, offset, normalize_line(raw)))
            offset += len(raw) + 1
        base += len(page.text) + 1  # the joining form feed
    return lines


def is_toc_line(text: str, regulator: str = "OCC") -> bool:
    """True when a normalized line looks like a table-of-contents row."""
    return any(p.search(text) for p in _patterns(regulator).toc)


def is_heading_candidate(text: str) -> bool:
    """Short line with no currency or percent sign and no column gap (table rows)."""
    return (
        0 < len(text) <= MAX_HEADING_CHARS
        and "$" not in text
        and "%" not in text
        and not _COLUMN_GAP.search(text)
    )


def _tag_toc(lines: list[_Line], pats: _Patterns, n_pages: int) -> set[int]:
    """Tag ToC rows and the ToC block of mostly-ToC pages; return the ToC page numbers."""
    toc_pages: set[int] = set()
    for line in lines:
        line.is_toc = bool(line.text) and any(p.search(line.text) for p in pats.toc)
    for number in range(1, n_pages + 1):
        page_lines = [ln for ln in lines if ln.page == number and ln.text]
        toc_rows = [i for i, ln in enumerate(page_lines) if ln.is_toc]
        if len(toc_rows) >= 3 and len(toc_rows) >= 0.3 * len(page_lines):
            toc_pages.add(number)
            for ln in page_lines[: toc_rows[-1] + 1]:
                ln.skip = True
    return toc_pages


def _tag_events_and_scope(lines: list[_Line], pats: _Patterns) -> None:
    """Record heading events and the running scope on every line."""
    scope = "institution"
    for line in lines:
        line.scope = scope
        if line.skip or line.is_toc or not is_heading_candidate(line.text):
            continue
        marker = next(
            (name for name, regs in pats.markers if any(r.search(line.text) for r in regs)),
            None,
        )
        if marker is not None:
            if scope != "appendix":
                scope = marker
            line.scope = scope
            line.event = ("marker", marker)
            continue
        section = next(
            (name for name, regs in pats.sections.items() if any(r.search(line.text) for r in regs)),
            None,
        )
        if section is not None:
            line.event = ("section", section)
        elif any(r.search(line.text) for r in pats.boundaries):
            line.event = ("boundary", line.text)


def _analyze(pages: list[Page], regulator: str) -> tuple[list[_Line], set[int]]:
    pats = _patterns(regulator)
    lines = _split_lines(pages)
    toc_pages = _tag_toc(lines, pats, len(pages))
    _tag_events_and_scope(lines, pats)
    return lines, toc_pages


# --------------------------------------------------------------------------- public API


def tag_pages(pages: list[Page], regulator: str) -> list[Page]:
    """Return copies of ``pages`` with ``scope``, ``is_toc`` and ``chars`` filled in.

    A page's scope is the scope covering most of its non-blank lines (a scope marker often
    follows the running page header); a page that is nothing but a table of contents gets
    scope ``toc``. Use :func:`scope_at` for the exact scope of a character offset.
    """
    lines, toc_pages = _analyze(pages, regulator)
    counts: dict[int, Counter[str]] = {}
    trailing: dict[int, int] = {}
    for line in lines:
        if line.text:
            counts.setdefault(line.page, Counter())[line.scope] += 1
            if not line.skip:
                trailing[line.page] = trailing.get(line.page, 0) + 1
    tagged = []
    for page in pages:
        is_toc = page.number in toc_pages
        if is_toc and trailing.get(page.number, 0) < 3:
            scope = "toc"
        elif counts.get(page.number):
            scope = counts[page.number].most_common(1)[0][0]
        else:
            scope = "institution"
        tagged.append(page.model_copy(update={"scope": scope, "is_toc": is_toc, "chars": len(page.text)}))
    return tagged


def scope_spans(pages: list[Page], regulator: str) -> list[tuple[int, int, Scope]]:
    """Line-level scope as (char_start, char_end, scope) spans covering the document text."""
    lines, _ = _analyze(pages, regulator)
    total = len(doc_text(pages))
    spans: list[tuple[int, int, Scope]] = []
    for line in lines:
        if spans and spans[-1][2] == line.scope:
            continue
        if spans:
            spans[-1] = (spans[-1][0], line.offset, spans[-1][2])
        spans.append((line.offset, total, line.scope))  # type: ignore[arg-type]
    return spans


def scope_at(spans: list[tuple[int, int, Scope]], offset: int) -> Scope:
    """Scope of one character offset given the spans from :func:`scope_spans`."""
    for start, end, scope in spans:
        if start <= offset < end:
            return scope
    return spans[-1][2] if spans else "unknown"


def _page_end_offset(pages: list[Page], number: int, text: str) -> int:
    """Offset just past the text of page ``number`` (before the joining form feed)."""
    end = sum(len(p.text) + 1 for p in pages[:number])
    return min(end - 1, len(text))


def _page_of(pages: list[Page], offset: int) -> int:
    pos = 0
    for page in pages:
        pos += len(page.text) + 1
        if offset < pos:
            return page.number
    return pages[-1].number


def _locate_from_lines(
    name: str, doc_id: str, pages: list[Page], lines: list[_Line], text: str, regulator: str
) -> Section | None:
    pats = _patterns(regulator)
    hits = [i for i, ln in enumerate(lines) if ln.event == ("section", name)]
    if not hits:
        return None
    wanted = pats.section_scope[name]
    in_scope = [i for i in hits if lines[i].scope == wanted]
    start_idx = in_scope[0] if in_scope else hits[0]
    start = lines[start_idx]
    end_offset = len(text)
    for ln in lines[start_idx + 1 :]:
        if ln.event is not None and ln.event != ("section", name):
            end_offset = ln.offset
            break
    max_pages = pats.max_pages.get(name)
    if max_pages:
        end_offset = min(end_offset, _page_end_offset(pages, start.page + max_pages - 1, text))
    return Section(
        name=name,
        doc_id=doc_id,
        page_start=start.page,
        page_end=_page_of(pages, max(end_offset - 1, start.offset)),
        char_start=start.offset,
        char_end=end_offset,
        text=text[start.offset : end_offset],
        scope=start.scope,  # type: ignore[arg-type]
        heading_matched=start.text,
        confidence=1.0 if start.scope == wanted else 0.5,
    )


def locate_section(name: str, doc_id: str, pages: list[Page], regulator: str) -> Section | None:
    """Locate one named section, or return None when no heading matches."""
    lines, _ = _analyze(pages, regulator)
    return _locate_from_lines(name, doc_id, pages, lines, doc_text(pages), regulator)


def locate_all(doc_id: str, pages: list[Page], regulator: str) -> tuple[dict[str, Section], list[str]]:
    """Locate every section in ``headings.yaml``; return (found sections, missing names)."""
    lines, _ = _analyze(pages, regulator)
    text = doc_text(pages)
    found: dict[str, Section] = {}
    missing: list[str] = []
    for name in section_names():
        section = _locate_from_lines(name, doc_id, pages, lines, text, regulator)
        if section is None:
            missing.append(name)
        else:
            found[name] = section
    return found, missing


# --------------------------------------------------------------------------- coverage


def coverage_row(doc_id: str, regulator: str, pages: list[Page]) -> dict[str, Any]:
    """One coverage-report row: found flag and start page per section, plus page counts."""
    found, missing = locate_all(doc_id, pages, regulator)
    tagged = tag_pages(pages, regulator)
    row: dict[str, Any] = {
        "doc_id": doc_id,
        "regulator": regulator,
        "n_pages": len(pages),
        "n_toc_pages": sum(p.is_toc for p in tagged),
        "n_institution_pages": sum(p.scope == "institution" for p in tagged),
    }
    for name in section_names():
        sec = found.get(name)
        row[f"found_{name}"] = sec is not None
        row[f"page_{name}"] = sec.page_start if sec else None
        row[f"scope_{name}"] = sec.scope if sec else None
    row["n_missing"] = len(missing)
    row["missing"] = ";".join(missing)
    return row


def coverage_report(docs: list[tuple[str, str, list[Page]]]) -> pd.DataFrame:
    """Coverage table over (doc_id, regulator, pages) triples, one row per document."""
    return pd.DataFrame([coverage_row(doc_id, reg, pages) for doc_id, reg, pages in docs])


def load_pages_json(path: Path) -> list[Page]:
    """Read ``data/text/{doc_id}.pages.json`` written by ``parse``.

    Accepts a list of Page dicts, a list of page strings, or a dict with a ``pages`` key.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("pages", [])
    pages = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            pages.append(Page(number=i + 1, text=item, chars=len(item)))
        else:
            pages.append(Page(number=item.get("number", i + 1), text=item["text"], chars=len(item["text"])))
    return pages


def _docs_from_manifest(manifest: Path, text_dir: Path) -> list[tuple[str, str, list[Page]]]:
    docs = []
    with open(manifest, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            path = text_dir / f"{row['doc_id']}.pages.json"
            if path.exists():
                docs.append((row["doc_id"], row["regulator"], load_pages_json(path)))
    return docs


def _docs_from_samples(samples_dir: Path) -> list[tuple[str, str, list[Page]]]:
    docs = []
    for txt in sorted(samples_dir.glob("*.txt")):
        regulator = txt.stem.split("_")[0]
        docs.append((txt.stem, regulator, pages_from_text(txt.read_text(encoding="utf-8"))))
    return docs


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m cra_extract.locate --manifest config/corpus.csv --report results/locator_coverage.csv``."""
    ap = argparse.ArgumentParser(description="Locator coverage report, one row per document.")
    ap.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    ap.add_argument("--text-dir", type=Path, default=TEXT_DIR, help="folder with {doc_id}.pages.json")
    ap.add_argument("--samples-dir", type=Path, help="run on pdftotext .txt files instead of the manifest")
    ap.add_argument("--report", type=Path, default=RESULTS_DIR / "locator_coverage.csv")
    args = ap.parse_args(argv)

    if args.samples_dir:
        docs = _docs_from_samples(args.samples_dir)
    elif args.manifest.exists():
        docs = _docs_from_manifest(args.manifest, args.text_dir)
    else:
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    if not docs:
        print("no documents with page text found", file=sys.stderr)
        return 1
    report = coverage_report(docs)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(args.report, index=False)
    found_cols = [c for c in report.columns if c.startswith("found_")]
    summary = report.groupby("regulator")[found_cols].mean().round(3)
    print(f"{len(report)} documents; report written to {args.report}")
    print(summary.T.to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
