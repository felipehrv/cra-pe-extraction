"""Tests for cra_extract.census on a synthetic manifest, census table and census sheet.

No network and no corpus text. The manifest has 40 unflagged corpus documents, three flagged
sections (one of them a dev document), one evaluable locator miss, one non-extractable miss,
one unflagged dev document, two unflagged test documents, one excluded row and one row
without a PDF. One test runs the context loader on the Liberty Bank sample text (a dev
document) to check the page claims of the module docstring.
"""

from __future__ import annotations

import csv
import inspect
import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from cra_extract import census as cs
from cra_extract import cli
from cra_extract.locate import pages_from_text
from cra_extract.models import DocRecord, FieldValue, Section, make_doc_id
from cra_extract.stats import clopper_pearson_ci

DISTRACTORS = {"fdic_no_evidence": False, "fdic_laws_reviewed_no_practices": True, "occ_pursuant_2024": False, "frb_new_york_compliance": False}
TEMPLATE_OF = {"FDIC": "fdic_no_evidence", "OCC": "occ_pursuant_2024", "FRB": "frb_new_york_compliance"}


def _doc(regulator: str, bank_id: str, split: str = "corpus", method: str = "small", extractable: bool | None = True, rating: str = "S") -> DocRecord:
    exam = date(2024, 3, 4)
    return DocRecord(
        doc_id=make_doc_id(regulator, bank_id, exam),
        regulator=regulator,
        bank_id=bank_id,
        bank_name=f"{regulator} Bank {bank_id}",
        exam_date=exam,
        release_date=date(2024, 6, 1),
        method=method,
        overall_rating=rating,
        url=f"https://example.invalid/{bank_id}.pdf",
        split=split,
        text_extractable=extractable,
    )


def did(regulator: str, bank_id: str) -> str:
    return make_doc_id(regulator, bank_id, date(2024, 3, 4))


POS_FDIC = did("FDIC", "5001")  # flagged, corpus, positive (lowered overall S -> NI)
NEG_OCC = did("OCC", "5002")  # flagged, corpus, negative
POS_DEV = did("FDIC", "5003")  # flagged, dev, positive (not lowered)
MISS_FRB = did("FRB", "5004")  # locator miss, hand-located, positive
MISS_SCAN = did("FRB", "5005")  # locator miss, text_extractable false: not in the census
DEV_UNFLAGGED = did("OCC", "5006")
TEST_IDS = {did("FDIC", "5007"), did("FRB", "5008")}
EXCLUDED = did("FDIC", "5009")
NO_PDF = did("FRB", "5010")


@pytest.fixture
def manifest() -> list[DocRecord]:
    docs = []
    for i in range(20):
        docs.append(_doc("FDIC", str(1000 + i), method="small" if i % 2 else "isb"))
    for i in range(10):
        docs.append(_doc("OCC", str(2000 + i)))
    for i in range(10):
        docs.append(_doc("FRB", str(3000 + i), method="isb"))
    docs += [
        _doc("FDIC", "5001", method="isb", rating="NI"),
        _doc("OCC", "5002"),
        _doc("FDIC", "5003", split="dev"),
        _doc("FRB", "5004"),
        _doc("FRB", "5005", extractable=False),
        _doc("OCC", "5006", split="dev"),
        _doc("FDIC", "5007", split="test"),
        _doc("FRB", "5008", split="test"),
        _doc("FDIC", "5009", split="excluded"),
        _doc("FRB", "5010", extractable=None),
    ]
    return docs


def _census_row(doc: DocRecord, located: bool, flagged: bool, template: str | None = None) -> dict:
    return {
        "doc_id": doc.doc_id,
        "regulator": doc.regulator,
        "located": located,
        "template_id": (template or TEMPLATE_OF[doc.regulator]) if located else None,
        "score": (100.0 if not flagged else 72.5) if located else None,
        "residual_chars": (0.0 if not flagged else 240.0) if located else None,
        "asserts_finding": flagged if located else None,
        "flagged": flagged,
        "residual_text": "examiners identified violations of ecoa" if flagged and located else "",
    }


@pytest.fixture
def census(manifest: list[DocRecord]) -> pd.DataFrame:
    rows = []
    for d in manifest:
        if d.doc_id == NO_PDF:
            continue
        if d.doc_id in (MISS_FRB, MISS_SCAN):
            rows.append(_census_row(d, located=False, flagged=True))
        elif d.doc_id in (POS_FDIC, NEG_OCC, POS_DEV):
            rows.append(_census_row(d, located=True, flagged=True))
        else:
            template = "fdic_laws_reviewed_no_practices" if d.regulator == "FDIC" and int(d.bank_id) % 2 else None
            rows.append(_census_row(d, located=True, flagged=False, template=template))
    return pd.DataFrame(rows)


def fake_loader(doc_id: str, regulator: str) -> cs.ReviewContext:
    if doc_id in (MISS_FRB, MISS_SCAN):
        return cs.ReviewContext(section=None, rating_pages="3", rating_text=f"rating page of {doc_id}", candidate_pages="3;12")
    section = Section(
        name="illegal_credit_practices_review", doc_id=doc_id, page_start=9, page_end=9,
        char_start=100, char_end=200, text=f"review section of {doc_id}",
    )
    return cs.ReviewContext(section=section, rating_pages="3", rating_text="rating", candidate_pages="9")


@pytest.fixture
def sample(census: pd.DataFrame, manifest: list[DocRecord]) -> list[str]:
    return cs.draw_random_unflagged(census, manifest, seed=7, drawn_at="2026-10-09", n=10)["doc_ids"]


@pytest.fixture
def sheet(census, manifest, sample) -> list[dict[str, str]]:
    return cs.build_sheet(census, manifest, sample, fake_loader)


