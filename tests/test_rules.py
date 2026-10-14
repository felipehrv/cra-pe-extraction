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


# --------------------------------------------------------------------------- 0.2.0: Group A
#
# Excerpts below are verbatim from corpus PEs (doc_id and page in each case); the triage that
# motivated each change is in results/group_a_triage.csv and config/group_a_triage.yaml.


def _extract_parts(parts: list[tuple[str | None, str]], regulator: str, doc_id: str = "X_1_20240101"):
    """Rules output for one page made of (section name or None, text) parts."""
    text, sections, offset = "", {}, 0
    for name, part in parts:
        if name:
            sections[name] = Section(
                name=name, doc_id=doc_id, page_start=1, page_end=1,
                char_start=offset, char_end=offset + len(part), text=part,
            )
        text += part
        offset += len(part)
    return _by_field(extract(doc_id, [Page(number=1, text=text)], sections, regulator))


def test_version_bumped_with_changelog():
    from cra_extract.rules import CHANGELOG

    assert RULES_VERSION == "0.2.3"
    assert CHANGELOG[-1][0] == RULES_VERSION
    versions = [v for v, _date, _text in CHANGELOG]
    assert versions == sorted(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
    assert len(set(versions)) == len(versions)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Feburary 5, 2024", date(2024, 2, 5)),  # FDIC_11674_20240205 p1
        ("Aprill 22, 2024", date(2024, 4, 22)),  # FDIC_13951_20240422 p1
        ("Decmeber 2, 2024", date(2024, 12, 2)),  # FDIC_16344_20241202 p1
        ("Janurary 21, 2025", date(2025, 1, 21)),  # FDIC_23373_20250121 p1
        ("August4,2025", date(2025, 8, 4)),  # FRB_980960_20250804 p1
        ("August 28th, 2023", date(2023, 8, 28)),  # OCC_11916_20230828 p1
        ("6/26/23", date(2023, 6, 26)),  # OCC_18609_20230626 p1
        ("Sept. 9, 2024", date(2024, 9, 9)),
        ("March 27, 2023", date(2023, 3, 27)),
        ("Suite 10, 2001 Butterfield Road", None),
        ("market 25, 2023", None),
    ],
)
def test_parse_cover_date(text, expected):
    from cra_extract.rules import parse_cover_date

    assert parse_cover_date(text) == expected


def test_tolerant_dates_stay_out_of_prose_rules():
    """The prose date parser is unchanged: a misspelled month is not a date there."""
    assert parse_date("Feburary 5, 2024") is None
    assert parse_date("6/26/23") is None


@pytest.mark.parametrize("token,month", [("decmeber", 12), ("Jan", 1), ("sept", 9), ("market", None), ("mayor", None), ("juneau", None)])
def test_fuzzy_month(token, month):
    from cra_extract.rules import fuzzy_month

    assert fuzzy_month(token) == month


