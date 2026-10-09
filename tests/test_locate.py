"""Tests for the page-wise, scope-aware section locator (``cra_extract.locate``).

One parametrized case per sample PE checks which sections are found, their page numbers,
a snippet that must sit inside each located section, and where the document leaves
institution scope. Expected values were read off the ``pdftotext`` ``.txt`` files; the line
numbers are cited next to each value. Pages are built by splitting the ``.txt`` on form
feeds, which is how ``pdftotext`` separates pages.
"""

from __future__ import annotations

import csv
import json
import re

import pandas as pd
import pytest
from conftest import SAMPLE_META, SAMPLES

from cra_extract import locate
from cra_extract.config import CORPUS_PATH, load_headings
from cra_extract.models import Page, Section

# Per sample: section -> (page_start, page_end, snippet inside the section text).
# Line numbers refer to tests/fixtures/samples/{stem}.txt.
EXPECTED: dict[str, dict[str, tuple[int, int, str]]] = {
    "FDIC_12299_SouthStar_2022_ISB_NI_fairlending": {
        "cover": (1, 1, "Certificate Number: 12299"),  # L14
        "rating": (3, 3, "This institution is rated Needs to Improve"),  # L56
        "description_of_institution": (4, 5, "headquartered in Moulton, Texas"),  # L104
        "description_of_assessment_area": (5, 6, "designated five assessment areas"),  # L165
        "scope_of_evaluation": (6, 8, "prior evaluation dated July 15, 2019"),  # L202
        "ltd_ratio": (8, 9, "averaged 100.7"),  # L322
        "aa_concentration": (9, 10, "A majority of loans are in the institution"),  # L349
        "illegal_credit_practices_review": (
            14,
            14,
            "lowered the CRA rating from Satisfactory to Needs to Improve",  # L599
        ),
    },
    "FDIC_26816_LibertyBank_2022_SM_SN": {
        "cover": (1, 1, "Certificate Number: 26816"),  # L14
        "rating": (3, 3, "rated Substantial Noncompliance"),  # L51
        "description_of_institution": (3, 4, "headquartered in Salt Lake City, Utah"),  # L80
        "description_of_assessment_area": (4, 6, "all of Salt Lake County"),  # L129
        "scope_of_evaluation": (6, 7, "previous evaluation dated April 12, 2021"),  # L224
        "ltd_ratio": (7, 8, "averaged 64.9 percent"),  # L281
        "aa_concentration": (8, 9, "LBI did not originate any"),  # L307
        "illegal_credit_practices_review": (9, 9, "Section 5 Unfair or Deceptive Acts"),  # L353
    },
    "FDIC_6078_OneAmericanBank_2024_ISB_NI": {
        "cover": (1, 1, "Certificate Number: 6078"),  # L14
        "rating": (3, 3, "The Lending Test is rated Needs to Improve."),  # L57
        "description_of_institution": (4, 5, "One American Financial Corporation"),  # L87
        "description_of_assessment_area": (5, 7, "designated a single assessment area"),  # L142
        "scope_of_evaluation": (7, 9, "dated December 3, 2018"),  # L261
        "ltd_ratio": (9, 9, "128.1"),  # L349
        "aa_concentration": (10, 11, "substantial majority of loans"),  # L363
        "illegal_credit_practices_review": (
            16,
            16,
            "did not identify any evidence of discriminatory",  # L658
        ),
    },
    "FRB_3664588_FieldPoint_2024_ISB_NI": {
        "cover": (1, 1, "RSSD No. 3664588"),  # L12
        "rating": (4, 4, "The Lending Test is rated: Needs to Improve."),  # L99
        "description_of_institution": (
            5,
            6,
            "state member bank headquartered in Greenwich",
        ),  # L135
        "description_of_assessment_area": (7, 7, "four bank delineated assessment areas"),  # L221
        "scope_of_evaluation": (8, 9, "Interagency Intermediate Small Institution"),  # L253
        "ltd_ratio": (10, 10, "was 91.1%"),  # L375
        "aa_concentration": (10, 11, "40.8% by number"),  # L383
        "illegal_credit_practices_review": (
            14,
            14,
            "No evidence of discriminatory or other illegal credit practices",  # L582
        ),
    },
    "FRB_663656_SmallBusinessBank_2024_SM_NI": {
        "cover": (1, 1, "RSSD# 663656"),  # L11
        "rating": (3, 3, "is rated Needs to Improve"),  # L54
        "description_of_institution": (3, 4, "total assets of $94.2 million"),  # L91
        "description_of_assessment_area": (4, 7, "KCMO-KS MSA AA"),  # L138
        "scope_of_evaluation": (
            3,
            3,
            "Interagency Examination Procedures for Small Institutions",  # L70, "SCOPE OF EXAMINATION"
        ),
        "ltd_ratio": (9, 9, "19-quarter average NLTD ratio"),  # L382, "Net Loan-to-Deposit Ratio"
        "aa_concentration": (10, 11, "only 21.3 percent"),  # L412
        "illegal_credit_practices_review": (12, 12, "No evidence of a pattern or"),  # L533
    },
    "OCC_11715_LemontNB_2024_SN": {
        "cover": (1, 1, "Charter Number 11715"),  # L8
        "rating": (3, 3, "The lending test is rated: Substantial Noncompliance."),  # L50
        "description_of_institution": (4, 4, "total assets of $65.4 million"),  # L122
        "scope_of_evaluation": (4, 5, "calendar years 2021 through 2023"),  # L142
        "ltd_ratio": (3, 3, "was 10.6 percent"),  # L70
        "aa_concentration": (4, 4, "54.5 percent of its total loans inside"),  # L99
        "illegal_credit_practices_review": (
            6,
            6,
            "has not identified that this institution",
        ),  # L192
    },
    "OCC_13682_FirstNeighborBank_2024_ISB_O": {
        "cover": (1, 1, "Charter Number 13682"),  # L9
        "rating": (3, 3, "The community development test is rated: Outstanding."),  # L60
        "description_of_institution": (4, 4, "$593.7 million"),  # L121
        "scope_of_evaluation": (5, 5, "January 1, 2021, to December 31, 2023"),  # L160
        "ltd_ratio": (3, 3, "averaged 85.5 percent"),  # L85
        "aa_concentration": (3, 4, "87.5 percent of its total loans"),  # L102
        "illegal_credit_practices_review": (
            6,
            6,
            "has not identified that this institution has engaged",  # L205
        ),
    },
    "OCC_703395_GNBank_2023_NI": {
        "cover": (1, 1, "Charter Number: 703395"),  # L8
        "rating": (3, 3, "The lending test is rated: Needs to Improve."),  # L54
        "description_of_institution": (4, 5, "acquired by Group Nduom in April 2016"),  # L106
        "scope_of_evaluation": (5, 5, "January 1, 2019 through December 31, 2021"),  # L161
        "ltd_ratio": (3, 3, "was 54.32 percent"),  # L70
        "aa_concentration": (3, 3, "purchased 294 unsecured consumer loans"),  # L83
        "illegal_credit_practices_review": (
            5,
            6,
            "Pursuant to 12 CFR 25.28(c), in determining",  # L186; paragraph continues on page 6
        ),
    },
}

# The three OCC samples print "Description of Assessment Area" only under "State Rating"
# (Lemont L207-208, First Neighbor L222-224, GN Bank L207-208), where it is a scope marker,
# so the institution-level section is absent from them.
EXPECTED_MISSING: dict[str, list[str]] = {
    stem: ["description_of_assessment_area"] if stem.startswith("OCC") else []
    for stem in SAMPLE_META
}

# Pages that are a table of contents (page 2 everywhere; FieldPoint's ToC runs over two pages,
# L36-L95).
EXPECTED_TOC_PAGES: dict[str, list[int]] = {
    stem: [2, 3] if "FieldPoint" in stem else [2] for stem in SAMPLE_META
}

# (first page outside institution scope and its scope, first appendix page).
# SouthStar: "AUSTIN MSA AA - Full-Scope Review" L606 (page 14), "APPENDICES" L1727 (page 39).
# Liberty: "APPENDICES" L367 (page 10). One American: "APPENDICES" L665 (page 17).
# FieldPoint: "STATE OF NEW YORK" L592 (page 15), "APPENDIX A" L2590 (page 53).
# Small Business Bank: "APPENDIX A - MAP OF THE ASSESSMENT AREA" L545 (page 13).
# Lemont: "State Rating" L207 (page 7), "Appendix A:" L373 (page 11).
# First Neighbor: "State Rating" L222 (page 7), appendix page 15.
# GN Bank: "State Rating" L207 (page 6), appendix page 10.
EXPECTED_SCOPE_CHANGE: dict[str, tuple[tuple[int, str] | None, int]] = {
    "FDIC_12299_SouthStar_2022_ISB_NI_fairlending": ((14, "assessment_area"), 39),
    "FDIC_26816_LibertyBank_2022_SM_SN": (None, 10),
    "FDIC_6078_OneAmericanBank_2024_ISB_NI": (None, 17),
    "FRB_3664588_FieldPoint_2024_ISB_NI": ((15, "state"), 53),
    "FRB_663656_SmallBusinessBank_2024_SM_NI": (None, 13),
    "OCC_11715_LemontNB_2024_SN": ((7, "state"), 11),
    "OCC_13682_FirstNeighborBank_2024_ISB_O": ((7, "state"), 15),
    "OCC_703395_GNBank_2023_NI": ((6, "state"), 10),
}