NEGATIVE = {"finding_present": "false", "distractor_laws_named": "false"}
LOWERED = {
    "finding_present": "true",
    "finding_type": "discrimination",
    "rating_effect": "substantive_lowered",
    "rating_lowered": "true",
    "rating_lowered_level": "overall",
    "rating_before": "S",
    "rating_after": "NI",
    "laws_cited": "ECOA_RegB|FHA",
    "practice_basis": "redlining",
    "finding_source": "this_CRA_exam",
    "distractor_laws_named": "false",
}
NOT_LOWERED = {
    "finding_present": "true",
    "finding_type": "other_illegal_credit_practice",
    "rating_effect": "substantive_not_lowered",
    "laws_cited": "FTC_s5_UDAP",
    "practice_basis": "absent_from_document",
    "finding_source": "this_CRA_exam",
    "distractor_laws_named": "false",
}


def fill(rows: list[dict[str, str]], overrides: dict[str, dict[str, str]] | None = None) -> list[dict[str, str]]:
    """A fully read copy of the sheet: three positives, every other row negative."""
    labels = {POS_FDIC: LOWERED, POS_DEV: NOT_LOWERED, MISS_FRB: NOT_LOWERED}
    labels.update(overrides or {})
    out = []
    for r in rows:
        r = dict(r)
        r.update(labels.get(r["doc_id"], NEGATIVE))
        r.update(evidence_page="9", quote="verbatim sentence", minutes="1.5", labeler="fh", labeled_at="2026-10-10T09:00:00")
        out.append(r)
    return out


def write_sheet(rows: list[dict[str, str]], path: Path, columns: list[str] | None = None) -> Path:
    cs.write_csv(rows, columns or cs.sheet_columns(), path)
    return path


# ----------------------------------------------------------------------------- inputs


def test_normalize_census_coerces_flags_and_checks_columns(census):
    raw = census.astype({"located": str, "flagged": str})
    out = cs.normalize_census(raw)
    assert out["located"].dtype == bool and out["flagged"].dtype == bool
    assert out["flagged"].sum() == 5
    with pytest.raises(cs.CensusError, match="missing columns"):
        cs.normalize_census(census.drop(columns=["residual_text"]))
    with pytest.raises(cs.CensusError, match="duplicate"):
        cs.normalize_census(pd.concat([census, census.head(1)]))


def test_read_census_round_trip(tmp_path, census):
    path = tmp_path / "census.csv"
    census.to_csv(path, index=False)
    back = cs.read_census(path)
    assert list(back["located"]) == list(census["located"])
    assert back["template_id"].isna().sum() == 2


def test_missing_from_census_lists_evaluable_documents_without_a_row(census, manifest):
    assert cs.missing_from_census(census, manifest) == []
    renamed = census[census["doc_id"] != did("OCC", "2003")]
    assert cs.missing_from_census(renamed, manifest) == [did("OCC", "2003")]
    rec = cs.draw_random_unflagged(renamed, manifest, 1, "2026-10-09", 5)
    assert rec["evaluable_missing_from_census"] == [did("OCC", "2003")]


def test_population_drops_excluded_and_non_extractable(census, manifest):
    pop = cs.census_population(census, manifest)
    ids = set(pop["doc_id"])
    assert MISS_SCAN not in ids and EXCLUDED not in ids and NO_PDF not in ids
    assert {POS_DEV, DEV_UNFLAGGED, MISS_FRB} | TEST_IDS <= ids
    assert len(pop) == 40 + 4 + 1 + 2
    assert set(pop.columns) >= {"split", "method", "bank_name", "exam_date", "overall_rating_index"}


# ----------------------------------------------------------------------------- draw


def test_random_pool_excludes_dev_test_flagged_and_misses(census, manifest):
    pool = cs.random_pool(cs.census_population(census, manifest))
    assert len(pool) == 40
    assert not set(pool) & ({POS_FDIC, NEG_OCC, POS_DEV, MISS_FRB, DEV_UNFLAGGED, EXCLUDED} | TEST_IDS)
    assert pool == sorted(pool)


def test_draw_excludes_dev_and_test_even_when_taking_the_whole_pool(census, manifest):
    rec = cs.draw_random_unflagged(census, manifest, seed=1, drawn_at="2026-10-09", n=40)
    assert len(rec["doc_ids"]) == 40
    assert not set(rec["doc_ids"]) & (TEST_IDS | {DEV_UNFLAGGED, POS_DEV, EXCLUDED})
    with pytest.raises(cs.CensusError, match="41 wanted, 40 in the pool"):
        cs.draw_random_unflagged(census, manifest, seed=1, drawn_at="2026-10-09", n=41)


def test_draw_is_deterministic_and_recorded(census, manifest):
    a = cs.draw_random_unflagged(census, manifest, seed=20261009, drawn_at="2026-10-09", n=10)
    b = cs.draw_random_unflagged(census, manifest, seed=20261009, drawn_at="2026-10-09", n=10)
    c = cs.draw_random_unflagged(census, manifest, seed=20261010, drawn_at="2026-10-09", n=10)
    assert a == b
    assert a["doc_ids"] != c["doc_ids"]
    assert a["doc_ids"] == sorted(a["doc_ids"]) and len(set(a["doc_ids"])) == 10
    assert a["doc_id_hash"] == cs.ids_hash(a["doc_ids"])
    assert a["pool"] == 40 and a["selection_probability"] == 0.25
    assert a["test_split_drawn"] is True and a["split_counts"]["test"] == 2
    assert sum(a["by_regulator"].values()) == 10
    # Input order of the census does not matter.
    shuffled = census.sample(frac=1.0, random_state=3)
    assert cs.draw_random_unflagged(shuffled, manifest, 20261009, "2026-10-09", 10)["doc_ids"] == a["doc_ids"]
    with pytest.raises(ValueError):
        cs.draw_random_unflagged(census, manifest, 1, "10/09/2026", 10)