@pytest.mark.parametrize(
    "sentence,expected",
    [
        # FDIC_14134_20240205 p6 and the next sentence of the same paragraph
        ("Examiners used the ISI Examination Procedures to evaluate PSB's CRA performance.", "isb"),
        ("These procedures include two tests: the CRA Small Bank Lending Test and the Community Development Test.", None),
        # FDIC_33708_20221114 p5
        ("Examiners used the ISB Examination Procedures to evaluate IBC's CRA performance.", "isb"),
        # FDIC_10474_20250428 p8 and p9
        ("Examiners used the Interagency Intermediate Small (ISB) Institution Examination Procedures to evaluate the bank's CRA performance.", "isb"),
        ("This is the bank's first CRA performance evaluation under the ISB procedures, as its assets exceeded the small bank asset thresholds of the prior two calendar years, January 1, 2025.", "isb"),
        # FDIC_17280_20221018 p6
        ("During that time, the bank fell below the asset threshold for Intermediate Small Institution Examination Procedures.", None),
        ("Therefore, examiners used the Interagency Small Institution Examination Procedures to evaluate The Bank of Tioga's CRA performance.", "small"),
        # FRB_528102_20221128 p4
        ("As the bank's assets were also below $376 million, the bank was not considered an intermediate small institution.", "small"),
        # FRB_214722_20241118 p4
        ("Chesapeake Bank (CB) was evaluated using the interagency examination procedures for intermediate- small institutions developed by the Federal Financial Institutions Examination Council (FFIEC).", "isb"),
        # OCC_3180_20221219 p5
        ("Examiners evaluated FNB using the Intermediate/Small Bank examination procedures, which included a lending test and a community development test.", "isb"),
        # OCC_13692_20230117 p5
        ("We evaluated CNB's CRA performance using small bank performance standards, which consists of the Lending Test.", "small"),
        # OCC_17106_20250616 p4
        ("The OCC evaluated LNB's CRA activities in meeting the credit needs of its AA by using small bank CRA performance criteria which includes a review of the bank's LTD ratio, responsiveness to CRA complaints, and the lending test.", "small"),
        # OCC_13835_20230724
        ("Millbury was evaluated using SB procedures, which assess the bank's performance according to the CRA under components of a Lending Test.", "small"),
        # FRB_1002953_20230508 and FRB_164975_20250818 p4 (footnote digit)
        ("Examiners utilized the Federal Financial Institutions Examination Council's Interagency Examination Procedures for Small Institutions to evaluate the bank's CRA performance.", "small"),
        ("The bank's CRA performance review was conducted using the Federal Financial Institutions Examination Council (FFIEC) Examination Procedures for Small Institutions1.", "small"),
        # FDIC_12402_20241021 (sic)
        ("Examiners used the Interagency Small Business Examination Procedures to evaluate Exchange State Bank's (ESB) CRA performance.", "small"),
        # transitions: OCC_10637_20250707 p5, OCC_24219_20250218
        ("The OCC evaluated FNB using Small Bank CRA examination procedures, which included the Lending Test, and Intermediate Small Bank CRA examination procedures for the portion of the evaluation period from January 1, 2024, through December 31, 2024, which includes the CD test.", "mixed"),
        ("This evaluation was conducted using CRA small bank performance standards for the 2021 analysis period, and intermediate-small bank (ISB) performance standards for the 2022-2023 analysis period.", "mixed"),
        # the prior evaluation's procedures (FDIC_14134_20240205 p4)
        ("PSB received an Outstanding rating at its previous FDIC CRA evaluation, dated February 8, 2021, based on Interagency Intermediate Small Institution (ISI) Examination Procedures.", None),
        # a bank whose abbreviation is ISB is not a method statement
        ("Iowa State Bank (ISB) offers residential and commercial loans.", None),
    ],
)
def test_method_from_sentence(sentence, expected):
    from cra_extract.rules import method_from_sentence

    assert method_from_sentence(sentence) == expected


def test_fdic_cd_rating_overrides_small_scope_sentence():
    """FDIC_1678_20230814: scope p5 says Small Institution, rating page p3 rates a CD Test."""
    by = _extract_parts([
        ("rating", "INSTITUTION'S CRA RATING: This institution is rated Satisfactory.\n"
                   "The Lending Test is rated Satisfactory.\n"
                   "The Community Development Test is rated Satisfactory.\n\n"),
        ("scope_of_evaluation", "SCOPE OF EVALUATION\n\nExaminers used the Interagency Small Institution "
                                "Examination Procedures to evaluate the institution's CRA performance.\n"),
    ], "FDIC")
    assert by["exam_method"].value == "isb"
    assert "overrides" in by["exam_method"].notes
    assert by["cd_test_rating"].value == "S"


def test_occ_small_scope_sentence_wins_over_cd_rating():
    """OCC_13770_20241028: the OCC rates an optional CD Test for some Small Banks."""
    by = _extract_parts([
        ("rating", "Institution's CRA Rating: This institution is rated Satisfactory.\n"
                   "The lending test is rated: Satisfactory.\n"
                   "The community development test is rated: Satisfactory.\n\n"),
        ("scope_of_evaluation", "Scope of the Evaluation\n\nWe completed a full-scope review of TBNA's CRA activities "
                                "in its AAs under the Small Bank CRA procedures, which includes the lending test and "
                                "the optional CD test.\n"),
    ], "OCC")
    assert by["exam_method"].value == "small"
    assert by["cd_test_rating"].status == "absent"


