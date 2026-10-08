"""Tests for the keyword baseline (src/cra_extract/baseline.py).

On the eight samples the baseline must find the two positives (SouthStar, Liberty) and none
of the six negatives, and say that only SouthStar's rating was lowered. The OCC boilerplate
test feeds the three paragraphs every OCC review section prints and checks that the
negation rule, not luck, keeps them negative.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import SAMPLE_META, SAMPLES

from cra_extract.baseline import census, detect, scan
from cra_extract.locate import doc_text, locate_section, pages_from_text
from cra_extract.models import Section

EXPECTED_PATH = Path(__file__).parent / "fixtures" / "expected_samples.yaml"

OCC_REVIEW = """Discriminatory or Other Illegal Credit Practices Review
Pursuant to 12 CFR 25.28(c), in determining a national bank’s or federal savings association’s CRA
rating, the OCC considers evidence of discriminatory or other illegal credit practices in any geography
by the institution, or in any AA by an affiliate whose loans have been considered as part of the
institution’s lending performance. As part of this evaluation process, the OCC consults with other
federal agencies with responsibility for compliance with the relevant laws and regulations, including the
U.S. Department of Justice, the U.S. Department of Housing and Urban Development, and the
Consumer Financial Protection Bureau, as applicable.

The OCC has not identified that this bank (or any affiliate whose loans have been considered as part of
the bank’s lending performance) has engaged in discriminatory or other illegal credit practices that
require consideration in this evaluation.

The OCC will consider any information that this institution engaged in discriminatory or other illegal
credit practices, identified by or provided to the OCC before the end of the institution’s next
                                                3
                                                                                     Charter Number: 703395

performance evaluation in that subsequent evaluation, even if the information concerns activities that
occurred during the evaluation period addressed in this performance evaluation.
"""

FRB_REVIEW = """FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW

Compliance with the substantive provisions of antidiscrimination and other consumer
protection laws and regulations, including the Equal Credit Opportunity Act and the Fair
Housing Act, was considered as part of this CRA evaluation. No evidence of a pattern or
practice of discrimination on a prohibited basis or of other illegal credit practices
inconsistent with helping to meet community credit needs was identified.
"""

FDIC_NEGATIVE = """DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW

Examiners did not identify any evidence of discriminatory or other illegal credit practices; therefore,
this consideration did not affect the institution’s overall CRA rating.
"""


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module")
def expected() -> dict:
    with open(EXPECTED_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)["samples"]


@pytest.fixture(scope="module")
def verdicts() -> dict[str, dict]:
    out = {}
    for stem, (regulator, *_rest) in SAMPLE_META.items():
        pages = pages_from_text((SAMPLES / f"{stem}.txt").read_text(encoding="utf-8"))
        review = locate_section("illegal_credit_practices_review", stem, pages, regulator)
        assert review is not None, f"locator miss on {stem}"
        values = {fv.field: fv for fv in detect(stem, review, pages, regulator)}
        out[stem] = {"pages": pages, "values": values}
    return out


# --------------------------------------------------------------------------- sample cases


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
def test_finding_present_matches_census(stem, expected, verdicts):
    fv = verdicts[stem]["values"]["finding_present"]
    assert fv.status == "value" and fv.system == "baseline"
    assert fv.value is expected[stem]["finding_present"]["value"], fv.notes


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
def test_rating_lowered_matches_census(stem, expected, verdicts):
    fv = verdicts[stem]["values"]["rating_lowered"]
    assert fv.status == "value"
    assert fv.value is expected[stem]["rating_lowered"]["value"], fv.notes


def test_two_positives_six_negatives(verdicts):
    positives = sorted(s for s, v in verdicts.items() if v["values"]["finding_present"].value)
    assert positives == ["FDIC_12299_SouthStar_2022_ISB_NI_fairlending", "FDIC_26816_LibertyBank_2022_SM_SN"]
    lowered = [s for s, v in verdicts.items() if v["values"]["rating_lowered"].value]
    assert lowered == ["FDIC_12299_SouthStar_2022_ISB_NI_fairlending"]


@pytest.mark.parametrize("stem", sorted(SAMPLE_META))
def test_quotes_are_verbatim_at_offsets(stem, verdicts):
    text = doc_text(verdicts[stem]["pages"])
    for fv in verdicts[stem]["values"].values():
        if fv.value:
            assert fv.quote and fv.char_start is not None
            assert _ws(text[fv.char_start : fv.char_end]) == _ws(fv.quote)
            assert fv.scope == "institution"
        else:
            assert fv.quote is None


def test_positive_quotes_are_the_triggering_sentences(verdicts):
    ss = verdicts["FDIC_12299_SouthStar_2022_ISB_NI_fairlending"]["values"]
    assert ss["finding_present"].quote.startswith("The FDIC lowered the CRA rating from Satisfactory to Needs to Improve")
    assert ss["rating_lowered"].quote == ss["finding_present"].quote
    lb = verdicts["FDIC_26816_LibertyBank_2022_SM_SN"]["values"]
    assert lb["finding_present"].quote.startswith("The FDIC identified illegal credit practices during the evaluation period")


# --------------------------------------------------------------------------- boilerplate


def test_occ_boilerplate_is_negated_not_absent():
    """Every OCC PE prints the phrase three times; only the 'has not identified' one is a hit."""
    hits = scan(OCC_REVIEW)
    assert [h.trigger for h in hits] == ["identified_practice"]
    assert hits[0].negated and hits[0].negator.lower() == "has not"
    assert hits[0].sentence.norm.startswith("The OCC has not identified")
    pages = pages_from_text(OCC_REVIEW + "\f")
    review = Section(
        name="illegal_credit_practices_review", doc_id="OCC_1_20240101", page_start=1, page_end=1,
        char_start=0, char_end=len(OCC_REVIEW), text=OCC_REVIEW,
    )
    by = {fv.field: fv for fv in detect("OCC_1_20240101", review, pages, "OCC")}
    assert by["finding_present"].value is False
    assert by["rating_lowered"].value is False
    assert "negated=1" in by["finding_present"].notes


def test_frb_no_evidence_was_identified_is_negated():
    hits = scan(FRB_REVIEW)
    assert len(hits) == 1
    assert hits[0].trigger == "practice_identified"
    assert hits[0].negated and hits[0].negator.lower() == "no evidence"


def test_fdic_did_not_identify_has_no_trigger():
    assert scan(FDIC_NEGATIVE) == []


@pytest.mark.parametrize(
    "text,trigger",
    [
        ("The FDIC lowered the CRA rating from Satisfactory to Needs to Improve due to discriminatory lending practices.", "lowered"),
        ("Examiners identified violations of the Equal Credit Opportunity Act and Fair Housing Act.", "violation_of"),
        ("The FDIC identified illegal credit practices during the evaluation period.", "identified_practice"),
        ("Examiners cited a substantive violation of the Fair Housing Act.", "substantive_violation"),
        ("Discriminatory practices on the basis of redlining were identified by examiners.", "practice_identified"),
    ],
)
def test_positive_triggers(text, trigger):
    hits = scan(text)
    assert trigger in [h.trigger for h in hits]
    assert not any(h.negated for h in hits)


@pytest.mark.parametrize(
    "text",
    [
        "The rating was not lowered as a result of the review.",
        "Examiners did not identify violations of the Fair Housing Act.",
        "No evidence of discriminatory or other illegal credit practices was identified.",
        "Management has not identified any illegal credit practices.",
    ],
)
def test_negated_triggers(text):
    hits = scan(text)
    assert hits and all(h.negated for h in hits)


def test_sentence_wide_negation_false_negative_class():
    """Documented weakness: a negated clause silences a finding in the same sentence."""
    hits = scan("The bank's rating was not lowered as a result of these violations of the Fair Housing Act.")
    assert hits and all(h.negated for h in hits)


def test_negation_only_looks_backwards_within_the_sentence():
    """A negator in an earlier sentence does not rescue a later positive sentence."""
    text = "Examiners did not identify any complaints. The FDIC lowered the CRA rating to Needs to Improve."
    hits = scan(text)
    assert [(h.trigger, h.negated) for h in hits] == [("lowered", False)]


def test_detect_abstains_without_review_section():
    values = detect("X_1_20240101", None, [], "FDIC")
    assert [fv.status for fv in values] == ["abstain", "abstain"]


def test_census_reports_negated_hits_per_document():
    docs = []
    for stem, (regulator, *_rest) in SAMPLE_META.items():
        docs.append((stem, regulator, pages_from_text((SAMPLES / f"{stem}.txt").read_text(encoding="utf-8"))))
    table = census(docs).set_index("doc_id")
    assert table["located"].all()
    occ = table[table["regulator"] == "OCC"]
    assert (occ["n_hits"] == 1).all() and (occ["n_negated_hits"] == 1).all()
    assert not occ["finding_present"].any()
    assert table.loc["FDIC_12299_SouthStar_2022_ISB_NI_fairlending", "finding_present"]
    assert table.loc["FDIC_6078_OneAmericanBank_2024_ISB_NI", "n_hits"] == 0
