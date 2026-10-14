"""Two-stage, rule-based grounding check for extracted values (design section 4.1).

Every LLM value must come with a verbatim quote. The check has no LLM judge:

Stage 1, locate. The quote is searched in every page's text with an OCR-tolerant
fuzzy match (``rapidfuzz.fuzz.partial_ratio_alignment``), after normalizing
whitespace and quote characters on both sides. A match needs a score of at least
``MIN_LOCATE_SCORE`` (90). The page named by the value is tried first; all pages
are scanned when it does not match. Offsets are mapped back to the original page
text.

Stage 2, support. (a) scope: an institution-level field is supported only by a
quote on an institution-scope page or inside an institution-scope section (a
state-level test rating must not support the institution's rating; see
``FRB_3664588_FieldPoint_2024_ISB_NI.txt`` lines 1165 to 1166, where the New York
state section repeats "The Lending Test is rated: Needs to Improve"). (b) value
presence: the normalized value appears in the quote (enums through their word
forms, dates in ISO or printed form, numbers within tolerance, sets element by
element). (c) Group C: a quote that asserts a finding must contain a finding
predicate and must not be a stored "no evidence" template (``templates.residual``
when ``cra_extract.templates`` exists, otherwise a small built-in negation check).

Failures are reported in three categories: ``quote_not_found``,
``quote_out_of_scope`` and ``quote_does_not_support``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

from rapidfuzz import fuzz

from cra_extract.config import field_spec, load_fields
from cra_extract.models import RATING_WORDS, FieldValue, Page, Section

GroundingFailure = Literal["quote_not_found", "quote_out_of_scope", "quote_does_not_support"]

MIN_LOCATE_SCORE = 90.0
TEMPLATE_MATCH_SCORE = 90.0
SUPPORTING_SCOPES = {"institution", "unknown"}

FINDING_PREDICATES = ("identified", "lowered", "violation", "substantive", "cited", "downgrade")

# Negated boilerplate seen in the samples (one phrase per regulator family):
#   FDIC "Examiners did not identify any evidence of discriminatory or other illegal credit
#        practices" (FDIC_6078_OneAmericanBank_2024_ISB_NI.txt line 658)
#   FRB  "No evidence of discriminatory or other illegal credit practices ... was identified"
#        (FRB_3664588_FieldPoint_2024_ISB_NI.txt lines 582 to 583)
#   OCC  the forward-looking "Pursuant to 12 CFR 25.28(c)" paragraph
#        (OCC_703395_GNBank_2023_NI.txt lines 186 to 191)
NEGATION_PATTERNS = (
    r"did not identify",
    r"no evidence of",
    r"has not identified",
    r"have not identified",
    r"was not identified",
    r"were not identified",
    r"in compliance with the substantive provisions",
    r"pursuant to 12 cfr",
)

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}

SCALE_WORDS = {"thousand": 1e3, "million": 1e6, "billion": 1e9}

# Word forms accepted as evidence for an enum code, as regular expressions searched in the
# normalized (lower-cased, whitespace-collapsed) quote. A code missing here falls back to its
# own tokens (underscores become spaces), matched as a substring. Short acronyms carry word
# boundaries; forms that would match unrelated prose (bare "order", "product", "prior",
# "affect", "collection", "violation") are phrased as the multi-word expression a PE prints.
WORD_FORMS: dict[str, list[str]] = {
    "O": ["outstanding"],
    "S": ["satisfactory"],
    "NI": ["needs to improve"],
    "SN": ["substantial noncompliance", "substantial non-compliance"],
    "OCC": ["comptroller of the currency", r"\bocc\b"],
    "FDIC": ["federal deposit insurance corporation", r"\bfdic\b"],
    "FRB": ["federal reserve", r"\bfrb\b"],
    "small": [r"(?<!intermediate )small (bank|institution)"],
    "isb": ["intermediate small bank", "intermediate small institution"],
    "home_mortgage": ["home mortgage", "residential", r"\bhmda\b", "mortgage"],
    "small_business": ["small business", "commercial"],
    "small_farm": ["small farm", "agricultur"],
    "consumer": ["consumer"],
    "all_reviewed_combined": ["total", "combined", "all loans", "loans reviewed"],
    "discrimination": ["discriminat", "redlining", "fair lending", "fair housing act", "equal credit"],
    "other_illegal_credit_practice": [
        "illegal credit practice", r"\budap\b", "unfair", "deceptive",
        # a violation of a law other than the fair lending statutes
        r"violations? of (?!(?:the )?(?:fair housing|equal credit|ecoa|fha|regulation b))",
    ],
    "technical_only_noted": ["technical", "isolated"],
    "substantive_not_lowered": ["did not affect", "not affect", "did not lower", "not lowered", "no effect", "did not result in"],
    "substantive_lowered": ["lowered", "downgrade", "reduced", "lower the"],
    "affected_unspecified": [r"(?<!not )(?<!no )affect", r"\bconsider", r"(?<!no )impact", "taken into account", "weighed"],
    "overall": ["cra rating", "overall"],
    "lending_test": ["lending test"],
    "cd_test": ["community development test"],
    "state_or_msa": [r"\bstate\b", r"\bmsa\b", "metropolitan"],
    "ECOA_RegB": ["equal credit opportunity", r"\becoa\b", "regulation b"],
    "FHA": ["fair housing act", r"\bfha\b"],
    "FTC_s5_UDAP": ["federal trade commission act", "section 5", r"\budap\b", "unfair or deceptive"],
    "CFPA_UDAAP": ["consumer financial protection act", r"\budaap\b", "abusive"],
    "HOEPA": ["home ownership and equity protection", r"\bhoepa\b"],
    "RESPA_s8": ["real estate settlement procedures", r"\brespa\b"],
    "TILA": ["truth in lending", r"\btila\b", "regulation z"],
    "SCRA": ["servicemembers civil relief", r"\bscra\b"],
    "MLA": ["military lending act", r"\bmla\b"],
    "redlining": ["redlining", "redline"],
    "pricing": ["pricing", "priced", r"\brates?\b"],
    "underwriting": ["underwriting", "denial", "denied", "approv"],
    "marketing": ["marketing", "advertis"],
    "servicing": ["servicing", "debt collection", "collection practice", "collection activit"],
    "UDAP_product": [r"\budap\b", "deceptive", "unfair", "add-on product", "ancillary product", "product feature"],
    "this_CRA_exam": ["this evaluation", "evaluation period", "review period", "examiners identified", "examiners cited"],
    "concurrent_compliance_exam": ["compliance exam", "concurrent"],
    "prior_exam_referenced": [r"(?:prior|previous) (?:cra )?(?:evaluation|examination|exam)\b"],
    "enforcement_action_referenced": ["consent order", "enforcement action", "cease and desist", "formal agreement", "civil money penalt"],
    "DOJ_HUD_referral": ["department of justice", r"\bdoj\b", r"\bhud\b", "referral", "referred"],
}

# Codes whose evidence is the conjunction of other codes' forms.
COMPOUND_CODES: dict[str, tuple[str, ...]] = {
    "both": ("discrimination", "other_illegal_credit_practice"),
}

# Codes that need no evidence in the quote because they state the absence of information.
ALWAYS_SUPPORTED = {"unstated", "other", "unspecified"}


@dataclass
class GroundingResult:
    """Outcome of ``check``. ``failure`` is ``None`` when the quote supports the value."""

    found: bool
    page: int | None
    char_start: int | None
    char_end: int | None
    score: float
    supports: bool
    failure: GroundingFailure | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------- normalization


_QUOTE_CHARS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "′": "'", "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ",
}


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Lower-case, unify quote and dash characters and collapse whitespace.

    Returns the normalized string and, for every normalized character, the index of
    the original character it came from, so match offsets can be mapped back.
    """
    out: list[str] = []
    index: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        ch = _QUOTE_CHARS.get(ch, ch)
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            index.append(i)
            prev_space = True
        else:
            out.append(ch.lower())
            index.append(i)
            prev_space = False
    if out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