@pytest.mark.parametrize(
    "rating,method,note",
    [
        # OCC_13692_20230117 p3
        ("The Lending Test is rated: Satisfactory.\nThe Optional Community Development (CD) Test is Rated: Outstanding.\n", "small", "optional"),
        # OCC_23771_20220801 p3
        ("The Lending Test is rated: Outstanding.\nThe CD Test is rated: Outstanding.\n", "isb", "CD test"),
        # OCC_17106_20250616 p3 without its scope sentence
        ("The lending test is rated: Satisfactory.\n", "small", "Lending Test and no CD Test"),
    ],
)
def test_method_fallbacks_on_the_rating_page(rating, method, note):
    by = _extract_parts([("rating", "Institution's CRA Rating: This institution is rated Satisfactory.\n" + rating)], "OCC")
    assert by["exam_method"].value == method
    assert note.lower() in by["exam_method"].notes.lower()


def test_method_transition_abstains():
    by = _extract_parts([
        ("scope_of_evaluation", "Scope of the Evaluation\n\nThe evaluation was conducted using Small Bank CRA examination "
                                "procedures with Intermediate Small Bank CRA performance standards considered for the "
                                "portion of the evaluation period between January 1, 2022, through December 31, 2022.\n"),
    ], "OCC")
    assert by["exam_method"].status == "abstain"
    assert "mixed" in by["exam_method"].notes


def test_method_from_fdic_appendix_heading_not_toc():
    """FDIC_17011_20230307: the appendix heading; the ToC row with a dot leader does not count."""
    toc = "INTERMEDIATE SMALL BANK PERFORMANCE CRITERIA .........................................16\n"
    by = _extract_parts([(None, toc)], "FDIC")
    assert by["exam_method"].status == "abstain"
    by = _extract_parts([(None, toc + "\nAPPENDICES\n\nINTERMEDIATE SMALL BANK PERFORMANCE CRITERIA\n\nLending Test\n")], "FDIC")
    assert by["exam_method"].value == "isb"
    assert by["exam_method"].quote == "INTERMEDIATE SMALL BANK PERFORMANCE CRITERIA"


def _cover(lines: list[str]) -> str:
    return "\n \n".join(["PUBLIC DISCLOSURE", "April 15, 2024", "COMMUNITY REINVESTMENT ACT", "PERFORMANCE EVALUATION", *lines]) + "\n"


@pytest.mark.parametrize(
    "id_line,expected",
    [
        ("Charter 13478", "13478"),  # OCC_13478_20240415 p1
        ("Charter #10161", "10161"),  # OCC_10161_20221219 p1
        ("Charter # 13557", "13557"),  # OCC_13557_20230530 p1
        ("Charter: 23393", "23393"),  # OCC_23393_20241113 p1
        ("Charter No. 23458", "23458"),  # OCC_23458_20240722 p1
        ("Charter Number 10180", "10180"),
        ("9773", "9773"),  # OCC_9773_20230109 p1, bare number under the name
        ("#22327", "22327"),  # OCC_22327_20221003 p1
        ("RSSD# 465243", "465243"),
        ("Institution’s Identification Number: 920210", "920210"),  # FRB_920210_20240716 p1
    ],
)
def test_bank_id_labels(id_line, expected):
    cover = _cover(["First National Bank of Pana", id_line, "306 South Locust Street", "Pana, IL 62557",
                    "Office of the Comptroller of the Currency"])
    by = _extract_parts([("cover", cover)], "OCC")
    assert by["bank_id"].value == expected
    assert by["bank_name"].value == "First National Bank of Pana"


def test_bank_name_stops_at_address_and_drops_label():
    # FRB Atlanta: address between the name and the id line (FRB_353238_20230925 p1)
    cover = _cover(["Central State Bank", "11025 Highway 25", "Calera, Alabama  35040", "RSSD ID NUMBER:  353238",
                    "FEDERAL RESERVE BANK OF ATLANTA"])
    by = _extract_parts([("cover", cover)], "FRB")
    assert by["bank_id"].value == "353238"
    assert by["bank_name"].value == "Central State Bank"
    # FRB Philadelphia: labelled name line (FRB_920210_20240716 p1)
    cover = _cover(["Name of Depository Institution:   JIM THORPE NEIGHBORHOOD BANK",
                    "Institution’s Identification Number: 920210", "Address:"])
    by = _extract_parts([("cover", cover)], "FRB")
    assert by["bank_name"].value == "JIM THORPE NEIGHBORHOOD BANK"
    assert by["bank_name"].quote.startswith("Name of Depository Institution:")


def test_chartered_in_is_not_an_id():
    by = _extract_parts([("cover", _cover(["Golden Bank", "The bank was chartered in 1985.", "Houston, Texas 77036"]))], "OCC")
    assert by["bank_id"].status == "abstain"