def test_draw_keeps_audit_documents_in_the_pool(census, manifest):
    before = cs.draw_random_unflagged(census, manifest, 5, "2026-10-09", 10)
    moved = [d.model_copy(update={"split": "audit"}) if d.doc_id == before["doc_ids"][0] else d for d in manifest]
    after = cs.draw_random_unflagged(census, moved, 5, "2026-10-09", 10)
    assert after["doc_ids"] == before["doc_ids"]


def test_save_record_refuses_to_overwrite(tmp_path, census, manifest):
    path = tmp_path / "census_sample.yaml"
    rec = cs.draw_random_unflagged(census, manifest, 7, "2026-10-09", 10)
    cs.save_record("random_unflagged", rec, path)
    assert cs.sample_ids(path) == rec["doc_ids"]
    assert path.read_text().startswith("# Written by python -m cra_extract.census")
    with pytest.raises(cs.CensusError, match="already holds"):
        cs.save_record("random_unflagged", rec, path)
    cs.save_record("random_unflagged", {**rec, "doc_ids": []}, path, force=True)
    assert cs.sample_ids(path) == []
    assert cs.sample_ids(tmp_path / "missing.yaml") == []


# ----------------------------------------------------------------------------- sheet


def test_build_sheet_strata_order_and_context(sheet, sample):
    strata = [r["stratum"] for r in sheet]
    assert strata == ["flagged"] * 3 + ["locator_miss"] + ["random_unflagged"] * 10
    assert [r["doc_id"] for r in sheet[:3]] == sorted([POS_FDIC, NEG_OCC, POS_DEV])
    assert [r["doc_id"] for r in sheet[4:]] == sorted(sample)
    flagged = sheet[0]
    assert flagged["page_start"] == "9" and flagged["section_text"].startswith("review section")
    assert flagged["template_id"] and flagged["score"] == "72.5" and flagged["residual_chars"] == "240"
    assert flagged["residual_text"]
    miss = sheet[3]
    assert miss["doc_id"] == MISS_FRB and miss["located"] == "false"
    assert miss["page_start"] == "" and miss["section_text"] == ""
    assert miss["rating_pages"] == "3" and miss["rating_text"].startswith("rating page") and miss["candidate_pages"] == "3;12"
    assert all(r["pdf_path"] == f"data/pdfs/{r['doc_id']}.pdf" for r in sheet)
    assert MISS_SCAN not in {r["doc_id"] for r in sheet}


def test_sheet_is_blind(sheet):
    columns = set(sheet[0])
    assert list(sheet[0]) == cs.sheet_columns()
    assert not columns & cs.SYSTEM_COLUMNS
    assert not [c for c in columns if c.startswith("pred_")]
    for r in sheet:
        for f in [*cs.group_c_fields(), *cs.LABEL_META_COLUMNS]:
            if f != "notes":
                assert r[f] == "", (r["doc_id"], f)
    # build_sheet takes no system output at all.
    assert set(inspect.signature(cs.build_sheet).parameters) == {
        "census", "manifest", "sample", "loader", "miss_sample", "cap"}


def test_build_sheet_refuses_above_the_reading_cap(census, manifest, sample):
    # 3 flagged located sections + 1 locator miss to hand-locate = 4 (plan step 3).
    assert len(cs.build_sheet(census, manifest, sample, fake_loader, cap=4)) == 4 + len(sample)
    with pytest.raises(cs.CensusError, match="exceed the reading cap of 3"):
        cs.build_sheet(census, manifest, sample, fake_loader, cap=3)


def test_locator_miss_sample_when_there_are_100_or_more(census, manifest, monkeypatch):
    monkeypatch.setattr(cs, "MISS_SAMPLE_THRESHOLD", 1)
    monkeypatch.setattr(cs, "MISS_SAMPLE_N", 1)
    pop = cs.census_population(census, manifest)
    assert cs.locator_misses(pop) == [MISS_FRB]
    with pytest.raises(cs.CensusError, match="run draw to record the sample"):
        cs.build_sheet(census, manifest, [], fake_loader)
    rec = cs.draw_locator_misses(census, manifest, seed=3, drawn_at="2026-10-09", n=1, threshold=1)
    assert rec["doc_ids"] == [MISS_FRB] and rec["misses"] == 1
    rows = cs.build_sheet(census, manifest, [], fake_loader, miss_sample=rec["doc_ids"])
    assert [r["doc_id"] for r in rows if r["stratum"] == "locator_miss"] == [MISS_FRB]
    assert cs.draw_locator_misses(census, manifest, seed=3, drawn_at="2026-10-09", threshold=2) is None


def test_locator_miss_rate_is_a_census_row(truth_df):
    df = cs.score(truth_df, [])
    row = _summary(df, "locator_miss_rate", "census", "", "all")
    assert (row["k"], row["n"]) == (1, len(truth_df))


def test_fp_rate_denominator_is_every_census_negative(truth_df):
    neg = truth_df[truth_df["finding_present"] == "false"]["doc_id"].tolist()
    occ_neg = truth_df[(truth_df["finding_present"] == "false") & (truth_df["regulator"] == "OCC")]["doc_id"].tolist()
    preds = [FieldValue(doc_id=d, field="finding_present", system="baseline", value=False) for d in neg]
    preds[neg.index(occ_neg[0])] = FieldValue(doc_id=occ_neg[0], field="finding_present", system="baseline", status="abstain")
    preds[neg.index(occ_neg[1])] = FieldValue(doc_id=occ_neg[1], field="finding_present", system="baseline", value=True)
    df = cs.score(truth_df, preds)
    occ = _summary(df, "fp_rate", "baseline", "finding_present", "OCC")
    assert (occ["k"], occ["n"]) == (1, len(occ_neg))  # the abstention stays in the denominator


def test_s5_rows_are_scored(truth_df, tmp_path):
    path = tmp_path / "predictions.csv"
    rows = [{"doc_id": d, "field": "finding_present", "system": s, "status": "value", "value": "false",
             "grounding_failure": "", "locator_miss": "0"}
            for d in truth_df["doc_id"][:3] for s in ("s5", "llm:gemini:x", "other")]
    pd.DataFrame(rows).to_csv(path, index=False)
    systems = {p.system for p in cs.llm_predictions(path)}
    assert systems == {"s5", "llm:gemini:x"}


