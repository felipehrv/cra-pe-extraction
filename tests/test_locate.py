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
from cra_extract.config import load_headings
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
    "^INSTITUTION'?S COMMUNITY REINVESTMENT ACT RATING$": (
        "INSTITUTION'S COMMUNITY REINVESTMENT ACT RATING"
    ),
    "^Description of Institution$": "Description of Institution",
    "^DESCRIPTION OF INSTITUTION$": "DESCRIPTION OF INSTITUTION",
    "^Description of Assessment Areas?$": "Description of Assessment Areas",
    "^DESCRIPTION OF ASSESSMENT AREAS?$": "DESCRIPTION OF ASSESSMENT AREA",
    "^Scope of (the )?Evaluation$": "Scope of the Evaluation",
    "^SCOPE OF EVALUATION$": "SCOPE OF EVALUATION",
    "^SCOPE OF EXAMINATION$": "SCOPE OF EXAMINATION",
    "^Loan-to-Deposit Ratio$": "Loan-to-Deposit Ratio",
    "^(Net )?Loan-to-Deposit Ratio$": "Net Loan-to-Deposit Ratio",
    "^Lending in (the )?Assessment Areas?$": "Lending in the Assessment Area",
    "^Assessment Area Concentration$": "Assessment Area Concentration",
    "^Discriminatory or Other Illegal Credit Practices Review$": (
        "Discriminatory or Other Illegal Credit Practices Review"
    ),
    "^DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW$": (
        "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW"
    ),
    "^FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW$": (
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