def test_bank_id_from_second_cover_page():
    """FRB_711520_20230925: decorative cover on p1 without an id, full cover on p2."""
    p1 = ("PUBLIC DISCLOSURE\n\nSeptember 25, 2023\n\nCOMMUNITY REINVESTMENT ACT PERFORMANCE EVALUATION\n\n"
          "Miners Exchange Bank\n   Coeburn, Virginia\nFederal Reserve Bank of Richmond\nRichmond, Virginia\n")
    p2 = ("PUBLIC DISCLOSURE\n\nSeptember 25, 2023\n\nCOMMUNITY REINVESTMENT ACT PERFORMANCE EVALUATION\n\n"
          "Miners Exchange Bank\n711520\n483 Front Street West\nCoeburn, VA 24230\nFederal Reserve Bank of Richmond\n")
    pages = [Page(number=1, text=p1), Page(number=2, text=p2)]
    cover = Section(name="cover", doc_id="FRB_711520_20230925", page_start=1, page_end=1,
                    char_start=0, char_end=len(p1), text=p1)
    by = _by_field(extract("FRB_711520_20230925", pages, {"cover": cover}, "FRB"))
    assert by["bank_id"].value == "711520"
    assert by["bank_id"].page == 2
    assert by["bank_id"].notes == "second cover page"
    assert by["bank_name"].value == "Miners Exchange Bank"
    assert by["exam_date"].value == date(2023, 9, 25)
    text = doc_text(pages)
    assert text[by["bank_id"].char_start : by["bank_id"].char_end] == "711520"


def test_exam_date_scope_fallback_and_consistency_note():
    scope = ("SCOPE OF EVALUATION\n\nThis evaluation covers the period from the prior evaluation dated October 22, 2018, "
             "to the current evaluation dated July 9, 2024.  Examiners used the Interagency Small Institution "
             "Examination Procedures to evaluate performance.\n")
    no_date_cover = "PUBLIC DISCLOSURE\n\nCOMMUNITY REINVESTMENT ACT\n\nPERFORMANCE EVALUATION\n\nPeoples Bank\nCertificate Number:  9366\n"
    by = _extract_parts([("cover", no_date_cover), ("scope_of_evaluation", scope)], "FDIC")  # FDIC_9366_20230821
    assert by["exam_date"].value == date(2024, 7, 9)
    assert "no date on the cover" in by["exam_date"].notes
    dated_cover = no_date_cover.replace("PUBLIC DISCLOSURE\n", "PUBLIC DISCLOSURE\n\nJuly 8, 2024\n")
    by = _extract_parts([("cover", dated_cover), ("scope_of_evaluation", scope)], "FDIC")  # FDIC_9438_20240709
    assert by["exam_date"].value == date(2024, 7, 8)
    assert by["exam_date"].notes == "scope section dates the current evaluation 2024-07-09 (p1)"


@pytest.mark.parametrize(
    "rating_text,code",
    [
        ("Institution's CRA rating: Satisfactory\nThe lending test rating: Satisfactory\n", "S"),  # OCC_706517_20230828 p3
        ("INSTITUTION'S CRA RATING: This institution is Satisfactory.\n", "S"),  # FDIC_4235_20250310 p3
        ("Bank of 1889 is rated Satisfactory. This rating is based on the following conclusions.\n", "S"),  # FRB_864846_20230821
        ("The Hamler State Bank (Hamler) is rated “Satisfactory.” This rating is based on the following\n", "S"),  # FRB_887827_20230515
        ("Wadena State Bank (the bank), Wadena, Minnesota, is rated Outstanding. This rating is based on\n", "O"),  # FRB_210256_20250224
        ("Clear Lake Bank and Trust Company is rated: Satisfactory\nThe Lending Test is rated: Satisfactory\n", "S"),  # FRB_865544_20221107
        ("The Community Reinvestment Act (CRA) rating for Western Bank (the bank) is Satisfactory.  This\n", "S"),  # FRB_723653_20230109
        ("New Century Bank’s (the bank) initial satisfactory Community Reinvestment Act (CRA) rating\n"
         "has been lowered to Needs to Improve based on substantive violations.\n", "NI"),  # FRB_392152_20210222
        ("Institution’s CRA Rating: This institution is rate Satisfactory.\n", "S"),  # OCC_5288_20231030 p3
    ],
)
def test_overall_rating_wordings(rating_text, code):
    by = _extract_parts([("rating", "INSTITUTION'S CRA RATING\n\n" + rating_text)], "FRB")
    assert by["overall_rating"].value == code