def test_build_sheet_rejects_a_bad_sample(census, manifest):
    with pytest.raises(cs.CensusError, match="not unflagged"):
        cs.build_sheet(census, manifest, [POS_FDIC], fake_loader)
    with pytest.raises(cs.CensusError, match="not in the census population"):
        cs.build_sheet(census, manifest, [EXCLUDED], fake_loader)


def test_build_sheet_without_text_notes_the_gap(census, manifest):
    rows = cs.build_sheet(census, manifest, [], lambda d, r: None)
    assert len(rows) == 4
    assert rows[0]["notes"].startswith("census located the section")
    assert rows[3]["notes"] == ""


def test_load_review_context_on_the_liberty_sample(tmp_path, samples_dir):
    text = (samples_dir / "FDIC_26816_LibertyBank_2022_SM_SN.txt").read_text(encoding="utf-8")
    pages = pages_from_text(text)
    doc_id = "FDIC_26816_20220809"
    (tmp_path / f"{doc_id}.pages.json").write_text(json.dumps([p.model_dump() for p in pages]), encoding="utf-8")
    ctx = cs.load_review_context(doc_id, "FDIC", tmp_path)
    assert ctx is not None and ctx.section is not None
    assert ctx.section.page_start == 9  # LB:352-360
    assert ctx.rating_pages == "3"
    assert "Examiners identified an illegal credit practice" in " ".join(ctx.rating_text.split())  # LB:74
    assert ctx.candidate_pages.split(";") == ["3", "9"]
    assert cs.load_review_context("FDIC_1_20240101", "FDIC", tmp_path) is None


# ----------------------------------------------------------------------------- completion


def test_complete_row_fills_implied_cells_only():
    neg = cs.complete_row({"doc_id": "x", "finding_present": "false", "distractor_laws_named": "true"})
    assert neg["rating_lowered"] == "false"
    assert all(neg[f] == "not_applicable" for f in cs.FINDING_ATTRIBUTES)
    pos = cs.complete_row({"finding_present": "true", "rating_effect": "substantive_not_lowered"})
    assert pos["rating_lowered"] == "false"
    assert all(pos[f] == "not_applicable" for f in cs.DOWNGRADE_ATTRIBUTES)
    assert pos.get("finding_type", "") == ""
    low = cs.complete_row({"finding_present": "true", "rating_effect": "substantive_lowered"})
    assert low["rating_lowered"] == "true" and low.get("rating_before", "") == ""
    typed = cs.complete_row({"finding_present": "false", "finding_type": "discrimination"})
    assert typed["finding_type"] == "discrimination"


def test_label_rows_shape(sheet):
    rows = fill(sheet)
    long, src = cs.label_rows(rows, {POS_FDIC: "NI"})
    n_fields = len(cs.group_c_fields())
    assert len(long) == len(rows) * n_fields + 1
    first = [r for r in long if r["doc_id"] == rows[0]["doc_id"]]
    assert {r["field"] for r in first} >= set(cs.group_c_fields())
    minutes = {r["field"]: r["minutes"] for r in first}
    assert minutes["finding_present"] == "1.5" and minutes["finding_type"] == "0"
    context = [r for r, s in zip(long, src, strict=True) if r["field"] == "overall_rating"]
    assert context == [r for r in long if r["field"] == "overall_rating"] and context[0]["value"] == "NI"
    assert src.count(0) == 1
    unread = [dict(r, finding_present="") for r in rows]
    assert cs.label_rows(unread) == ([], [])


# ----------------------------------------------------------------------------- validate


def test_valid_sheet_has_no_problems(tmp_path, sheet, census, manifest, sample):
    path = write_sheet(fill(sheet), tmp_path / "sheet.csv")
    assert cs.validate(path, census, manifest, sample) == []
    assert cs.validate(path) == []


def test_unread_rows_are_reported(tmp_path, sheet):
    path = write_sheet(sheet, tmp_path / "sheet.csv")
    problems = cs.validate(path)
    assert len(problems) == len(sheet)
    assert {p.rule for p in problems} == {"unread"}


def _rules(problems) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for p in problems:
        out[(p.doc_id, p.rule)] = p.row
    return out


def test_group_c_rules_come_from_label_validate(tmp_path, sheet, manifest):
    overrides = {
        POS_FDIC: {**LOWERED, "rating_after": "S"},  # level overall but rating_after != index NI
        POS_DEV: {**NOT_LOWERED, "rating_lowered": "true"},  # derived rule broken
        MISS_FRB: {**NOT_LOWERED, "finding_type": ""},  # positive with an attribute missing
        NEG_OCC: {**NEGATIVE, "laws_cited": "FHA"},  # law on a negative
    }
    rows = fill(sheet, overrides)
    path = write_sheet(rows, tmp_path / "sheet.csv")
    problems = cs.validate(path, manifest=manifest)
    rules = _rules(problems)
    row_of = {r["doc_id"]: i for i, r in enumerate(rows, start=1)}
    assert rules[(POS_FDIC, "equals_overall_when_level_overall")] == row_of[POS_FDIC]
    assert rules[(POS_DEV, "derived_rating_lowered")] == row_of[POS_DEV]
    assert rules[(MISS_FRB, "empty")] == row_of[MISS_FRB]
    assert rules[(NEG_OCC, "na_iff")] == row_of[NEG_OCC]
    # Without the manifest the overall-rating rule cannot run.
    assert (POS_FDIC, "equals_overall_when_level_overall") not in _rules(cs.validate(path))


