"""Tests for cra_extract.grounding on a small fake document and on two sample PEs.

The fake document mimics the layout of an FRB ISB PE: an institution rating page, a
description page with total assets, a review section, a state rating page that
repeats the component ratings (as in ``FRB_3664588_FieldPoint_2024_ISB_NI.txt``
lines 1163 to 1166) and an appendix page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cra_extract import grounding
from cra_extract.config import field_spec
from cra_extract.models import FieldValue, Page, Section

RATING_PAGE = (
    "INSTITUTION'S CRA RATING: This institution is rated Needs to Improve.\n"
    "The Lending Test is rated: Needs to Improve.\n"
    "The Community Development Test is rated: Satisfactory.\n"
)
DESCRIPTION_PAGE = (
    "DESCRIPTION OF INSTITUTION\n"
    "As of March 31, 2024, the institution reported total assets of $463 million, "
    "total loans of $327 million and total deposits of $400 million.\n"
    "The bank has designated two assessment areas. The average net loan-to-deposit "
    "ratio over the evaluation period was 81.6 percent.\n"
)
REVIEW_PAGE = (
    "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n"
    "No evidence of discriminatory or other illegal credit practices inconsistent with "
    "helping to meet the community credit needs of the assessment area was identified.\n"
)
STATE_PAGE = (
    "STATE OF CONNECTICUT\n"
    "CRA RATING FOR CONNECTICUT: OUTSTANDING.\n"
    "The Lending Test is rated: Outstanding.\n"
    "The Community Development Test is rated: Outstanding.\n"
)
APPENDIX_PAGE = "APPENDIX A\nSummary of ratings: Lending Test Outstanding, Community Development Test Outstanding.\n"


@pytest.fixture
def pages() -> list[Page]:
    return [
        Page(number=1, text="PUBLIC DISCLOSURE\nTable of Contents\nInstitution Rating .... 1", scope="toc", is_toc=True),
        Page(number=2, text=RATING_PAGE, scope="institution"),
        Page(number=3, text=DESCRIPTION_PAGE, scope="institution"),
        Page(number=4, text=REVIEW_PAGE, scope="institution"),
        Page(number=5, text=STATE_PAGE, scope="state"),
        Page(number=6, text=APPENDIX_PAGE, scope="appendix"),
    ]


def fv(field: str, value, quote: str | None, page: int | None = None, status: str = "value") -> FieldValue:
    return FieldValue(doc_id="FRB_1_20240116", field=field, system="llm", status=status, value=value, quote=quote, page=page)


# ------------------------------------------------------------- stage 1


def test_exact_quote_is_found_with_offsets(pages):
    res = grounding.check(fv("cd_test_rating", "S", "The Community Development Test is rated: Satisfactory."), pages, {}, regulator="FRB")
    assert res.supports and res.failure is None
    assert res.page == 2 and res.score == 100.0
    assert RATING_PAGE[res.char_start:res.char_end] == "The Community Development Test is rated: Satisfactory."


def test_ocr_mangled_quote_is_found(pages):
    quote = "The Comunity Developrnent Test is rated: Satisfactory"
    res = grounding.check(fv("cd_test_rating", "S", quote), pages, {}, regulator="FRB")
    assert res.found and res.page == 2
    assert 90 <= res.score < 100
    assert res.supports


def test_whitespace_and_curly_quotes_are_normalized(pages):
    quote = "The   Community\nDevelopment Test is rated:\tSatisfactory"
    res = grounding.check(fv("cd_test_rating", "S", quote), pages, {}, regulator="FRB")
    assert res.supports
    pages[2].text = pages[2].text.replace("'", "’")
    res = grounding.check(fv("total_assets_asof", "2024-03-31", "As of March 31, 2024, the institution reported total assets"), pages, {}, regulator="FRB")
    assert res.supports


def test_quote_not_found(pages):
    res = grounding.check(fv("cd_test_rating", "S", "The Investment Test is rated: High Satisfactory."), pages, {}, regulator="FRB")
    assert not res.found and res.failure == "quote_not_found" and not res.supports
    res = grounding.check(fv("cd_test_rating", "S", "   "), pages, {}, regulator="FRB")
    assert res.failure == "quote_not_found"


def test_preferred_page_is_tried_first(pages):
    quote = "The Community Development Test is rated:"
    res = grounding.locate_quote(quote, pages, preferred_page=5)
    assert res is not None and res[0] == 5
    res = grounding.locate_quote(quote, pages, preferred_page=None)
    assert res is not None and res[0] == 2


def test_nothing_to_ground_for_absent_and_abstain(pages):
    for status in ("absent", "abstain"):
        res = grounding.check(fv("cd_test_rating", None, None, status=status), pages, {}, regulator="FRB")
        assert res.supports and res.failure is None and not res.found


# ------------------------------------------------------------- stage 2: scope


def test_state_level_rating_does_not_support_institution_field(pages):
    res = grounding.check(fv("cd_test_rating", "O", "The Community Development Test is rated: Outstanding."), pages, {}, regulator="FRB")
    assert res.found and res.page == 5
    assert res.failure == "quote_out_of_scope" and not res.supports


def test_appendix_quote_is_out_of_scope(pages):
    res = grounding.check(fv("lending_test_rating", "O", "Summary of ratings: Lending Test Outstanding"), pages, {}, regulator="FRB")
    assert res.page == 6 and res.failure == "quote_out_of_scope"


def test_institution_section_rescues_quote_on_untagged_or_state_page(pages):
    # The locator may tag a whole page as state while the institution section continues on it.
    pages[4].text = STATE_PAGE + "\nTotal assets were $463 million as of March 31, 2024."
    sec = Section(name="description_of_institution", doc_id="d", page_start=3, page_end=5, char_start=0, char_end=10,
                  text=DESCRIPTION_PAGE + "Total assets were $463 million as of March 31, 2024.", scope="institution")
    quote = "Total assets were $463 million as of March 31, 2024."
    res = grounding.check(fv("total_assets_usd", 463_000_000, quote, page=5), pages, {}, regulator="FRB")
    assert res.failure == "quote_out_of_scope"
    res = grounding.check(fv("total_assets_usd", 463_000_000, quote, page=5), pages, {"description_of_institution": sec}, regulator="FRB")
    assert res.supports


def test_untagged_pages_are_not_penalized():
    pages = [Page(number=1, text=RATING_PAGE)]
    res = grounding.check(fv("overall_rating", "NI", "This institution is rated Needs to Improve."), pages, {}, regulator="FRB")
    assert res.supports


# ------------------------------------------------------- stage 2: value presence


def test_quote_not_containing_the_value(pages):
    res = grounding.check(fv("cd_test_rating", "O", "The Community Development Test is rated: Satisfactory."), pages, {}, regulator="FRB")
    assert res.found and res.failure == "quote_does_not_support"
    res = grounding.check(fv("overall_rating", "NI", "The Lending Test is rated: Needs to Improve."), pages, {}, regulator="FRB")
    assert res.supports  # the rating word is present; scope is institution


@pytest.mark.parametrize(
    "field,value,quote,ok",
    [
        ("total_assets_usd", 463_000_000, "reported total assets of $463 million", True),
        ("total_assets_usd", 465_000_000, "reported total assets of $463 million", True),   # within 1 percent
        ("total_assets_usd", 500_000_000, "reported total assets of $463 million", False),
        ("total_assets_usd", 463_000, "reported total assets of $463 million", False),       # unit error
        ("total_assets_asof", "2024-03-31", "As of March 31, 2024, the institution", True),
        ("total_assets_asof", "2024-06-30", "As of March 31, 2024, the institution", False),
        ("total_assets_asof", "2024-03-31", "as of 3/31/2024", True),
        ("avg_ltd_ratio_pct", 81.6, "loan-to-deposit ratio over the evaluation period was 81.6 percent", True),
        ("avg_ltd_ratio_pct", 81.65, "loan-to-deposit ratio over the evaluation period was 81.6 percent", True),
        ("avg_ltd_ratio_pct", 82.0, "loan-to-deposit ratio over the evaluation period was 81.6 percent", False),
        ("n_assessment_areas", 2, "The bank has designated two assessment areas", True),
        ("n_assessment_areas", 3, "The bank has designated two assessment areas", False),
        ("exam_method", "isb", "Intermediate Small Bank performance criteria", True),
        ("exam_method", "small", "Intermediate Small Bank performance criteria", False),  # ISB is not Small
        ("exam_method", "small", "evaluated under the Small Bank performance criteria", True),
        ("primary_product", "home_mortgage", "home mortgage loans are the primary product", True),
        ("primary_product", "unstated", "no product named", True),
        ("loan_products_evaluated", "home_mortgage|small_business", "home mortgage and small business loans", True),
        ("loan_products_evaluated", ["home_mortgage", "small_farm"], "home mortgage and small business loans", False),
        ("loan_products_evaluated", [], "home mortgage and small business loans", False),
        ("bank_name", "Fieldpoint Private Bank & Trust", "Fieldpoint Private Bank and Trust, Greenwich", True),
        ("bank_name", "Liberty Bank", "Fieldpoint Private Bank and Trust, Greenwich", False),
        ("finding_present", True, "Examiners identified violations of the Equal Credit Opportunity Act", True),
        ("finding_present", True, "No evidence of discriminatory or other illegal credit practices was identified", False),
        ("finding_present", False, "No evidence of discriminatory or other illegal credit practices was identified", True),
        ("finding_present", False, "Examiners identified violations of the Equal Credit Opportunity Act", False),
        # A finding sentence followed by a negated sentence still asserts the finding.
        ("finding_present", True, "Examiners identified violations of the Equal Credit Opportunity Act. No evidence of other illegal credit practices was identified.", True),
        # Numbers inside dates never stand in for an asset figure or a count.
        ("total_assets_usd", 2_030_000_000, "As of March 31, 2024, the bank", False),
        ("total_assets_usd", 31_000_000, "As of March 31, 2024, the bank", False),
        ("n_assessment_areas", 31, "As of March 31, 2024", False),
        ("total_assets_usd", 1_034_567_000, "Total Assets as of 6/30/2022 (000s) 1,034,567", True),
        # Group C word forms must not be satisfied by unrelated prose.
        ("finding_type", "both", "discriminatory lending practices were identified", False),
        ("finding_type", "both", "discriminatory and other illegal credit practices were identified", True),
        ("finding_type", "other_illegal_credit_practice", "violations of the Fair Housing Act", False),
        ("finding_type", "other_illegal_credit_practice", "violations of the Truth in Lending Act", True),
        ("rating_effect", "affected_unspecified", "did not affect the rating", False),
        ("rating_effect", "affected_unspecified", "was considered in assigning the rating", True),
        ("finding_source", "prior_exam_referenced", "prior to the evaluation", False),
        ("finding_source", "prior_exam_referenced", "cited at the prior evaluation", True),
        ("finding_source", "enforcement_action_referenced", "in order to meet credit needs", False),
        ("finding_source", "enforcement_action_referenced", "subject to a consent order", True),
        ("practice_basis", ["servicing"], "data collection", False),
        ("practice_basis", ["UDAP_product"], "loan products", False),
    ],
)
def test_value_in_quote(field, value, quote, ok):
    assert grounding.value_in_quote(value, quote, field_spec(field)) is ok


def test_parse_helpers():
    assert grounding.parse_set("[\"a\", \"b\"]") == {"a", "b"}
    assert grounding.parse_set("a; b") == {"a", "b"}
    assert grounding.parse_set(None) == set()
    assert grounding.parse_bool("TRUE") is True and grounding.parse_bool("no") is False
    assert grounding.parse_bool("maybe") is None
    assert grounding.parse_date("January 16, 2024").isoformat() == "2024-01-16"
    assert grounding.parse_date("not a date") is None
    assert 1e9 in grounding.numbers_in("$1.0 billion") and 1034567000.0 in grounding.numbers_in("$1,034,567 (000s)")


# ----------------------------------------------------------- Group C rules


SOUTHSTAR_FINDING = (
    "The FDIC lowered the CRA rating from Satisfactory to Needs to Improve due to discriminatory "
    "lending practices present during the review period. Examiners identified violations of the Equal "
    "Credit Opportunity Act and Fair Housing Act, pertaining to the bank's lending practices on the "
    "basis of redlining."
)


@pytest.fixture
def fdic_pages() -> list[Page]:
    return [
        Page(number=1, text="DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n" + SOUTHSTAR_FINDING, scope="institution"),
        Page(number=2, text=(
            "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n"
            "Examiners did not identify any evidence of discriminatory or other illegal credit practices; "
            "therefore, this consideration did not affect the institution's overall CRA rating."
        ), scope="institution"),
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("finding_present", True),
        ("finding_type", "discrimination"),
        ("rating_effect", "substantive_lowered"),
        ("rating_lowered", True),
        ("rating_lowered_level", "overall"),
        ("rating_before", "S"),
        ("rating_after", "NI"),
        ("laws_cited", "ECOA_RegB|FHA"),
        ("practice_basis", ["redlining"]),
        ("finding_source", "this_CRA_exam"),
    ],
)
def test_group_c_positive_finding_is_supported(fdic_pages, field, value):
    res = grounding.check(fv(field, value, SOUTHSTAR_FINDING), fdic_pages, {}, regulator="FDIC")
    assert res.supports, res


def test_group_c_quote_matching_boilerplate_fails(fdic_pages):
    boiler = "Examiners did not identify any evidence of discriminatory or other illegal credit practices"
    res = grounding.check(fv("finding_present", True, boiler), fdic_pages, {}, regulator="FDIC")
    assert res.found and res.failure == "quote_does_not_support"
    res = grounding.check(fv("finding_type", "discrimination", boiler), fdic_pages, {}, regulator="FDIC")
    assert res.failure == "quote_does_not_support"
    # The same boilerplate correctly supports a negative finding.
    res = grounding.check(fv("finding_present", False, boiler), fdic_pages, {}, regulator="FDIC")
    assert res.supports


def test_group_c_quote_without_predicate_fails(fdic_pages):
    fdic_pages.append(Page(number=3, text="The bank's practices were discriminatory in nature.", scope="institution"))
    res = grounding.check(fv("finding_type", "discrimination", "The bank's practices were discriminatory in nature."), fdic_pages, {}, regulator="FDIC")
    assert res.failure == "quote_does_not_support" and "predicate" in res.detail


def test_matches_template_without_templates_module(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "cra_extract.templates", None)  # import raises ImportError
    assert grounding.matches_template("No evidence of discriminatory or other illegal credit practices was identified.", "FRB")
    assert not grounding.matches_template(SOUTHSTAR_FINDING, "FDIC")
    occ = "Pursuant to 12 CFR 25.28(c), in determining a national bank's CRA rating, the OCC considers evidence of discriminatory or other illegal credit practices"
    assert grounding.matches_template(occ, "OCC")


def test_matches_template_with_real_templates_module():
    pytest.importorskip("cra_extract.templates")
    assert grounding.matches_template("Examiners did not identify any evidence of discriminatory or other illegal credit practices", "FDIC")
    assert not grounding.matches_template(SOUTHSTAR_FINDING, "FDIC")
    # A short genuine finding with no template match is not boilerplate.
    assert not grounding.matches_template("The FDIC lowered the CRA rating.", "FDIC")
    # The template followed by a short finding sentence is not boilerplate either.
    assert not grounding.matches_template(
        "Examiners did not identify any evidence of discriminatory or other illegal credit practices; "
        "therefore, this consideration did not affect the institution's overall CRA rating. "
        "The FDIC lowered the CRA rating.", "FDIC")


def test_matches_template_uses_templates_module_when_present(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("cra_extract.templates")
    mod.residual = lambda text, regulator=None: ""  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cra_extract.templates", mod)
    assert grounding.matches_template("anything at all", "FDIC")
    # A residual without a finding sentence is boilerplate whatever its length; one that
    # asserts a finding is not, however short.
    mod.residual = lambda text, regulator=None: "x" * 100  # type: ignore[attr-defined]
    assert grounding.matches_template("anything at all", "FDIC")
    mod.residual = lambda text, regulator=None: "The FDIC lowered the rating."  # type: ignore[attr-defined]
    assert not grounding.matches_template("anything at all", "FDIC")
    # A TemplateMatch-like result: boilerplate only when a template matched and little is left.
    mod.residual = lambda text, regulator=None: types.SimpleNamespace(residual_text="", score=95.0, removed=["t"])  # type: ignore[attr-defined]
    assert grounding.matches_template("anything at all", "FDIC")
    mod.residual = lambda text, regulator=None: types.SimpleNamespace(residual_text="short", score=50.0, removed=[])  # type: ignore[attr-defined]
    assert not grounding.matches_template("anything at all", "FDIC")


# ------------------------------------------------------------ real samples


SAMPLES = Path(__file__).parent / "fixtures" / "samples"


def _pages(stem: str) -> list[Page]:
    text = (SAMPLES / f"{stem}.txt").read_text(encoding="utf-8")
    return [Page(number=i + 1, text=t) for i, t in enumerate(text.split("\f"))]


def test_fieldpoint_component_rating_from_sample():
    pages = _pages("FRB_3664588_FieldPoint_2024_ISB_NI")
    # lines 99 to 100 of the .txt: institution rating page
    res = grounding.check(fv("cd_test_rating", "S", "The Community Development Test is rated: Satisfactory."), pages, {}, regulator="FRB")
    assert res.supports and res.page == 4
    res = grounding.check(fv("cd_test_rating", "O", "The Community Development Test is rated: Satisfactory."), pages, {}, regulator="FRB")
    assert res.failure == "quote_does_not_support"


def test_southstar_finding_from_sample():
    pages = _pages("FDIC_12299_SouthStar_2022_ISB_NI_fairlending")
    # lines 599 to 602 of the .txt
    res = grounding.check(fv("rating_effect", "substantive_lowered", SOUTHSTAR_FINDING), pages, {}, regulator="FDIC")
    assert res.supports and res.score >= 90
    res = grounding.check(fv("laws_cited", ["ECOA_RegB", "FHA", "TILA"], SOUTHSTAR_FINDING), pages, {}, regulator="FDIC")
    assert res.failure == "quote_does_not_support"


def test_cli(capsys):
    rc = grounding.main([
        str(SAMPLES / "FDIC_6078_OneAmericanBank_2024_ISB_NI.txt"),
        "--field", "total_assets_usd", "--value", "463000000",
        "--quote", "the institution reported total assets of $463 million", "--regulator", "FDIC",
    ])
    assert rc == 0
    assert '"supports": true' in capsys.readouterr().out


# ----------------------------------------------------------- two-digit-year dates


@pytest.mark.parametrize(
    "value,quote,ok",
    [
        # FDIC_16733_20240410 p9: the LTD table header prints the as-of date with a two-digit year.
        ("2023-12-31", "Total Assets as of 12/31/23 $(000s)", True),
        ("2022-06-30", "Total Assets as of 6/30/22 ($000s)", True),
        ("2022-06-30", "Total Assets as of 06/30/22 ($000s)", True),
        ("2022-06-30", "Total Assets as of 06/30/2022 ($000s)", True),
        # A two-digit form is not read inside a longer date, nor a short date inside another.
        ("2020-12-31", "Total Assets as of 12/31/2021", False),
        ("2023-02-28", "Total Assets as of 12/28/2023", False),
        ("2023-02-28", "Total Assets as of 12/28/23", False),
        ("2023-12-31", "Total Assets as of 12/31/24", False),
    ],
)
def test_two_digit_year_dates_in_quote(value, quote, ok):
    assert grounding.value_in_quote(value, quote, field_spec("total_assets_asof")) is ok


def test_date_forms_include_two_digit_years():
    from datetime import date

    forms = grounding.date_forms(date(2023, 6, 5))
    assert "6/5/23" in forms and "06/05/23" in forms