@pytest.fixture(scope="module")
def sample_pages() -> dict[str, list[Page]]:
    return {
        stem: locate.pages_from_text((SAMPLES / f"{stem}.txt").read_text(encoding="utf-8"))
        for stem in SAMPLE_META
    }


@pytest.fixture(scope="module")
def located(sample_pages) -> dict[str, tuple[dict[str, Section], list[str]]]:
    return {
        stem: locate.locate_all(stem, pages, SAMPLE_META[stem][0])
        for stem, pages in sample_pages.items()
    }


# --------------------------------------------------------------------------- page splitting


def test_pages_from_text_splits_on_form_feed_and_drops_trailing_empty_page():
    pages = locate.pages_from_text("first\nline\n\fsecond\n\f")
    assert [p.number for p in pages] == [1, 2]
    assert pages[0].text == "first\nline\n"
    assert pages[1].text == "second\n"
    assert locate.doc_text(pages) == "first\nline\n\fsecond\n"


def test_sample_page_counts_and_doc_text_round_trip(sample, sample_pages):
    pages = sample_pages[sample["stem"]]
    raw = sample["txt"].read_text(encoding="utf-8")
    text = locate.doc_text(pages)
    assert raw.startswith(text)
    assert len(raw) - len(text) <= 1  # only the final form feed is dropped
    assert len(pages) == raw.count("\f")


# --------------------------------------------------------------------------- line helpers


@pytest.mark.parametrize(
    "line",
    [
        "DESCRIPTION OF INSTITUTION .......................................................... 2",
        "Overall CRA Rating……...……………………………………………………………… 1",
        "Appendix B: Summary of MMSA and State Ratings…………………………………... B-1",
        "WHITE PLAINS, NY-NJ) .............................................................",
        "Table of Contents",
    ],
)
def test_is_toc_line_recognizes_dot_leaders_and_ellipses(line):
    assert locate.is_toc_line(locate.normalize_line(line))


@pytest.mark.parametrize(
    "line",
    ["DESCRIPTION OF INSTITUTION", "Loan-to-Deposit Ratio", "The bank originated 54.5 percent..."],
)
def test_is_toc_line_rejects_body_lines(line):
    assert not locate.is_toc_line(line)


def test_is_heading_candidate_rejects_table_rows_and_long_lines():
    assert locate.is_heading_candidate("Loan-to-Deposit Ratio")
    assert not locate.is_heading_candidate("Total      12     3.4   345     96.4")  # column gap
    assert not locate.is_heading_candidate("Lending Test 85.5%")
    assert not locate.is_heading_candidate("x" * 101)
    assert not locate.is_heading_candidate("")


def test_normalize_line_straightens_curly_quotes():
    assert locate.normalize_line("  INSTITUTION’S CRA RATING  ") == "INSTITUTION'S CRA RATING"


@pytest.mark.parametrize("line", ["4", "ii", "Page 11 of 20", "Charter Number: 11715"])
def test_is_furniture_line(line):
    assert locate.is_furniture_line(locate.normalize_line(line))
    assert not locate.is_furniture_line("The bank has 4 branches.")


# --------------------------------------------------------------------------- per-sample cases


def test_sections_found_and_missing(sample, located):
    found, missing = located[sample["stem"]]
    assert set(found) == set(EXPECTED[sample["stem"]])
    assert missing == EXPECTED_MISSING[sample["stem"]]
    assert set(found) | set(missing) == set(locate.section_names())


def test_section_pages_snippets_and_scope(sample, located):
    found, _ = located[sample["stem"]]
    for name, (page_start, page_end, snippet) in EXPECTED[sample["stem"]].items():
        section = found[name]
        assert (section.page_start, section.page_end) == (page_start, page_end), name
        assert snippet in section.text, name
        assert section.scope == "institution", name
        assert section.confidence == 1.0, name
        assert section.doc_id == sample["stem"]


def test_section_offsets_index_the_joined_document_text(sample, sample_pages, located):
    pages = sample_pages[sample["stem"]]
    text = locate.doc_text(pages)
    found, _ = located[sample["stem"]]
    for section in found.values():
        assert text[section.char_start : section.char_end] == section.text
        first_line = locate.normalize_line(section.text.split("\n", 1)[0])
        assert first_line == section.heading_matched
        # the section ends on a content line, not on page furniture
        last_line = locate.normalize_line(section.text.rstrip("\n").rsplit("\n", 1)[-1])
        assert last_line and not locate.is_furniture_line(last_line)


def test_toc_lines_never_start_a_section(sample, sample_pages, located):
    stem = sample["stem"]
    regulator = sample["regulator"]
    tagged = locate.tag_pages(sample_pages[stem], regulator)
    toc_pages = [p.number for p in tagged if p.is_toc]
    assert toc_pages == EXPECTED_TOC_PAGES[stem]
    found, _ = located[stem]
    for section in found.values():
        assert section.page_start not in toc_pages, section.name
        assert not locate.is_toc_line(section.heading_matched, regulator), section.name


def test_page_scopes_leave_institution_where_expected(sample, sample_pages):
    stem = sample["stem"]
    tagged = locate.tag_pages(sample_pages[stem], sample["regulator"])
    first_change, first_appendix = EXPECTED_SCOPE_CHANGE[stem]
    non_institution = [p for p in tagged if p.scope not in ("institution", "toc")]
    if first_change is None:
        assert (non_institution[0].number, non_institution[0].scope) == (first_appendix, "appendix")
    else:
        assert (non_institution[0].number, non_institution[0].scope) == first_change
    assert all(p.scope == "appendix" for p in tagged if p.number >= first_appendix)
    assert tagged[0].scope == "institution"  # cover
    for p in tagged:
        assert p.chars == len(p.text)


def test_fieldpoint_state_and_msa_pages_are_scoped(sample_pages):
    """FieldPoint (FRB, 95 pages, 4 states): STATE OF NEW YORK L592 (p15), MD 35614 L637 (p16),
    STATE OF CONNECTICUT L1163 (p26), MSA 14860 L1210 (p27), STATE OF GEORGIA L1687 (p36),
    MSA 12060 L1731 (p37), STATE OF FLORIDA L2155 (p45), MSA 36740 L2200 (p46),
    APPENDIX A L2590 (p53)."""
    pages = sample_pages["FRB_3664588_FieldPoint_2024_ISB_NI"]
    tagged = locate.tag_pages(pages, "FRB")
    scopes = {p.number: p.scope for p in tagged}
    assert [n for n, s in scopes.items() if s == "institution"] == [1] + list(range(4, 15))
    assert [n for n, s in scopes.items() if s == "toc"] == [2, 3]
    assert [n for n, s in scopes.items() if s == "state"] == [15, 26, 36, 45]
    msa_pages = [n for n, s in scopes.items() if s == "msa"]
    assert msa_pages == (
        list(range(16, 26)) + list(range(27, 36)) + list(range(37, 45)) + list(range(46, 53))
    )
    assert [n for n, s in scopes.items() if s == "appendix"] == list(range(53, 96))

    text = locate.doc_text(pages)
    spans = locate.scope_spans(pages, "FRB")
    # the institution-level review section (L579) is institution scope; the state, MSA and
    # appendix headings are searched after it so that the ToC copies (pages 2 and 3) are skipped
    review_section = locate.locate_section("illegal_credit_practices_review", "fp", pages, "FRB")
    assert review_section is not None
    review = review_section.char_start
    assert locate.scope_at(spans, review) == "institution"
    assert locate.scope_at(spans, text.index("STATE OF NEW YORK", review)) == "state"
    assert locate.scope_at(spans, text.index("CRA RATING FOR CONNECTICUT", review)) == "state"
    assert locate.scope_at(spans, text.index("OPERATIONS IN MD 35614", review)) == "msa"
    assert locate.scope_at(spans, text.index("APPENDIX D", review)) == "appendix"
    # the state-level Scope of Evaluation (L610) must not displace the institution one (L249)
    section = locate.locate_section("scope_of_evaluation", "fp", pages, "FRB")
    assert section is not None and section.page_start == 8


def test_occ_small_bank_rating_section_prints_lending_test_sentence(located):
    for stem in ("OCC_11715_LemontNB_2024_SN", "OCC_703395_GNBank_2023_NI"):
        rating = located[stem][0]["rating"]
        assert re.search(r"The lending test is rated: ", rating.text)
        assert "Loan-to-Deposit Ratio" not in rating.text  # the next heading is not included


def test_review_sections_are_short(located):
    """The review section is one to three short paragraphs in all eight samples.

    FDIC and FRB print one or two paragraphs of 2 to 10 lines; the OCC prints three boilerplate
    paragraphs (14 wrapped lines, e.g. GN Bank L186-L203), so the bound is on paragraphs.
    """
    for stem, (found, _) in located.items():
        section = found["illegal_credit_practices_review"]
        body = [
            ln.strip()
            for ln in section.text.split("\n")[1:]
            if not ln.strip()
            or not (locate.is_furniture_line(ln.strip()) or locate.COLUMN_GAP.search(ln))
        ]
        paragraphs = [p for p in "\n".join(body).split("\n\n") if p.strip()]
        content_lines = [ln for ln in body if ln]
        # GN Bank's last paragraph straddles a page break (L199-L203), which adds one split
        assert 1 <= len(paragraphs) <= 4, (stem, len(paragraphs))
        assert 2 <= len(content_lines) <= 14, (stem, len(content_lines))
        if not stem.startswith("OCC"):
            assert len(content_lines) <= 10, (stem, len(content_lines))


