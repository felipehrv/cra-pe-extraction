"""Rule-based extractors (system S1, design section 4) over located sections of one PE.

:func:`extract` takes the per-page text, the sections found by ``locate.locate_all`` and the
regulator, and returns one :class:`~cra_extract.models.FieldValue` per field in
:data:`RULE_FIELDS` with ``system="rules"``. Every value carries the sentence or table row it
was read from as a verbatim quote (whitespace collapsed) with page, scope and character
offsets into the document text (pages joined by a form feed, as in ``locate.doc_text``).

Status vocabulary: ``value`` when a pattern matched, ``absent`` only when the applicability
table in ``config/fields.yaml`` says the cell is absent from the document (the Lending Test
rating on FDIC and FRB Small Bank PEs, the Community Development Test rating on every Small
Bank PE), and ``abstain`` whenever no pattern matched. The rules never guess: a field whose
section was not located, or whose sentence is not where the eight sample PEs print it,
abstains.

Each rule reads prose sentence by sentence (:func:`section_sentences`): table rows are
skipped, and page furniture inside a sentence that straddles a page break is kept so that the
quote stays one contiguous span. The rules read whatever ``locate`` returns for the
institution-scope section names: on the eight samples every located section is institution
scope at confidence 1.0, but ``locate`` falls back to the first heading of any scope at
confidence 0.5 when no in-scope heading exists (``locate._locate_from_lines``), and that
section's scope and confidence are carried into ``FieldValue.scope`` so the grounding scope
check can flag the value.

Known abstentions on the samples, documented in ``tests/test_rules.py``:

- ``n_assessment_areas`` on OCC_11715_LemontNB_2024_SN: the count is printed only in the
  State of Illinois section (line 222 of the sample text), which is state scope.
- ``inside_aa_share_number_pct`` on FDIC_12299_SouthStar: the inside/outside table has no
  Total row (lines 363 to 368); gold takes the primary product's subtotal, which needs the
  weighting sentence of the Scope section, so the rule abstains.

CHANGELOG
---------
- 0.1.0 (2026-10-07): first version, patterns seeded on the eight sample PEs.
- 0.2.0 (2026-10-08): Group A fixes from the corpus-wide comparison with the agency index
  (design 6.3; triage in ``results/group_a_triage.csv``, hand entries in
  ``config/group_a_triage.yaml``). exam_method: ISB abbreviations and hyphenation, Small Bank
  "performance standards/criteria" wordings, negated ISB, mid-period transitions abstain, the
  FDIC/FRB CD Test rating overrides a Small Bank scope sentence, optional OCC CD Test means
  Small Bank, FDIC appendix heading and OCC Lending-Test-only rating page as fallbacks.
  bank_id and bank_name: more id labels, bare id lines, the FRB Richmond second cover, address
  lines and the "Name of Depository Institution:" label kept out of the name. exam_date:
  misspelled months, ordinal days and two-digit years on the cover, the Scope "current
  evaluation dated" sentence as fallback and as a consistency note. overall_rating:
  "Institution's CRA rating: X", "This institution is X", "CRA rating for <bank> is X", a
  lowered rating and a named subject "<bank> is rated X". Lending and CD Test ratings also
  read "test rating: X" and "CD Test is rated X".
- 0.2.1 (2026-10-08): the regulator rule matches the cover line after ``normalize_line``, so
  the 46 FRB Chicago covers printed with U+00A0 spaces name their agency. No other pattern
  changed; the version also records that the S1 inputs changed with
  the locator and ``config/headings.yaml`` tuning of the same day (no-break-space and hyphen
  glyphs in ``locate.normalize_line``, leaderless table-of-contents pages, wrapped headings,
  FRB heading variants by district, case-sensitive FRB state marker, running headers and
  table runs as section ends). The 45 FRB Chicago PEs printed with U+00A0 spaces go from
  abstaining on every field to values, and sections that started on a table-of-contents page
  now start at the real heading. ``results/rules_0.2.0_head_locator.csv`` holds 0.2.0 on the
  previous locator for comparison.
- 0.2.2 (2026-10-08): no pattern changed; S1 inputs changed. Locate scope rules
  (``config/headings.yaml`` scope_rules): the fair lending or illegal credit practices review
  section is institution scope wherever printed, and in a document with one assessment-area
  unit the LTD ratio and AA concentration sections are institution scope, so ``FieldValue.scope``
  changes for those sections. Manifest overrides (``config/manifest_overrides.yaml``):
  FDIC_445_20280408 is FDIC_445_20240408, FRB_785473_20220418 is parsed from its embedded PE.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from cra_extract.config import CORPUS_PATH, RESULTS_DIR, TEXT_DIR, applicability
from cra_extract.locate import (
    COLUMN_GAP,
    doc_text,
    is_furniture_line,
    load_pages_json,
    locate_all,
    normalize_line,
    pages_from_text,
)
from cra_extract.models import RATING_WORDS, FieldValue, Page, Rating, Section

RULES_VERSION = "0.2.2"
SYSTEM = "rules"
CHANGELOG: list[tuple[str, str, str]] = [
    ("0.1.0", "2026-10-07", "First version, patterns seeded on the eight sample PEs."),
    (
        "0.2.0",
        "2026-10-08",
        "Group A fixes from the corpus-wide index comparison: exam_method wordings, negation, "
        "transitions and fallbacks; more cover id labels, bare id lines and the FRB Richmond "
        "second cover; tolerant cover dates and the Scope date fallback; more overall rating "
        "wordings; 'test rating: X' and 'CD Test is rated X'.",
    ),
    (
        "0.2.1",
        "2026-10-08",
        "Regulator rule reads the cover line after normalize_line (FRB Chicago U+00A0 "
        "spaces). No other pattern change; S1 inputs changed with the locator and headings "
        "tuning of the same day (no-break-space glyphs, leaderless ToC pages, wrapped headings, FRB heading "
        "variants, case-sensitive FRB state marker, running headers and table runs as section "
        "ends); 45 FRB Chicago PEs go from all-abstain to values.",
    ),
    (
        "0.2.2",
        "2026-10-08",
        "No pattern change; S1 inputs changed: locate scope_rules (review section always institution "
        "scope; LTD and AA concentration institution scope in one-unit documents) and the manifest "
        "overrides (FDIC_445_20240408 renamed, FRB_785473_20220418 parsed from its embedded PE).",
    ),
]

RULE_FIELDS = [
    "regulator",
    "bank_name",
    "bank_id",
    "exam_date",
    "exam_method",
    "overall_rating",
    "lending_test_rating",
    "cd_test_rating",
    "prior_evaluation_date",
    "prior_rating",
    "total_assets_usd",
    "total_assets_asof",
    "avg_ltd_ratio_pct",
    "inside_aa_share_number_pct",
    "inside_aa_share_product",
    "n_assessment_areas",
    "loan_products_evaluated",
]

# --------------------------------------------------------------------------- normalization

RATING_RE = r"(outstanding|satisfactory|needs\s+to\s+improve|substantial\s+non-?\s?compliance)"
_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
LONG_DATE_RE = r"(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})"
NUM_DATE_RE = r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"
DATE_RE = re.compile(f"{LONG_DATE_RE}|{NUM_DATE_RE}", re.IGNORECASE)
MONEY_RE = re.compile(r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|thousand)?", re.IGNORECASE)
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:percent|%)", re.IGNORECASE)
_SCALES = {"billion": 10**9, "million": 10**6, "thousand": 10**3}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


def ws(text: str) -> str:
    """Collapse runs of whitespace (including form feeds) to one space and strip."""
    return re.sub(r"\s+", " ", text).strip()


def normalize_rating(word: str) -> Rating | None:
    """Map a printed rating phrase to O, S, NI or SN; None when it is not a rating."""
    w = ws(word).lower()
    if w.startswith("substantial"):
        return "SN"
    return RATING_WORDS.get(w)


def parse_money(text: str, unit: str | None = None) -> int | None:
    """Integer dollars from "$1.2 billion", "$345.6 million", "$123,456 thousand" or "84,182".

    ``unit`` applies when the text names no scale word (a table in $000s passes "thousand").
    Decimal arithmetic keeps "$84.7 million" at exactly 84700000.
    """
    m = MONEY_RE.search(text)
    if not m:
        return None
    amount = Decimal(m.group(1).replace(",", ""))
    scale = _SCALES.get((m.group(2) or unit or "").lower(), 1)
    return int(amount * scale)


def parse_date(text: str) -> date | None:
    """First date in the text, in "March 27, 2023" or "03/27/2023" form."""
    found = find_dates(text)
    return found[0][1] if found else None


def find_dates(text: str) -> list[tuple[int, date]]:
    """Every parseable date in the text as (offset, date), in order of appearance."""
    out: list[tuple[int, date]] = []
    for m in DATE_RE.finditer(text):
        try:
            if m.group(1):
                d = date(int(m.group(3)), _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
            else:
                d = date(int(m.group(6)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            continue
        out.append((m.start(), d))
    return out


# Cover-page dates as some PEs print them: a misspelled month ("Feburary 5, 2024",
# FDIC_11674_20240205 p1; "Aprill 22, 2024", FDIC_13951_20240422 p1; "Decmeber 2, 2024",
# FDIC_16344_20241202 p1; "Janurary 21, 2025", FDIC_23373_20250121 p1), no space between
# month and day ("August4,2025", FRB_980960_20250804 p1), an ordinal day ("August 28th, 2023",
# OCC_11916_20230828 p1) and a two-digit year ("6/26/23", OCC_18609_20230626 p1). Used only by
# the cover rule, never by the prose rules, so a stray token cannot become a prior date.
_LOOSE_DATE_RE = re.compile(r"\b([A-Za-z]{3,10})\.?\s*(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b")
_SHORT_NUM_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b(?!/)")


def fuzzy_month(token: str) -> int | None:
    """Month number for a month name, its three-letter abbreviation or a near miss.

    A near miss shares the first three letters and has a rapidfuzz ratio of at least 85 with
    the month name ("decmeber" 87.5, "janurary" 93.3), which keeps "market" and "mayor" out.
    """
    t = token.lower().rstrip(".")
    for i, month in enumerate(_MONTHS):
        if t in (month, month[:3]) or (i == 8 and t == "sept"):
            return i + 1
        if len(t) > 3 and t[:3] == month[:3] and fuzz.ratio(t, month) >= 85:
            return i + 1
    return None


def parse_cover_date(text: str) -> date | None:
    """First date on a cover-page line: :func:`parse_date`, then the tolerant cover forms."""
    strict = parse_date(text)
    if strict:
        return strict
    for m in _LOOSE_DATE_RE.finditer(text):
        month = fuzzy_month(m.group(1))
        if month is None:
            continue
        try:
            return date(int(m.group(3)), month, int(m.group(2)))
        except ValueError:
            continue
    m = _SHORT_NUM_DATE_RE.search(text)
    if m:
        try:
            return date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- sentences


_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


@dataclass(frozen=True)
class Sentence:
    """One prose sentence (or one line) as a contiguous span of the document text."""

    text: str
    start: int
    end: int
    page: int

    @property
    def quote(self) -> str:
        """The span with whitespace collapsed, otherwise verbatim."""
        return ws(self.text)

    @property
    def norm(self) -> str:
        """The quote with curly quotes straightened and a leading bullet dropped, for matching."""
        return _BULLET_RE.sub("", self.quote.translate(_QUOTES))


# A sentence ends at . ! or ? followed by whitespace and either a character that is neither
# lowercase nor a digit, or a bare page number on its own line (OCC and FRB print the page
# number before the running header of the next page).
_SENT_BOUNDARY = re.compile(r"[.!?][”\"’')]*(\s+)(?=[^a-z\d\s]|\d{1,3}[ \t]*\n)")
# Bullet glyphs: bullet (U+2022), black circle (U+25CF), en dash (U+2013), asterisk, hyphen and
# the Private Use Area (U+F000 to U+F8FF, where pdftotext puts Wingdings bullets).
_BULLET_CHARS = "".join(chr(c) for c in (0x2022, 0x25CF, 0x2013)) + "*-"
_PUA_RANGE = chr(0xF000) + "-" + chr(0xF8FF)
_BULLET_RE = re.compile("^[" + re.escape(_BULLET_CHARS) + _PUA_RANGE + r"]\s+")
_HEADING_END_RE = re.compile(r"[.!?:;,]$")


def _clean_line(raw: str) -> str:
    """Normalized line with the form feed and a leading bullet glyph removed."""
    return _BULLET_RE.sub("", normalize_line(raw.replace("\f", "")))


def _classify(raw: str, rank: int) -> str:
    text = _clean_line(raw)
    if not text:
        return "blank"
    if is_furniture_line(text) or (rank < 3 and COLUMN_GAP.search(text)):
        return "furniture"
    if COLUMN_GAP.search(text):
        return "table"
    return "prose"


def _is_heading(text: str) -> bool:
    """Short title-like line with no terminal punctuation ("Overall CRA Rating")."""
    words = text.split()
    return 0 < len(words) <= 6 and len(text) <= 60 and text[0].isupper() and not _HEADING_END_RE.search(text)


def _paragraphs(text: str, rank0: int = 0) -> list[tuple[int, int]]:
    """Spans of prose paragraphs in ``text``, with page furniture kept inline.

    A blank line ends a paragraph unless it belongs to a page-break run (page number,
    charter-number footer, running header) between two prose lines; a table row always ends
    one, and a heading line opening a paragraph is a paragraph of its own. ``rank0`` is the
    number of non-blank lines already seen on the first page.
    """
    spans: list[tuple[int, int]] = []
    cur: list[int] | None = None
    pending_blank = pending_furniture = False
    offset, rank = 0, rank0
    for raw in text.split("\n"):
        if raw.startswith("\f"):
            rank = 0
        kind = _classify(raw, rank)
        if kind != "blank":
            rank += 1
        start, end = offset, offset + len(raw)
        if kind == "prose":
            opens = cur is None or (pending_blank and not pending_furniture)
            if opens and cur is not None:
                spans.append((cur[0], cur[1]))
                cur = None
            if cur is None:
                cur = [start, end]
                if _is_heading(normalize_line(raw.replace("\f", ""))):
                    spans.append((cur[0], cur[1]))
                    cur = None
            else:
                cur[1] = end
            pending_blank = pending_furniture = False
        elif kind == "table":
            if cur is not None:
                spans.append((cur[0], cur[1]))
                cur = None
            pending_blank = pending_furniture = False
        elif kind == "furniture":
            pending_furniture = True
        else:
            pending_blank = True
        offset = end + 1
    if cur is not None:
        spans.append((cur[0], cur[1]))
    return spans


def _trim_furniture(chunk: str) -> tuple[int, int]:
    """Offsets within ``chunk`` after dropping blank and furniture lines at either edge."""
    lines = chunk.split("\n")
    starts, offset = [], 0
    for ln in lines:
        starts.append(offset)
        offset += len(ln) + 1
    def is_edge_furniture(ln: str) -> bool:
        text = _clean_line(ln)
        return not text or is_furniture_line(text) or bool(COLUMN_GAP.search(text))

    keep = [i for i, ln in enumerate(lines) if not is_edge_furniture(ln)]
    if not keep:
        return 0, 0
    first, last = keep[0], keep[-1]
    return starts[first], starts[last] + len(lines[last])


def split_sentences(text: str, base: int = 0, page_of=None, rank0: int = 0) -> list[Sentence]:
    """Prose sentences of ``text`` as contiguous spans; offsets are shifted by ``base``.

    Sentences end at ``.``, ``!`` or ``?`` followed by whitespace and a character that is
    neither lowercase nor a digit, which keeps "S.S.B. demonstrated", "No. 3664588" and
    "54.32 percent" whole. ``page_of`` maps a document offset to a page number.
    """
    out: list[Sentence] = []
    for p_start, p_end in _paragraphs(text, rank0):
        para = text[p_start:p_end]
        last = 0
        for m in _SENT_BOUNDARY.finditer(para):
            out.append(_sentence(para, last, m.start(1), p_start + base, page_of))
            last = m.end()
        out.append(_sentence(para, last, len(para), p_start + base, page_of))
    return [s for s in out if s.text]


def _sentence(para: str, start: int, end: int, base: int, page_of) -> Sentence:
    chunk = para[start:end]
    t0, t1 = _trim_furniture(chunk)
    chunk = chunk[t0:t1]
    lead = len(chunk) - len(chunk.lstrip())
    trail = len(chunk) - len(chunk.rstrip())
    s, e = base + start + t0 + lead, base + start + t1 - trail
    page = page_of(s) if page_of else 1
    return Sentence(chunk.strip(), s, e, page)


# --------------------------------------------------------------------------- document context


class Doc:
    """One document: pages, located sections and cached sentence and line views."""

    def __init__(self, doc_id: str, regulator: str, pages: list[Page], sections: dict[str, Section]):
        self.doc_id = doc_id
        self.regulator = regulator
        self.pages = pages
        self.sections = sections
        self.text = doc_text(pages)
        self._starts: list[int] = []
        offset = 0
        for p in pages:
            self._starts.append(offset)
            offset += len(p.text) + 1
        self._sentences: dict[str, list[Sentence]] = {}

    def page_of(self, offset: int) -> int:
        """1-based page number holding a document offset."""
        return max(1, bisect_right(self._starts, offset))

    def sentences(self, name: str) -> list[Sentence]:
        """Prose sentences of a located section, empty when it was not located."""
        if name not in self._sentences:
            sec = self.sections.get(name)
            self._sentences[name] = section_sentences(sec, self) if sec else []
        return self._sentences[name]

    def lines(self, name: str) -> list[Sentence]:
        """Non-blank lines of a located section as spans (for cover pages and table rows)."""
        sec = self.sections.get(name)
        if sec is None:
            return []
        return self._span_lines(sec.text, sec.char_start)

    def page_lines(self, number: int) -> list[Sentence]:
        """Non-blank lines of one page (1-based) as spans; empty when the page does not exist."""
        if not 1 <= number <= len(self.pages):
            return []
        return self._span_lines(self.pages[number - 1].text, self._starts[number - 1])

    def _span_lines(self, text: str, base: int) -> list[Sentence]:
        out = []
        offset = base
        for raw in text.split("\n"):
            clean = raw.replace("\f", "")
            if clean.strip():
                lead = len(clean) - len(clean.lstrip())
                start = offset + (len(raw) - len(clean)) + lead
                out.append(Sentence(clean.strip(), start, start + len(clean.strip()), self.page_of(start)))
            offset += len(raw) + 1
        return out


def section_sentences(section: Section, doc: Doc) -> list[Sentence]:
    """Sentences of one located section with document offsets and page numbers."""
    page_start = doc._starts[doc.page_of(section.char_start) - 1]
    rank0 = sum(bool(ln.strip()) for ln in doc.text[page_start : section.char_start].split("\n"))
    return split_sentences(section.text, section.char_start, doc.page_of, rank0)


# --------------------------------------------------------------------------- emitters


def _fv(doc: Doc, field: str, status: str, value: Any = None, span: Sentence | None = None,
        notes: str | None = None, section: str | None = None) -> FieldValue:
    scope = doc.sections[section].scope if section and section in doc.sections else None
    return FieldValue(
        doc_id=doc.doc_id,
        field=field,
        system=SYSTEM,
        status=status,  # type: ignore[arg-type]
        value=value,
        quote=span.quote if span else None,
        page=span.page if span else None,
        scope=scope if span else None,
        char_start=span.start if span else None,
        char_end=span.end if span else None,
        notes=notes,
    )


def _value(doc: Doc, field: str, value: Any, span: Sentence, section: str, notes: str | None = None) -> FieldValue:
    return _fv(doc, field, "value", value, span, notes, section)


def _abstain(doc: Doc, field: str, notes: str | None = None) -> FieldValue:
    return _fv(doc, field, "abstain", notes=notes)


def _absent(doc: Doc, field: str, notes: str | None = None) -> FieldValue:
    return _fv(doc, field, "absent", notes=notes)


def _first(doc: Doc, section: str, pattern: re.Pattern[str]) -> tuple[Sentence, re.Match[str]] | None:
    for s in doc.sentences(section):
        m = pattern.search(s.norm)
        if m:
            return s, m
    return None


# --------------------------------------------------------------------------- cover rules

REGULATOR_PATTERNS = [
    ("OCC", re.compile(r"comptroller of the currency", re.IGNORECASE)),
    ("FDIC", re.compile(r"federal deposit insurance corporation", re.IGNORECASE)),
    ("FRB", re.compile(r"federal reserve bank", re.IGNORECASE)),
]
# Id line labels seen on covers: "Charter Number 10180", "Charter Number: 18558", "Charter
# 13478" (OCC_13478_20240415 p1), "Charter #10161" (OCC_10161_20221219 p1), "Charter # 13557"
# (OCC_13557_20230530 p1), "Charter: 23393" (OCC_23393_20241113 p1), "Charter No. 23458"
# (OCC_23458_20240722 p1), "Certificate Number: 5891", "RSSD #853113", "RSSD# 465243",
# "RSSD ID NUMBER: 353238" (FRB Atlanta, FRB_353238_20230925 p1) and "Institution's
# Identification Number: 920210" (FRB Philadelphia, FRB_920210_20240716 p1, curly apostrophe).
ID_RE = re.compile(
    r"(?:\bcharter(?:\s+(?:number|no\.?))?|certificate\s+number|\brssd(?:\s*id)?(?:\s*(?:number|no\.?))?|"
    r"institution\W?s\s+identification\s+number)\s*:?\s*#?\s*(\d{1,10})\b",
    re.IGNORECASE,
)
# A bare number on its own line right under the institution name: OCC covers print "9773"
# (OCC_9773_20230109 p1) or "#22327" (OCC_22327_20221003 p1), FRB Richmond second covers print
# "711520" (FRB_711520_20230925 p2).
BARE_ID_RE = re.compile(r"^#?\s*(\d{1,10})$")
# "PERFORMANCE EVALUATION" on its own line, or the one-line FRB Richmond title "COMMUNITY
# REINVESTMENT ACT PERFORMANCE EVALUATION" (FRB_711520_20230925 p1 and p2).
TITLE_RE = re.compile(
    r"^(?:community\s+reinvestment\s+act\s+(?:\(cra\)\s+)?)?performance\s+evaluation$", re.IGNORECASE
)
COVER_HEADING_RE = re.compile(r"^public\s+disclosure$", re.IGNORECASE)
ADDRESS_LINE_RE = re.compile(r"^\d|^p\.?\s*o\.?\s+box\b|\b\d{5}(?:-\d{4})?$", re.IGNORECASE)
NAME_LABEL_RE = re.compile(r"^name\s+of\s+(?:depository\s+)?institution\s*:\s*", re.IGNORECASE)


def rule_regulator(doc: Doc) -> FieldValue:
    """Agency named on the cover page.

    The line is matched after ``normalize_line``: FRB Chicago covers print U+00A0 for every
    space ("Federal\xa0Reserve\xa0Bank\xa0of\xa0Chicago", FRB_180845_20241104 p1).
    """
    for line in doc.lines("cover"):
        for code, pat in REGULATOR_PATTERNS:
            if pat.search(normalize_line(line.text)):
                return _value(doc, "regulator", code, line, "cover")
    return _abstain(doc, "regulator", "no agency name on the cover")


def _id_line(lines: list[Sentence]) -> tuple[int, str] | None:
    """Index and digits of the id line: a labelled id anywhere on the cover, else a bare
    number within the three lines after the title (the name takes at least one line)."""
    for i, ln in enumerate(lines):
        m = ID_RE.search(ln.text)
        if m:
            return i, m.group(1)
    title = next((i for i, ln in enumerate(lines) if TITLE_RE.match(normalize_line(ln.text))), None)
    if title is None:
        return None
    for i in range(title + 2, min(title + 5, len(lines))):
        m = BARE_ID_RE.match(lines[i].text.strip())
        if m:
            return i, m.group(1)
    return None


def _second_cover(doc: Doc) -> list[Sentence]:
    """Lines of a second cover on the page after the located one, when that page opens with
    "PUBLIC DISCLOSURE". FRB Richmond prints a decorative cover with no id on page 1 and the
    full cover, id included, on page 2 (FRB_711520_20230925, FRB_8631_20240129)."""
    sec = doc.sections.get("cover")
    if sec is None:
        return []
    lines = doc.page_lines(sec.page_end + 1)
    if any(COVER_HEADING_RE.match(normalize_line(ln.text)) for ln in lines[:3]):
        return lines
    return []


def _cover_with_id(doc: Doc) -> tuple[list[Sentence], int, str, str | None] | None:
    """(cover lines, id line index, digits, note) from the located cover or a second cover."""
    for lines, note in ((doc.lines("cover"), None), (_second_cover(doc), "second cover page")):
        hit = _id_line(lines)
        if hit:
            return lines, hit[0], hit[1], note
    return None


def rule_bank_id(doc: Doc) -> FieldValue:
    """Charter, certificate or RSSD number on the cover page, leading zeros stripped."""
    found = _cover_with_id(doc)
    if found is None:
        return _abstain(doc, "bank_id", "no charter, certificate or RSSD line on the cover")
    lines, i, digits, note = found
    return _value(doc, "bank_id", digits.lstrip("0") or "0", lines[i], "cover", note)


def rule_bank_name(doc: Doc) -> FieldValue:
    """Institution name: the line(s) between "PERFORMANCE EVALUATION" and the id line."""
    found = _cover_with_id(doc)
    if found is None or found[1] == 0:
        return _abstain(doc, "bank_name", "no id line on the cover to anchor the name")
    lines, i, _, note = found
    title = next((j for j in range(i - 1, -1, -1) if TITLE_RE.match(normalize_line(lines[j].text))), None)
    name_lines = lines[title + 1 : i] if title is not None and title + 1 < i else lines[i - 1 : i]
    # FRB Atlanta prints the address between the name and the id line ("Central State Bank /
    # 11025 Highway 25 / Calera, Alabama 35040 / RSSD ID NUMBER: 353238", FRB_353238_20230925
    # p1): the name ends before the first street, post-office-box or ZIP line.
    for k, ln in enumerate(name_lines):
        if k and ADDRESS_LINE_RE.search(ln.text):
            name_lines = name_lines[:k]
            break
    span = Sentence(
        doc.text[name_lines[0].start : name_lines[-1].end],
        name_lines[0].start,
        name_lines[-1].end,
        name_lines[0].page,
    )
    # FRB Philadelphia labels the line: "Name of Depository Institution: JIM THORPE NEIGHBORHOOD
    # BANK" (FRB_920210_20240716 p1); the value drops the label, the quote keeps the line.
    value = NAME_LABEL_RE.sub("", span.quote).strip()
    return _value(doc, "bank_name", value or span.quote, span, "cover", note)


# The Scope section's statement of the current evaluation date (FDIC: "to the current
# evaluation dated July 9, 2024", FDIC_9438_20240709 p7). Used as a fallback when the cover
# prints no date (FDIC_9366_20230821 p1) and as a consistency note when the two differ.
CURRENT_EVAL_RE = re.compile(
    r"\b(?:current|this)\s+(?:cra\s+)?(?:performance\s+)?evaluation,?\s+(?:dated|date\s+of|as\s+of)\s+"
    r"(" + LONG_DATE_RE + "|" + NUM_DATE_RE + ")",
    re.IGNORECASE,
)


def _scope_eval_date(doc: Doc) -> tuple[Sentence, date] | None:
    for s in doc.sentences("scope_of_evaluation"):
        m = CURRENT_EVAL_RE.search(s.norm)
        if m:
            d = parse_date(m.group(1))
            if d:
                return s, d
    return None


def rule_exam_date(doc: Doc) -> FieldValue:
    """Evaluation date printed on the cover page (the first date there).

    When the cover prints no date, the Scope section's "current evaluation dated" sentence is
    read instead (noted); when both exist and differ, the cover wins and the note records the
    Scope date for the consistency check.
    """
    scope = _scope_eval_date(doc)
    for line in doc.lines("cover"):
        d = parse_cover_date(line.text)
        if d:
            note = None
            if scope and scope[1] != d:
                note = f"scope section dates the current evaluation {scope[1].isoformat()} (p{scope[0].page})"
            return _value(doc, "exam_date", d, line, "cover", note)
    if scope:
        return _value(doc, "exam_date", scope[1], scope[0], "scope_of_evaluation",
                      "no date on the cover; current evaluation date from the scope section")
    return _abstain(doc, "exam_date", "no date on the cover")


# --------------------------------------------------------------------------- rating rules

# "is rate" is a typo printed by OCC_5288_20231030 p3 ("This institution is rate Satisfactory.").
OVERALL_RE = re.compile(r"(?:institution|bank\)?)\s+is\s+rated?:?\s*[\"“]?\s*" + RATING_RE, re.IGNORECASE)
# Other wordings of the institution rating on the rating page: "Institution's CRA rating:
# Satisfactory" (OCC_706517_20230828 p3), "This institution is Satisfactory." (FDIC_4235_20250310
# p3), "The Community Reinvestment Act (CRA) rating for Western Bank (the bank) is Satisfactory."
# (FRB_723653_20230109 p3), a lowered rating "initial satisfactory Community Reinvestment Act (CRA)
# rating has been lowered to Needs to Improve" (FRB_392152_20210222 p3), and a named subject:
# "Bank of 1889 is rated Satisfactory." (FRB_864846_20230821 p3), "The Hamler State Bank
# (Hamler) is rated "Satisfactory."" (FRB_887827_20230515 p3), "Wadena State Bank (the bank),
# Wadena, Minnesota, is rated Outstanding." (FRB_210256_20250224 p3).
OVERALL_LABEL_RE = re.compile(r"institution\W?s\s+cra\s+rating\s*(?:is)?\s*:?\s*[\"“]?\s*" + RATING_RE, re.IGNORECASE)
OVERALL_BARE_RE = re.compile(r"\bthis\s+institution\s+is\s+[\"“]?\s*" + RATING_RE, re.IGNORECASE)
OVERALL_FOR_RE = re.compile(
    r"\b(?:cra|community\s+reinvestment\s+act(?:\s+\(cra\))?)\s+rating\s+for\s+[^:;]{1,120}?\s+is:?\s*[\"“]?\s*"
    + RATING_RE,
    re.IGNORECASE,
)
OVERALL_LOWERED_RE = re.compile(
    r"\brating\s+(?:has\s+been|was|is)\s+(?:lowered|downgraded)\s+to\s+(?:a\s+|an\s+)?[\"“]?\s*" + RATING_RE,
    re.IGNORECASE,
)
# The optional label prefix is lazy so that "Clear Lake Bank and Trust Company is rated:
# Satisfactory The Lending Test is rated: ..." (FRB_865544_20221107 p3, one sentence) takes the
# bank as the subject, not the text after the first colon.
OVERALL_NAMED_RE = re.compile(
    r"^(?:[^:]{0,60}:\s*)??(?P<subject>[^:;]{2,120}?)\s+is\s+rated:?\s*[\"“]?\s*" + RATING_RE, re.IGNORECASE
)
# A named subject that is not the institution: a component test, a state, MSA or area,
# performance in a rated area, or the prior evaluation. "State Bank" in a name is allowed.
NOT_INSTITUTION_RE = re.compile(
    r"\btest\b|\bstate\s+of\b|\bmsa\b|\bmultistate\b|\bassessment\s+areas?\b|\baa\b|\bprior\b|\bprevious\b|"
    r"\bcomponent\b|\bperformance\s+in\b",
    re.IGNORECASE,
)
OVERALL_RES = (OVERALL_RE, OVERALL_LABEL_RE, OVERALL_BARE_RE, OVERALL_FOR_RE, OVERALL_LOWERED_RE)
# Component test ratings, "is rated: X" or "rating: X" ("The lending test rating: Satisfactory",
# OCC_706517_20230828 p3; "The community development test rating: Satisfactory",
# OCC_11544_20230531 p3; "The CD Test is rated: Outstanding.", OCC_23771_20220801 p3). "The
# Lending Test rating is based on ..." does not match.
LENDING_RE = re.compile(r"lending\s+test\s+(?:is\s+rated|rating)\s*:?\s*[\"“]?\s*" + RATING_RE, re.IGNORECASE)
CD_RE = re.compile(
    r"(?:community\s+development\s+(?:\(cd\)\s+)?|\bcd\s+)test\s+(?:is\s+rated|rating)\s*:?\s*[\"“]?\s*" + RATING_RE,
    re.IGNORECASE,
)


def rule_overall_rating(doc: Doc) -> FieldValue:
    """"This institution is rated X." and its variants, from the first rating-page sentence
    that states the institution's rating."""
    for s in doc.sentences("rating"):
        n = s.norm
        m = next((m for m in (p.search(n) for p in OVERALL_RES) if m), None)
        if m is None:
            named = OVERALL_NAMED_RE.search(n)
            if named and not NOT_INSTITUTION_RE.search(named.group("subject")):
                m = named
        if m:
            return _value(doc, "overall_rating", normalize_rating(m.group(m.lastindex or 1)), s, "rating")
    return _abstain(doc, "overall_rating", "no 'institution is rated' sentence in the rating section")


