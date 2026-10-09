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
   ``boundary_patterns`` heading, capped by ``max_pages``. Trailing page furniture (blank
   lines, page numbers, "Charter Number" lines, security markings, running headers) is
   trimmed so that ``page_end`` is the page of the last content line, not of the next heading.
   Running headers and footers are found per document by :func:`running_lines` (lines that
   recur at the top or bottom of many pages). A section whose spec sets ``end_at_table_run``
   also ends where a one-cell-per-line table begins (:func:`table_run_start`): pymupdf can
   print a table after the review paragraph on the same page.

Character offsets index the document text defined as the pages joined by a form feed,
as defined by ``parse.doc_text``; this module imports that function and the separator so
there is a single definition of the offset convention.
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
from cra_extract.parse import PAGE_SEPARATOR as FORM_FEED
from cra_extract.parse import doc_text

MAX_HEADING_CHARS = 100
TOC_TITLE = re.compile(r"^table of contents$", re.IGNORECASE)
TOC_TITLE_RANK = 12  # a ToC title sits among the first non-blank lines of its page
# A criterion name followed by its review period is a row of the FRB "Performance Criterion /
# Time Period" table in the scope section ("LTD Ratio" then "March 31, 2018 - March 31, 2023",
# FRB_1007051_20230515 page 3), not the heading of that criterion's section.
PERIOD_ROW = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r" \d{1,2}, \d{4} *(-|\u2013|through|to) "
)
# A bullet glyph on a line of its own (bullet, black small square, white bullet, middle dot,
# asterisk, hyphen, or the Private Use Area where pdftotext puts Wingdings bullets): the line
# after it is a list item ("Net LTD Ratio" in the scope list, FRB_3350658_20230612 page 3).
# The column header just above the first row of that table, when the row is followed by the
# products reviewed instead of a period ("Time Period" then "Net LTD Ratio", FRB_3350658_20230612
# page 3).
PERIOD_HEADER = re.compile(r"^(Time|Evaluation|Review) Period$", re.IGNORECASE)
LONE_BULLET = re.compile(r"^[\u2022\u25aa\u25e6\u00b7*\-\uf000-\uf8ff]$")
_HEADING_END = re.compile(r"[.!?:;,]$")  # a line ending a sentence does not wrap a heading
COLUMN_GAP = re.compile(r"\S {3,}\S")  # pdftotext column gap: a table row or a running header
FURNITURE_PATTERNS = [  # page furniture, matched against a normalized line
    re.compile(r"^\d{1,3}$"),  # bare page number
    re.compile(r"^[ivxl]{1,6}$", re.IGNORECASE),  # roman page number on front matter
    re.compile(r"^(page )?\d+ of \d+$", re.IGNORECASE),  # "Page 11 of 25", FRB "8 of 29"
    re.compile(r"^charter number:? \d+$", re.IGNORECASE),
    # security markings printed on every page: "CONTROLLED//FDIC INTERNAL ONLY" (FDIC_11777_20240924
    # p14), "NONCONFIDENTIAL // EXTERNAL" (FRB_426141_20221212 p13), "PUBLIC/OFFICIAL RELEASE //
    # EXTERNAL" (FRB_680130_20230724 p10), "RESTRICTED FR // FRSONLY", "NONPUBLIC//FDIC BUSINESS"
    re.compile(
        r"^(nonconfidential|controlled|nonpublic|public/official release|restricted fr)"
        r" ?// ?[a-z /]+$",
        re.IGNORECASE,
    ),
]
# Running headers and footers (running_lines): a line among the first RUNNING_TOP or last
# RUNNING_BOTTOM non-blank lines of a page that recurs on at least RUNNING_MIN_PAGES pages and
# on at least RUNNING_SHARE of the pages of the document.
RUNNING_TOP = 6
RUNNING_BOTTOM = 4
RUNNING_MIN_PAGES = 3
RUNNING_SHARE = 0.3
# A table printed one cell per line (table_run_start): TABLE_RUN_LINES consecutive non-blank
# lines of at most TABLE_CELL_CHARS characters. Prose lines in the PEs are 60 to 110 long.
TABLE_RUN_LINES = 6
TABLE_CELL_CHARS = 25
TABLE_TITLE_MAX_LINES = 4  # short lines directly above the first cell that go with the table
TABLE_TITLE_MAX_CHARS = 80
# Curly quotes, plus the glyphs some FRB (Chicago) PEs print in place of ASCII: no-break space
# (U+00A0, U+202F) between every word, the Unicode hyphen (U+2010, U+2011) in "Loan-to-Deposit"
# and the modifier letter prime (U+02B9) as apostrophe in "BANK'S CRA RATING"
# (FRB_180845_20241104 pages 1 to 3). Each maps to one character, so line lengths are kept.
_QUOTES = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "\u02b9": "'",
        "\u00a0": " ",
        "\u202f": " ",
        "\u2010": "-",
        "\u2011": "-",
    }
)


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