# --------------------------------------------------------------------------- synthetic docs


def _doc(*page_texts: str) -> list[Page]:
    return [Page(number=i + 1, text=t, chars=len(t)) for i, t in enumerate(page_texts)]


@pytest.mark.parametrize(
    ("regulator", "toc_row"),
    [
        ("FDIC", "DESCRIPTION OF INSTITUTION ........................................ 2"),
        ("OCC", "Description of Institution…………….………………………………………………. 2"),
        (
            "FRB",
            "DESCRIPTION OF INSTITUTION .................................................... 2",
        ),
    ],
)
def test_toc_row_does_not_start_section_in_synthetic_doc(regulator, toc_row):
    heading = "Description of Institution" if regulator == "OCC" else "DESCRIPTION OF INSTITUTION"
    scope_heading = "Scope of the Evaluation" if regulator == "OCC" else "SCOPE OF EVALUATION"
    pages = _doc(
        "PUBLIC DISCLOSURE\n\nSome Bank\n",
        f"Table of Contents\n{toc_row}\n{scope_heading} ............... 3\nLENDING TEST ...... 4\n",
        f"{heading}\nThe bank is small.\n\n{scope_heading}\nFull scope.\n",
    )
    section = locate.locate_section("description_of_institution", "d", pages, regulator)
    assert section is not None
    assert section.page_start == 3
    assert section.text == f"{heading}\nThe bank is small.\n"
    assert locate.tag_pages(pages, regulator)[1].is_toc


def test_section_prefers_heading_in_declared_scope():
    """A state-level repeat of a heading is used only when no institution-level one exists."""
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        "STATE OF TEXAS\nSCOPE OF EVALUATION\nState scope.\n",
        "SCOPE OF EVALUATION\nInstitution scope.\n",
    )
    # scope never returns to institution, so page 3 is still state scope: confidence drops
    section = locate.locate_section("scope_of_evaluation", "d", pages, "FDIC")
    assert section is not None
    assert section.page_start == 2
    assert section.scope == "state"
    assert section.confidence == 0.5


def test_max_pages_caps_section_length():
    pages = _doc(
        "PUBLIC DISCLOSURE\nSome Bank\n",
        "page two\n",
        "page three\n",
    )
    section = locate.locate_section("cover", "d", pages, "FDIC")
    assert section is not None
    assert (section.page_start, section.page_end) == (1, 1)
    assert section.text == "PUBLIC DISCLOSURE\nSome Bank\n"


def test_missing_section_returns_none():
    pages = _doc("PUBLIC DISCLOSURE\n", "Nothing here.\n")
    assert locate.locate_section("ltd_ratio", "d", pages, "OCC") is None
    found, missing = locate.locate_all("d", pages, "OCC")
    assert list(found) == ["cover"]
    assert "ltd_ratio" in missing


# Example heading per pattern in config/headings.yaml; every pattern must appear here.
HEADING_EXAMPLES: dict[str, str] = {
    "^PUBLIC DISCLOSURE$": "PUBLIC DISCLOSURE",
    "^Overall CRA Rating$": "Overall CRA Rating",
    "^INSTITUTION'?S CRA RATING": "INSTITUTION'S CRA RATING: This institution is rated Satisfactory.",
    "^This institution is rated": "This institution is rated Satisfactory.",
    "^INSTITUTION RATING$": "INSTITUTION RATING",
    "^(INSTITUTION|INTUITION)('?S)? COMMUNITY REINVESTMENT ACT (\\(CRA\\) )?RATING$": (
        "INSTITUTION'S COMMUNITY REINVESTMENT ACT (CRA) RATING"
    ),
    "^([A-Z0-9&.,' -]+ )?BANK('?S)? CRA RATING$": "COMMERCIAL SAVINGS BANK'S CRA RATING",
    "^Description of Institution$": "Description of Institution",
    "^DESCRIPTION OF INSTITUTION$": "DESCRIPTION OF INSTITUTION",
    "^DESCRIPTION OF (THE )?INSTITUTION$": "DESCRIPTION OF THE INSTITUTION",
    "^Description of Assessment Areas?$": "Description of Assessment Areas",
    "^DESCRIPTION OF ASSESSMENT AREAS?$": "DESCRIPTION OF ASSESSMENT AREA",
    "^DESCRIPTION OF (THE )?ASSESSMENT AREAS?\\d?$": "DESCRIPTION OF ASSESSMENT AREA1",
    "^Scope of (the )?Evaluation$": "Scope of the Evaluation",
    "^SCOPE OF EVALUATION$": "SCOPE OF EVALUATION",
    "^SCOPE OF EXAMINATION$": "SCOPE OF EXAMINATION",
    "^SCOPE OF THE (EVALUATION|EXAMINATION)$": "SCOPE OF THE EVALUATION",
    "^Loan-to-Deposit Ratio$": "Loan-to-Deposit Ratio",
    "^(Net )?Loan[-\u2013 ]to[- ]Deposit (\\((N?LTD)\\) )?Ratio( \\(N?LTD\\))?\\d?$": (
        "Loan-to-Deposit (LTD) Ratio"
    ),
    "^LOAN-TO-DEPOSIT RATIO ANALYSIS$": "LOAN-TO-DEPOSIT RATIO ANALYSIS",
    "^(Net )?LTD Ratio$": "LTD Ratio",
    "^Net-Loan-to Deposit-Ratio$": "Net-Loan-to Deposit-Ratio",
    "^Lending in (the )?Assessment Areas?$": "Lending in the Assessment Area",
    "^Assessment Area Concentration$": "Assessment Area Concentration",
    "^AA Concentration$": "AA Concentration",
    "^COMPARISON OF CREDIT EXTENDED INSIDE AND OUTSIDE (OF )?THE (ASSESSMENT AREAS?|AAS?)$": (
        "COMPARISON OF CREDIT EXTENDED INSIDE AND OUTSIDE THE ASSESSMENT AREAS"
    ),
    "^Discriminatory or Other Illegal Credit Practices Review$": (
        "Discriminatory or Other Illegal Credit Practices Review"
    ),
    "^DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW$": (
        "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW"
    ),
    # FDIC_22876_20240624 p12 prints the singular "PRACTICE"; FDIC_57069_20220214 p11 "EVIEW"
    "^DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES? R?EVIEW$": (
        "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICE REVIEW"
    ),
    "^FAIR LENDING OR OTHER ILLEGAL (CREDIT )?PRACTICES( REVIEW)?$": (
        "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW"
    ),
}


def _section_pattern_cases() -> list[tuple[str, str, str]]:
    cases = []
    for name, spec in load_headings()["sections"].items():
        for regulator in ("OCC", "FDIC", "FRB"):
            for pattern in spec.get(regulator, []):
                cases.append((regulator, name, pattern))
    return cases


@pytest.mark.parametrize(("regulator", "name", "pattern"), _section_pattern_cases())
def test_every_section_pattern_is_exercised(regulator, name, pattern):
    assert pattern in HEADING_EXAMPLES, f"add an example heading for {pattern!r}"
    heading = HEADING_EXAMPLES[pattern]
    assert re.search(pattern, heading, re.IGNORECASE)
    pages = _doc("Front matter\n", f"{heading}\nBody line one.\n\nLENDING TEST\nx\n")
    section = locate.locate_section(name, "d", pages, regulator)
    markers = load_headings()["scope_markers"]
    is_marker = any(
        re.search(p, heading, re.IGNORECASE)
        for spec in markers.values()
        for p in spec.get(regulator, [])
    )
    if is_marker:
        # OCC prints "Description of Assessment Area" only inside state sections, where it is
        # a scope marker; the marker wins and the section is reported missing.
        assert section is None
        assert locate.tag_pages(pages, regulator)[1].scope == "assessment_area"
    else:
        assert section is not None, (regulator, name, pattern)
        assert section.heading_matched == heading
        assert section.text == f"{heading}\nBody line one.\n"


# --------------------------------------------------------------------------- coverage report


def test_coverage_report_on_samples():
    docs = locate._docs_from_samples(SAMPLES)
    report = locate.coverage_report(docs)
    assert len(report) == 8
    assert set(report["doc_id"]) == set(SAMPLE_META)
    found_cols = [f"found_{n}" for n in locate.section_names()]
    assert all(c in report.columns for c in found_cols)
    # the only misses are the OCC institution-level assessment-area descriptions
    misses = report.loc[~report["found_description_of_assessment_area"], "regulator"]
    assert list(misses) == ["OCC"] * 3
    other_cols = [c for c in found_cols if c != "found_description_of_assessment_area"]
    assert report[other_cols].all().all()
    assert (report["n_toc_pages"] >= 1).all()


def test_load_pages_json_accepts_parse_format_and_plain_strings(tmp_path):
    pages = _doc("PUBLIC DISCLOSURE\n", "Second page\n")
    path = tmp_path / "x.pages.json"
    path.write_text(json.dumps([p.model_dump() for p in pages]), encoding="utf-8")
    assert locate.load_pages_json(path) == pages
    path.write_text(json.dumps(["PUBLIC DISCLOSURE\n", "Second page\n"]), encoding="utf-8")
    assert [p.text for p in locate.load_pages_json(path)] == [p.text for p in pages]


def test_cli_samples_dir(tmp_path, capsys):
    report = tmp_path / "coverage.csv"
    assert locate.main(["--samples-dir", str(SAMPLES), "--report", str(report)]) == 0
    table = pd.read_csv(report)
    assert len(table) == 8
    assert "8 documents" in capsys.readouterr().out