# --------------------------------------------------------------------------- exam method
#
# The Scope section names the examination procedures in one sentence. Wordings that 0.1.0
# missed or misread, with the corpus document that established each:
# - ISB abbreviations and hyphenation: "ISI Examination Procedures" (FDIC_14134_20240205 p6),
#   "ISB Examination Procedures" (FDIC_33708_20221114 p5), "Intermediate Small (ISB)
#   Institution" (FDIC_10474_20250428 p8), "intermediate- small institutions"
#   (FRB_214722_20241118 p4), "Intermediate-Small Bank" (OCC_12523_20221031 p5),
#   "Intermediate/Small Bank" (OCC_3180_20221219 p5).
# - Small Bank wordings without "procedures": "small bank performance standards"
#   (OCC_13692_20230117 p5), "small bank performance criteria" (OCC_17106_20250616 p4), "the
#   Small Bank criterion" (OCC_9183_20230213), "SB procedures" (OCC_13835_20230724), "Procedures
#   for Small Institutions1" with a footnote digit (FRB_164975_20250818 p4), "Interagency Small
#   Business Examination Procedures" (sic, FDIC_12402_20241021).
# - ISB sentences that name the Small Bank Lending Test ("These procedures include two tests:
#   the CRA Small Bank Lending Test and the Community Development Test", FDIC_14134_20240205 p6)
#   or the small bank asset threshold (FDIC_10474_20250428 p9) are not Small Bank evidence.
# - Negated ISB: "the bank fell below the asset threshold for Intermediate Small Institution
#   Examination Procedures" (FDIC_17280_20221018 p6) is skipped; "the bank was not considered an
#   intermediate small institution" (FRB_528102_20221128 p4) is Small Bank evidence.
# - Both procedures in one sentence, a transition during the evaluation period: "Small Bank CRA
#   examination procedures ... and Intermediate Small Bank CRA examination procedures for the
#   portion of the evaluation period" (OCC_10637_20250707 p5, OCC_24798_20240226 p5,
#   OCC_24219_20250218). The OCC index labels these small twice and isb once, so the rule
#   abstains (definitional, see results/group_a_triage.csv).
# - Sentences about the prior evaluation are skipped.
ISB_TERM_RE = re.compile(
    r"intermediate[\s\-/]*small|\bIS[BI]\b(?=\s+(?:CRA\s+)?(?:examination\s+|evaluation\s+)?"
    r"(?:procedures|performance|examination|evaluation|standards|criteria))",
    re.IGNORECASE,
)
SMALL_TERM_RE = re.compile(
    r"\bsmall\s+(?:bank|institution)s?\d*\b(?!\s+lending\s+test)(?!\s+(?:asset|size)\b)(?!\s+thresholds?\b)"
    r"|\bsmall\s+business\s+(?:cra\s+)?examination\s+procedures|\bSB\s+(?:cra\s+)?(?:procedures|performance)",
    re.IGNORECASE,
)
PROCEDURES_RE = re.compile(r"procedures|standards|criteri(?:a|on)", re.IGNORECASE)
PRIOR_EVAL_RE = re.compile(
    r"\b(?:prior|previous|preceding|last)\b[^.]{0,60}?\b(?:evaluation|examination|PE)\b", re.IGNORECASE
)
NEGATION_RE = re.compile(r"\bnot\b|\bbelow\b|\bno\s+longer\b|\bfell\b|\bdropped\b", re.IGNORECASE)
NOT_ISB_RE = re.compile(
    r"\bnot\s+(?:considered|designated|classified|deemed)?\s*(?:as\s+)?an?\s+intermediate[\s\-]*small", re.IGNORECASE
)
OPTIONAL_RE = re.compile(r"\boptional\b", re.IGNORECASE)
# FDIC appendix headings, one per method (FDIC_17280_20221018: "SMALL BANK PERFORMANCE
# CRITERIA"; FDIC_17011_20230307: "INTERMEDIATE SMALL BANK PERFORMANCE CRITERIA").
APPENDIX_METHOD_RE = re.compile(
    r"^[ \t]*(?P<isb>INTERMEDIATE[ \t]+)?SMALL[ \t]+BANK[ \t]+PERFORMANCE[ \t]+CRITERIA[ \t]*$", re.MULTILINE
)