@pytest.mark.parametrize(
    "rating_text",
    [
        "The Lending Test is rated: Satisfactory.\n",  # OCC_17548_20190128 p3 prints no institution rating
        "The bank's performance in the State of Iowa is rated Satisfactory.\n",
        "The bank's CRA performance was rated Satisfactory at the previous performance evaluation.\n",
    ],
)
def test_overall_rating_not_read_from_other_subjects(rating_text):
    by = _extract_parts([("rating", "Overall CRA Rating\n\n" + rating_text)], "OCC")
    assert by["overall_rating"].status == "abstain"


def test_component_ratings_read_rating_colon_form():
    """OCC_11544_20230531 p3: 'The lending test rating: Satisfactory'."""
    by = _extract_parts([("rating", "Institution's CRA rating: Satisfactory\nThe lending test rating: Satisfactory\n"
                                    "The community development test rating: Outstanding\n")], "OCC")
    assert by["lending_test_rating"].value == "S"
    assert by["cd_test_rating"].value == "O"
    assert by["exam_method"].value == "isb"


# --------------------------------------------------------------------------- FRB locator tuning


def test_rules_read_frb_chicago_no_break_space_layout():
    """FRB_180845_20241104 pages 3 and 4 (Chicago): every space is U+00A0 and the heading
    apostrophe is U+02B9 ("BANK'S CRA RATING"); before the 2026-10-08 locator tuning no section
    of these PEs was located, so every rule abstained."""
    nbsp = " "
    page1 = "PUBLIC DISCLOSURE \n\nIllini State Bank \nRSSD# 180845 \n"
    page3 = (
        "BANKʹS CRA RATING \n\nIllini State Bank is rated Satisfactory. \n\n"
        "SCOPE OF EXAMINATION \n\nIllini State Bankʹs performance was evaluated using the "
        "Federal Financial Institutions Examination \nCouncil (FFIEC) Small Institution CRA "
        "Examination Procedures. \n"
    )
    pages = [
        Page(number=i + 1, text=t.replace(" ", nbsp), chars=len(t))
        for i, t in enumerate([page1, "Table of Contents\n", page3])
    ]
    values = {v.field: v for v in extract_document("FRB_180845_20241104", pages, "FRB")}
    assert values["overall_rating"].status == "value"
    assert values["overall_rating"].value == "S"
    assert values["overall_rating"].scope == "institution"


def test_regulator_rule_reads_no_break_space_cover():
    """FRB_180845_20241104 p1 (Chicago): the agency line is "Federal\xa0Reserve\xa0Bank\xa0of
    \xa0Chicago"; rules 0.2.0 abstained on regulator for all 46 such covers."""
    page1 = (
        "PUBLIC DISCLOSURE \n\nNovember 4, 2024 \n\nCOMMUNITY REINVESTMENT ACT \n"
        "PERFORMANCE EVALUATION \n\nIllini State Bank \nRSSD# 180845 \n\n"
        "Federal Reserve Bank of Chicago \n"
    ).replace(" ", "\u00a0")
    pages = [Page(number=1, text=page1, chars=len(page1))]
    values = {v.field: v for v in extract_document("FRB_180845_20241104", pages, "FRB")}
    assert values["regulator"].status == "value"
    assert values["regulator"].value == "FRB"
    assert values["regulator"].quote == "Federal Reserve Bank of Chicago"  # whitespace collapsed
    span = page1[values["regulator"].char_start : values["regulator"].char_end]
    assert span == "Federal\u00a0Reserve\u00a0Bank\u00a0of\u00a0Chicago"  # offsets stay verbatim


# --------------------------------------------------------------------------- FDIC total assets
# The LTD comparison table excerpts below are verbatim pymupdf text (``parse.extract_pages``,
# one table cell per line), the layout of every ``data/text`` document; the pdftotext fixtures
# print the same tables as one row per line (SS:336-340, LB:297-300), which the sample cases
# above cover. Rules 0.2.2 read only the pdftotext layout, so on parsed documents the FDIC
# figure always came from the rounded description sentence.