def test_section_level_rules_reported_once_per_row(tmp_path, sheet):
    rows = fill(sheet)
    rows[5]["evidence_page"] = ""
    rows[6]["minutes"] = ""
    rows[7]["finding_present"] = "maybe"
    path = write_sheet(rows, tmp_path / "sheet.csv")
    problems = cs.validate(path)
    page = [p for p in problems if p.rule == "missing_evidence_page"]
    assert len(page) == 1 and page[0].row == 6 and page[0].field == "evidence_page"
    mins = [p for p in problems if p.rule == "missing_minutes"]
    assert len(mins) == 1 and mins[0].row == 7
    assert any(p.rule == "bad_value" and p.row == 8 and p.field == "finding_present" for p in problems)


def test_census_rules(tmp_path, sheet, census, manifest, sample):
    rows = fill(sheet)
    dropped = rows.pop(0)  # a flagged row missing
    rows[0]["stratum"] = "random_unflagged"  # a flagged row in the wrong stratum
    rows.append(dict(rows[-1]))  # duplicate
    extra = dict(rows[-1], doc_id=DEV_UNFLAGGED, stratum="oops")
    rows.append(extra)
    path = write_sheet(rows, tmp_path / "sheet.csv")
    problems = cs.validate(path, census, manifest, sample)
    rules = {(p.doc_id, p.rule) for p in problems}
    assert (dropped["doc_id"], "missing_row") in rules
    assert (rows[0]["doc_id"], "wrong_stratum") in rules
    assert (rows[-2]["doc_id"], "duplicate_row") in rules
    assert (DEV_UNFLAGGED, "bad_stratum") in rules
    assert (DEV_UNFLAGGED, "unexpected_row") in rules


def test_validate_refuses_a_sheet_with_system_columns(tmp_path, sheet):
    rows = [dict(r, n_hits="2", pred_value="true") for r in fill(sheet)]
    path = write_sheet(rows, tmp_path / "sheet.csv", cs.sheet_columns() + ["n_hits", "pred_value"])
    problems = cs.validate(path)
    assert [p.rule for p in problems] == ["not_blind"]
    assert "n_hits" in problems[0].message and "pred_value" in problems[0].message


def test_validate_reports_missing_columns(tmp_path, sheet):
    path = write_sheet(sheet, tmp_path / "sheet.csv", ["doc_id", "stratum"])
    assert [p.rule for p in cs.validate(path)] == ["columns"]


# ----------------------------------------------------------------------------- truth


@pytest.fixture
def truth_df(sheet, census, manifest) -> pd.DataFrame:
    rows = fill(sheet, {sheet[-1]["doc_id"]: {**NOT_LOWERED}})  # one positive among the random sample
    return cs.truth(rows, census, manifest, DISTRACTORS)


def test_truth_sources_and_strata(truth_df, sheet):
    assert len(truth_df) == 47
    src = truth_df.set_index("doc_id")["truth_source"]
    assert (src == "hand").sum() == len(sheet)
    assert (src == "template").sum() == 47 - len(sheet)
    assert (src == "unread").sum() == 0
    t = truth_df.set_index("doc_id")
    assert t.at[DEV_UNFLAGGED, "stratum"] == "unflagged" and t.at[DEV_UNFLAGGED, "finding_present"] == "false"
    assert t.at[MISS_FRB, "stratum"] == "locator_miss" and t.at[MISS_FRB, "finding_present"] == "true"
    assert t.at[POS_DEV, "rating_lowered"] == "false" and t.at[POS_DEV, "rating_before"] == "not_applicable"
    template_rows = t[t["truth_source"] == "template"]
    assert (template_rows["laws_cited"] == "not_applicable").all()
    fdic = template_rows[template_rows["regulator"] == "FDIC"]
    odd = fdic.index.str.split("_").str[1].astype(int) % 2 == 1
    assert odd.any() and (fdic.loc[odd, "distractor_laws_named"] == "true").all()
    assert (fdic.loc[~odd, "distractor_laws_named"] == "false").all()
    assert (template_rows.loc[template_rows["regulator"] == "OCC", "distractor_laws_named"] == "false").all()


def test_truth_marks_unread_flagged_sections(sheet, census, manifest):
    rows = fill(sheet)
    rows[0]["finding_present"] = ""
    t = cs.truth(rows, census, manifest, DISTRACTORS).set_index("doc_id")
    assert t.at[rows[0]["doc_id"], "truth_source"] == "unread"
    assert t.at[rows[0]["doc_id"], "finding_present"] == ""
    # A random row left unread falls back to the template negative.
    rows[-1]["finding_present"] = ""
    t = cs.truth(rows, census, manifest, DISTRACTORS).set_index("doc_id")
    assert t.at[rows[-1]["doc_id"], "truth_source"] == "template"
    assert t.at[rows[-1]["doc_id"], "stratum"] == "random_unflagged"


def test_truth_uses_the_template_library_by_default(sheet, census, manifest):
    t = cs.truth(fill(sheet), census, manifest).set_index("doc_id")
    assert t.at[did("FDIC", "1001"), "distractor_laws_named"] == "true"
    assert t.at[did("FDIC", "1000"), "distractor_laws_named"] == "false"


def test_truth_labels(truth_df):
    labels = cs.truth_labels(truth_df)
    by_key = {(lab.doc_id, lab.field): lab for lab in labels}
    assert by_key[(POS_FDIC, "laws_cited")].value == ["ECOA_RegB", "FHA"]
    assert by_key[(POS_FDIC, "finding_present")].value is True
    assert by_key[(POS_DEV, "practice_basis")].null_reason == "absent_from_document"
    assert by_key[(DEV_UNFLAGGED, "finding_type")].null_reason == "not_applicable"
    assert by_key[(DEV_UNFLAGGED, "rating_lowered")].value is False
    assert len(labels) == 47 * len(cs.group_c_fields())


# ----------------------------------------------------------------------------- score