def test_cli_manifest_reads_pages_json_and_skips_unparsed_docs(tmp_path):
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    stem = "FDIC_26816_LibertyBank_2022_SM_SN"
    pages = locate.pages_from_text((SAMPLES / f"{stem}.txt").read_text(encoding="utf-8"))
    (text_dir / "FDIC_26816_20220809.pages.json").write_text(
        json.dumps([p.model_dump() for p in pages]), encoding="utf-8"
    )
    manifest = tmp_path / "corpus.csv"
    with open(manifest, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["doc_id", "regulator"])
        writer.writeheader()
        writer.writerow({"doc_id": "FDIC_26816_20220809", "regulator": "FDIC"})
        writer.writerow({"doc_id": "FDIC_99999_20220101", "regulator": "FDIC"})  # not parsed
    report = tmp_path / "coverage.csv"
    rc = locate.main(
        ["--manifest", str(manifest), "--text-dir", str(text_dir), "--report", str(report)]
    )
    assert rc == 0
    table = pd.read_csv(report)
    assert list(table["doc_id"]) == ["FDIC_26816_20220809"]
    assert bool(table.loc[0, "found_illegal_credit_practices_review"])
    assert int(table.loc[0, "page_illegal_credit_practices_review"]) == 9
    assert int(table.loc[0, "n_missing"]) == 0


def test_cli_missing_manifest_fails(tmp_path):
    assert locate.main(["--manifest", str(tmp_path / "none.csv")]) == 1


# --------------------------------------------------------------------------- FRB district layouts
# Corpus tuning of 2026-10-08 (design 6.1): the FRB headings outside the New York and Kansas City
# seeds. Each case is a short excerpt of a corpus PE (doc_id and page cited), not a fixture file.

NBSP = " "


@pytest.mark.parametrize(
    ("name", "heading", "source"),
    [
        ("rating", "BANK'S CRA RATING", "FRB_180845_20241104 p3"),
        ("rating", "BANK CRA RATING", "FRB_807348_20230710 p3"),
        ("rating", "BLISSFIELD STATE BANK'S CRA RATING", "FRB_937843_20231016 p3"),
        (
            "rating",
            "INSTITUTION'S COMMUNITY REINVESTMENT ACT (CRA) RATING",
            "FRB_150727_20250512 p3",
        ),
        ("rating", "INSTITUTION COMMUNITY REINVESTMENT ACT RATING", "FRB_645465_20240401 p3"),
        ("rating", "INTUITION'S COMMUNITY REINVESTMENT ACT RATING", "FRB_884358_20250303 p3"),
        ("description_of_institution", "DESCRIPTION OF THE INSTITUTION", "FRB_1015252_20230306 p4"),
        (
            "description_of_assessment_area",
            "DESCRIPTION OF ASSESSMENT AREA1",
            "FRB_180845_20241104 p5",
        ),
        (
            "description_of_assessment_area",
            "DESCRIPTION OF THE ASSESSMENT AREA",
            "FRB_368054_20230612 p4",
        ),
        ("scope_of_evaluation", "SCOPE OF THE EVALUATION", "FRB_1015252_20230306 p3"),
        ("ltd_ratio", "Loan-to-Deposit (LTD) Ratio", "FRB_1007051_20230515 p8"),
        ("ltd_ratio", "Loan–to-Deposit (LTD) Ratio", "FRB_49241_20230605 p7"),
        ("ltd_ratio", "Net Loan-to-Deposit Ratio (NLTD)", "FRB_150727_20250512 p8"),
        ("ltd_ratio", "LOAN TO DEPOSIT RATIO", "FRB_92902_20230403 p10"),
        ("ltd_ratio", "Net Loan-to-Deposit Ratio2", "FRB_352651_20230417 p4"),
        ("ltd_ratio", "LOAN-TO-DEPOSIT RATIO ANALYSIS", "FRB_126553_20230508 p10"),
        ("ltd_ratio", "LTD Ratio", "FRB_353238_20230925 p13"),
        ("ltd_ratio", "Net-Loan-to Deposit-Ratio", "FRB_371362_20231023 p5"),
        ("aa_concentration", "AA Concentration", "FRB_177135_20240819 p12"),
        (
            "aa_concentration",
            "COMPARISON OF CREDIT EXTENDED INSIDE AND OUTSIDE THE ASSESSMENT AREAS",
            "FRB_153054_20240212 p7",
        ),
        (
            "aa_concentration",
            "COMPARISON OF CREDIT EXTENDED INSIDE AND OUTSIDE THE AA",
            "FRB_382050_20240129 p10",
        ),
        (
            "illegal_credit_practices_review",
            "Fair Lending or Other Illegal Practices Review",
            "FRB_1864197_20230117 p9",
        ),
        (
            "illegal_credit_practices_review",
            "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES",
            "FRB_2785477_20250602 p36",
        ),
    ],
)
def test_frb_district_heading_variants(name, heading, source):
    pages = _doc("PUBLIC DISCLOSURE\n", f"{heading}\nBody line one.\n\nLENDING TEST\nx\n")
    section = locate.locate_section(name, "d", pages, "FRB")
    assert section is not None, source
    assert section.heading_matched == heading
    assert section.text == f"{heading}\nBody line one.\n"


def test_normalize_line_maps_frb_chicago_glyphs():
    """FRB_180845_20241104 p3: no-break spaces between words, the modifier letter prime as
    apostrophe and the Unicode hyphen; each glyph maps to one character."""
    raw = "BANKʹS CRA RATING "
    assert locate.normalize_line(raw) == "BANK'S CRA RATING"
    assert locate.normalize_line("Loan‐to‑Deposit Ratio") == "Loan-to-Deposit Ratio"
    assert len("a b".translate(locate._QUOTES)) == 3


def test_frb_chicago_no_break_space_layout_is_located():
    """FRB_180845_20241104 pages 1 to 5 (Chicago), every space printed as U+00A0."""

    def nb(text: str) -> str:
        return text.replace(" ", NBSP)

    pages = _doc(
        nb("PUBLIC DISCLOSURE \n\nIllini State Bank \nRSSD# 180845 \n"),
        nb(
            "TABLE OF CONTENTS \n"
            "SCOPE OF EXAMINATION ........................................ 2 \n"
            "DESCRIPTION OF INSTITUTION .................................. 3 \n"
            "LENDING TEST ................................................ 10 \n"
        ),
        nb(
            "2 \nBANKʹS CRA RATING \n\nIllini State Bank is rated Satisfactory. \n\n"
            "SCOPE OF EXAMINATION \n\nIllini State Bankʹs performance was evaluated. \n"
        ),
        nb(
            "3 \nDESCRIPTION OF INSTITUTION \n\nIllini State Bank is a wholly owned subsidiary. \n\n"
            "DESCRIPTION OF ASSESSMENT AREA1 \n\nThe assessment area is comprised of 13 tracts. \n"
        ),
    )
    found, missing = locate.locate_all("d", pages, "FRB")
    assert missing == ["ltd_ratio", "aa_concentration", "illegal_credit_practices_review"]
    assert found["rating"].heading_matched == "BANK'S CRA RATING"  # the page number is not joined
    assert f"rated{NBSP}Satisfactory" in found["rating"].text  # section text stays verbatim
    assert (
        found["scope_of_evaluation"].page_start,
        found["description_of_institution"].page_start,
    ) == (3, 4)
    assert (
        found["description_of_assessment_area"].heading_matched == "DESCRIPTION OF ASSESSMENT AREA1"
    )
    assert all(s.scope == "institution" for s in found.values())


def test_frb_criterion_table_rows_are_not_headings():
    """FRB_1007051_20230515 (St. Louis): the scope section lists each criterion with its review
    period (p3); the sections themselves start on p8. FRB_3350658_20230612 p3 prints the
    products instead of the period, under a "Time Period" column header."""
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        "SCOPE OF EXAMINATION\nThe following table includes the time period for each category.\n"
        "Performance Criterion\nTime Period\nLTD Ratio\nMarch 31, 2018 – March 31, 2023\n"
        "Assessment Area Concentration\nMarch 19, 2018 – December 31, 2021\n",
        "Lending Test Performance\nCriterion\nProducts Selected for Review\nTime Period\n"
        "Net LTD Ratio\n•\nLoans reported under the Home\n",
        "CONCLUSIONS WITH RESPECT TO PERFORMANCE CRITERIA\n"
        "Loan-to-Deposit (LTD) Ratio\nThis performance criterion evaluates the average LTD ratio.\n"
        "Assessment Area Concentration\nThe bank originated a majority of its loans inside the AA.\n",
    )
    found, _ = locate.locate_all("d", pages, "FRB")
    assert found["ltd_ratio"].page_start == 4
    assert found["ltd_ratio"].heading_matched == "Loan-to-Deposit (LTD) Ratio"
    assert found["aa_concentration"].page_start == 4
    assert "majority of its loans inside" in found["aa_concentration"].text


def test_frb_list_item_after_lone_bullet_is_not_a_heading():
    """FRB_2736714_20250707 p5: the scope list prints each criterion after a Wingdings bullet on
    its own line; the LTD section heading follows on a later page."""
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        "LENDING TEST\nThe lending portion of the evaluation was based on:\n"
        "\nLTD Ratio\n\nLending inside versus outside the assessment area\n",
        "LTD Ratio\nThe bank's average LTD ratio is reasonable.\n",
    )
    section = locate.locate_section("ltd_ratio", "d", pages, "FRB")
    assert section is not None and section.page_start == 3