def is_furniture_line(text: str) -> bool:
    """True for a normalized line that is a page number, "Page x of y", a charter-number
    footer or a security marking."""
    return any(p.search(text) for p in FURNITURE_PATTERNS)


def running_lines(pages: list[Page]) -> frozenset[str]:
    """Normalized lines that are running headers or footers of this document.

    A line counts when it is among the first ``RUNNING_TOP`` or last ``RUNNING_BOTTOM``
    non-blank lines of at least ``RUNNING_MIN_PAGES`` pages and of at least ``RUNNING_SHARE``
    of all pages. FRB PEs print the bank name, "CRA Performance Evaluation", the city and the
    exam date at the top of every page (FRB_137447_20230724 p10), which pymupdf emits as
    separate lines that no furniture pattern can describe.
    """
    counts: Counter[str] = Counter()
    for page in pages:
        kept = [t for t in (normalize_line(raw) for raw in page.text.split("\n")) if t]
        counts.update(set(kept[:RUNNING_TOP]) | set(kept[-RUNNING_BOTTOM:]))
    need = max(RUNNING_MIN_PAGES, RUNNING_SHARE * len(pages))
    return frozenset(t for t, n in counts.items() if n >= need)


# --------------------------------------------------------------------------- patterns


@dataclass(frozen=True)
class _Patterns:
    """Compiled heading configuration for one regulator."""

    toc: list[re.Pattern[str]]
    sections: dict[str, list[re.Pattern[str]]]
    section_scope: dict[str, str]
    max_pages: dict[str, int | None]
    end_at_table_run: dict[str, bool]
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
        end_at_table_run={
            name: bool(spec.get("end_at_table_run")) for name, spec in sections.items()
        },
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
    rank: int = 0  # index among the non-blank lines of its page (running headers rank low)
    heading: str = ""  # the joined text when a heading is wrapped over two lines
    running: bool = False  # a running header or footer of this document (running_lines)


def _split_lines(pages: list[Page]) -> list[_Line]:
    lines: list[_Line] = []
    base = 0
    for page in pages:
        offset = base
        rank = 0
        for raw in page.text.split("\n"):
            line = _Line(page.number, offset, normalize_line(raw), rank=rank)
            lines.append(line)
            offset += len(raw) + 1
            rank += bool(line.text)
        base += len(page.text) + 1  # the joining form feed
    return lines


def is_toc_line(text: str, regulator: str = "OCC") -> bool:
    """True when a normalized line looks like a table-of-contents row."""
    return any(p.search(text) for p in _patterns(regulator).toc)


def _is_prose(text: str) -> bool:
    """A running-text line: at least 60 characters and 9 words, 3 of them lowercase, and no
    column gap (a capitalized ToC entry such as "APPENDIX D - LIMITED-SCOPE REVIEW ASSESSMENT
    AREA LENDING TABLES" is not prose)."""
    words = text.split()
    return (
        len(text) >= 60
        and len(words) >= 9
        and sum(w.islower() for w in words) >= 3
        and not COLUMN_GAP.search(text)
    )