def normalize(text: str) -> str:
    """Normalized text without the offset map."""
    return normalize_with_map(text)[0]


# ------------------------------------------------------------- stage 1: locate


def locate_quote(
    quote: str,
    pages: list[Page],
    preferred_page: int | None = None,
    min_score: float = MIN_LOCATE_SCORE,
) -> tuple[int, int, int, float] | None:
    """Fuzzy-locate ``quote`` in the page texts.

    Returns ``(page_number, char_start, char_end, score)`` in original page-text
    offsets, or ``None`` when no page reaches ``min_score``. The preferred page is
    tried first and accepted if it matches; otherwise the best page wins.
    """
    needle = normalize(quote)
    if not needle:
        return None
    order = sorted(pages, key=lambda p: (p.number != preferred_page, p.number))
    best: tuple[int, int, int, float] | None = None
    for page in order:
        hay, index = normalize_with_map(page.text)
        if not hay:
            continue
        al = fuzz.partial_ratio_alignment(needle, hay)
        if al is None:
            continue
        score = float(al.score)
        if score >= min_score:
            start = index[al.dest_start] if al.dest_start < len(index) else len(page.text)
            end_idx = max(al.dest_start, al.dest_end - 1)
            end = index[end_idx] + 1 if end_idx < len(index) else len(page.text)
            hit = (page.number, start, end, score)
            if page.number == preferred_page:
                return hit
            if best is None or score > best[3]:
                best = hit
    return best