def _baseline(truth_df: pd.DataFrame) -> pd.DataFrame:
    """S0: hits POS_FDIC (lowered) and two negatives (one distractor FDIC, one OCC); abstains on the miss."""
    rows = []
    for r in truth_df.to_dict("records"):
        fp = r["doc_id"] in (POS_FDIC, did("FDIC", "1001"), NEG_OCC)
        located = r["doc_id"] != MISS_FRB
        rows.append(
            {
                "doc_id": r["doc_id"],
                "regulator": r["regulator"],
                "located": located,
                "finding_present": fp if located else None,
                "rating_lowered": (r["doc_id"] == POS_FDIC) if located else None,
                "n_hits": 1,
                "n_negated_hits": {"OCC": 3, "FDIC": 1, "FRB": 0}[r["regulator"]],
            }
        )
    return pd.DataFrame(rows)


def _summary(df: pd.DataFrame, metric: str, system: str, field: str = "", stratum: str = "all") -> dict:
    hit = df[(df["metric"] == metric) & (df["system"] == system) & (df["field"] == field) & (df["stratum"] == stratum)]
    assert len(hit) == 1, (metric, system, field, stratum)
    return hit.iloc[0].to_dict()


def test_baseline_predictions_abstain_on_a_miss(truth_df):
    preds = cs.baseline_predictions(_baseline(truth_df))
    miss = [p for p in preds if p.doc_id == MISS_FRB]
    assert {p.status for p in miss} == {"abstain"} and len(miss) == 2
    pos = [p for p in preds if p.doc_id == POS_FDIC and p.field == "rating_lowered"]
    assert pos[0].value is True and pos[0].system == "baseline"


def test_census_counts(truth_df):
    df = cs.score(truth_df, [])
    prev = _summary(df, "prevalence", "census", "finding_present")
    assert (prev["k"], prev["n"]) == (4, 47)
    lo, hi = clopper_pearson_ci(4, 47)
    assert prev["ci_low"] == pytest.approx(lo) and prev["ci_high"] == pytest.approx(hi)
    assert _summary(df, "prevalence", "census", "rating_lowered")["k"] == 1
    assert _summary(df, "prevalence", "census", "rating_lowered", "FDIC/isb")["k"] == 1
    assert _summary(df, "prevalence", "census", "finding_present", "FRB")["k"] == 1
    miss = _summary(df, "miss_bound", "census", "finding_present", "random_unflagged")
    assert (miss["k"], miss["n"]) == (1, 10)
    assert miss["ci_high"] == pytest.approx(clopper_pearson_ci(1, 10)[1])
    laws = df[(df["metric"] == "attribute_count") & (df["field"] == "laws_cited")].set_index("stratum")["k"]
    assert laws.to_dict() == {"ECOA_RegB": 1, "FHA": 1, "FTC_s5_UDAP": 3}
    basis = df[(df["metric"] == "attribute_count") & (df["field"] == "practice_basis")].set_index("stratum")["k"]
    assert basis.to_dict() == {"absent_from_document": 3, "redlining": 1}
    types = df[(df["metric"] == "attribute_count") & (df["field"] == "finding_type")].set_index("stratum")["k"]
    assert types.to_dict() == {"discrimination": 1, "other_illegal_credit_practice": 3}
    docs = _summary(df, "documents", "census", "", "truth_source=hand")
    assert docs["n"] == 47 and math.isnan(docs["estimate"])
    assert _summary(df, "documents_read", "census", "", "random_unflagged")["k"] == 10


def test_s0_precision_recall_and_fp_rate(truth_df):
    base = _baseline(truth_df)
    preds = cs.baseline_predictions(base)
    hits = cs.negated_hits_by_regulator(base, truth_df["doc_id"])
    df = cs.score(truth_df, preds, hits)
    p = _summary(df, "precision", "baseline", "finding_present")
    r = _summary(df, "recall", "baseline", "finding_present")
    assert (p["k"], p["n"]) == (1, 3)
    assert (r["k"], r["n"]) == (1, 3)  # the miss abstains; POS_DEV and the random positive are FN
    assert _summary(df, "abstention", "baseline", "finding_present")["k"] == 1
    rl = _summary(df, "precision", "baseline", "rating_lowered")
    assert (rl["k"], rl["n"]) == (1, 1)
    occ = _summary(df, "fp_rate", "baseline", "finding_present", "OCC")
    n_occ_neg = int(((truth_df["regulator"] == "OCC") & (truth_df["finding_present"] == "false")).sum())
    assert (occ["k"], occ["n"]) == (1, n_occ_neg)
    assert occ["note"].endswith(f"n_negated_hits={hits['OCC']}")
    assert _summary(df, "fp_rate", "baseline", "finding_present", "all")["k"] == 2
    assert _summary(df, "n_negated_hits", "baseline", "", "OCC")["k"] == hits["OCC"]
    dis = _summary(df, "fp_by_distractor", "baseline", "finding_present", "distractor_laws_named=true")
    assert dis["k"] == 1
    assert _summary(df, "fp_by_distractor", "baseline", "finding_present", "distractor_laws_named=false")["k"] == 1


def _llm_preds(system: str, docs: list[str], truth_df: pd.DataFrame) -> list[FieldValue]:
    """An LLM that copies census truth except: wrong rating_before on POS_FDIC, an extra law."""
    t = truth_df.set_index("doc_id")
    out = []
    for d in docs:
        for f in cs.group_c_fields():
            cell = t.at[d, f]
            if cell in cs.GOLD_NULLS:
                out.append(FieldValue(doc_id=d, field=f, system=system, status="absent"))
                continue
            value = cell
            if d == POS_FDIC and f == "rating_before":
                value = "O"
            if d == POS_FDIC and f == "laws_cited":
                value = "ECOA_RegB|FHA|TILA"
            out.append(FieldValue(doc_id=d, field=f, system=system, value=value, quote="q", page=9))
    return out