def is_heading_candidate(text: str) -> bool:
    """Short line with no currency or percent sign and no column gap (table rows)."""
    return (
        0 < len(text) <= MAX_HEADING_CHARS
        and "$" not in text
        and "%" not in text
        and not COLUMN_GAP.search(text)
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
            # rows after the last leader row belong to the ToC unless prose follows them
            # ("APPENDIX D - GLOSSARY" without a leader, FRB_623454_20250310 page 2)
            tail = page_lines[toc_rows[-1] + 1 :]
            prose_after = any(_is_prose(ln.text) for ln in tail)
            last = toc_rows[-1] + 1 if prose_after else len(page_lines)
            for ln in page_lines[:last]:
                ln.skip = True
            continue
        # A ToC whose page numbers sit in a separate text column carries no leaders: a page
        # titled "Table of Contents" near its top with no prose line after the title is a ToC
        # page as a whole (FRB_925653_20240916 page 2, OCC_11312_20220906 page 2).
        title = next(
            (i for i, ln in enumerate(page_lines[:TOC_TITLE_RANK]) if TOC_TITLE.search(ln.text)),
            None,
        )
        if title is not None and not any(_is_prose(ln.text) for ln in page_lines[title + 1 :]):
            toc_pages.add(number)
            for ln in page_lines:
                ln.skip = True
    return toc_pages


def _classify(text: str, pats: _Patterns) -> tuple[str, str] | None:
    """Heading event of one normalized heading text: a scope marker, a section or a boundary."""
    marker = next(
        (name for name, regs in pats.markers if any(r.search(text) for r in regs)),
        None,
    )
    if marker is not None:
        return ("marker", marker)
    section = next(
        (name for name, regs in pats.sections.items() if any(r.search(text) for r in regs)),
        None,
    )
    if section is not None:
        return ("section", section)
    if any(r.search(text) for r in pats.boundaries):
        return ("boundary", text)
    return None


def _next_text(lines: list[_Line], i: int, step: int = 1) -> int | None:
    """Index of the next (``step=-1``: previous) non-blank line on the page of line ``i``."""
    j = i + step
    while 0 <= j < len(lines) and lines[j].page == lines[i].page:
        if lines[j].text:
            return j
        j += step
    return None


def _tag_events_and_scope(lines: list[_Line], pats: _Patterns) -> None:
    """Record heading events and the running scope on every line.

    A heading that pdftotext wrapped over two lines ("FAIR LENDING OR OTHER ILLEGAL CREDIT
    PRACTICES" then "REVIEW") is matched on the two lines joined by a space when the first
    line matches nothing on its own; the event sits on the first line and the second line is
    consumed. Page numbers, running headers and one-word lines never start a pair ("LENDING"
    over "TEST" is a column header of the FRB rating table, FRB_1864197_20230117 page 3), and a
    section heading needs three words on its first line ("Assessment Area" over
    "Concentration" is a table header, FDIC_10635_20240122 page 7). Scope markers are never
    joined: wrapped area names are cells of the FRB lending tables ("MSA 36740 (Orlando-" over
    "Kissimmee-Sanford, FL)", FRB_3664588_20240116 page 12), and a false marker would move
    every later line out of institution scope. A
    section heading followed by a review period ("LTD Ratio" then "March 31, 2018 - March 31,
    2023") or preceded by the table's "Time Period" column header is a row of the FRB criterion
    table, and one preceded by a lone bullet glyph is a list item; neither records an event.
    """
    scope = "institution"
    consumed: set[int] = set()
    for i, line in enumerate(lines):
        line.scope = scope
        if i in consumed or line.skip or line.is_toc or not is_heading_candidate(line.text):
            continue
        event = _classify(line.text, pats)
        nxt = _next_text(lines, i)
        above = lines[i - 1] if i > 0 and lines[i - 1].page == line.page else None
        joinable = not (
            line.running
            or _HEADING_END.search(line.text)
            or is_furniture_line(line.text)
            or len(line.text.split()) < 2
            # the first line of a wrapped heading starts a block: the line above is blank,
            # furniture or a finished sentence, never the previous line of a paragraph. Without
            # this the last line of a review paragraph joins the rated-area title after it
            # ("... Examiners did not" / "identify any discriminatory or other illegal credit
            # practices" / "KANSAS - Full-Scope Review", FDIC_17636_20241028 p11).
            or (
                above is not None
                and bool(above.text)
                and not above.running
                and not is_furniture_line(above.text)
                and not _HEADING_END.search(above.text)
            )
        )
        if event is None and nxt is not None and joinable:
            follower = lines[nxt]
            if not (follower.skip or follower.is_toc) and is_heading_candidate(follower.text):
                joined = f"{line.text} {follower.text}"
                event = _classify(joined, pats) if is_heading_candidate(joined) else None
                if event is not None and (
                    event[0] == "marker"
                    or (event[0] == "section" and len(line.text.split()) < 3)
                ):
                    event = None  # a wrapped table cell, not a heading (see the docstring)
                if event is not None:
                    line.heading = joined
                    consumed.add(nxt)
                    nxt = _next_text(lines, nxt)
        if event is None:
            continue
        if event[0] == "section":
            prev = _next_text(lines, i, -1)
            if nxt is not None and PERIOD_ROW.search(lines[nxt].text):
                continue
            if prev is not None and (
                LONE_BULLET.search(lines[prev].text) or PERIOD_HEADER.search(lines[prev].text)
            ):
                continue
        if event[0] == "marker" and scope != "appendix":
            scope = event[1]
        line.scope = scope
        line.event = event


def _analyze(pages: list[Page], regulator: str) -> tuple[list[_Line], set[int]]:
    pats = _patterns(regulator)
    lines = _split_lines(pages)
    running = running_lines(pages)
    for line in lines:
        line.running = line.text in running
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
        tagged.append(
            page.model_copy(update={"scope": scope, "is_toc": is_toc, "chars": len(page.text)})
        )
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


def _first_line_of_page(lines: list[_Line], number: int) -> int:
    """Index of the first line on page ``number`` (``len(lines)`` when past the last page)."""
    return next((i for i, ln in enumerate(lines) if ln.page >= number), len(lines))


def _is_furniture(line: _Line) -> bool:
    """Blank line, page number or footer, or a running header (column gap near the page top,
    or a line that recurs at the top or bottom of many pages)."""
    return (
        not line.text
        or line.running
        or is_furniture_line(line.text)
        or (line.rank < 3 and bool(COLUMN_GAP.search(line.text)))
    )


def table_run_start(lines: list[_Line], start_idx: int, end_idx: int) -> int:
    """Index of the first line of a one-cell-per-line table inside ``lines[start_idx:end_idx]``.

    A table is ``TABLE_RUN_LINES`` consecutive non-blank lines of at most
    ``TABLE_CELL_CHARS`` characters; running headers are skipped and a blank line resets the
    count. Up to ``TABLE_TITLE_MAX_LINES`` lines directly above the first cell go with the
    table when they are at most ``TABLE_TITLE_MAX_CHARS`` long and do not end a sentence
    ("Lending Inside and Outside of the Assessment Area", FDIC_109_20250224 p8). Returns
    ``end_idx`` when there is no table.
    """
    run = 0
    run_start = end_idx
    for i in range(start_idx + 1, end_idx):
        line = lines[i]
        if not line.text:
            run = 0
            continue
        if line.running:
            continue
        if len(line.text) > TABLE_CELL_CHARS:
            run = 0
            continue
        if run == 0:
            run_start = i
        run += 1
        if run >= TABLE_RUN_LINES:
            cut = run_start
            for _ in range(TABLE_TITLE_MAX_LINES):
                prev = lines[cut - 1].text
                if (
                    cut - 1 <= start_idx
                    or not prev
                    or prev.endswith((".", "!", "?"))
                    or len(prev) > TABLE_TITLE_MAX_CHARS
                ):
                    break
                cut -= 1
            return cut
    return end_idx


def _trim_end(lines: list[_Line], start_idx: int, end_idx: int) -> int:
    """Move a section end back over trailing furniture; the heading line is always kept."""
    while end_idx > start_idx + 1 and _is_furniture(lines[end_idx - 1]):
        end_idx -= 1
    return end_idx


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
    end_idx = next(
        (
            i
            for i in range(start_idx + 1, len(lines))
            if lines[i].event is not None and lines[i].event != ("section", name)
        ),
        len(lines),
    )
    max_pages = pats.max_pages.get(name)
    if max_pages:
        end_idx = min(end_idx, _first_line_of_page(lines, start.page + max_pages))
    if pats.end_at_table_run.get(name):
        end_idx = table_run_start(lines, start_idx, end_idx)
    end_idx = _trim_end(lines, start_idx, end_idx)
    end_offset = lines[end_idx].offset if end_idx < len(lines) else len(text)
    return Section(
        name=name,
        doc_id=doc_id,
        page_start=start.page,
        page_end=lines[end_idx - 1].page,
        char_start=start.offset,
        char_end=end_offset,
        text=text[start.offset : end_offset],
        scope=start.scope,  # type: ignore[arg-type]
        heading_matched=start.heading or start.text,
        confidence=1.0 if start.scope == wanted else 0.5,
    )


def locate_section(name: str, doc_id: str, pages: list[Page], regulator: str) -> Section | None:
    """Locate one named section, or return None when no heading matches."""
    lines, _ = _analyze(pages, regulator)
    return _locate_from_lines(name, doc_id, pages, lines, doc_text(pages), regulator)


def locate_all(
    doc_id: str, pages: list[Page], regulator: str
) -> tuple[dict[str, Section], list[str]]:
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
            pages.append(
                Page(number=item.get("number", i + 1), text=item["text"], chars=len(item["text"]))
            )
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
    ap.add_argument(
        "--text-dir", type=Path, default=TEXT_DIR, help="folder with {doc_id}.pages.json"
    )
    ap.add_argument(
        "--samples-dir", type=Path, help="run on pdftotext .txt files instead of the manifest"
    )
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
