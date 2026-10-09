"""Tests for cra_extract.reliability (Group B reliability check) on a synthetic manifest."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from cra_extract import label, reliability, report
from cra_extract.models import DocRecord, make_doc_id
from cra_extract.strata import SAMPLE_DOC_IDS


def _doc(regulator: str, bank_id: str, split: str, exam: date = date(2024, 3, 4)) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(regulator, bank_id, exam), regulator=regulator, bank_id=bank_id,
        bank_name=f"Bank {bank_id}", exam_date=exam, release_date=date(2024, 6, 1), method="isb",
        overall_rating="S", url="https://example.invalid/x.pdf", split=split, text_extractable=True,
    )


@pytest.fixture
def manifest() -> list[DocRecord]:
    docs = [_doc("FDIC", str(100 + i), "test") for i in range(12)]
    docs += [_doc("OCC", str(200 + i), "dev") for i in range(10)]
    docs += [_doc("FRB", str(300 + i), "corpus") for i in range(5)]
    # A fixture sample in test: never drawn by the outside branch.
    reg, bank, exam = SAMPLE_DOC_IDS[0].split("_")
    docs.append(_doc(reg, bank, "test", date(int(exam[:4]), int(exam[4:6]), int(exam[6:]))))
    return docs


def test_draw_pools_and_reproducibility(manifest):
    rec = reliability.draw(manifest, "outside", seed=11, drawn_at="2026-10-20")
    assert rec["n"] == 8 and rec["pool"] == 12 and rec["split"] == "test"
    assert SAMPLE_DOC_IDS[0] not in reliability.pool(manifest, "outside")
    assert rec == reliability.draw(manifest, "outside", seed=11, drawn_at="2026-10-20")
    assert reliability.draw(manifest, "outside", seed=12, drawn_at="2026-10-20")["doc_ids"] != rec["doc_ids"]
    fb = reliability.draw(manifest, "fallback", seed=3, drawn_at="2026-10-13")
    assert fb["n"] == 6 and all(d.startswith("OCC_") for d in fb["doc_ids"])
    with pytest.raises(reliability.ReliabilityError, match="unknown branch"):
        reliability.draw(manifest, "other", seed=1, drawn_at="2026-10-13")
    with pytest.raises(reliability.ReliabilityError, match="in the pool"):
        reliability.draw(manifest, "outside", seed=1, drawn_at="2026-10-13", n=20)


def test_sheet_is_blind_and_limited_to_the_fields(manifest, tmp_path):
    rec = reliability.draw(manifest, "outside", seed=11, drawn_at="2026-10-20")
    rows = reliability.build_sheet(manifest, rec)
    assert {r["field"] for r in rows} == set(reliability.RELIABILITY_FIELDS)
    assert {r["doc_id"] for r in rows} == set(rec["doc_ids"])
    assert all(r["value"] == "" for r in rows)
    path = tmp_path / "rec.yaml"
    reliability.save_record(rec, path)
    with pytest.raises(reliability.ReliabilityError, match="already holds"):
        reliability.save_record(rec, path)
    assert reliability.load_record(path)["doc_id_hash"] == rec["doc_id_hash"]


def _row(doc_id: str, field: str, value: str = "", null: str = "", minutes: str = "2") -> dict[str, str]:
    return {"doc_id": doc_id, "field": field, "value": value, "null_reason": null, "minutes": minutes}


def test_compare_canonical_values_kappa_and_sets():
    docs = [f"D{i}" for i in range(4)]
    first = [
        _row("D0", "n_assessment_areas", "1"), _row("D1", "n_assessment_areas", "2"),
        _row("D2", "n_assessment_areas", "3"), _row("D3", "n_assessment_areas", "2"),
        _row("D0", "lending_data_period_end", "2023-12-31"), _row("D1", "lending_data_period_end", "2023-12-31"),
        _row("D0", "n_full_scope", null="not_applicable"), _row("D1", "n_full_scope", "2"),
        _row("D0", "loan_products_evaluated", "home_mortgage|small_business"),
        _row("D1", "loan_products_evaluated", "home_mortgage"),
    ]
    second = [
        _row("D0", "n_assessment_areas", "1.0", minutes="4"), _row("D1", "n_assessment_areas", "2"),
        _row("D2", "n_assessment_areas", "2"), _row("D3", "n_assessment_areas", "2"),
        _row("D0", "lending_data_period_end", "December 31, 2023"), _row("D1", "lending_data_period_end", "2023-12-31"),
        _row("D0", "n_full_scope", null="not_applicable"), _row("D1", "n_full_scope", "1"),
        _row("D0", "loan_products_evaluated", "small_business|home_mortgage"),
        _row("D1", "loan_products_evaluated", "home_mortgage|small_farm"),
    ]
    t = reliability.compare(first, second, "outside").set_index("field")
    aa = t.loc["n_assessment_areas"]
    assert (aa["n"], aa["n_agree"]) == (4, 3) and aa["statistic"] == "kappa"
    assert aa["kappa"] == pytest.approx(reliability.cohen_kappa(["1", "2", "3", "2"], ["1", "2", "2", "2"]))
    assert aa["minutes_first_mean"] == 2.0 and aa["minutes_second_mean"] == 2.5
    end = t.loc["lending_data_period_end"]
    assert (end["n"], end["n_agree"]) == (2, 2) and end["statistic"] == "percent_agreement"
    assert end["majority_prevalence"] == 1.0 and math.isnan(end["kappa"])
    fs = t.loc["n_full_scope"]
    assert (fs["n"], fs["n_agree"]) == (2, 1)
    lp = t.loc["loan_products_evaluated"]
    assert (lp["n"], lp["n_agree"]) == (2, 1)
    assert (lp["n_elements"], lp["n_elements_agree"]) == (10, 9)
    assert reliability.manual_review_error(t.reset_index()) == pytest.approx(1 - 7 / 10)
    assert len(docs) == 4


def test_cli_round_trip_feeds_the_report(manifest, tmp_path, capsys):
    mpath = tmp_path / "corpus.csv"
    cols = list(DocRecord.model_fields)
    pd.DataFrame([{c: ("" if v is None else v) for c, v in d.model_dump().items()} for d in manifest])[cols].to_csv(mpath, index=False)
    sample = tmp_path / "reliability_sample.yaml"
    base = ["--manifest", str(mpath), "--sample-file", str(sample)]
    assert reliability.main([*base, "draw", "--branch", "outside", "--seed", "5", "--date", "2026-10-20"]) == 0
    rec = yaml.safe_load(sample.read_text(encoding="utf-8"))
    sheet = tmp_path / "reliability_sheet.csv"
    assert reliability.main([*base, "sheet", "--out", str(sheet)]) == 0
    assert reliability.main([*base, "sheet", "--out", str(sheet)]) == 1  # refuses to overwrite
    rows = label.read_sheet(sheet)
    for r in rows:
        r.update(minutes="1", labeler="coder", labeled_at="2026-10-21T10:00:00")
        if r["field"] == "n_assessment_areas":
            r.update(value="2", evidence_page="3", quote="two assessment areas")
        elif r["field"] == "n_full_scope":
            r.update(value="2", evidence_page="3", quote="full-scope")
        elif r["field"] == "loan_products_evaluated":
            r.update(value="home_mortgage", evidence_page="4", quote="home mortgage loans")
        elif r["field"] == "lending_data_period_start":
            r.update(null_reason="absent_from_document")
        else:
            r.update(value="2023-12-31", evidence_page="4", quote="December 31, 2023")
    label.write_sheet(rows, sheet)
    first = tmp_path / "labels_test.csv"
    first_rows = [dict(r) for r in rows]
    for r in first_rows:
        if r["field"] == "n_assessment_areas" and r["doc_id"] == rec["doc_ids"][0]:
            r["value"] = "3"
    label.write_sheet(first_rows, first)
    out = tmp_path / "results" / "reliability.csv"
    assert reliability.main([*base, "compare", "--sheet", str(sheet), "--first", str(first), "--out", str(out)]) == 0
    t = pd.read_csv(out).set_index("field")
    assert (t.loc["n_assessment_areas", "n"], t.loc["n_assessment_areas", "n_agree"]) == (8, 7)
    paths = report.ReportPaths.under(tmp_path)
    assert report.manual_review_error(paths) == pytest.approx(1 - (7 + 8 + 8 + 8 + 8) / 40)
    assert isinstance(Path(out), Path)


def test_every_stage_is_exposed_in_the_cli_and_the_makefile():
    from cra_extract import cli

    for stage in ("overrides", "census", "routing", "corpus-scoring", "audit", "reliability", "report"):
        assert stage in cli.STAGES, stage
    text = (Path(__file__).parent.parent / "Makefile").read_text(encoding="utf-8")
    for target in ("overrides", "overrides-check", "tuning-docs", "census-draw", "census-sheet", "census-score",
                   "census-double", "census-compare", "s5-routing", "s5-apply", "s5-test", "group-a-corpus",
                   "risk-corpus", "audit-sheet", "audit", "reliability-draw", "reliability-sheet",
                   "reliability-compare", "report", "eval-pair", "ablation", "closed-book", "dev-rerun", "project"):
        assert f"\n{target}:" in text, target
    assert "TEST_SEED ?= 20261013" in text and "AUDIT_SEED ?= 20261016" in text
    assert "--seed $(TEST_SEED)" in text and "--seed $(AUDIT_SEED)" in text
    assert "--check results/table1_test.csv" in text and "--check results/table1.csv" not in text
    assert "AUDIT_SYSTEM ?= s5" in text
