"""Evidence checks for the review-template library (design 5.2, corpus tuning of 2026-10-08).

Every template added from the corpus lists the doc_ids it was read from (``evidence``). These
tests check, per template, that (1) it matches the located review section of each evidence
document at ``MATCH_THRESHOLD`` or above, (2) it asserts no finding, and (3) it never removes a
finding sentence from the two positive samples (SouthStar, FDIC_12299_20220912: rating lowered
for redlining under ECOA and the FHA; Liberty, FDIC_26816_20220809: UDAP violation). The six
negative samples must stay unflagged.

The corpus text under ``data/text/`` is not committed (``.gitignore``), so the evidence test
skips a template whose evidence documents are not parsed locally; the sample checks always run.
"""

from __future__ import annotations

import pytest
from conftest import SAMPLE_META, SAMPLES
from rapidfuzz import fuzz

from cra_extract import templates
from cra_extract.config import TEXT_DIR
from cra_extract.locate import load_pages_json, locate_section, pages_from_text, running_lines

# sample stem -> (corpus doc_id, phrase of the finding that must survive in the residual)
POSITIVES = {
    "FDIC_12299_SouthStar_2022_ISB_NI_fairlending": (
        "FDIC_12299_20220912",
        "lowered the cra rating from satisfactory to needs to improve",
    ),
    "FDIC_26816_LibertyBank_2022_SM_SN": ("FDIC_26816_20220809", "udap"),
}
NEGATIVES = [stem for stem in SAMPLE_META if stem not in POSITIVES]

LIBRARY = templates.load_templates()
WITH_EVIDENCE = [t for t in LIBRARY if t.evidence]


def _sample_section(stem: str) -> tuple[str, frozenset[str]]:
    pages = pages_from_text((SAMPLES / f"{stem}.txt").read_text(encoding="utf-8"))
    section = locate_section("illegal_credit_practices_review", stem, pages, SAMPLE_META[stem][0])
    assert section is not None, stem
    return section.text, running_lines(pages)


def _corpus_section(doc_id: str, regulator: str) -> tuple[str, frozenset[str]] | None:
    path = TEXT_DIR / f"{doc_id}.pages.json"
    if not path.exists():
        return None
    pages = load_pages_json(path)
    section = locate_section("illegal_credit_practices_review", doc_id, pages, regulator)
    assert section is not None, doc_id
    return section.text, running_lines(pages)


def test_corpus_templates_cite_evidence_and_assert_nothing():
    assert WITH_EVIDENCE, "the corpus variants of 2026-10-08 carry evidence doc_ids"
    for t in LIBRARY:
        assert templates.asserting_sentences(t.text) == [], t.id
        for doc_id in t.evidence:
            assert doc_id.startswith(f"{t.regulator}_"), (t.id, doc_id)


@pytest.mark.parametrize("template", WITH_EVIDENCE, ids=lambda t: t.id)
def test_template_matches_its_evidence_sections(template):
    sections = [_corpus_section(d, template.regulator) for d in template.evidence]
    if any(s is None for s in sections):
        pytest.skip("corpus text not parsed locally (data/text is not committed)")
    for doc_id, (text, furniture) in zip(template.evidence, sections, strict=True):
        normalized = templates.normalize(text, template.regulator, furniture)
        score = fuzz.partial_ratio(template.normalized, normalized)
        assert score >= templates.MATCH_THRESHOLD, (template.id, doc_id, score)
        assert not templates.residual(text, template.regulator, furniture).flagged, doc_id


@pytest.mark.parametrize("stem", sorted(POSITIVES))
def test_no_template_removes_a_finding_from_the_positive_samples(stem):
    text, furniture = _sample_section(stem)
    normalized = templates.normalize(text, "FDIC", furniture)
    for t in templates.templates_for("FDIC"):
        aligned = fuzz.partial_ratio_alignment(t.normalized, normalized)
        if aligned is not None and aligned.score >= templates.MATCH_THRESHOLD:
            span = normalized[aligned.dest_start : aligned.dest_end]
            assert templates.asserting_sentences(span) == [], (stem, t.id)
    match = templates.residual(text, "FDIC", furniture)
    assert match.flagged and match.asserting, stem
    assert POSITIVES[stem][1] in match.residual_text, stem


@pytest.mark.parametrize("stem", NEGATIVES)
def test_negative_samples_stay_unflagged_with_the_grown_library(stem):
    text, furniture = _sample_section(stem)
    match = templates.residual(text, SAMPLE_META[stem][0], furniture)
    assert not match.flagged, (stem, match.score, match.residual_text)
    assert match.asserting == []