# ------------------------------------------------------- stage 2: scope check


def in_supporting_scope(
    page_number: int,
    quote: str,
    pages: list[Page],
    sections: dict[str, Section],
    required_scope: str = "institution",
) -> bool:
    """True when the located page has the required scope (or no scope tag), or when the
    quote lies inside a section with that scope (sections are matched by page range and
    a fuzzy containment test, so no offset convention is needed)."""
    page = next((p for p in pages if p.number == page_number), None)
    if page is not None and not page.is_toc and page.scope in SUPPORTING_SCOPES | {required_scope}:
        return True
    needle = normalize(quote)
    for sec in sections.values():
        if sec.scope != required_scope:
            continue
        if not (sec.page_start <= page_number <= sec.page_end):
            continue
        if fuzz.partial_ratio(needle, normalize(sec.text)) >= MIN_LOCATE_SCORE:
            return True
    return False


# ------------------------------------------------ stage 2: value presence


def parse_date(value: Any) -> date | None:
    """Parse ISO or common printed date forms; ``None`` when not a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%b. %d, %Y", "%m/%d/%Y", "%B %d %Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def date_forms(d: date) -> list[str]:
    """Normalized strings under which a date may be printed in a PE, including the
    two-digit-year forms of FDIC table headers (M/D/YY and MM/DD/YY: "Total Assets as of
    12/31/23 $(000s)", FDIC_16733_20240410 p9)."""
    forms = [
        d.isoformat(),
        f"{d:%B} {d.day}, {d.year}",
        f"{d:%B} {d.day} {d.year}",
        f"{d:%b} {d.day}, {d.year}",
        f"{d:%b}. {d.day}, {d.year}",
        f"{d.month}/{d.day}/{d.year}",
        f"{d:%m/%d/%Y}",
        f"{d.month}/{d.day}/{d:%y}",
        f"{d:%m/%d/%y}",
    ]
    return [normalize(f) for f in forms]


def date_in_text(d: date, norm_text: str) -> bool:
    """True when ``norm_text`` (normalized) prints ``d`` in one of :func:`date_forms`. A
    numeric form must not continue another number on either side, so "12/31/20" is not
    read inside "12/31/2021" and "2/28/2023" is not read inside "12/28/2023"."""
    return any(
        re.search(rf"(?<![\d/]){re.escape(f)}(?![\d/])", norm_text) for f in date_forms(d)
    )


UNIT_MARKER_RE = re.compile(r"\(?000s?\)?|thousand|million|billion|\$")
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_SPAN_RE = re.compile(
    rf"(?:{_MONTHS})\.? \d{{1,2}},? \d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|\b(?:19|20)\d{{2}}\b"
)


def numbers_in(text: str, scale_bare: bool = True) -> list[float]:
    """Numbers in ``text`` with their scale words applied (``$1.0 billion`` gives 1e9),
    plus spelled-out small integers. With ``scale_bare`` (the default for dollar and
    percent fields) and a unit marker in the text (``(000s)``, ``thousand``, ``$``),
    every bare number also yields its thousand and million multiples to cover
    ``(000s)`` tables. Numbers that are part of a date (``March 31, 2024``,
    ``3/31/2024``, ``2024-03-31``, a bare year) are left out, so a date in the quote
    cannot stand in for an asset figure or an assessment-area count."""
    norm = normalize(text)
    date_spans = [m.span() for m in DATE_SPAN_RE.finditer(norm)]
    has_unit = bool(UNIT_MARKER_RE.search(norm))
    out: list[float] = []
    for m in re.finditer(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(thousand|million|billion)?", norm):
        raw = m.group(1).replace(",", "") + (m.group(2) or "")
        try:
            x = float(raw)
        except ValueError:
            continue
        if any(a <= m.start() < b for a, b in date_spans):
            continue
        scale = SCALE_WORDS.get(m.group(3) or "", 1.0)
        out.append(x * scale)
        if scale == 1.0 and scale_bare and has_unit:
            out.extend([x * 1e3, x * 1e6])
    for word, n in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", norm):
            out.append(float(n))
    return out


def number_tolerance(spec: dict[str, Any]) -> tuple[float, float]:
    """``(abs_tol, rel_tol)`` implied by the field's tolerance rule."""
    tol = spec.get("tolerance", "exact")
    if tol == "within_1pct_same_quarter":
        return (0.0, 0.01)
    if tol == "within_0.1pp":
        return (0.1, 0.0)
    return (0.0, 1e-9)


def number_matches(target: float, candidates: list[float], abs_tol: float, rel_tol: float) -> bool:
    for c in candidates:
        if abs(c - target) <= max(abs_tol, rel_tol * abs(target)) + 1e-9:
            return True
    return False


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"true", "1", "yes", "t"}:
        return True
    if s in {"false", "0", "no", "f"}:
        return False
    return None


def parse_set(value: Any) -> set[str]:
    """Parse a set value from a list, a JSON list string or a delimited string."""
    if value is None:
        return set()
    if isinstance(value, (set, frozenset, list, tuple)):
        return {str(v).strip() for v in value if str(v).strip()}
    s = str(value).strip()
    if not s:
        return set()
    if s.startswith("["):
        try:
            return {str(v).strip() for v in json.loads(s)}
        except json.JSONDecodeError:
            s = s.strip("[]")
    return {p.strip().strip("'\"") for p in re.split(r"[|;,]", s) if p.strip().strip("'\"")}


def _has_negation(norm_quote: str) -> bool:
    return any(re.search(p, norm_quote) for p in NEGATION_PATTERNS)


def _has_predicate(norm_quote: str) -> bool:
    return any(p in norm_quote for p in FINDING_PREDICATES)


def enum_in_quote(code: str, norm_quote: str) -> bool:
    """True when one of the code's word forms (regular expressions) matches the normalized
    quote. A compound code (``both``) needs every component code to be present."""
    if code in ALWAYS_SUPPORTED:
        return True
    if code in COMPOUND_CODES:
        return all(enum_in_quote(part, norm_quote) for part in COMPOUND_CODES[code])
    forms = WORD_FORMS.get(code)
    if forms is None:
        # Ratings may also be given by their long name as the value itself.
        forms = [re.escape(normalize(code.replace("_", " ")))]
        for word, short in RATING_WORDS.items():
            if normalize(code) == normalize(word):
                forms = WORD_FORMS[short]
    return any(re.search(f, norm_quote) for f in forms)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+")


def _sentences(norm_quote: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT_RE.split(norm_quote) if part.strip()]


def asserts_finding_sentence(norm_quote: str) -> bool:
    """True when at least one sentence of the quote carries a finding predicate and no
    negation pattern, so a finding followed by a negated "no other practices" sentence
    still counts as an assertion."""
    return any(_has_predicate(sent) and not _has_negation(sent) for sent in _sentences(norm_quote))


def value_in_quote(value: Any, quote: str, spec: dict[str, Any]) -> bool:
    """Stage 2(b) and (d): does the normalized value appear in the quote?"""
    norm_quote = normalize(quote)
    ftype = spec.get("type", "str")
    if ftype == "enum":
        return enum_in_quote(str(value), norm_quote)
    if ftype == "set":
        elements = parse_set(value)
        return bool(elements) and all(enum_in_quote(e, norm_quote) for e in elements)
    if ftype == "bool":
        b = parse_bool(value)
        if b is None:
            return False
        if b:
            return asserts_finding_sentence(norm_quote)
        return not asserts_finding_sentence(norm_quote)
    if ftype == "date":
        d = parse_date(value)
        return d is not None and date_in_text(d, norm_quote)
    if ftype in {"int", "float"}:
        try:
            target = float(str(value).replace(",", "").replace("$", "").replace("%", ""))
        except ValueError:
            return False
        abs_tol, rel_tol = number_tolerance(spec)
        scale_bare = not (ftype == "int" and spec.get("tolerance", "exact") == "exact")
        return number_matches(target, numbers_in(quote, scale_bare=scale_bare), abs_tol, rel_tol)
    # Free strings (bank_name): fuzzy containment.
    return fuzz.partial_ratio(normalize(str(value)), norm_quote) >= MIN_LOCATE_SCORE


# ------------------------------------------------ stage 2: Group C templates


def _asserts_finding(value: Any, spec: dict[str, Any]) -> bool:
    """A Group C value asserts a finding unless it is a false boolean."""
    if spec.get("type") == "bool":
        return parse_bool(value) is True
    return value is not None and str(value).strip() != ""


def matches_template(quote: str, regulator: str) -> bool:
    """True when the quote is boilerplate: a stored template matches it (score at least
    ``TEMPLATE_MATCH_SCORE``) and what ``templates.residual`` leaves carries no finding
    predicate, or (without that module) the quote carries a negation and no finding
    predicate outside the negated phrase. A short residual that still asserts a finding
    is a finding sentence appended to the template (design 4.1(c): the quote is not within
    edit distance k of the template), not boilerplate; the length of the residual is not
    used."""
    try:
        templates = importlib.import_module("cra_extract.templates")
    except ImportError:
        templates = None
    if templates is not None and hasattr(templates, "residual"):
        res = templates.residual(quote, regulator)
        if isinstance(res, str):
            return _residual_is_boilerplate(res)
        text = getattr(res, "residual_text", None)
        if isinstance(text, str):
            matched = bool(getattr(res, "removed", None)) or float(getattr(res, "score", 0.0)) >= TEMPLATE_MATCH_SCORE
            return matched and _residual_is_boilerplate(text)
    norm = normalize(quote)
    if not _has_negation(norm):
        return False
    stripped = norm
    for pat in NEGATION_PATTERNS:
        stripped = re.sub(pat + r"[^.]*\.?", " ", stripped)
    return not _has_predicate(stripped)


def _residual_is_boilerplate(residual_text: str) -> bool:
    """Nothing of substance is left once the template is removed: no sentence of the
    residual asserts a finding (predicate without negation)."""
    return not asserts_finding_sentence(normalize(residual_text))


# ----------------------------------------------------------------- check


def check(
    value: FieldValue,
    pages: list[Page],
    sections: dict[str, Section],
    spec: dict[str, Any] | None = None,
    regulator: str = "",
) -> GroundingResult:
    """Run both stages for one extracted value and classify the outcome."""
    spec = spec if spec is not None else field_spec(value.field)
    if value.status != "value":
        return GroundingResult(False, None, None, None, 0.0, True, None, "nothing to ground")
    if not value.quote or not value.quote.strip():
        return GroundingResult(False, None, None, None, 0.0, False, "quote_not_found", "empty quote")

    hit = locate_quote(value.quote, pages, preferred_page=value.page)
    if hit is None:
        return GroundingResult(False, None, None, None, 0.0, False, "quote_not_found", "no page at score >= 90")
    page_no, start, end, score = hit

    if not in_supporting_scope(page_no, value.quote, pages, sections):
        page = next((p for p in pages if p.number == page_no), None)
        detail = f"page {page_no} scope {page.scope if page else 'missing'}"
        return GroundingResult(True, page_no, start, end, score, False, "quote_out_of_scope", detail)

    # The value must appear in the quote or in the page span the quote was matched to
    # (the span is the verbatim source; the quote may carry OCR or transcription noise).
    page = next((p for p in pages if p.number == page_no), None)
    span = page.text[start:end] if page is not None else ""
    if not (value_in_quote(value.value, value.quote, spec) or (span and value_in_quote(value.value, span, spec))):
        return GroundingResult(True, page_no, start, end, score, False, "quote_does_not_support", "value not in quote")

    if spec.get("group") == "C" and _asserts_finding(value.value, spec):
        norm = normalize(value.quote)
        if not _has_predicate(norm):
            return GroundingResult(True, page_no, start, end, score, False, "quote_does_not_support", "no finding predicate")
        if matches_template(value.quote, regulator):
            return GroundingResult(True, page_no, start, end, score, False, "quote_does_not_support", "quote is a no-evidence template")

    return GroundingResult(True, page_no, start, end, score, True, None, "")


# -------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """Check one value against a page-wise text file (pages split on form feed)."""
    parser = argparse.ArgumentParser(description="Grounding check for one extracted value.")
    parser.add_argument("text_file", help="document text with pages separated by form feed")
    parser.add_argument("--field", required=True, choices=sorted(load_fields()["fields"]))
    parser.add_argument("--value", required=True)
    parser.add_argument("--quote", required=True)
    parser.add_argument("--page", type=int, default=None)
    parser.add_argument("--regulator", default="")
    args = parser.parse_args(argv)
    with open(args.text_file, encoding="utf-8") as fh:
        texts = fh.read().split("\f")
    pages = [Page(number=i + 1, text=t, chars=len(t)) for i, t in enumerate(texts)]
    fv = FieldValue(doc_id="cli", field=args.field, system="cli", value=args.value, quote=args.quote, page=args.page)
    result = check(fv, pages, {}, regulator=args.regulator)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.supports else 1


if __name__ == "__main__":
    raise SystemExit(main())