def test_llm_systems_are_scored_on_their_documents(truth_df):
    all_docs = list(truth_df["doc_id"])
    s3 = _llm_preds("llm:gemini:gemini-3.8-flash", all_docs, truth_df)
    subset = [POS_FDIC, POS_DEV, NEG_OCC, DEV_UNFLAGGED]
    s4 = _llm_preds("llm:ollama:qwen2.5:7b-instruct-q4_K_M", subset, truth_df)
    df = cs.score(truth_df, s3 + s4)
    s3n = "llm:gemini:gemini-3.8-flash"
    assert _summary(df, "precision", s3n, "finding_present")["estimate"] == 1.0
    assert _summary(df, "recall", s3n, "finding_present")["k"] == 4
    acc = _summary(df, "accuracy_among_positives", s3n, "rating_before", "census_positives")
    assert (acc["k"], acc["n"]) == (0, 1)
    assert _summary(df, "accuracy_among_positives", s3n, "rating_effect", "census_positives")["k"] == 4
    ep = _summary(df, "element_precision", s3n, "laws_cited", "census_positives")
    er = _summary(df, "element_recall", s3n, "laws_cited", "census_positives")
    assert (ep["k"], ep["n"]) == (5, 6) and (er["k"], er["n"]) == (5, 5)
    s4n = "llm:ollama:qwen2.5:7b-instruct-q4_K_M"
    docs = _summary(df, "documents", s4n)
    assert (docs["k"], docs["n"]) == (4, 47)
    assert _summary(df, "recall", s4n, "finding_present")["n"] == 2
    assert df[(df["system"] == s3n) & (df["metric"] == "fp_rate")].empty  # S0 only (plan)
    assert list(df.columns) == cs.SUMMARY_COLUMNS