def _negated(text: str, start: int) -> bool:
    """A negation word in the 60 characters before ``start``."""
    return bool(NEGATION_RE.search(text[max(0, start - 60) : start]))


def method_from_sentence(norm: str) -> str | None:
    """``isb``, ``small``, ``mixed`` or None for one Scope sentence (see the notes above)."""
    if PRIOR_EVAL_RE.search(norm):
        return None
    if NOT_ISB_RE.search(norm):
        return "small"
    if not PROCEDURES_RE.search(norm):
        return None
    isb = [m for m in ISB_TERM_RE.finditer(norm) if not _negated(norm, m.start())]
    if any(ISB_TERM_RE.finditer(norm)) and not isb:
        return None
    rest = ISB_TERM_RE.sub(" ", norm)
    small = [m for m in SMALL_TERM_RE.finditer(rest) if not _negated(rest, m.start())]
    if isb and small:
        return "mixed"
    if isb:
        return "isb"
    if small:
        return "small"
    return None


def _appendix_method(doc: Doc) -> tuple[str, Sentence] | None:
    m = APPENDIX_METHOD_RE.search(doc.text)
    if not m:
        return None
    start = m.start() + len(m.group(0)) - len(m.group(0).lstrip())
    end = m.start() + len(m.group(0).rstrip())
    return ("isb" if m.group("isb") else "small"), Sentence(doc.text[start:end], start, end, doc.page_of(start))