SS_DESCRIPTION = (
    "As of June 30, 2022, assets totaled approximately $1.2 billion, consisting primarily of net loans and \n"
    "leases of $800.6 million, securities of 184.4 million, and cash of $114.2 million. \n"
)  # FDIC_12299_20220912 p4
SS_LTD_TABLE = (
    "LTD Ratio Comparison \nBank \nTotal Assets as of \n06/30/2022 \n($000s) \nAverage Net  \nLTD Ratio \n(%) \n"
    "SouthStar Bank, S.S.B., Moulton, TX \n1,159,094 \n100.7 \nHorizon Bank, SSB, Austin, TX \n2,424,193 \n70.3 \n"
    "R Bank, Round Rock, TX \n953,809 \n85.9 \nThe First National Bank of Shiner, Shiner, TX \n867,875 \n17.3 \n"
    "Source:  Reports of Condition and Income (09/30/2019 – 06/30/2022) \n"
)  # FDIC_12299_20220912 p9
LB_DESCRIPTION = (
    "As of June 30, 2022, the Consolidated Reports of Conditions and Income (Call Report) reported \n"
    "assets totaling $12.6 million, loans totaling $7.7 million, deposits totaling $10.8 million and \n"
    "securities of $691,000. \n"
)  # FDIC_26816_20220809 p4
LB_LTD_TABLE = (
    "Loan-to-Deposit Ratio  \nBank \nTotal Assets as of \n6/30/2022 \n($000s) \nAverage Net  \nLTD Ratio \n(%) \n"
    "Liberty Bank, Inc. \n12,614 \n64.9 \nSource:  Call Reports  \n"
)  # FDIC_26816_20220809 p8


def _assets(parts: list[tuple[str | None, str]], regulator: str = "FDIC") -> dict[str, FieldValue]:
    by = _extract_parts(parts, regulator)
    text = "".join(p for _name, p in parts)
    for field in ("total_assets_usd", "total_assets_asof"):
        fv = by[field]
        if fv.status == "value":
            assert _ws(text[fv.char_start : fv.char_end]) == fv.quote
    return by


def test_fdic_total_assets_prefer_ltd_table_one_cell_per_line():
    """SouthStar: the subject row is matched by the cover name; the quote runs from the header
    to the figure so it carries the ($000s) marker."""
    cover = _cover(["SouthStar Bank, S.S.B.", "Certificate Number: 12299", "Federal Deposit Insurance Corporation"])
    by = _assets([("cover", cover), ("description_of_institution", SS_DESCRIPTION), ("ltd_ratio", SS_LTD_TABLE)])
    assert by["bank_name"].value == "SouthStar Bank, S.S.B."
    assert by["total_assets_usd"].value == 1159094000
    assert by["total_assets_usd"].quote.startswith("Total Assets as of 06/30/2022 ($000s)")
    assert by["total_assets_usd"].quote.endswith("SouthStar Bank, S.S.B., Moulton, TX 1,159,094")
    assert by["total_assets_usd"].notes == "FDIC LTD comparison table, subject bank row"
    assert by["total_assets_asof"].value == date(2022, 6, 30)
    assert by["total_assets_asof"].quote == "Total Assets as of 06/30/2022"


def test_fdic_total_assets_one_row_table_without_bank_name():
    """Liberty: a one-row table is read even when the cover gave no name."""
    by = _assets([("description_of_institution", LB_DESCRIPTION), ("ltd_ratio", LB_LTD_TABLE)])
    assert by["total_assets_usd"].value == 12614000
    assert by["total_assets_asof"].value == date(2022, 6, 30)