def test_toc_page_without_leaders_is_skipped():
    """FRB_925653_20240916 p2 (Dallas): the ToC page numbers sit on their own lines, so no row
    carries a dot leader; the whole page is ToC and the appendix rows set no scope."""
    toc = (
        "Bank of Texas\nMidland, Texas\nTABLE OF CONTENTS\n"
        "INSTITUTION'S COMMUNITY REINVESTMENT ACT RATING\n1\nSCOPE OF THE EVALUATION\n1\n"
        "DESCRIPTION OF THE INSTITUTION\n2\nEL PASO METROPOLITAN ASSESSMENT AREA\n6\n"
        "DESCRIPTION OF THE INSTITUTION'S OPERATIONS\n6\n"
        "APPENDIX D - LIMITED-SCOPE REVIEW ASSESSMENT AREA LENDING TABLES\n23\n"
        "APPENDIX E - GLOSSARY\n26\n"
    )
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        toc,
        "INSTITUTION'S COMMUNITY REINVESTMENT ACT RATING\nThis institution is rated Satisfactory.\n"
        "SCOPE OF THE EVALUATION\nExaminers utilized the Interagency procedures.\n",
    )
    tagged = locate.tag_pages(pages, "FRB")
    assert tagged[1].is_toc and tagged[1].scope == "toc"
    assert tagged[2].scope == "institution"
    found, _ = locate.locate_all("d", pages, "FRB")
    assert found["rating"].page_start == 3 and found["rating"].scope == "institution"
    assert found["scope_of_evaluation"].page_start == 3
    # a cover page with a stray "Table of Contents" line and prose stays a body page
    # (FRB_560214_20241113 p1)
    cover = _doc(
        "Table of Contents\nPUBLIC DISCLOSURE\nNOTE:\n"
        "This document is an evaluation of this bank's record of meeting the credit needs of its\n"
    )
    assert not locate.tag_pages(cover, "FRB")[0].is_toc


def test_toc_rows_after_the_last_leader_row_are_skipped():
    """FRB_623454_20250310 p2: the last ToC row "APPENDIX D - GLOSSARY" has no leader; it must
    not open the appendix scope before the body starts."""
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        "TABLE OF CONTENTS\n"
        "SCOPE OF THE EVALUATION ............................................... 1\n"
        "DESCRIPTION OF THE INSTITUTION ........................................ 2\n"
        "CONCLUSIONS WITH RESPECT TO PERFORMANCE TESTS .......................... 2\n"
        "APPENDIX D – GLOSSARY\n",
        "SCOPE OF THE EVALUATION\nExaminers utilized the Interagency procedures.\n",
    )
    assert locate.tag_pages(pages, "FRB")[2].scope == "institution"
    section = locate.locate_section("scope_of_evaluation", "d", pages, "FRB")
    assert section is not None and section.scope == "institution" and section.confidence == 1.0


def test_frb_state_marker_is_case_sensitive():
    """FRB_242949_20250324 p8: "State of Iowa" is a row of the population table; the state
    heading is printed in capitals (FRB_825847_20240923 p10)."""
    row = _doc("PUBLIC DISCLOSURE\n", "Population Change\nArea\nAssessment Area\nState of Iowa\n")
    assert [p.scope for p in locate.tag_pages(row, "FRB")] == ["institution", "institution"]
    heading = _doc("PUBLIC DISCLOSURE\n", "STATE OF IOWA\nCRA RATING FOR IOWA: Satisfactory\n")
    assert locate.tag_pages(heading, "FRB")[1].scope == "state"


# FDIC state and multistate MSA markers (2026-10-08): the state pattern and the name prefix of
# the msa pattern are case-sensitive. The excerpts are shortened from the cited corpus PEs.
FDIC_LTD_PAGE = (
    "Loan-to-Deposit Ratio\n\n"
    "The bank's net loan-to-deposit ratio is reasonable given its size and financial condition.\n\n"
    "Assessment Area Concentration\n\n"
    "The bank made a majority of its home mortgage and small business loans in its AAs.\n"
)


def _marker_events(pages: list[Page], regulator: str = "FDIC") -> list[tuple[str, str]]:
    lines, _ = locate._analyze(pages, regulator)
    return [(ln.event[1], ln.text) for ln in lines if ln.event and ln.event[0] == "marker"]


@pytest.mark.parametrize(
    ("excerpt", "source"),
    [
        (
            "STATE OF OHIO\n\nCRA RATING FOR STATE OF OHIO:  SATISFACTORY\n\n"
            "The Lending Test is rated:  Satisfactory\n"
            "The Community Development Test is rated: Satisfactory\n",
            "FDIC_9463_20240701 p21",
        ),
        (
            "DESCRIPTION OF INSTITUTION’S OPERATIONS IN STATE OF\nOKLAHOMA\n\n"
            "Cattlemens Bank designated one AA in the State of Oklahoma: the Oklahoma Non-MSA AA.\n",
            "FDIC_12285_20250721 p11",
        ),
    ],
)
def test_fdic_state_heading_in_capitals_opens_state_scope(excerpt, source):
    pages = _doc("PUBLIC DISCLOSURE\n", FDIC_LTD_PAGE, excerpt)
    assert [kind for kind, _ in _marker_events(pages)] == ["state"], source
    assert locate.tag_pages(pages, "FDIC")[2].scope == "state", source
    lines, _ = locate._analyze(pages, "FDIC")
    assert locate.aa_units(lines, ("assessment_area", "msa")) == (set(), True), source


@pytest.mark.parametrize(
    ("excerpt", "source"),
    [
        # unemployment table, the state row between county rates and the national average
        (
            "County or Area\nDec 2024\nLogan County\n4.7\nMason County\n4.9\nTazewell County\n"
            "4.6\nState of Illinois\n4.3\nNational Average\n4.1\n"
            "Source: Bureau of Labor Statistics\n",
            "FDIC_10077_20250218 p6",
        ),
        # loans by rated area, a state row between counts and percentages
        (
            "$(000s)\n%\n#\n%\nState of Georgia\n280,770\n78.8\n386,410\n90.8\n8\n88.9\n"
            "State of Florida\n75,716\n",
            "FDIC_15699_20240624 p7",
        ),
        # community development table: the title, the state row, then the column headers
        (
            "Community Development Lending\nState of Louisiana\nAffordable\nHousing\nCommunity\n"
            "Services\n",
            "FDIC_8141_20240304 p17",
        ),
        # a prose line of the state's Lending Test conclusion
        (
            "Century Bank demonstrated a satisfactory Lending Test in the State of Mississippi.\n"
            "State of Mississippi proves consistent with the Lending Test conclusions for both the"
            " Mississippi\nNon-MSA AA and Gulfport-Biloxi-Pascagoula MSA AA.\n",
            "FDIC_11448_20241118 p33",
        ),
    ],
)
def test_fdic_mixed_case_state_row_or_prose_is_not_a_marker(excerpt, source):
    """Matched without case, these lines opened state scope before the institution-level LTD
    and AA concentration sections (283 FDIC PEs of split corpus)."""
    pages = _doc("PUBLIC DISCLOSURE\n", excerpt, FDIC_LTD_PAGE)
    assert _marker_events(pages) == [], source
    assert [p.scope for p in locate.tag_pages(pages, "FDIC")] == ["institution"] * 3, source
    found, _ = locate.locate_all("d", pages, "FDIC")
    for name in ("ltd_ratio", "aa_concentration"):
        assert (found[name].scope, found[name].confidence) == ("institution", 1.0), source


@pytest.mark.parametrize(
    ("excerpt", "heading", "source"),
    [
        (
            "SALISBURY, MD-DE MULTISTATE MSA – Full-Scope Review\n"
            "CRA RATING FOR SALISBURY, MD-DE MULTISTATE MSA:  SATISFACTORY\n"
            "The Lending Test is rated:  Satisfactory\n",
            "SALISBURY, MD-DE MULTISTATE MSA – Full-Scope Review",
            "FDIC_5874_20221024 p12",
        ),
        (
            "WHEELING, WEST VIRGINIA-OHIO\nMULTISTATE METROPOLITAN STATISTICAL AREA\n"
            "Full-Scope Review\n\nCRA RATING FOR WHEELING, WV-OH MULTISTATE MSA AA: SATISFACTORY\n",
            "CRA RATING FOR WHEELING, WV-OH MULTISTATE MSA AA: SATISFACTORY",
            "FDIC_9463_20240701 p12",
        ),
        (
            "CONCLUSIONS ON PERFORMANCE CRITERIA IN MULTISTATE MSA\nLENDING TEST\n"
            "BankProv demonstrated reasonable performance under the Lending Test.\n",
            "CONCLUSIONS ON PERFORMANCE CRITERIA IN MULTISTATE MSA",
            "FDIC_90141_20221024 p17",
        ),
    ],
)
def test_fdic_multistate_msa_heading_in_capitals_opens_msa_scope(excerpt, heading, source):
    pages = _doc("PUBLIC DISCLOSURE\n", FDIC_LTD_PAGE, excerpt)
    assert _marker_events(pages)[0] == ("msa", heading), source
    # the line scope from the heading on (the page scope is that of most of its lines)
    text = locate.doc_text(pages)
    spans = locate.scope_spans(pages, "FDIC")
    assert locate.scope_at(spans, text.index(heading)) == "msa", source
    assert locate.scope_at(spans, len(text) - 2) == "msa", source
    assert locate.scope_at(spans, text.index("Loan-to-Deposit Ratio")) == "institution", source