def rule_exam_method(doc: Doc) -> FieldValue:
    """Small or ISB procedures. Evidence in order:

    1. the first Scope sentence naming the procedures (:func:`method_from_sentence`); a sentence
       naming both is a mid-period transition and the rule abstains;
    2. for FDIC and FRB, a Community Development Test rating on the rating page overrides a
       Small Bank Scope sentence: neither agency rates a CD Test on a Small Bank PE (the Scope
       sentence of FDIC_1678_20230814 p5, FDIC_28348_20240813 p8 and FDIC_58691_20250407 p9 says
       "Small Institution" while the rating page, the appendix and the index say ISB);
    3. without a Scope sentence: the CD Test rating sentence (isb, or small when the OCC calls
       it the "Optional" CD Test, OCC_13692_20230117 p3), then the FDIC appendix heading, then,
       for the OCC, a rating page that rates the Lending Test and no CD Test (small).
    """
    scope_hit = None
    for s in doc.sentences("scope_of_evaluation"):
        method = method_from_sentence(s.norm)
        if method:
            scope_hit = (method, s)
            break
    cd = _first(doc, "rating", CD_RE)
    if scope_hit and scope_hit[0] == "mixed":
        return _abstain(doc, "exam_method", f"mixed Small Bank and ISB procedures (p{scope_hit[1].page})")
    if scope_hit:
        method, s = scope_hit
        if method == "small" and cd and not OPTIONAL_RE.search(cd[0].norm) and doc.regulator in ("FDIC", "FRB"):
            return _value(doc, "exam_method", "isb", cd[0], "rating",
                          f"CD Test rating overrides the Small Bank scope sentence on p{s.page}")
        return _value(doc, "exam_method", method, s, "scope_of_evaluation")
    if cd:
        if OPTIONAL_RE.search(cd[0].norm):
            return _value(doc, "exam_method", "small", cd[0], "rating", "inferred from the optional CD Test rating")
        return _value(doc, "exam_method", "isb", cd[0], "rating", "inferred from the CD test rating sentence")
    appendix = _appendix_method(doc)
    if appendix:
        return _value(doc, "exam_method", appendix[0], appendix[1], None, "inferred from the appendix heading")
    lending = _first(doc, "rating", LENDING_RE)
    if lending and doc.regulator == "OCC":
        return _value(doc, "exam_method", "small", lending[0], "rating",
                      "inferred: the rating page rates the Lending Test and no CD Test")
    return _abstain(doc, "exam_method", "no procedures sentence in the scope section")


