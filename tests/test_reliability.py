"""Tests for cra_extract.reliability: inter-reader agreement on tiny synthetic reader files, and
the withdrawn second-coding branch on a synthetic manifest."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from cra_extract import label, reliability, report
from cra_extract.models import DocRecord, make_doc_id
from cra_extract.stats import cohen_kappa, wilson_ci
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
                   "reliability-compare", "reliability", "report", "eval-pair", "ablation", "closed-book", "dev-rerun", "project"):
        assert f"\n{target}:" in text, target
    assert "TEST_SEED ?= 20261013" in text and "AUDIT_SEED ?= 20261016" in text
    assert "--seed $(TEST_SEED)" in text and "--seed $(AUDIT_SEED)" in text
    assert "--check results/table1_test.csv" in text and "--check results/table1.csv" not in text
    assert "AUDIT_SYSTEM ?= s5" in text
    assert "-m cra_extract.reliability agreement" in text


# ----------------------------------------------------------------------------- agreement

READ_COLUMNS = ["doc_id", "field", "value", "null_reason", "evidence_page", "quote", "notes"]
CENSUS_FIELDS = [
    "finding_present", "finding_type", "rating_effect", "rating_lowered", "rating_lowered_level",
    "rating_before", "rating_after", "laws_cited", "practice_basis", "finding_source",
    "distractor_laws_named",
]


def _write_doc_reads(folder: Path, doc_id: str, reader: str, cells: dict[str, tuple[str, str]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"doc_id": doc_id, "field": f, "value": v, "null_reason": nr, "evidence_page": "",
             "quote": "", "notes": ""} for f, (v, nr) in cells.items()]
    pd.DataFrame(rows, columns=READ_COLUMNS).to_csv(folder / f"{doc_id}.{reader}.csv", index=False)


def _census_row(doc_id: str, **cells: str) -> dict[str, str]:
    row = {"doc_id": doc_id, **dict.fromkeys(CENSUS_FIELDS, "NA"), "evidence_page": "", "quote": "", "notes": ""}
    row.update(finding_present="false", rating_lowered="false", distractor_laws_named="false")
    row.update(cells)
    return row


@pytest.fixture
def gold(tmp_path) -> Path:
    """Four dev documents and three census sections, read by A and B."""
    g = tmp_path / "gold"
    dev = g / "dev_reads"
    # overall_rating: A = S S NI O, B = S S S O (3 of 4 agree; both readers use >= 2 classes).
    # finding_present: false for every document by both readers (one class only).
    # loan_products_evaluated: order, case and spacing differ but the sets agree on D0..D2;
    #   D3 differs by one element.
    # n_full_scope: null code vs "NA" in the value cell is the same category.
    a_rating = ["S", "S", "NI", "O"]
    b_rating = ["S", "S", "S", "O"]
    a_lp = ["home_mortgage|small_business", "consumer", "small_farm", "home_mortgage"]
    b_lp = ["Small_Business | home_mortgage", "consumer", "small_farm", "home_mortgage|consumer"]
    for i in range(4):
        doc = f"FDIC_{100 + i}_20240101"
        _write_doc_reads(dev, doc, "a", {
            "overall_rating": (a_rating[i], ""), "finding_present": ("false", ""),
            "loan_products_evaluated": (a_lp[i], ""), "n_full_scope": ("", "not_applicable"),
        })
        _write_doc_reads(dev, doc, "b", {
            "overall_rating": (b_rating[i], ""), "finding_present": ("FALSE", ""),
            "loan_products_evaluated": (b_lp[i], ""), "n_full_scope": ("NA", ""),
        })
    pd.DataFrame([
        {"doc_id": "FDIC_102_20240101", "field": "overall_rating", "reader_a": "NI", "reader_b": "S", "final": "NI", "note": ""},
        {"doc_id": "FDIC_103_20240101", "field": "loan_products_evaluated", "reader_a": "", "reader_b": "", "final": "", "note": ""},
    ]).to_csv(g / "dev_disputes.csv", index=False)
    census = g / "census_reads"
    census.mkdir(parents=True)
    a_rows = [
        _census_row("OCC_1_20230101", finding_present="true", finding_type="discrimination",
                    laws_cited="ECOA_RegB|FHA", practice_basis="ABS", finding_source="this_CRA_exam"),
        _census_row("OCC_2_20230101"),
        _census_row("OCC_3_20230101", distractor_laws_named="true"),
    ]
    b_rows = [
        _census_row("OCC_1_20230101", finding_present="True", finding_type="discrimination",
                    laws_cited="FHA|ECOA_RegB", practice_basis="redlining", finding_source="this_CRA_exam"),
        _census_row("OCC_2_20230101"),
        _census_row("OCC_3_20230101", distractor_laws_named="false"),
    ]
    pd.DataFrame(a_rows).to_csv(census / "batch1.a.csv", index=False)
    pd.DataFrame(b_rows).to_csv(census / "batch1.b.csv", index=False)
    pd.DataFrame([
        {"doc_id": "OCC_1_20230101", "fields": "practice_basis", "adjudicated": "True"},
        {"doc_id": "OCC_3_20230101", "fields": "distractor_laws_named|finding_source", "adjudicated": "True"},
    ]).to_csv(g / "census_disputes.csv", index=False)
    return g


def test_canonical_value_categories():
    cv = reliability.canonical_value
    assert cv("n_full_scope", "", "not_applicable") == cv("n_full_scope", "NA") == "null:not_applicable"
    assert cv("practice_basis", "ABS") == cv("practice_basis", "", "absent_from_document") == "null:absent_from_document"
    assert cv("practice_basis", "ABS") != cv("practice_basis", "NA")
    assert cv("laws_cited", "FHA | ECOA_RegB") == cv("laws_cited", "ecoa_regb|fha") == "ecoa_regb|fha"
    assert cv("finding_present", "True") == "true" and cv("finding_present", "no") == "false"
    assert cv("bank_name", "  First   State\tBank ") == cv("bank_name", "FIRST STATE BANK") == "first state bank"
    assert cv("exam_date", "March 4, 2024") == "2024-03-04"
    assert cv("n_assessment_areas", "2.0") == "2" and cv("avg_ltd_ratio_pct", "71.50") == "71.5"
    assert cv("bank_name", "") == ""


def test_agreement_arithmetic_and_wilson(gold):
    table, summary = reliability.agreement(gold)
    assert list(table.columns) == reliability.AGREEMENT_COLUMNS
    t = table.set_index(["split", "field"])
    r = t.loc[("dev", "overall_rating")]
    assert (r["n"], r["agree"]) == (4, 3)
    assert r["pct"] == pytest.approx(75.0)
    lo, hi = wilson_ci(3, 4)
    assert (r["ci_low"], r["ci_high"]) == (pytest.approx(100 * lo), pytest.approx(100 * hi))
    assert (t.loc[("dev", "n_full_scope"), "n"], t.loc[("dev", "n_full_scope"), "agree"]) == (4, 4)
    pooled = t.loc[("dev", "pooled")]
    # 4 fields x 4 documents; disagreements: overall_rating D2, loan_products_evaluated D3.
    assert (pooled["n"], pooled["agree"]) == (16, 14)
    assert summary["dev"]["documents"] == 4 and summary["dev"]["fields"] == 4
    census = t.loc[("census", "pooled")]
    assert (census["n"], census["agree"]) == (33, 31)  # 11 fields x 3 sections
    overall = t.loc[("all", "pooled")]
    assert (overall["n"], overall["agree"]) == (49, 45)
    fp = t.loc[("all", "finding_present")]
    assert fp["n"] == 7 and "splits census, dev" in fp["note"]


def test_kappa_only_when_both_readers_use_two_classes(gold):
    table, _ = reliability.agreement(gold)
    t = table.set_index(["split", "field"])
    r = t.loc[("dev", "overall_rating")]
    assert r["kappa"] == pytest.approx(cohen_kappa(["s", "s", "ni", "o"], ["s", "s", "s", "o"]))
    fp = t.loc[("dev", "finding_present")]
    assert fp["agree"] == 4 and math.isnan(fp["kappa"])
    assert "kappa not computed" in fp["note"] and "'false', prevalence 1.000" in fp["note"]
    # Census: finding_present true on one section, false on two, by both readers.
    assert t.loc[("census", "finding_present"), "kappa"] == pytest.approx(1.0)
    # distractor_laws_named: A uses two classes, B only one -> no kappa.
    dl = t.loc[("census", "distractor_laws_named")]
    assert math.isnan(dl["kappa"]) and "reader A uses 2 and reader B 1 category" in dl["note"]
    assert math.isnan(t.loc[("dev", "pooled"), "kappa"])


def test_sets_compare_as_sorted_elements(gold):
    table, _ = reliability.agreement(gold)
    t = table.set_index(["split", "field"])
    lp = t.loc[("dev", "loan_products_evaluated")]
    assert (lp["n"], lp["agree"]) == (4, 3)
    assert "element-wise 19/20 agree over 5 elements" in lp["note"]
    laws = t.loc[("census", "laws_cited")]
    assert laws["agree"] == 3  # "ECOA_RegB|FHA" and "FHA|ECOA_RegB" agree
    pb = t.loc[("census", "practice_basis")]
    assert pb["agree"] == 2  # ABS (a null) differs from redlining


def test_adjudication_rate_and_dispute_checks(gold):
    table, summary = reliability.agreement(gold)
    t = table.set_index(["split", "field"])
    assert summary["dev"]["n_disputed"] == 2
    assert "adjudication rate 2/16 = 12.5%" in t.loc[("dev", "pooled"), "note"]
    assert "adjudicated 1 of 4" in t.loc[("dev", "overall_rating"), "note"]
    # Census: "fields" holds several fields joined with "|"; finding_source is disputed
    # although both readers agree on it, and every disagreement is listed.
    assert summary["census"]["n_disputed"] == 3
    assert "adjudication rate 3/33" in t.loc[("census", "pooled"), "note"]
    assert "1 disputed item(s) agree" in t.loc[("census", "finding_source"), "note"]
    assert summary["census"]["unlisted"] == 0
    assert "adjudication rate 5/49" in t.loc[("all", "pooled"), "note"]
    assert "2 items were sent to adjudication (adjudication rate 12.5 percent)" in reliability.split_sentence("dev", summary["dev"])


def test_unlisted_disagreement_and_unpaired_items(gold):
    (gold / "dev_disputes.csv").unlink()
    doc = "FDIC_900_20240101"
    _write_doc_reads(gold / "dev_reads", doc, "a", {"overall_rating": ("S", "")})
    table, summary = reliability.agreement(gold, splits=("dev",))
    t = table.set_index(["split", "field"])
    assert summary["dev"]["n_disputed"] == 0 and summary["dev"]["unlisted"] == 2
    assert "1 disagreement(s) not in the dispute list" in t.loc[("dev", "overall_rating"), "note"]
    assert "1 item(s) read by one reader only" in t.loc[("dev", "pooled"), "note"]
    assert ("all", "pooled") not in t.index  # one split: no overall rows
    assert "not in the dispute list" in reliability.split_sentence("dev", summary["dev"])


def test_agreement_cli_writes_csv_and_markdown(gold, tmp_path, capsys):
    out = tmp_path / "results" / "reliability.csv"
    assert reliability.main(["agreement", "--gold-dir", str(gold), "--out", str(out)]) == 0
    df = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert list(df.columns) == reliability.AGREEMENT_COLUMNS
    assert {"dev", "census", "all"} == set(df["split"])
    assert df[(df["split"] == "test")].empty
    md = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "| split | field | n | agree | pct |" in md
    assert "- dev: readers A and B agreed on 14 of 16 items" in md
    assert "- test: no reader files found" in md
    assert "\u2014" not in md and "\u2013" not in md
    assert "dev: readers A and B agreed" in capsys.readouterr().out
    assert reliability.main(["agreement", "--gold-dir", str(tmp_path / "none"), "--out", str(out)]) == 1