@pytest.mark.parametrize(
    ("excerpt", "source"),
    [
        (
            "Metropolitan Division (MD) and the Cambridge-Newton-Framingham, MA MD, which are"
            " both in\nthe Boston-Cambridge-Newton, MA-NH multistate MSA.\n"
            "The State of NH AA consists of 71 census tracts located in the partial counties.\n",
            "FDIC_24540_20221114 p5",
        ),
        (
            "and Borrower Profile criteria supports this rating.  CBT's overall performance was"
            " consistent across the\nMD-DE Multistate MSA and Virginia non-MSA assessment areas.\n\n"
            "This section presents the institution's overall performance under the Lending Test.\n",
            "FDIC_5874_20221024 p7",
        ),
        (
            "of Oregon and consists of all 87 CTs in Clackamas, 197 CTs in Multnomah and all 134"
            " CTs in\nHillsboro Multistate MSA. No changes were made to this AA since the previous"
            " evaluation.\nEconomic and Demographic Data\n",
            "FDIC_58752_20250707 p12",
        ),
        # a row of the loans-by-area table
        ("#\n$(000s)\n#\n$(000s)\nMD-DE Multistate MSA\n2\n457\n0\n0\n", "FDIC_5874_20221024 p9"),
    ],
)
def test_fdic_prose_or_table_multistate_msa_is_not_a_marker(excerpt, source):
    """Matched without case, these lines opened msa scope before the LTD section
    (FDIC_5874_20221024 p7 precedes "Loan-to-Deposit Ratio" on the same page) and each counted
    as one more assessment-area unit."""
    pages = _doc("PUBLIC DISCLOSURE\n", excerpt, FDIC_LTD_PAGE)
    assert _marker_events(pages) == [], source
    assert [p.scope for p in locate.tag_pages(pages, "FDIC")] == ["institution"] * 3, source
    lines, _ = locate._analyze(pages, "FDIC")
    assert locate.aa_units(lines, ("assessment_area", "msa")) == (set(), False), source
    found, _ = locate.locate_all("d", pages, "FDIC")
    for name in ("ltd_ratio", "aa_concentration"):
        assert (found[name].scope, found[name].confidence) == ("institution", 1.0), source


@pytest.mark.parametrize(
    ("regulator", "kind", "pattern", "matches", "skips"),
    [
        ("FDIC", "state", "^(?-i:STATE OF [A-Z ]+)$", "STATE OF OHIO", "State of Ohio"),
        ("FRB", "state", "^(?-i:STATE OF [A-Z ]+)$", "STATE OF IOWA", "State of Iowa"),
        (
            "FDIC",
            "msa",
            "^(?-i:[A-Z ,-]+ MULTISTATE (MSA|METROPOLITAN))",
            "PORTLAND-VANCOUVER-HILLSBORO MULTISTATE MSA – Full-Scope",
            "PVH Multistate MSA",
        ),
    ],
)
def test_case_sensitive_marker_patterns_are_configured(regulator, kind, pattern, matches, skips):
    """The case-sensitive groups live in the configuration: the locator compiles every pattern
    with re.IGNORECASE and the inline (?-i:...) group turns it off for its part only (the
    "Full-Scope" tail of a multistate MSA title stays free)."""
    assert pattern in load_headings()["scope_markers"][kind][regulator]
    compiled = [p for scope, regs in locate._patterns(regulator).markers if scope == kind for p in regs]
    assert pattern in [p.pattern for p in compiled]
    one = next(p for p in compiled if p.pattern == pattern)
    assert one.flags & re.IGNORECASE
    assert one.search(matches) and not one.search(skips)


@pytest.mark.parametrize(
    ("heading", "scope", "source"),
    [
        ("DESCRIPTION OF INSTITUTION'S OPERATIONS IN NEW YORK", "state", "FieldPoint sample L48"),
        (
            "DESCRIPTION OF INSTITUTION'S OPERATIONS IN THE STATE OF CONNECTICUT",
            "state",
            "FieldPoint sample L57",
        ),
        (
            "DESCRIPTION OF INSTITUTION'S OPERATIONS IN THE LITTLE ROCK",
            "assessment_area",
            "FRB_453446_20240903 p10",
        ),
        (
            "DESCRIPTION OF THE INSTITUTION'S OPERATIONS IN THE DALLAS METROPOLITAN ASSESSMENT",
            "assessment_area",
            "FRB_726953_20250218 p5",
        ),
        (
            "DESCRIPTION OF THE INSTITUTION'S OPERATIONS",
            "assessment_area",
            "FRB_336361_20221128 p7",
        ),
        (
            "DESCRIPTION OF THE INSTITUTIONS OPERATIONS IN THE DEL RIO NONMETROPOLITAN",
            "assessment_area",
            "FRB_623454_20250310 p8",
        ),
        ("DESCRIPTION OF OPERATIONS IN STANISLAUS", "assessment_area", "FRB_1864197_20230117 p10"),
    ],
)
def test_frb_operations_headings_open_state_or_assessment_area_scope(heading, scope, source):
    pages = _doc("PUBLIC DISCLOSURE\n", f"{heading}\nThe bank operates two branches.\n")
    assert locate.tag_pages(pages, "FRB")[1].scope == scope, source


def test_frb_review_section_ends_before_the_next_area_title():
    """The institution review is followed by the first area's title line: Dallas
    (FRB_336361_20221128 p6-7) and San Francisco (FRB_1864197_20230117 p9-10)."""
    review = (
        "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n\n"
        "No evidence of discriminatory or other illegal credit practices was identified.\n\n"
    )
    for title, operations in [
        (
            "DENTON AND WISE COUNTIES METROPOLITAN ASSESSMENT AREA",
            "DESCRIPTION OF THE INSTITUTION'S OPERATIONS",
        ),
        ("FULL-SCOPE ASSESSMENT AREA CONCLUSIONS", "DESCRIPTION OF OPERATIONS IN STANISLAUS"),
    ]:
        pages = _doc(
            "PUBLIC DISCLOSURE\n",
            review + f"{title}\n{operations}\nThe AA consists of two counties.\n",
        )
        section = locate.locate_section("illegal_credit_practices_review", "d", pages, "FRB")
        assert section is not None
        assert title not in section.text
        assert section.text.rstrip().endswith("was identified.")


def test_wrapped_headings_are_joined():
    """A wrapped section heading is matched on the joined text; page numbers, one-word lines,
    two-word section starts and scope markers are not joined ("2" above "BANK'S CRA RATING",
    FRB_2044820_20231127 p3; "LENDING" over "TEST", FRB_1864197_20230117 p3; "Assessment Area"
    over "Concentration", FDIC_10635_20240122 p7; the lending-table cell "MSA 36740 (Orlando-"
    over "Kissimmee-Sanford, FL)", FRB_3664588_20240116 p12)."""
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        "FAIR LENDING OR OTHER ILLEGAL CREDIT\nPRACTICES REVIEW\nNo evidence was identified.\n",
        "LENDING TEST\nAssessment Area\nMSA 36740 (Orlando-\nKissimmee-Sanford, FL)\n"
        "Assessment Area\n0\n",
    )
    section = locate.locate_section("illegal_credit_practices_review", "d", pages, "FRB")
    assert section is not None
    assert section.heading_matched == "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW"
    assert section.text.endswith("No evidence was identified.\n")
    assert locate.tag_pages(pages, "FRB")[2].scope == "institution"

    rating_table = _doc(
        "PUBLIC DISCLOSURE\n",
        "INSTITUTION RATING\nThis institution is rated Satisfactory.\n"
        "PERFORMANCE\nLEVELS\nLENDING\nTEST\nOUTSTANDING\n",
    )
    rating = locate.locate_section("rating", "d", rating_table, "FRB")
    assert rating is not None and rating.text.rstrip().endswith("OUTSTANDING")

    sample_table = _doc(
        "PUBLIC DISCLOSURE\n",
        "Loan Products Reviewed\nAssessment Area\nConcentration\nUniverse Inside\n",
        "Assessment Area Concentration\nThe bank originated a majority of loans inside.\n",
    )
    concentration = locate.locate_section("aa_concentration", "d", sample_table, "FDIC")
    assert concentration is not None and concentration.page_start == 3


# --------------------------------------------------------------------------- review census
# Additions of 2026-10-08 for the review-section census: security markings and "x of y" page
# footers as furniture, running headers per document, and the end of the review section at a
# one-cell-per-line table (end_at_table_run in config/headings.yaml).


@pytest.mark.parametrize(
    "line",
    [
        "CONTROLLED//FDIC INTERNAL ONLY",  # FDIC_11777_20240924 p14
        "NONCONFIDENTIAL // EXTERNAL",  # FRB_426141_20221212 p13
        "PUBLIC/OFFICIAL RELEASE // EXTERNAL",  # FRB_680130_20230724 p10
        "8 of 29",  # FRB_114840_20241202 p10
    ],
)
def test_security_markings_and_bare_page_of_footer_are_furniture(line):
    assert locate.is_furniture_line(locate.normalize_line(line))