def test_fdic_total_assets_fall_back_to_sentence():
    # Several rows and no bank name: the subject row is unknown.
    by = _assets([("description_of_institution", SS_DESCRIPTION), ("ltd_ratio", SS_LTD_TABLE)])
    assert by["total_assets_usd"].value == 1200000000
    assert by["total_assets_asof"].value == date(2022, 6, 30)
    assert by["total_assets_usd"].quote.startswith("As of June 30, 2022, assets totaled")
    # OCC and FRB read the description sentence only.
    by = _assets([("description_of_institution", LB_DESCRIPTION), ("ltd_ratio", LB_LTD_TABLE)], "FRB")
    assert by["total_assets_usd"].value == 12600000
    # A ($000s) table that prints $ millions (FDIC_14995_20240325 p8) gives an implausible figure.
    river = (
        "Loan-to-Deposit Ratio Comparison \nBank \nTotal Assets as of \n12/31/2023 \n($000s) \nAverage Net \n"
        "LTD Ratio \n(%) \nRiver Bank \n1,073 \n113.4 \nCitizens State Bank La Crosse \n628 \n109.9 \n"
        "Source:  Reports of Condition and Income 6/30/2021 through 12/31/2023 \n"
    )
    cover = _cover(["River Bank", "Certificate Number: 14995", "Federal Deposit Insurance Corporation"])
    sentence = "As of December 31, 2023, the bank reported total assets of $1.1 billion. \n"
    by = _assets([("cover", cover), ("description_of_institution", sentence), ("ltd_ratio", river)])
    assert by["total_assets_usd"].value == 1100000000
    assert by["total_assets_asof"].value == date(2023, 12, 31)


def test_fdic_total_assets_subject_row_not_first_and_short_year():
    """FDIC_16733_20240410 p9: name and city on two lines, subject in the fifth row, header
    date "12/31/23"."""
    table = (
        "Loan-to-Deposit Ratio Comparison \nBank \nTotal Assets as of \n12/31/23 $(000s) \nAverage Net LTD \n"
        "Ratio (%) \nBankCherokee \nSaint Paul, MN \n375,627 \n60.5 \nCitizens Independent Bank \n"
        "Saint Louis Park, MN \n344,578 \n63.8 \nCrown Bank \nEdina, MN \n382,495 \n94.3 \nGateway Bank \n"
        "Mendota Heights, MN \n271,713 \n73.6 \nNorth Star Bank \nRoseville, MN \n352,077 \n71.5 \n"
        "Village Bank \nSaint Francis, MN \n410,440 \n62.9 \nSource: Reports of Condition 6/30/2021 through 12/31/2023 \n"
    )
    cover = _cover(["North Star Bank", "Certificate Number: 16733", "Federal Deposit Insurance Corporation"])
    by = _assets([("cover", cover), ("ltd_ratio", table)])
    assert by["total_assets_usd"].value == 352077000
    assert by["total_assets_usd"].quote.endswith("North Star Bank Roseville, MN 352,077")
    assert by["total_assets_asof"].value == date(2023, 12, 31)


@pytest.mark.parametrize(
    "name,rows,expected",
    [
        # Same name twice: the bank under evaluation is listed first (FDIC_1218_20240923 p7).
        ("First State Bank", "First State Bank, Spearman, TX \n180,243 \n81.5 \nFirst State Bank, Stratford, TX \n334,864 \n71.4 \n", 180243000),
        # Initials (FDIC_10111_20250203 p7).
        ("Ohnward Bank & Trust", "OBT, Cascade, Iowa \n445,445 \n89.3 \nCentral State Bank, Coralville, Iowa \n477,336 \n99.7 \n", 445445000),
        # The only row the PE does not anonymize (FDIC_10458_20220706 p7).
        ("Farmers State Bank & Trust Co.", "FSBTC \n114,295 \n74.9 \nSimilarly-Situated Institution #1 \n127,881 \n48.5 \n", 114295000),
        # A cell broken after its comma (FDIC_254_20231127 p7).
        ("First Whitney Bank and Trust", "First Whitney Bank and Trust, Atlantic, Iowa \n266, 880 \n68.3 \nTS Bank, Treynor, Iowa \n423,696 \n40.3 \n", 266880000),
        # A generic word in common is not a match: "R Bank" never stands in for SouthStar Bank.
        ("SouthStar Bank, S.S.B.", "Horizon Bank, SSB, Austin, TX \n2,424,193 \n70.3 \nR Bank, Round Rock, TX \n953,809 \n85.9 \n", None),
    ],
)
def test_fdic_total_assets_subject_row(name, rows, expected):
    table = "Bank \nTotal Assets as of \n6/30/2024 \n($000s) \nAverage Net \nLoan-to-Deposit Ratio \n(%) \n" + rows + "Source: Call Reports \n"
    cover = _cover([name, "Certificate Number: 1", "Federal Deposit Insurance Corporation"])
    by = _assets([("cover", cover), ("ltd_ratio", table)])
    if expected is None:
        assert by["total_assets_usd"].status == "abstain"
    else:
        assert by["total_assets_usd"].value == expected
        assert by["total_assets_asof"].value == date(2024, 6, 30)
