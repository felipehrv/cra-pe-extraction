"""Tests for the template-residual census (``cra_extract.templates``, design 5.2).

The two positive samples must be flagged: SouthStar (FDIC, rating lowered for discriminatory
lending under ECOA and the FHA, L599-L602 of its ``.txt``) and Liberty (FDIC, UDAP violation,
rating not affected, L352-L361). The six negative samples must not be flagged, and every
template must match the review section it was seeded from.
"""

from __future__ import annotations

import re

import pandas as pd
import pytest
from conftest import SAMPLE_META, SAMPLES
from rapidfuzz import fuzz

from cra_extract import templates
from cra_extract.config import CORPUS_PATH
from cra_extract.locate import locate_section, pages_from_text

POSITIVES = {
    "FDIC_12299_SouthStar_2022_ISB_NI_fairlending": "lowered the cra rating",
    "FDIC_26816_LibertyBank_2022_SM_SN": "udap",
}


@pytest.fixture(scope="module")
def review_sections() -> dict[str, str]:
    out = {}
    for stem, (regulator, *_rest) in SAMPLE_META.items():
        pages = pages_from_text((SAMPLES / f"{stem}.txt").read_text(encoding="utf-8"))
        section = locate_section("illegal_credit_practices_review", stem, pages, regulator)
        assert section is not None, stem
        out[stem] = section.text
    return out


# --------------------------------------------------------------------------- library


def test_template_library_is_well_formed():
    lib = templates.load_templates()
    assert len(lib) >= 8
    ids = [t.id for t in lib]
    assert len(ids) == len(set(ids))
    for t in lib:
        assert t.regulator in ("OCC", "FDIC", "FRB")
        assert t.id.startswith(t.regulator.lower())
        assert t.source and ".txt" in t.source
        assert len(t.text) > 80
        assert not re.search("[‘’“”]", t.text), t.id  # straight quotes only
        if t.regulator == "FRB":
            assert t.district
    assert {t.regulator for t in lib} == {"OCC", "FDIC", "FRB"}
    # the sample-seeded FDIC paragraph comes first; the 2026-10-08 corpus variants follow it
    fdic = [t.id for t in templates.templates_for("FDIC")]
    assert fdic[0] == "fdic_no_evidence"
    assert len(fdic) == 17
    corpus_variants = [t for t in lib if t.source.startswith("data/text/")]
    assert len(corpus_variants) == 30
    for t in corpus_variants:
        # source names the first evidence document; evidence lists every document read
        assert t.evidence and t.note, t.id
        assert t.source.startswith(f"data/text/{t.evidence[0]}.txt p"), t.id
    for t in lib:
        if not t.source.startswith("data/text/"):
            assert not t.evidence, t.id


def test_no_template_evidence_comes_from_a_held_out_split():
    """Templates are tuned on the corpus split only (design 6.1, 6.8): no dev evidence."""
    manifest = pd.read_csv(CORPUS_PATH, usecols=["doc_id", "split"])
    split = dict(zip(manifest["doc_id"], manifest["split"], strict=True))
    for t in templates.load_templates():
        for doc_id in t.evidence:
            assert split.get(doc_id) == "corpus", (t.id, doc_id)


def test_each_template_matches_the_section_it_was_seeded_from(review_sections):
    """Sample-seeded templates only; corpus variants are checked in test_template_evidence.py."""
    for t in templates.load_templates():
        if t.source.startswith("data/text/"):
            continue
        stem = re.match(r"(\S+)\.txt", t.source).group(1)
        normalized = templates.normalize(review_sections[stem], t.regulator)
        assert fuzz.partial_ratio(t.normalized, normalized) >= templates.MATCH_THRESHOLD, t.id


# --------------------------------------------------------------------------- normalization


def test_normalize_drops_furniture_and_heading_and_straightens_quotes():
    raw = (
        "Discriminatory or Other Illegal Credit Practices Review\n"
        "The OCC has not identified that this institution’s affiliates\n"
        "                                       4\n"
        "                                  Charter Number: 11715\n"
        "Some Bank                              CRA Performance Evaluation\n"
        "Page 3 of 10\n"
        "  engaged  in “illegal”  practices.\n"
    )
    assert templates.normalize(raw, "OCC") == (
        "the occ has not identified that this institution's affiliates "
        'engaged in "illegal" practices.'
    )
    # without a regulator the heading is kept
    assert templates.normalize(raw).startswith("discriminatory or other illegal")