def test_running_lines_finds_repeated_page_headers():
    header = "Some Bank\nCRA Performance Evaluation\nTown, Kansas\nMay 8, 2023\n"
    pages = _doc(
        *(f"{header}Body text of page {i} that is long enough to be prose.\n" for i in range(5))
    )
    running = locate.running_lines(pages)
    assert {"Some Bank", "CRA Performance Evaluation", "Town, Kansas", "May 8, 2023"} <= running
    assert not any(line.startswith("Body text") for line in running)
    assert locate.running_lines(pages[:2]) == frozenset()  # fewer than RUNNING_MIN_PAGES pages


def test_review_section_ends_before_running_header_and_table():
    """A table printed one cell per line after the review paragraph is not part of the section
    (FDIC_109_20250224 p8), and the next page's running header is trimmed (FRB_137447_20230724)."""
    header = "Some Bank\nCRA Performance Evaluation\nTown, Kansas\n"
    body = "Prose line long enough to read as running text in a performance evaluation.\n"
    pages = _doc(
        f"{header}PUBLIC DISCLOSURE\n{body}",
        f"{header}{body}",
        f"{header}DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n\n"
        "Examiners did not identify any discriminatory or other illegal credit practices.\n\n"
        "Lending Inside and Outside of the Assessment Area\n"
        "Inside\nOutside\nInside\nOutside\n#\n%\n174\n67.7\n",
        f"{header}{body}",
    )
    section = locate.locate_section("illegal_credit_practices_review", "d", pages, "FDIC")
    assert section is not None
    assert section.text.rstrip().endswith("illegal credit practices.")
    assert "Lending Inside" not in section.text
    lines = locate._split_lines(pages)
    start = next(i for i, ln in enumerate(lines) if ln.text.startswith("DISCRIMINATORY"))
    cut = locate.table_run_start(lines, start, len(lines))
    assert lines[cut].text == "Lending Inside and Outside of the Assessment Area"


def test_table_run_needs_six_short_lines():
    pages = _doc("HEADING\nshort\nlines\nonly\nfive\nhere\n")
    lines = locate._split_lines(pages)
    assert locate.table_run_start(lines, 0, len(lines)) == len(lines)


_FDIC_REVIEW = (
    "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n\n"
    "The bank's compliance with the laws relating to discrimination and other illegal credit\n"
    "practices was reviewed, including the Fair Housing Act and the Equal Credit Opportunity\n"
    "Act.  Examiners did not\n"
    "identify any discriminatory or other illegal credit practices\n"
)


@pytest.mark.parametrize(
    ("after", "source"),
    [
        (
            "\n\nKANSAS – Full-Scope Review\n\nCRA RATING FOR KANSAS:  SATISFACTORY\n",
            "FDIC_17636_20241028 p11",
        ),
        (
            "\nTEXAS NON-MSA AA – Full-Scope Review\n\nDESCRIPTION OF INSTITUTION'S OPERATIONS IN\n",
            "FDIC_1720_20221003 p14",
        ),
    ],
)
def test_last_paragraph_line_is_not_joined_with_the_next_title(after, source):
    """The last line of the review paragraph, which ends without a period, must not be read
    as the first half of a wrapped heading with the rated-area title after it."""
    pages = _doc("PUBLIC DISCLOSURE\n", _FDIC_REVIEW + after + "The AA has two branches.\n")
    section = locate.locate_section("illegal_credit_practices_review", "d", pages, "FDIC")
    assert section is not None, source
    assert section.text.rstrip().endswith("illegal credit practices"), source
    assert "Full-Scope" not in section.text


@pytest.mark.parametrize(
    ("regulator", "title", "source"),
    [
        ("FDIC", "NON-MSA ASSESSMENT AREA – Full-Scope Review", "FDIC_10685_20240826"),
        ("FDIC", "ILLINOIS NON-MSA ASSESSMENT AREA– Full-Scope Review", "FDIC_17320_20220811 p13"),
        ("FDIC", "STATE OF OREGON-Full-Scope Review", "FDIC_16243_20230207"),
        (
            "FDIC",
            "NONMETROPOLITAN MINNESOTA ASSESSMENT AREA\nFull-Scope Review",
            "FDIC_9737_20240306 p12",
        ),
        (
            "FDIC",
            "PENNSYLVANIA NON-METROPOLITAN STATISTICAL AREAS – Full-\nScope Review",
            "FDIC_7614_20250303 p12",
        ),
        ("FDIC", "NEBRASKA\n\nCRA RATING FOR NEBRASKA: SATISFACTORY", "FDIC_10641_20231211 p13"),
        (
            "FDIC",
            "DESCRIPTION OF INSTITUTION'S OPERATIONS\nIN THE CARBONDALE-MARION, IL MSA",
            "FDIC_57897_20220920 p8",
        ),
        (
            "FRB",
            "NONMETROPOLITAN ILLINOIS STATEWIDE AREA\n(Full-Scope Review)",
            "FRB_137447_20230724 p10",
        ),
        ("FRB", "NONMETROPOLITAN AREA\n(Full Review)", "FRB_21359_20240212 p6"),
        ("FRB", "METROPOLITAN AREA – FULL REVIEW", "FRB_680457_20220912 p10"),
        (
            "FRB",
            "EVANSVILLE, INDIANA-KENTUCKY\nMETROPOLITAN STATISTICAL AREA2",
            "FRB_618740_20250113 p10",
        ),
        ("FRB", "MULTISTATE METROPOLITAN AREA", "FRB_274829_20250303 p9"),
        ("FRB", "Appendix A\nGlossary of Common CRA Terms", "FRB_823656_20230814 p20"),
        ("FRB", "APPENDIX\n\nGLOSSARY", "FRB_528102_20221128 p16"),
        (
            "OCC",
            "United States\n\nCRA rating for the United States1: Satisfactory",
            "OCC_713570_20250519 p7",
        ),
        ("OCC", "Fargo Combined Statistical Area Rating", "OCC_702983_20231023 p7"),
        ("OCC", "Multi-state Metropolitan Statistical Area Rating", "OCC_15379_20250127 p6"),
    ],
)
def test_review_section_ends_at_rated_area_titles(regulator, title, source):
    heading = {
        "OCC": "Discriminatory or Other Illegal Credit Practices Review",
        "FDIC": "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW",
        "FRB": "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW",
    }[regulator]
    paragraph = "No evidence of discriminatory or other illegal credit practices was identified."
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        f"{heading}\n\n{paragraph}\n\n{title}\n\nThe area has two branches and one ATM.\n",
    )
    section = locate.locate_section("illegal_credit_practices_review", "d", pages, regulator)
    assert section is not None, source
    assert "two branches" not in section.text, source
    # a bare area name above a "CRA rating for ..." boundary ("NEBRASKA", "United States")
    # stays in the section; it is one short line of residual
    kept = section.text.split(paragraph, 1)[1].split()
    assert kept in ([], title.split("\n", 1)[0].split()), source


@pytest.mark.parametrize(
    "line",
    [
        "Full Scope",  # scope-table cell, FDIC_10011_20221219 p5
        "FS – Full-Scope; LS – Limited-Scope",  # table legend, FDIC_35530_20240715 p8
        "FS – Full-scope",  # table legend, FDIC_57095_20230620
        "A description of each assessment area is presented in the NMSA Assessment Area-Full-Scope",
        "Description of Institution's Operations sections.",  # prose, FDIC_15809_20221116
    ],
)
def test_fdic_title_boundaries_skip_table_cells_and_prose(line):
    patterns = [re.compile(p, re.IGNORECASE) for p in load_headings()["boundary_patterns"]["FDIC"]]
    assert not any(p.search(line) for p in patterns)


# --------------------------------------------------------------------------- scope rules
# scope_rules in config/headings.yaml (single-AA scope decision, docs/corpus_pass_2.md). The
# excerpts are shortened from the cited corpus PEs.

FRB_REVIEW = "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW"
FRB_REVIEW_BODY = (
    "No evidence of discriminatory or other illegal credit practices inconsistent with helping\n"
    "to meet community credit needs was identified.\n"
)
FRB_LTD_PAGE = (  # FRB_3150205_20250407 p10 and p11 (Dallas)
    "Net Loan-to-Deposit Ratio\n\n"
    "Worthington's 27-quarter NLTD ratio is reasonable and within the range of the NLTD ratios\n"
    "of similarly situated banks.\n\n"
    "Assessment Area Concentration\n\n"
    "Worthington originated a majority of loans both by number and dollar amount inside the AA.\n"
)


def _frb_doc(*area_pages: str) -> list[Page]:
    """FRB PE: cover, rating and scope pages, the given area pages, LTD page, review page."""
    return _doc(
        "PUBLIC DISCLOSURE\n\nWorthington Bank\n",
        "INSTITUTION'S CRA RATING\n\nThis institution is rated Satisfactory.\n",
        *area_pages,
        FRB_LTD_PAGE,
        f"{FRB_REVIEW}\n\n{FRB_REVIEW_BODY}",
    )


def _lone_aa(pages: list[Page], regulator: str = "FRB") -> bool:
    lines, _ = locate._analyze(pages, regulator)
    return locate.lone_aa_unit(lines, locate._patterns(regulator).aa_unit_scopes)