def rule_test_ratings(doc: Doc, method: str | None) -> list[FieldValue]:
    """Lending and CD test ratings, honoring the applicability table when the method is known."""
    out = []
    for field, pat in (("lending_test_rating", LENDING_RE), ("cd_test_rating", CD_RE)):
        hit = _first(doc, "rating", pat)
        app = applicability(field, doc.regulator, method) if method else "value"
        if app == "absent_from_document":
            note = f"applicability {doc.regulator}/{method}: absent_from_document"
            if hit:
                note += f"; applicability_exception_candidate: {hit[0].norm}"
            out.append(_absent(doc, field, note))
        elif hit:
            out.append(_value(doc, field, normalize_rating(hit[1].group(1)), hit[0], "rating"))
        else:
            out.append(_abstain(doc, field, "no test rating sentence in the rating section"))
    return out


# --------------------------------------------------------------------------- prior evaluation

PRIOR_RE = re.compile(r"\b(?:prior|previous|preceding|last)\b[^.]{0,80}?\b(?:evaluation|examination)\b", re.IGNORECASE)
AT_ITS_RE = re.compile(r"\bat\s+its\s+" + LONG_DATE_RE + r"\s+(?:cra\s+)?(?:performance\s+)?evaluation", re.IGNORECASE)
DATED_RE = re.compile(r"\bdated\s+(" + LONG_DATE_RE + "|" + NUM_DATE_RE + ")", re.IGNORECASE)
BEFORE_EVAL_RE = re.compile("(" + LONG_DATE_RE + r"),?\s+(?:cra\s+)?(?:performance\s+)?evaluation", re.IGNORECASE)
RATING_WORD_RE = re.compile(RATING_RE, re.IGNORECASE)