@pytest.mark.parametrize(
    ("heading", "source"),
    [
        ("FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW", "FRB_180845_20241104 p17"),
        ("FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES", "FRB_2785477_20250602 p36"),
        ("Fair Lending or Other Illegal Practices Review", "FRB_1864197_20230117 p9"),
    ],
)
def test_normalize_frb_review_glyphs_and_heading_variants(heading, source):
    """Chicago PEs print U+00A0 for every space and U+2010 for hyphens (FRB_180845_20241104);
    the FRB heading variants of the 2026-10-08 locator tuning are dropped like the seeded one."""
    body = "No evidence of discriminatory or other illegal credit practices was identified."
    plain = templates.normalize(f"{heading}\n{body}\n", "FRB")
    chicago = f"{heading}\n{body}\n".replace(" ", " ").replace("-", "‐")
    assert plain == body.lower(), source
    assert templates.normalize(chicago, "FRB") == plain, source


# --------------------------------------------------------------------------- residual


def test_sample_sections_flagged_only_for_positives(sample, review_sections):
    stem = sample["stem"]
    match = templates.residual(review_sections[stem], sample["regulator"])
    if stem in POSITIVES:
        assert match.flagged
        assert match.score < templates.FLAG_SCORE
        assert len(match.residual_text) > templates.FLAG_RESIDUAL_CHARS
        assert POSITIVES[stem] in match.residual_text
    else:
        assert not match.flagged, (stem, match.score, match.residual_text)
        assert match.score >= templates.MATCH_THRESHOLD
        assert len(match.residual_text) <= templates.FLAG_RESIDUAL_CHARS
        assert match.template_id is not None
        assert match.template_id.startswith(sample["regulator"].lower())
        assert match.removed


def test_residual_of_empty_section_is_flagged():
    match = templates.residual("", "OCC")
    assert match.flagged
    assert match.template_id is None
    assert match.score == 0.0
    assert match.residual_text == ""


def test_residual_keeps_novel_text_after_removing_boilerplate():
    by_id = {t.id: t for t in templates.load_templates()}
    novel = (
        "The OCC identified a pattern or practice of discrimination on the basis of race in the "
        "bank's mortgage pricing, in violation of the Fair Housing Act, and lowered the rating."
    )
    section = "\n\n".join(
        [
            "Discriminatory or Other Illegal Credit Practices Review",
            by_id["occ_pursuant_2024"].text,
            novel,
            by_id["occ_will_consider"].text,
        ]
    )
    match = templates.residual(section, "OCC")
    assert match.flagged
    assert match.score >= templates.MATCH_THRESHOLD  # flagged by residual length, not by score
    # the 2023 and 2024 "Pursuant" paragraphs are near-identical, so either may be removed
    assert "occ_will_consider" in match.removed
    assert any(t.startswith("occ_pursuant") for t in match.removed)
    assert "lowered the rating" in match.residual_text
    assert "pursuant to 12 cfr" not in match.residual_text

    short = "The bank opened one branch."
    match = templates.residual(section.replace(novel, short), "OCC")
    assert not match.flagged
    assert match.residual_text == short.lower()


def test_distractor_laws_in_negated_boilerplate_do_not_flag():
    """The Kansas City FRB paragraph names ECOA and the FHA inside 'no evidence' wording."""
    t = next(t for t in templates.load_templates() if t.id == "frb_kansas_city_compliance")
    assert t.distractor_laws_named
    assert "Equal Credit Opportunity Act" in t.text
    match = templates.residual(
        "FAIR LENDING OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n" + t.text, "FRB"
    )
    assert not match.flagged
    assert match.template_id == "frb_kansas_city_compliance"


def test_wrong_regulator_templates_do_not_match():
    fdic = next(t for t in templates.load_templates() if t.id == "fdic_no_evidence")
    assert templates.residual(fdic.text, "OCC").flagged
    assert not templates.residual(fdic.text, "FDIC").flagged


# --------------------------------------------------------------------------- census


def test_census_rows_and_locator_miss(review_sections):
    docs = [
        (
            "FDIC_6078_OneAmericanBank_2024_ISB_NI",
            "FDIC",
            review_sections["FDIC_6078_OneAmericanBank_2024_ISB_NI"],
        ),
        ("FDIC_missing", "FDIC", None),
    ]
    table = templates.census(docs)
    assert isinstance(table, pd.DataFrame)
    assert list(table["located"]) == [True, False]
    assert list(table["flagged"]) == [False, True]
    assert pd.isna(table.loc[1, "template_id"])
    assert table.loc[0, "residual_chars"] == 0


def test_cli_samples_dir(tmp_path, capsys):
    out = tmp_path / "census.csv"
    assert templates.main(["--samples-dir", str(SAMPLES), "--out", str(out)]) == 0
    table = pd.read_csv(out)
    assert len(table) == 8
    assert set(table.loc[table["flagged"], "doc_id"]) == set(POSITIVES)
    assert table["located"].all()
    assert "2 flagged" in capsys.readouterr().out


def test_cli_missing_manifest_fails(tmp_path):
    assert templates.main(["--manifest", str(tmp_path / "none.csv")]) == 1