def test_llm_predictions_reads_only_llm_group_c_rows(tmp_path):
    path = tmp_path / "predictions_corpus.csv"
    rows = [
        {"doc_id": POS_FDIC, "field": "finding_present", "system": "llm:gemini:m", "status": "value", "value": "true", "grounding_failure": "", "locator_miss": "0"},
        {"doc_id": POS_FDIC, "field": "finding_present", "system": "baseline", "status": "value", "value": "true", "grounding_failure": "", "locator_miss": "0"},
        {"doc_id": POS_FDIC, "field": "total_assets_usd", "system": "llm:gemini:m", "status": "value", "value": "5", "grounding_failure": "", "locator_miss": "0"},
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    preds = cs.llm_predictions(path)
    assert [(p.system, p.field) for p in preds] == [("llm:gemini:m", "finding_present")]


def test_positives_registry(truth_df):
    reg = cs.positives_registry(truth_df)
    assert list(reg.columns) == cs.POSITIVES_COLUMNS
    assert len(reg) == 4 and list(reg["doc_id"]) == sorted(reg["doc_id"])
    row = reg.set_index("doc_id").loc[POS_FDIC]
    assert (row["rating_before"], row["rating_after"], row["laws_cited"]) == ("S", "NI", "ECOA_RegB|FHA")
    assert row["bank_name"] == "FDIC Bank 5001" and row["exam_date"] == "2024-03-04"


# ----------------------------------------------------------------------------- reliability


def test_allocate_is_proportional_and_capped():
    assert cs.allocate({"FDIC": 6, "OCC": 2, "FRB": 2}, 20, {"FDIC": 50, "OCC": 50, "FRB": 50}) == {"FDIC": 12, "FRB": 4, "OCC": 4}
    assert cs.allocate({"FDIC": 1, "OCC": 1, "FRB": 1}, 20, {"FDIC": 50, "OCC": 50, "FRB": 50}) == {"FDIC": 7, "FRB": 7, "OCC": 6}
    assert cs.allocate({"FDIC": 3, "FRB": 1}, 20, {"FDIC": 50, "FRB": 2}) == {"FDIC": 18, "FRB": 2}
    assert cs.allocate({"FDIC": 3, "OCC": 0}, 4, {"FDIC": 2, "OCC": 9}) == {"FDIC": 2}
    with pytest.raises(cs.CensusError):
        cs.allocate({"FDIC": 0}, 20, {"FDIC": 10})


def test_draw_double_read(truth_df):
    rec = cs.draw_double_read(truth_df, seed=11, drawn_at="2026-10-13", n_negatives=5)
    t = truth_df.set_index("doc_id")
    assert rec["positives"] == sorted(t.index[t["finding_present"] == "true"])
    assert rec["n_negatives"] == 5
    for d in rec["negatives"]:
        assert t.at[d, "stratum"] == "random_unflagged" and t.at[d, "finding_present"] == "false"
    weights = rec["positives_by_regulator"]
    assert sum(weights.values()) == 4 and weights["FDIC"] >= 2 and weights["FRB"] >= 1
    alloc = rec["allocation"]
    assert sum(alloc.values()) == 5
    assert alloc["OCC"] == 0 or weights["OCC"] > 0
    by_reg = pd.Series([t.at[d, "regulator"] for d in rec["negatives"]]).value_counts().to_dict()
    assert all(by_reg.get(r, 0) == alloc[r] for r in alloc)
    assert rec == cs.draw_double_read(truth_df, seed=11, drawn_at="2026-10-13", n_negatives=5)
    assert rec["doc_id_hash"] == cs.ids_hash(rec["doc_ids"])
    assert rec["fixture_sample_positives"] == []


def test_build_double_sheet_is_blind(truth_df, sheet):
    first = fill(sheet, {sheet[-1]["doc_id"]: {**NOT_LOWERED}})
    rec = cs.draw_double_read(truth_df, seed=11, drawn_at="2026-10-13", n_negatives=5)
    rows = cs.build_double_sheet(rec, first)
    assert [r["doc_id"] for r in rows] == sorted(rec["doc_ids"])
    assert list(rows[0]) == cs.double_sheet_columns()
    forbidden = {"stratum", "template_id", "score", "residual_text", "residual_chars", "located", "split"}
    assert not set(rows[0]) & (forbidden | cs.SYSTEM_COLUMNS)
    for r in rows:
        assert all(r[f] == "" for f in [*cs.group_c_fields(), *cs.LABEL_META_COLUMNS])
    with pytest.raises(cs.CensusError, match="not on the first sheet"):
        cs.build_double_sheet({"doc_ids": ["FDIC_9_20240101"]}, first)


def test_cohen_kappa_known_values():
    assert cs.cohen_kappa([1, 1, 0, 0], [1, 1, 0, 0]) == pytest.approx(1.0)
    # 20 items: 10 yes/yes, 5 no/no, 2 yes/no, 3 no/yes -> po 0.75, pe 0.6*0.65+0.4*0.35 = 0.53
    a = ["y"] * 10 + ["n"] * 5 + ["y"] * 2 + ["n"] * 3
    b = ["y"] * 10 + ["n"] * 5 + ["n"] * 2 + ["y"] * 3
    assert cs.cohen_kappa(a, b) == pytest.approx((0.75 - 0.53) / (1 - 0.53))
    assert math.isnan(cs.cohen_kappa(["a", "a"], ["a", "a"]))
    assert math.isnan(cs.cohen_kappa([], []))
    with pytest.raises(ValueError):
        cs.cohen_kappa([1], [1, 2])


def test_reliability_per_field(sheet):
    first = fill(sheet)
    second = [dict(r) for r in first]
    second[0] = dict(second[0], **NEGATIVE, finding_type="", rating_effect="", rating_lowered="", rating_lowered_level="", rating_before="", rating_after="", laws_cited="", practice_basis="", finding_source="", minutes="3")
    table = cs.reliability(first, second).set_index("field")
    fp = table.loc["finding_present"]
    assert fp["n_items"] == len(first) and fp["n_agree"] == len(first) - 1
    assert fp["statistic"] == "kappa" and fp["kappa"] < 1
    dist = table.loc["distractor_laws_named"]
    assert dist["statistic"] == "percent_agreement" and dist["percent_agreement"] == 1.0
    assert dist["majority_class"] == "false" and dist["majority_prevalence"] == 1.0
    laws = table.loc["laws_cited"]
    assert laws["n_items"] == len(first) * 10  # one item per section and allowed element
    assert laws["n_agree"] == laws["n_items"] - 2  # ECOA_RegB and FHA dropped on one section
    mins = table.loc["minutes_per_section"]
    assert mins["minutes_first_mean"] == pytest.approx(1.5)
    assert mins["minutes_second_mean"] == pytest.approx((3 + 1.5 * (len(first) - 1)) / len(first))


# ----------------------------------------------------------------------------- CLI


def _write_manifest(manifest: list[DocRecord], path: Path) -> None:
    cols = list(DocRecord.model_fields)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for d in manifest:
            w.writerow({c: cs._cell(getattr(d, c)) for c in cols})


def test_cli_end_to_end(tmp_path, census, manifest, truth_df):
    mpath, cpath, spath = tmp_path / "corpus.csv", tmp_path / "census.csv", tmp_path / "census_sample.yaml"
    _write_manifest(manifest, mpath)
    census.to_csv(cpath, index=False)
    common = ["--manifest", str(mpath), "--census", str(cpath), "--sample-file", str(spath)]
    assert cs.main([*common, "draw", "--seed", "3", "--date", "2026-10-09", "--n", "10"]) == 0
    rec = yaml.safe_load(spath.read_text())["random_unflagged"]
    assert rec["census_sha256"] and len(rec["doc_ids"]) == 10
    assert cs.main([*common, "draw", "--seed", "4", "--date", "2026-10-09", "--n", "10"]) == 1

    sheet_path = tmp_path / "census_sheet.csv"
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    assert cs.main([*common, "sheet", "--out", str(sheet_path), "--text-dir", str(text_dir)]) == 0
    assert cs.main([*common, "sheet", "--out", str(sheet_path), "--text-dir", str(text_dir)]) == 1
    assert cs.main([*common, "validate", str(sheet_path)]) == 1

    rows = fill(cs.read_sheet(sheet_path))
    write_sheet(rows, sheet_path)
    assert cs.main([*common, "validate", str(sheet_path)]) == 0

    base = tmp_path / "baseline.csv"
    _baseline(cs.truth(rows, census, manifest, DISTRACTORS)).to_csv(base, index=False)
    out, pos = tmp_path / "census_summary.csv", tmp_path / "census_positives.csv"
    args = [*common, "score", "--sheet", str(sheet_path), "--baseline", str(base), "--predictions", str(tmp_path / "none.csv"), "--out", str(out), "--positives-out", str(pos)]
    assert cs.main(args) == 0
    summary = pd.read_csv(out)
    assert set(summary["system"]) == {"census", "baseline"}
    assert len(pd.read_csv(pos)) == 3

    double = tmp_path / "census_double.csv"
    dargs = [*common, "double", "--sheet", str(sheet_path), "--out", str(double)]
    assert cs.main(dargs) == 2
    assert cs.main([*dargs, "--seed", "5", "--date", "2026-10-13", "--n-negatives", "4"]) == 0
    assert yaml.safe_load(spath.read_text())["double_read"]["n_negatives"] == 4
    second = cs.read_sheet(double)
    by_id = {r["doc_id"]: r for r in rows}
    filled = [dict(r, **{k: by_id[r["doc_id"]][k] for k in [*cs.group_c_fields(), *cs.LABEL_META_COLUMNS]}) for r in second]
    write_sheet(filled, double, cs.double_sheet_columns())
    rel = tmp_path / "rel.csv"
    assert cs.main([*dargs, "--compare", str(double), "--reliability-out", str(rel)]) == 0
    table = pd.read_csv(rel).set_index("field")
    assert table.loc["finding_present", "percent_agreement"] == 1.0

    rows[0]["finding_present"] = ""
    write_sheet(rows, sheet_path)
    assert cs.main(args) == 1
    assert cs.main([*args, "--allow-problems"]) == 0


def test_cli_stage_is_registered():
    assert cli.STAGES["census"][0] == "cra_extract.census"