def _prior_date(norm: str) -> date | None:
    for pat in (DATED_RE, BEFORE_EVAL_RE):
        m = pat.search(norm)
        if m:
            return parse_date(m.group(1))
    return parse_date(norm)


def rule_prior(doc: Doc) -> list[FieldValue]:
    """Date and rating of the previous evaluation as stated in the Description of Institution."""
    for section in ("description_of_institution", "scope_of_evaluation"):
        for s in doc.sentences(section):
            n = s.norm
            if not (PRIOR_RE.search(n) or AT_ITS_RE.search(n)):
                continue
            d = _prior_date(n)
            if d is None:
                continue
            out = [_value(doc, "prior_evaluation_date", d, s, section)]
            r = RATING_WORD_RE.search(n)
            if r:
                out.append(_value(doc, "prior_rating", normalize_rating(r.group(1)), s, section))
            else:
                out.append(_abstain(doc, "prior_rating", "prior evaluation sentence names no rating"))
            return out
    note = "no prior evaluation sentence with a date"
    return [_abstain(doc, "prior_evaluation_date", note), _abstain(doc, "prior_rating", note)]


# --------------------------------------------------------------------------- total assets

ASSETS_RES = [
    re.compile(
        r"(?:total\s+)?assets\s+(?:of|totaled|totaling|were|was|at|reached)\s+"
        r"(?:approximately\s+|about\s+|roughly\s+)?(\$\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand)?)",
        re.IGNORECASE,
    ),
    re.compile(r"(\$\s?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand)?)\s*\(total\s+assets\)", re.IGNORECASE),
]
ASSETS_HEADER_RE = re.compile(r"total\s+assets\s+as\s+of", re.IGNORECASE)
_HEADER_ROW_RE = re.compile(r"ratio|000|\(%\)|/\d{4}", re.IGNORECASE)


