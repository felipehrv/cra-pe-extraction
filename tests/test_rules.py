"""Tests for the rule-based extractors (src/cra_extract/rules.py).

The sample cases read ``tests/fixtures/expected_samples.yaml`` (hand-derived values with
.txt line citations), build pages from the pdftotext text, locate the sections and compare
every rule's value. Two documented abstentions are asserted explicitly so that a change in
behavior shows up as a failure rather than silently becoming a value.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
import yaml
from conftest import SAMPLE_META, SAMPLES

from cra_extract.config import applicability
from cra_extract.locate import doc_text, pages_from_text
from cra_extract.models import FieldValue, Page, Section
from cra_extract.rules import (
    RULE_FIELDS,
    RULES_VERSION,
    Doc,
    extract,
    extract_document,
    normalize_rating,
    parse_date,
    parse_money,
    split_sentences,
)

EXPECTED_PATH = Path(__file__).parent / "fixtures" / "expected_samples.yaml"

# (sample stem, field): the rules abstain on purpose; see the module docstring of rules.py.
KNOWN_ABSTAIN = {
    ("OCC_11715_LemontNB_2024_SN", "n_assessment_areas"),
    ("FDIC_12299_SouthStar_2022_ISB_NI_fairlending", "inside_aa_share_number_pct"),
    ("FDIC_12299_SouthStar_2022_ISB_NI_fairlending", "inside_aa_share_product"),
}
APPLICABILITY_FIELDS = {"lending_test_rating", "cd_test_rating"}


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module")
def expected() -> dict:
    with open(EXPECTED_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["samples"]


@pytest.fixture(scope="module")
def extracted() -> dict[str, tuple[str, list[Page], list[FieldValue]]]:
    """Rules output per sample, computed once."""
    out = {}
    for stem, (regulator, *_rest) in SAMPLE_META.items():
        pages = pages_from_text((SAMPLES / f"{stem}.txt").read_text(encoding="utf-8"))
        out[stem] = (regulator, pages, extract_document(stem, pages, regulator))
    return out


def _by_field(values: list[FieldValue]) -> dict[str, FieldValue]:
    return {fv.field: fv for fv in values}


def _matches(actual, exp) -> bool:
    if isinstance(exp, float):
        return isinstance(actual, (int, float)) and abs(float(actual) - exp) < 0.051
    if isinstance(exp, list):
        return sorted(actual) == sorted(exp)
    if isinstance(exp, date):
        return actual == exp
    return str(actual) == str(exp)


# --------------------------------------------------------------------------- sample cases


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
def test_every_field_emitted_once(stem, extracted):
    _, _, values = extracted[stem]
    fields = [fv.field for fv in values]
    assert sorted(fields) == sorted(RULE_FIELDS)
    assert all(fv.system == "rules" and fv.doc_id == stem for fv in values)


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
@pytest.mark.parametrize("field", RULE_FIELDS)
def test_rule_value_matches_expected(stem, field, expected, extracted):
    exp = expected[stem][field]
    fv = _by_field(extracted[stem][2])[field]
    if (stem, field) in KNOWN_ABSTAIN:
        assert fv.status == "abstain", f"{stem} {field}: documented abstention changed to {fv.status} {fv.value!r}"
        return
    if "null_reason" in exp:
        if field in APPLICABILITY_FIELDS:
            assert fv.status == "absent", f"{stem} {field}: applicability cell must be absent, got {fv.status}"
        else:
            assert fv.status != "value", f"{stem} {field}: fabricated {fv.value!r} for a field absent from the document"
        return
    assert fv.status == "value", f"{stem} {field}: expected {exp['value']!r}, got {fv.status} ({fv.notes})"
    assert _matches(fv.value, exp["value"]), f"{stem} {field}: expected {exp['value']!r}, got {fv.value!r} from {fv.quote!r}"


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
def test_quotes_are_verbatim_at_offsets(stem, extracted):
    regulator, pages, values = extracted[stem]
    text = doc_text(pages)
    starts = []
    offset = 0
    for p in pages:
        starts.append(offset)
        offset += len(p.text) + 1
    for fv in values:
        if fv.status != "value":
            assert fv.quote is None and fv.char_start is None
            continue
        assert fv.quote, f"{stem} {fv.field}: value without a quote"
        assert fv.char_start is not None and fv.char_end is not None and fv.char_start < fv.char_end
        assert _ws(text[fv.char_start : fv.char_end]) == _ws(fv.quote), f"{stem} {fv.field}: quote not at offsets"
        page = max(i + 1 for i, s in enumerate(starts) if s <= fv.char_start)
        assert fv.page == page, f"{stem} {fv.field}: page {fv.page} but offset is on page {page}"
        assert fv.scope == "institution"


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
def test_group_a_agrees_with_manifest(stem, extracted):
    regulator, bank_id, exam_date, method, rating = SAMPLE_META[stem]
    by = _by_field(extracted[stem][2])
    assert by["regulator"].value == regulator
    assert by["bank_id"].value == bank_id
    assert by["exam_date"].value == date.fromisoformat(exam_date)
    assert by["exam_method"].value == method
    assert by["overall_rating"].value == rating


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
def test_applicability_cells_are_absent_never_fabricated(stem, extracted):
    regulator, _, _, method, rating = SAMPLE_META[stem]
    by = _by_field(extracted[stem][2])
    for field in APPLICABILITY_FIELDS:
        if applicability(field, regulator, method) == "absent_from_document":
            assert by[field].status == "absent"
            assert by[field].value is None
        else:
            assert by[field].status == "value"
    if regulator == "OCC" and method == "small":
        assert by["lending_test_rating"].value == by["overall_rating"].value


def test_quotes_never_come_from_state_scope(extracted):
    """FieldPoint repeats the Lending Test sentence in the State of New York section."""
    _, pages, values = extracted["FRB_3664588_FieldPoint_2024_ISB_NI"]
    by = _by_field(values)
    assert by["lending_test_rating"].page == 4
    assert by["n_assessment_areas"].value == 4  # not the prior evaluation's two areas


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$1.2 billion", 1_200_000_000),
        ("$345.6 million", 345_600_000),
        ("$123,456 thousand", 123_456_000),
        ("$84.7 million", 84_700_000),
        ("$463 million", 463_000_000),
        ("$691,000", 691_000),
    ],
)
def test_parse_money(text, expected):
    assert parse_money(text) == expected


def test_parse_money_table_unit_and_miss():
    assert parse_money("1,159,094", "thousand") == 1_159_094_000
    assert parse_money("$1.2 billion", "thousand") == 1_200_000_000  # printed unit wins
    assert parse_money("no figure here") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("March 27, 2023", date(2023, 3, 27)),
        ("dated June 19,\n2021.", date(2021, 6, 19)),
        ("03/27/2023", date(2023, 3, 27)),
        ("as of 6/30/2022", date(2022, 6, 30)),
        ("September 30, 2023,", date(2023, 9, 30)),
        ("no date", None),
        ("February 30, 2023", None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "word,code",
    [
        ("Outstanding", "O"),
        ("satisfactory", "S"),
        ("Needs to Improve", "NI"),
        ("Needs to\nImprove", "NI"),
        ("Substantial Noncompliance", "SN"),
        ("Substantial Non-Compliance", "SN"),
        ("reasonable", None),
    ],
)
def test_normalize_rating(word, code):
    assert normalize_rating(word) == code


def test_split_sentences_keeps_decimals_and_abbreviations():
    text = (
        "The LTD ratio averaged 54.32 percent. SouthStar Bank, S.S.B. demonstrated performance.\n"
        "RSSD No. 3664588 is the id.\n\n"
        "Second paragraph here.\n"
    )
    sents = [s.norm for s in split_sentences(text)]
    assert sents == [
        "The LTD ratio averaged 54.32 percent.",
        "SouthStar Bank, S.S.B. demonstrated performance.",
        "RSSD No. 3664588 is the id.",
        "Second paragraph here.",
    ]


def test_split_sentences_offsets_and_page_break():
    """A sentence straddling a page break keeps one contiguous span; furniture is trimmed."""
    text = "The OCC will consider any information before the end of the institution's next\n\n   3\n   Charter Number: 703395\n\nperformance evaluation.\nNext one.\n"
    sents = split_sentences(text, base=100)
    assert [s.norm for s in sents][0].startswith("The OCC will consider")
    assert sents[0].norm.endswith("performance evaluation.")
    assert text[sents[0].start - 100 : sents[0].end - 100] == sents[0].text
    assert sents[1].norm == "Next one."


def test_split_sentences_skips_tables_and_bullets_are_prose():
    text = (
        "Loan Category                          $(000s)     %\n"
        "Consumer Loans                           3,950    0.5\n"
        "\n"
        "         The bank has total assets of $94.2 million as of June 30, 2024.\n"
        "         The bank operates one office.\n"
    )
    sents = [s.norm for s in split_sentences(text)]
    assert sents == [
        "The bank has total assets of $94.2 million as of June 30, 2024.",
        "The bank operates one office.",
    ]


def test_version_and_changelog():
    assert re.fullmatch(r"\d+\.\d+\.\d+", RULES_VERSION)


# --------------------------------------------------------------------------- abstention


def _section(name: str, text: str, doc_id: str = "X_1_20240101") -> Section:
    return Section(name=name, doc_id=doc_id, page_start=1, page_end=1, char_start=0, char_end=len(text), text=text)


def test_abstains_when_sections_are_missing():
    pages = [Page(number=1, text="PUBLIC DISCLOSURE\n\nNothing useful here.\n")]
    values = extract("X_1_20240101", pages, {}, "FDIC")
    assert {fv.field for fv in values} == set(RULE_FIELDS)
    assert all(fv.status == "abstain" for fv in values)
    assert all(fv.value is None and fv.quote is None for fv in values)


def test_abstains_on_unmatched_patterns_but_absent_on_applicability():
    text = "INSTITUTION RATING\n\nINSTITUTION'S CRA RATING: This institution is rated Satisfactory.\n"
    scope = "SCOPE OF EVALUATION\n\nExaminers used the Interagency Small Institution Examination Procedures to evaluate the bank.\n"
    pages = [Page(number=1, text=text + scope)]
    sections = {
        "rating": _section("rating", text),
        "scope_of_evaluation": Section(
            name="scope_of_evaluation", doc_id="X_1_20240101", page_start=1, page_end=1,
            char_start=len(text), char_end=len(text) + len(scope), text=scope,
        ),
    }
    by = _by_field(extract("X_1_20240101", pages, sections, "FDIC"))
    assert by["overall_rating"].value == "S"
    assert by["exam_method"].value == "small"
    assert by["lending_test_rating"].status == "absent"
    assert by["cd_test_rating"].status == "absent"
    assert by["avg_ltd_ratio_pct"].status == "abstain"
    assert by["prior_evaluation_date"].status == "abstain"


def test_isb_rating_page_prints_component_ratings():
    text = (
        "INSTITUTION'S CRA RATING: This institution is rated Needs to Improve.\n"
        "The Lending Test is rated: Needs to Improve.\n"
        "The Community Development Test is rated: Satisfactory.\n"
    )
    pages = [Page(number=1, text=text)]
    by = _by_field(extract("FRB_1_20240101", pages, {"rating": _section("rating", text)}, "FRB"))
    assert by["overall_rating"].value == "NI"
    assert by["lending_test_rating"].value == "NI"
    assert by["cd_test_rating"].value == "S"
    assert by["exam_method"].value == "isb"  # inferred from the CD test sentence
    assert "inferred" in (by["exam_method"].notes or "")


def test_aa_count_skips_geography_nouns():
    """FRB_663656 lines 241 and 284: '14 AA counties' counts counties, not assessment areas."""
    from cra_extract.rules import AA_COUNT_RE

    assert AA_COUNT_RE.search("families living below the poverty level in the 14 AA counties ranged from a low") is None
    assert AA_COUNT_RE.search("Affordability ratios within the 14 AA counties ranged from 33.0 percent") is None
    assert AA_COUNT_RE.search("The bank has designated two assessment areas.").group("n") == "two"
    assert AA_COUNT_RE.search("The bank operates in 4 AAs within the state.").group("n") == "4"


def test_ltd_rejects_state_average_sentence():
    """FDIC_6078 lines 354 to 355: the host-state average must not be read as the bank's ratio."""
    text = (
        "The report provided an average net loan-to-deposit ratio of 60 percent for South Dakota. "
        "Given this information, the loan-to-deposit ratio is reasonable.\n"
    )
    pages = [Page(number=1, text=text)]
    by = _by_field(extract("FDIC_1_20240101", pages, {"ltd_ratio": _section("ltd_ratio", text)}, "FDIC"))
    assert by["avg_ltd_ratio_pct"].status == "abstain"
    own = "The bank's average net loan-to-deposit ratio is 128.1 percent over the past 22 calendar quarters.\n"
    by = _by_field(extract("FDIC_1_20240101", [Page(number=1, text=own)], {"ltd_ratio": _section("ltd_ratio", own)}, "FDIC"))
    assert by["avg_ltd_ratio_pct"].value == 128.1


def test_doc_page_of():
    pages = [Page(number=1, text="abc\n"), Page(number=2, text="defg\n"), Page(number=3, text="h\n")]
    doc = Doc("X_1_20240101", "OCC", pages, {})
    # page starts are 0, 5 and 11; the joining form feed belongs to the page before it
    assert [doc.page_of(o) for o in (0, 3, 4, 5, 9, 10, 11, 12)] == [1, 1, 1, 2, 2, 2, 3, 3]