DALLAS_AA = (  # FRB_3150205_20250407 p5
    "DESCRIPTION OF THE INSTITUTION'S OPERATIONS IN THE DFW METROPOLITAN\n\n"
    "The DFW Metropolitan AA consists of Denton and Tarrant Counties.\n"
)
ATLANTA_AA = (  # FRB_648130_20250203 p7
    "DESCRIPTION OF INSTITUTION'S OPERATIONS IN THE MIAMI, FLORIDA AA\n\n"
    "The AA is located in the southeastern tip of Florida along the Atlantic Ocean.\n"
)
NEW_YORK_MSA = (  # FRB_722816_20230626 p7; the title repeats over the tables up to p13
    "MSA 45060 (Syracuse, NY)\n\nSolvay has one assessment area, Onondaga County.\n"
)
NEW_YORK_TABLE = "MSA 45060 (Syracuse, NY)\n\nThe table shows the income of census tracts.\n"
CHICAGO_AA_1 = (  # FRB_930442_20240715 p13 and p50, two of its four AAs
    "DESCRIPTION OF INSTITUTION'S OPERATIONS IN ILLINOIS NON-MSA\n\n"
    "The bank operates eight branches in the Illinois Non-MSA AA.\n"
)
CHICAGO_AA_2 = (
    "DESCRIPTION OF INSTITUTION'S OPERATIONS IN PEORIA, IL MSA\n\n"
    "The bank operates two branches in the Peoria MSA AA.\n"
)


def test_review_heading_resets_scope_to_institution_after_all_aa_sections():
    """Chicago multi-AA layout: the review follows the last AA section (FRB_930442_20240715
    p56 after the Peoria AA on p50). The section is institution scope at confidence 1.0, while
    the page and line scopes stay those of the markers."""
    pages = _frb_doc(CHICAGO_AA_1, CHICAGO_AA_2)
    assert not _lone_aa(pages)
    section = locate.locate_section("illegal_credit_practices_review", "d", pages, "FRB")
    assert section is not None
    assert (section.page_start, section.scope, section.confidence) == (6, "institution", 1.0)
    assert section.text.startswith(FRB_REVIEW)
    assert locate.tag_pages(pages, "FRB")[5].scope == "assessment_area"
    spans = locate.scope_spans(pages, "FRB")
    assert locate.scope_at(spans, section.char_start) == "assessment_area"


@pytest.mark.parametrize(
    ("regulator", "marker", "heading", "source"),
    [
        # an FDIC state heading before the review, FDIC_9463_20240701 p21 order reversed (the
        # table cell "State of Illinois", FDIC_10077_20250218 p6, is no marker since 2026-10-08)
        (
            "FDIC",
            "STATE OF OHIO",
            "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW",
            "FDIC_9463_20240701",
        ),
        # a prose line read as an appendix marker, FDIC_2787_20221205 p8 (review p13)
        (
            "FDIC",
            "Appendix A for additional details regarding each test.",
            "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW",
            "FDIC_2787_20221205",
        ),
        # OCC state rating section before the review, OCC_6488_20230123 pp5-6 order reversed
        (
            "OCC",
            "State of Minnesota",
            "Discriminatory or Other Illegal Credit Practices Review",
            "OCC_6488_20230123",
        ),
        ("FRB", "MSA 45060 (Syracuse, NY)", FRB_REVIEW, "FRB_722816_20230626"),
    ],
)
def test_review_heading_resets_scope_whatever_scope_is_in_effect(
    regulator, marker, heading, source
):
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        f"{marker}\n\nThe area has two branches.\n",
        f"{heading}\n\n{FRB_REVIEW_BODY}",
    )
    lines, _ = locate._analyze(pages, regulator)
    assert any(ln.event and ln.event[0] == "marker" for ln in lines), source
    assert locate.tag_pages(pages, regulator)[2].scope != "institution", source
    section = locate.locate_section("illegal_credit_practices_review", "d", pages, regulator)
    assert section is not None, source
    assert (section.scope, section.confidence) == ("institution", 1.0), source


@pytest.mark.parametrize(
    ("area_pages", "source"),
    [
        ((DALLAS_AA,), "FRB_3150205_20250407"),
        ((ATLANTA_AA,), "FRB_648130_20250203"),
        ((NEW_YORK_MSA, NEW_YORK_TABLE, NEW_YORK_TABLE), "FRB_722816_20230626"),
    ],
)
def test_lone_aa_ltd_and_concentration_are_institution_scope(area_pages, source):
    pages = _frb_doc(*area_pages)
    assert _lone_aa(pages), source
    found, _ = locate.locate_all("d", pages, "FRB")
    for name in ("ltd_ratio", "aa_concentration", "illegal_credit_practices_review"):
        section = found[name]
        assert (section.scope, section.confidence) == ("institution", 1.0), (source, name)
    assert found["ltd_ratio"].page_start == len(pages) - 1
    assert "27-quarter NLTD ratio" in found["ltd_ratio"].text
    assert "Assessment Area Concentration" not in found["ltd_ratio"].text
    # the line scope is that of the marker; only the Section record is overridden
    marker_scope = "msa" if source == "FRB_722816_20230626" else "assessment_area"
    assert locate.tag_pages(pages, "FRB")[len(pages) - 2].scope == marker_scope


@pytest.mark.parametrize(
    ("area_pages", "scope", "case"),
    [
        ((CHICAGO_AA_1, CHICAGO_AA_2), "assessment_area", "two AA operations headings"),
        # two Dallas AAs that both print the bare operations heading under the area title
        (
            (
                "DESCRIPTION OF THE INSTITUTION'S OPERATIONS\n\nDenton County.\n",
                "DESCRIPTION OF THE INSTITUTION'S OPERATIONS\n\nWise County.\n",
            ),
            "assessment_area",
            "two bare Dallas headings",
        ),
        (
            ("MSA 45060 (Syracuse, NY)\n\nOne.\n", "MSA 10580 (Albany, NY)\n\nTwo.\n"),
            "msa",
            "two MSA codes",
        ),
        # a state marker before the one AA marker: a rated state, never a lone unit
        (("STATE OF TEXAS\n\nThe state.\n", DALLAS_AA), "assessment_area", "state and AA"),
    ],
)
def test_multi_aa_ltd_and_concentration_keep_the_scope_found(area_pages, scope, case):
    pages = _frb_doc(*area_pages)
    assert not _lone_aa(pages), case
    found, _ = locate.locate_all("d", pages, "FRB")
    for name in ("ltd_ratio", "aa_concentration"):
        assert (found[name].scope, found[name].confidence) == (scope, 0.5), (case, name)
    review = found["illegal_credit_practices_review"]
    assert (review.scope, review.confidence) == ("institution", 1.0), case


def test_lone_aa_rule_leaves_sections_without_markers_and_in_appendix_alone():
    """No marker: the sections are institution scope as before. A lone AA unit does not lift
    an LTD heading found in the sticky appendix scope."""
    plain = _frb_doc()
    assert not _lone_aa(plain)
    found, _ = locate.locate_all("d", plain, "FRB")
    assert all(s.scope == "institution" and s.confidence == 1.0 for s in found.values())

    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        ATLANTA_AA,
        "APPENDIX A - GLOSSARY\n\nTerms used in this evaluation.\n",
        FRB_LTD_PAGE,
    )
    assert _lone_aa(pages)
    section = locate.locate_section("ltd_ratio", "d", pages, "FRB")
    assert section is not None
    assert (section.scope, section.confidence) == ("appendix", 0.5)


def test_scope_rules_do_not_move_a_section():
    """The institution-level heading is still preferred: in a lone-AA document with an LTD
    heading before and after the AA marker, the first one is located."""
    pages = _doc(
        "PUBLIC DISCLOSURE\n",
        "Loan-to-Deposit Ratio\n\nThe bank's LTD ratio is reasonable.\n",
        DALLAS_AA,
        "Loan-to-Deposit Ratio\n\nThe AA has few lenders.\n",
    )
    section = locate.locate_section("ltd_ratio", "d", pages, "FRB")
    assert section is not None
    assert (section.page_start, section.scope, section.confidence) == (2, "institution", 1.0)


def test_scope_rules_config_is_well_formed():
    cfg = load_headings()
    rules = cfg["scope_rules"]
    sections = set(cfg["sections"])
    markers = set(cfg["scope_markers"])
    assert rules["reset_to_institution"]["sections"] == ["illegal_credit_practices_review"]
    assert rules["lone_aa_institution"]["sections"] == ["ltd_ratio", "aa_concentration"]
    assert set(rules["lone_aa_institution"]["unit_scopes"]) == {"assessment_area", "msa"}
    assert set(rules["lone_aa_institution"]["unit_scopes"]) <= markers
    for rule in rules.values():
        assert set(rule["sections"]) <= sections
        for name in rule["sections"]:
            assert cfg["sections"][name]["scope"] == "institution"
    pats = locate._patterns("FRB")
    assert pats.reset_sections == {"illegal_credit_practices_review"}
    assert pats.lone_aa_sections == {"ltd_ratio", "aa_concentration"}


def test_scope_rule_evidence_is_a_tuning_document_outside_test_and_audit():
    """Evidence cited by the scope rules is recorded as tuning documents, so its banks leave
    the test and audit pools (plan, Leakage), and no evidence document sits in test or audit."""
    from cra_extract import strata

    manifest = pd.read_csv(CORPUS_PATH, usecols=["doc_id", "split"]).set_index("doc_id")
    evidence = [
        doc_id for rule in load_headings()["scope_rules"].values() for doc_id in rule["evidence"]
    ]
    assert {"FRB_3150205_20250407", "FRB_930442_20240715"} <= set(evidence)
    tuning = strata.load_tuning_documents()
    for doc_id in evidence:
        assert manifest.loc[doc_id, "split"] not in ("test", "audit"), doc_id
        assert doc_id in tuning, doc_id