def _assets_from_table(doc: Doc, bank_name: str | None) -> list[FieldValue] | None:
    """FDIC LTD comparison table: the subject bank's "Total Assets as of" cell in $000s."""
    lines = doc.lines("ltd_ratio")
    i = next((k for k, ln in enumerate(lines) if ASSETS_HEADER_RE.search(ln.text)), None)
    if i is None:
        return None
    header = " ".join(ln.text for ln in lines[i : i + 4])
    asof = parse_date(header[header.lower().find("as of") :])
    thousands = "000" in header
    j = next((k for k in range(i, min(i + 4, len(lines))) if DATE_RE.search(lines[k].text)), i)
    header_span = Sentence(doc.text[lines[i].start : lines[j].end], lines[i].start, lines[j].end, lines[i].page)
    for row in lines[i + 1 : i + 15]:
        if not COLUMN_GAP.search(row.text) or _HEADER_ROW_RE.search(row.text):
            continue
        parts = re.split(r" {3,}", row.text.strip(), maxsplit=1)
        if len(parts) < 2:
            continue
        label, rest = parts
        nums = re.findall(r"\d[\d,]*(?:\.\d+)?", rest)
        if not nums or re.fullmatch(r"[\d,.\s]*", label):
            continue
        if bank_name and fuzz.partial_ratio(label.lower(), bank_name.lower()) < 80:
            continue
        value = parse_money(nums[0], "thousand" if thousands else None)
        if value is None:
            continue
        note = "FDIC LTD comparison table, subject bank row" + ("" if thousands else "; no $000s marker")
        out = [_value(doc, "total_assets_usd", value, row, "ltd_ratio", note)]
        if asof:
            out.append(_value(doc, "total_assets_asof", asof, header_span, "ltd_ratio", "table header"))
        else:
            out.append(_abstain(doc, "total_assets_asof", "table header carries no date"))
        return out
    return None


def rule_total_assets(doc: Doc, bank_name: str | None) -> list[FieldValue]:
    """Total assets and their as-of date: exact table figure when printed, else the sentence."""
    table = _assets_from_table(doc, bank_name)
    if table:
        return table
    for s in doc.sentences("description_of_institution"):
        n = s.norm
        hits = [m for m in (p.search(n) for p in ASSETS_RES) if m]
        if not hits:
            continue
        m = min(hits, key=lambda h: h.start())
        value = parse_money(m.group(1))
        if value is None:
            continue
        out = [_value(doc, "total_assets_usd", value, s, "description_of_institution")]
        d = parse_date(n)
        if d:
            out.append(_value(doc, "total_assets_asof", d, s, "description_of_institution"))
        else:
            out.append(_abstain(doc, "total_assets_asof", "total assets sentence carries no date"))
        return out
    note = "no total assets sentence in the description of institution"
    return [_abstain(doc, "total_assets_usd", note), _abstain(doc, "total_assets_asof", note)]


# --------------------------------------------------------------------------- loan-to-deposit

AVERAGE_RE = re.compile(r"\baverage[sd]?\b", re.IGNORECASE)
LTD_WORD_RE = re.compile(r"\b(?:n?ltd|loan-to-deposit|loan to deposit)\b", re.IGNORECASE)
# A sentence about someone else's ratio: peers, the prior evaluation, or a state or
# host-state average such as "The report provided an average net loan-to-deposit ratio of 60
# percent for South Dakota" (FDIC_6078_OneAmericanBank_2024_ISB_NI.txt lines 354 to 355).
# Tested on the text before "average" only: the bank's own sentence often ends with a
# comparison clause ("which was comparable to the previous evaluation's", FRB_3664588 line
# 375; "decrease from the 107.9 percent average ... recorded at the prior evaluation",
# FDIC_12299 line 324).
COMPARATOR_RE = re.compile(
    r"peer|similarly|comparison|comparable|competitor|prior|previous|report provided|"
    r"state average|host-state|interagency",
    re.IGNORECASE,
)


def rule_ltd(doc: Doc) -> FieldValue:
    """Average (net) LTD ratio: the first percent after "average" in the bank's own sentence
    (a comparator before "average" disqualifies the sentence)."""
    for s in doc.sentences("ltd_ratio"):
        n = s.norm
        a = AVERAGE_RE.search(n)
        if not a or not LTD_WORD_RE.search(n) or COMPARATOR_RE.search(n[: a.start()]):
            continue
        p = PCT_RE.search(n, a.end())
        if p:
            return _value(doc, "avg_ltd_ratio_pct", float(p.group(1)), s, "ltd_ratio")
    return _abstain(doc, "avg_ltd_ratio_pct", "no average LTD sentence with a percent")


# --------------------------------------------------------------------------- inside-AA share

PRODUCT_KEYWORDS: dict[str, re.Pattern[str]] = {
    "home_mortgage": re.compile(r"home\s+mortgage|residential|hmda|home\s+purchase|refinanc|1-4\s+family", re.IGNORECASE),
    "small_business": re.compile(r"small\s+business|business\s+loans?|commercial", re.IGNORECASE),
    "small_farm": re.compile(r"small\s+farm|agricultur|farm\s+loans?", re.IGNORECASE),
    "consumer": re.compile(r"consumer", re.IGNORECASE),
}
BY_NUMBER_RE = re.compile(
    r"(\d{1,3}(?:\.\d+)?)\s*(?:percent|%)(?:\s+of\s+(?:its|the)\s+(?:bank's|institution's)?\s*(?:total\s+)?(?:loans|lending))?\s+by\s+number",
    re.IGNORECASE,
)
TOTAL_ROW_RE = re.compile(r"^total(?:\s+loans)?\b(.*)$", re.IGNORECASE)


def rule_inside_aa(doc: Doc) -> list[FieldValue]:
    """Share of loans by number inside the AA: the narrative sentence, else the table Total row."""
    for s in doc.sentences("aa_concentration"):
        n = s.norm
        if "inside" not in n.lower():
            continue
        m = BY_NUMBER_RE.search(n) if "by number" in n.lower() else PCT_RE.search(n)
        if not m:
            continue
        segment = n[m.start() : n.lower().find("inside")] if n.lower().find("inside") > m.start() else ""
        product = next((p for p, pat in PRODUCT_KEYWORDS.items() if pat.search(segment)), "all_reviewed_combined")
        return [
            _value(doc, "inside_aa_share_number_pct", float(m.group(1)), s, "aa_concentration"),
            _value(doc, "inside_aa_share_product", product, s, "aa_concentration"),
        ]
    for row in doc.lines("aa_concentration"):
        if not COLUMN_GAP.search(row.text):
            continue
        m = TOTAL_ROW_RE.match(row.text)
        if not m:
            continue
        nums = re.findall(r"\$?(\d[\d,]*(?:\.\d+)?)%?", m.group(1))
        if len(nums) >= 2 and "." in nums[1] and 0 <= float(nums[1]) <= 100:
            return [
                _value(doc, "inside_aa_share_number_pct", float(nums[1]), row, "aa_concentration", "table Total row, number column"),
                _value(doc, "inside_aa_share_product", "all_reviewed_combined", row, "aa_concentration", "table Total row"),
            ]
    note = "no inside-AA sentence with a percent and no Total row"
    return [_abstain(doc, "inside_aa_share_number_pct", note), _abstain(doc, "inside_aa_share_product", note)]


# --------------------------------------------------------------------------- assessment areas

_NUM = "|".join(_NUMBER_WORDS)
# The AA noun must not be followed by a geography or population noun: "the 14 AA counties"
# (FRB_663656_SmallBusinessBank_2024_SM_NI.txt lines 241 and 284) counts counties, not areas.
AA_COUNT_RE = re.compile(
    rf"(?<!table )\b(?P<n>{_NUM}|a\s+single|single|\d{{1,2}})\s+(?!(?:or|of)\b)(?:[a-z][a-z-]*\s+){{0,3}}?(?:assessment\s+areas?|AAs?)\b"
    r"(?!\s+(?:counties|county|census|tracts?|population|families|households|geograph))",
    re.IGNORECASE,
)
AA_SINGULAR_RES = [
    re.compile(r"\b(?:its|bank's|institution's)\s+(?:delineated\s+|designated\s+)?(?:AA|assessment\s+area)\s+(?:as\b|is\b|consists|includes|encompasses|comprises)", re.IGNORECASE),
    re.compile(r"\b(?:only|sole)\s+(?:AA|assessment\s+area)\b", re.IGNORECASE),
]
# Sentences that must not count: the prior evaluation's areas (FRB_3664588 line 213), the
# regulation's "one or more assessment areas" (FDIC_6078 line 141) and the OCC boilerplate
# "combined and evaluated as a single AA" (OCC_11715 lines 161 to 163).
AA_SKIP_RE = re.compile(
    r"\b(?:prior|previous|preceding)\b|or more|evaluated as a single (?:AA|assessment area)", re.IGNORECASE
)
AA_SECTIONS = ("description_of_assessment_area", "description_of_institution", "scope_of_evaluation")


def rule_n_assessment_areas(doc: Doc) -> FieldValue:
    """Count of designated assessment areas from a counting sentence in institution scope."""
    for section in AA_SECTIONS:
        for s in doc.sentences(section):
            n = s.norm
            if AA_SKIP_RE.search(n):
                continue
            m = AA_COUNT_RE.search(n)
            if m:
                word = ws(m.group("n")).lower()
                count = 1 if word in ("a single", "single") else _NUMBER_WORDS.get(word) or int(word)
                return _value(doc, "n_assessment_areas", count, s, section)
            if any(p.search(n) for p in AA_SINGULAR_RES):
                return _value(doc, "n_assessment_areas", 1, s, section, "singular definite AA statement")
    return _abstain(doc, "n_assessment_areas", "no counting sentence in institution scope")


# --------------------------------------------------------------------------- loan products

PRODUCT_TRIGGER_RE = re.compile(
    r"\b(?:reviewed|analyzed|analysed|sampled|sample\s+of|evaluated|considers|considered|"
    r"primary\s+(?:loan|lending)\s+products?|primary\s+product\s+lines?|major\s+product\s+lines?|products?\s+evaluated)\b",
    re.IGNORECASE,
)
PRODUCT_NEGATION_RE = re.compile(r"\b(?:not|no\s+other|excluded|no\s+longer)\b", re.IGNORECASE)


def rule_loan_products(doc: Doc) -> FieldValue:
    """Products the Lending Test reviewed: keywords in review sentences of the Scope section.

    A sentence contributes only when it has a review trigger ("reviewed", "sampled", "major
    product lines", ...) and no negation ("did not", "excluded", "no other"); products named in
    negated sentences are simply not added, never subtracted, because "Lemont does not purchase
    home mortgage loans" (OCC_11715 line 144) sits next to the sentence that makes home
    mortgage the reviewed product.
    """
    found: dict[str, Sentence] = {}
    first: Sentence | None = None
    for s in doc.sentences("scope_of_evaluation"):
        n = s.norm
        if not PRODUCT_TRIGGER_RE.search(n) or PRODUCT_NEGATION_RE.search(n):
            continue
        for product, pat in PRODUCT_KEYWORDS.items():
            if pat.search(n) and product not in found:
                found[product] = s
                first = first or s
    if not found or first is None:
        return _abstain(doc, "loan_products_evaluated", "no review sentence naming a product")
    note = "; ".join(f"{p}: p{s.page}" for p, s in found.items())
    return _value(doc, "loan_products_evaluated", sorted(found), first, "scope_of_evaluation", note)


# --------------------------------------------------------------------------- public API


def extract(doc_id: str, pages: list[Page], sections: dict[str, Section], regulator: str) -> list[FieldValue]:
    """Run every rule on one document; one FieldValue per field in :data:`RULE_FIELDS`."""
    doc = Doc(doc_id, regulator, pages, sections)
    out = [rule_regulator(doc)]
    name = rule_bank_name(doc)
    out += [name, rule_bank_id(doc), rule_exam_date(doc)]
    method = rule_exam_method(doc)
    out += [method, rule_overall_rating(doc)]
    out += rule_test_ratings(doc, method.value if method.status == "value" else None)
    out += rule_prior(doc)
    out += rule_total_assets(doc, name.value if name.status == "value" else None)
    out.append(rule_ltd(doc))
    out += rule_inside_aa(doc)
    out += [rule_n_assessment_areas(doc), rule_loan_products(doc)]
    return out


def extract_document(doc_id: str, pages: list[Page], regulator: str) -> list[FieldValue]:
    """Locate the sections and run :func:`extract`."""
    sections, _ = locate_all(doc_id, pages, regulator)
    return extract(doc_id, pages, sections, regulator)


# --------------------------------------------------------------------------- CLI


def _display(value: Any) -> str:
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    return "" if value is None else str(value)


def _rows(values: list[FieldValue]) -> list[dict[str, Any]]:
    rows = []
    for fv in values:
        d = fv.model_dump(mode="json")
        d["value"] = _display(fv.value)
        d["rules_version"] = RULES_VERSION
        rows.append(d)
    return rows


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m cra_extract.rules --samples-dir tests/fixtures/samples --out results/rules.csv``."""
    ap = argparse.ArgumentParser(description="Rule-based extraction (S1), one row per (doc, field).")
    ap.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    ap.add_argument("--text-dir", type=Path, default=TEXT_DIR, help="folder with {doc_id}.pages.json")
    ap.add_argument("--samples-dir", type=Path, help="run on pdftotext .txt files instead of the manifest")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "rules.csv")
    args = ap.parse_args(argv)

    docs: list[tuple[str, str, list[Page]]] = []
    if args.samples_dir:
        for txt in sorted(args.samples_dir.glob("*.txt")):
            docs.append((txt.stem, txt.stem.split("_")[0], pages_from_text(txt.read_text(encoding="utf-8"))))
    elif args.manifest.exists():
        with open(args.manifest, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                path = args.text_dir / f"{row['doc_id']}.pages.json"
                if path.exists():
                    docs.append((row["doc_id"], row["regulator"], load_pages_json(path)))
    else:
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    if not docs:
        print("no documents with page text found", file=sys.stderr)
        return 1

    rows: list[dict[str, Any]] = []
    for doc_id, regulator, pages in docs:
        rows += _rows(extract_document(doc_id, pages, regulator))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    n_abstain = sum(r["status"] == "abstain" for r in rows)
    print(f"rules {RULES_VERSION}: {len(docs)} documents, {len(rows)} values, {n_abstain} abstentions; written to {args.out}")
    for r in rows:
        print(f"{r['doc_id'][:40]:40s} {r['field']:28s} {r['status']:8s} {r['value'][:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
