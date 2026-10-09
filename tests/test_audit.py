"""Tests for cra_extract.audit on synthetic strata records, sheets and predictions.

No PDFs, no corpus files: every design is a hand-written ``splits.audit`` record (or one
drawn by ``strata.draw_audit`` from a synthetic manifest), every sheet is built by
``build_sheet`` and filled with values that pass ``label.validate``, and every estimate is
compared with a value computed by hand in the test.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pandas as pd
import pytest
import yaml

from cra_extract import audit, label, strata
from cra_extract.models import DocRecord, FieldValue
from cra_extract.stats import wilson_ci

SYSTEM = "llm:gemini:test-model"
GROUP_B = audit.group_b_fields()

# A filled Group B label per field that passes label.validate (exam dates are in 2024).
GOLD = {
    "total_assets_usd": "100000000",
    "total_assets_asof": "2023-12-31",
    "lending_data_period_start": "2021-01-01",
    "lending_data_period_end": "2022-12-31",
    "n_assessment_areas": "2",
    "n_full_scope": "1",
    "loan_products_evaluated": "home_mortgage|small_business",
    "primary_product": "home_mortgage",
    "inside_aa_share_number_pct": "80.5",
    "inside_aa_share_product": "all_reviewed_combined",
    "avg_ltd_ratio_pct": "75.2",
}

LOW = ["FDIC_101_20240110", "OCC_102_20240111"]
MID = ["FDIC_203_20240112", "FRB_201_20240113", "OCC_202_20240114", "OCC_204_20240115"]
HIGH = ["FRB_301_20240116", "FDIC_302_20240117"]


def _record(low: list[str], mid: list[str], high: list[str], pops: tuple[int, int, int]) -> dict:
    strata_rows, docs = [], []
    for name, ids, pop in zip(("low", "mid", "high"), (low, mid, high), pops, strict=True):
        strata_rows.append({
            "name": f"tertile:{name}", "population": pop, "pool": pop, "drawn": len(ids),
            "selection_probability": round(len(ids) / pop, 6), "weight": round(pop / len(ids), 6),
            "detail": {},
        })
        docs += [{"doc_id": d, "stratum": f"tertile:{name}"} for d in ids]
    return {"split": "audit", "strata": strata_rows, "docs": docs}


@pytest.fixture
def design() -> audit.AuditDesign:
    # w_low = 20 / 2 = 10, w_mid = 16 / 4 = 4, w_high = 4 / 2 = 2.
    return audit.design_from_record(_record(LOW, MID, HIGH, (20, 16, 4)))


def _preds(doc_ids: list[str], system: str = SYSTEM, **override: str | None) -> list[FieldValue]:
    """Predictions equal to GOLD for every document; ``override`` maps field -> value
    (None for status absent, 'ABSTAIN' to drop the row)."""
    out = []
    for d in doc_ids:
        for f in GROUP_B:
            v = override.get(f, GOLD[f]) if d == doc_ids[0] else GOLD[f]
            if v == "ABSTAIN":
                continue
            out.append(FieldValue(
                doc_id=d, field=f, system=system, status="absent" if v is None else "value",
                value=v, quote=f"QUOTE {d} {f}" if v is not None else None, page=3,
            ))
    return out


def _fill(rows: list[dict[str, str]], minutes: str = "1.5") -> list[dict[str, str]]:
    out = []
    for r in rows:
        r = dict(r)
        r.update(value=GOLD[r["field"]], evidence_page="3", quote="verbatim quote", minutes=minutes,
                 labeler="author", labeled_at="2026-10-16T10:00:00")
        out.append(r)
    return out


def _write(rows: list[dict[str, str]], path: Path) -> Path:
    audit.write_sheet(rows, path)
    return path


# ----------------------------------------------------------------------------- design


def test_weights_from_a_strata_draw(tmp_path):
    manifest = [
        DocRecord(doc_id=f"FDIC_{i}_20240101", regulator="FDIC", bank_id=str(i), bank_name=f"B{i}",
                  method="small", url="https://example.invalid/x.pdf")
        for i in range(1, 94)
    ]
    risk = {d.doc_id: float(i % 7) for i, d in enumerate(manifest)}
    draw = strata.draw_audit(manifest, risk, seed=5, drawn_at="2026-10-16")
    path = tmp_path / "strata.yaml"
    strata.save_draw(draw, path)
    design = audit.load_design(path)
    assert design.population == {"low": 31, "mid": 31, "high": 31}
    assert design.drawn == {"low": 10, "mid": 10, "high": 10}
    assert design.weight("low") == pytest.approx(3.1)
    assert sorted(design.doc_ids) == draw.doc_ids
    modes = audit.blinding_modes(design)
    by = {t: [modes[d] for d in design.docs_in(t)] for t in audit.TERTILES}
    assert by["low"] == ["blind"] * 10
    assert by["high"] == ["shown"] * 10
    assert by["mid"] == ["blind", "shown"] * 5


def test_design_weights_are_exact_ratios(design):
    assert design.weight("low") == 10.0
    assert design.weight("mid") == 4.0
    assert design.weight("high") == 2.0


def test_design_rejects_inconsistent_records(tmp_path):
    rec = _record(LOW, MID, HIGH, (20, 16, 4))
    rec["strata"][0]["weight"] = 9.0
    with pytest.raises(audit.AuditError, match="weight"):
        audit.design_from_record(rec)
    rec = _record(LOW, MID, HIGH, (20, 16, 4))
    rec["docs"] = rec["docs"][1:]
    with pytest.raises(audit.AuditError, match="listed"):
        audit.design_from_record(rec)
    rec = _record(LOW, MID, HIGH, (20, 16, 4))
    rec["strata"] = rec["strata"][:2]
    with pytest.raises(audit.AuditError, match="tertiles"):
        audit.design_from_record(rec)
    path = tmp_path / "strata.yaml"
    path.write_text(yaml.safe_dump({"splits": {}}), encoding="utf-8")
    with pytest.raises(audit.AuditError, match="draw-audit"):
        audit.load_design(path)


def test_blinding_by_tertile_and_mid_position(design):
    modes = audit.blinding_modes(design)
    assert {modes[d] for d in LOW} == {"blind"}
    assert {modes[d] for d in HIGH} == {"shown"}
    # Sorted mid list: FDIC_203, FRB_201, OCC_202, OCC_204 -> positions 1..4.
    assert sorted(MID) == ["FDIC_203_20240112", "FRB_201_20240113", "OCC_202_20240114", "OCC_204_20240115"]
    assert [modes[d] for d in sorted(MID)] == ["blind", "shown", "blind", "shown"]


# ----------------------------------------------------------------------------- sheet


def test_sheet_rows_modes_and_model_columns(design):
    preds = _preds(design.doc_ids) + _preds(design.doc_ids, system="rules")
    rows = audit.build_sheet(design, preds, SYSTEM)
    assert len(rows) == len(design.doc_ids) * 11 == 88
    assert [r["field"] for r in rows[:11]] == GROUP_B
    modes = audit.blinding_modes(design)
    for r in rows:
        assert r["mode"] == modes[r["doc_id"]]
        assert r["pdf_path"] == f"data/pdfs/{r['doc_id']}.pdf"
        assert r["field_group"] == "B"
        if r["mode"] == "blind":
            assert all(r[c] == "" for c in audit.MODEL_COLUMNS)
        else:
            assert r["model_status"] == "value"
            assert r["model_value"] == GOLD[r["field"]]
            assert r["model_quote"] == f"QUOTE {r['doc_id']} {r['field']}"


def test_sheet_never_contains_gold(design, tmp_path):
    # A gold file sitting next to the sheet must not leak into it, and no gold column is
    # ever filled, even on shown rows where the model value equals the gold value.
    planted = tmp_path / "labels_test.csv"
    planted.write_text("doc_id,field,value\nFRB_301_20240116,primary_product,GOLD_SENTINEL\n", encoding="utf-8")
    preds = _preds(design.doc_ids)
    rows = audit.build_sheet(design, preds, SYSTEM)
    path = _write(rows, tmp_path / "audit_sheet.csv")
    with open(path, newline="", encoding="utf-8") as fh:
        written = list(csv.DictReader(fh))
    assert list(written[0]) == audit.SHEET_COLUMNS
    assert "prefilled" not in written[0]
    for r in written:
        for c in (*audit.GOLD_COLUMNS, "minutes", "labeler", "labeled_at", "notes"):
            assert r[c] == "", (r["doc_id"], r["field"], c)
    assert "GOLD_SENTINEL" not in path.read_text(encoding="utf-8")
    # Blind rows show nothing of the model; QUOTE strings appear only on shown rows.
    for r in written:
        if r["mode"] == "blind":
            assert "QUOTE" not in "".join(r.values())


def test_sheet_shows_absent_and_abstain_status(design):
    first_shown = sorted(HIGH)[0]
    preds = [p for p in _preds(design.doc_ids) if not (p.doc_id == first_shown and p.field == "n_full_scope")]
    preds = [
        p.model_copy(update={"status": "absent", "value": None, "quote": None})
        if (p.doc_id == first_shown and p.field == "avg_ltd_ratio_pct") else p
        for p in preds
    ]
    rows = {(r["doc_id"], r["field"]): r for r in audit.build_sheet(design, preds, SYSTEM)}
    assert rows[(first_shown, "n_full_scope")]["model_status"] == "abstain"
    assert rows[(first_shown, "avg_ltd_ratio_pct")]["model_status"] == "absent"
    assert rows[(first_shown, "avg_ltd_ratio_pct")]["model_value"] == ""


def test_sheet_refuses_unknown_system(design):
    with pytest.raises(audit.AuditError, match="no prediction"):
        audit.build_sheet(design, _preds(design.doc_ids), "llm:other:model")


# ----------------------------------------------------------------------------- validate


def test_validate_clean_filled_sheet(design, tmp_path):
    rows = _fill(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM))
    assert audit.validate(_write(rows, tmp_path / "s.csv"), design) == []


def _rules(problems) -> set[str]:
    return {p.rule for p in problems}


def test_validate_mode_and_blinding(design, tmp_path):
    rows = _fill(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM))
    low_row = next(i for i, r in enumerate(rows) if r["doc_id"] == LOW[0])
    rows[low_row]["mode"] = "shown"
    rows[low_row + 1]["model_value"] = "leaked"
    problems = audit.validate(_write(rows, tmp_path / "s.csv"), design)
    assert _rules(problems) == {"mode", "blind_leak"}
    assert {p.row for p in problems} == {low_row + 1, low_row + 2}


def test_validate_minutes_on_every_row(design, tmp_path):
    rows = _fill(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM))
    rows[5]["minutes"] = ""
    problems = audit.validate(_write(rows, tmp_path / "s.csv"), design)
    assert [(p.rule, p.row) for p in problems] == [("missing_minutes", 6)]


def test_validate_reuses_label_rules_with_sheet_rows(design, tmp_path):
    rows = _fill(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM))
    i = next(i for i, r in enumerate(rows) if r["doc_id"] == HIGH[0] and r["field"] == "n_full_scope")
    rows[i]["value"] = "5"  # more full-scope areas than assessment areas
    j = next(i for i, r in enumerate(rows) if r["doc_id"] == MID[0] and r["field"] == "avg_ltd_ratio_pct")
    rows[j]["quote"] = ""
    problems = audit.validate(_write(rows, tmp_path / "s.csv"), design)
    assert {(p.rule, p.row) for p in problems} == {("lte_n_assessment_areas", i + 1), ("missing_quote", j + 1)}


def test_validate_coverage(design, tmp_path):
    rows = _fill(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM))
    dropped = rows.pop(0)
    extra = dict(rows[0], field="overall_rating", value="S")
    stranger = dict(rows[0], doc_id="OCC_999_20240101")
    problems = audit.validate(_write([*rows, extra, stranger], tmp_path / "s.csv"), design)
    rules = {(p.rule, p.doc_id, p.field) for p in problems}
    assert ("missing_row", dropped["doc_id"], dropped["field"]) in rules
    assert ("not_group_b", extra["doc_id"], "overall_rating") in rules
    assert ("not_audit_doc", "OCC_999_20240101", stranger["field"]) in rules


# ----------------------------------------------------------------------------- scoring


def test_score_audit_cells_and_error_indicator(design):
    doc = sorted(design.doc_ids)[0]
    rows = _fill(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM))
    for r in rows:
        if r["doc_id"] == doc and r["field"] == "n_full_scope":
            r.update(value="", null_reason="not_applicable", evidence_page="", quote="")
        if r["doc_id"] == doc and r["field"] == "inside_aa_share_number_pct":
            r.update(value="", null_reason="absent_from_document", evidence_page="", quote="")
        if r["doc_id"] == doc and r["field"] == "inside_aa_share_product":
            r.update(value="", null_reason="not_applicable", evidence_page="", quote="")
    order = sorted(design.doc_ids)
    preds = _preds(
        order,
        n_full_scope="1",                    # gold NA, system value -> fabrication
        inside_aa_share_number_pct=None,     # gold ABS, system absent -> correct_null
        inside_aa_share_product="ABSTAIN",   # no prediction -> abstention
        avg_ltd_ratio_pct="75.25",           # within 0.1pp -> correct
        primary_product="small_farm",        # wrong
        lending_data_period_end=None,        # gold value, system absent -> fabricated_absence
        total_assets_asof="2024-03-31",      # wrong date, and total_assets_usd is then out of quarter
    )
    values = audit.score_audit(rows, preds, design, SYSTEM)
    assert len(values) == 88
    cell = values[values["doc_id"] == doc].set_index("field")["cell"].to_dict()
    assert cell == {
        "total_assets_usd": "wrong",
        "total_assets_asof": "wrong",
        "lending_data_period_start": "correct",
        "lending_data_period_end": "fabricated_absence",
        "n_assessment_areas": "correct",
        "n_full_scope": "fabrication",
        "loan_products_evaluated": "correct",
        "primary_product": "wrong",
        "inside_aa_share_number_pct": "correct_null",
        "inside_aa_share_product": "abstention",
        "avg_ltd_ratio_pct": "correct",
    }
    one = values[values["doc_id"] == doc].set_index("field")
    assert one.loc["inside_aa_share_number_pct", "y"] == 0.0
    assert math.isnan(one.loc["inside_aa_share_product", "y"])
    assert not one.loc["inside_aa_share_product", "answered"]
    assert one["y"].sum() == 5
    assert set(values["weight"]) == {10.0, 4.0, 2.0}
    assert values[values["doc_id"] != doc]["y"].sum() == 0
    assert set(values["regulator"]) == {"OCC", "FDIC", "FRB"}


# ----------------------------------------------------------------------------- estimate


def _values(spec: list[tuple[str, str, float, str, str, list[float | None]]]) -> pd.DataFrame:
    """spec rows: (doc_id, tertile, weight, regulator, mode, y list with None = abstention)."""
    rows = []
    for doc, tertile, w, reg, mode, ys in spec:
        for i, y in enumerate(ys):
            rows.append({
                "doc_id": doc, "field": GROUP_B[i], "tertile": tertile, "weight": w,
                "regulator": reg, "mode": mode, "minutes": 2.0 if mode == "blind" else 1.0,
                "cell": "abstention" if y is None else ("wrong" if y else "correct"),
                "answered": y is not None, "y": math.nan if y is None else float(y),
            })
    return pd.DataFrame(rows)


HAND = [
    ("L1", "low", 10.0, "FDIC", "blind", [0, 0]),
    ("L2", "low", 10.0, "OCC", "blind", [0, None]),
    ("M1", "mid", 4.0, "FDIC", "blind", [1, 0]),
    ("M2", "mid", 4.0, "FRB", "shown", [0, 0]),
    ("H1", "high", 2.0, "FRB", "shown", [1, 1]),
    ("H2", "high", 2.0, "OCC", "shown", [1, 0]),
]
HAND_DESIGN = audit.AuditDesign(
    tertile_of={"L1": "low", "L2": "low", "M1": "mid", "M2": "mid", "H1": "high", "H2": "high"},
    population={"low": 20, "mid": 8, "high": 4},
    drawn={"low": 2, "mid": 2, "high": 2},
)


def _summary(est: audit.AuditEstimates, quantity: str, stratum: str) -> pd.Series:
    s = est.summary
    return s[(s["quantity"] == quantity) & (s["stratum"] == stratum)].iloc[0]


def test_estimator_matches_hand_computation():
    values = _values(HAND)
    est = audit.estimate(values, HAND_DESIGN, n_boot=200)
    # Answered: low 3 values x 10 (0 errors), mid 4 x 4 (1 error), high 4 x 2 (3 errors).
    # sum(w y) = 0 + 4 + 6 = 10; sum(w) = 30 + 16 + 8 = 54.
    head = _summary(est, "design_weighted_error", "all")
    assert head["estimate"] == pytest.approx(10 / 54)
    assert head["ci_lo"] <= head["estimate"] <= head["ci_hi"]
    assert head["interval"].startswith("stratified_bootstrap")
    assert (head["n_values"], head["n_answered"], head["n_errors"]) == (12, 11, 4)
    # Abstention: weight 10 of 40 + 16 + 8 = 64.
    assert _summary(est, "design_weighted_abstention_rate", "all")["estimate"] == pytest.approx(10 / 64)
    # The unweighted error would be 4 / 11; the weights pull it down.
    assert head["estimate"] < 4 / 11
    tert = est.by_tertile.set_index("tertile")
    assert tert.loc["low", "error_raw"] == 0.0
    assert tert.loc["mid", "error_raw"] == pytest.approx(0.25)
    assert tert.loc["high", "error_raw"] == pytest.approx(0.75)
    assert (tert.loc["high", "ci_lo"], tert.loc["high", "ci_hi"]) == pytest.approx(wilson_ci(3, 4))
    assert tert.loc["low", "weight"] == 10.0
    assert tert.loc["mid", "n_docs_blind"] == 1 and tert.loc["mid", "n_docs_shown"] == 1
    # By regulator, design-weighted: FDIC (0,0 at w10; 1,0 at w4) = 4 / 28.
    assert _summary(est, "error_by_regulator", "FDIC")["estimate"] == pytest.approx(4 / 28)
    # OCC: L2 answered 0 at w10, H2 (1, 0) at w2 -> 2 / 14.
    assert _summary(est, "error_by_regulator", "OCC")["estimate"] == pytest.approx(2 / 14)
    # Middle tertile by mode: blind 1/2, shown 0/2.
    assert _summary(est, "mid_tertile_error_by_mode", "blind")["estimate"] == 0.5
    assert _summary(est, "mid_tertile_error_by_mode", "shown")["estimate"] == 0.0
    assert not (est.summary["quantity"] == "wilson_upper_bound").any()


def test_estimator_bootstrap_is_the_stats_function():
    from cra_extract.stats import stratified_weighted_mean

    values = _values(HAND)
    est = audit.estimate(values, HAND_DESIGN, n_boot=300, seed=0)
    ans = values[values["answered"]].reset_index(drop=True)
    expected = stratified_weighted_mean(ans, "y", stratum_col="tertile", n_boot=300, seed=0)
    head = _summary(est, "design_weighted_error", "all")
    assert (head["estimate"], head["ci_lo"], head["ci_hi"]) == pytest.approx(expected)


NH_SPEC = [
    ("A", "low", 5.0, "FDIC", "blind", [1, 0, None]),
    ("B", "low", 5.0, "FDIC", "blind", [0, 0]),
    ("C", "mid", 2.0, "OCC", "blind", [None, None]),
    ("D", "mid", 2.0, "OCC", "shown", [0, 1]),
    ("E", "high", 2.0, "FRB", "shown", [0, 0, 0]),
]
NH_DESIGN = audit.AuditDesign(
    tertile_of={"A": "low", "B": "low", "C": "mid", "D": "mid", "E": "high"},
    population={"low": 10, "mid": 4, "high": 2},
    drawn={"low": 2, "mid": 2, "high": 1},
)


def test_bootstrap_keeps_n_h_when_a_document_abstained_on_every_value():
    """Plan, Estimator: documents are resampled within tertile with n_h kept. C abstained on
    every value; it must stay in the mid tertile's resampling (n_h = 2), so the exact
    bootstrap has 16 equiprobable resamples with minimum 0 and maximum 7/17."""
    import itertools

    values = _values(NH_SPEC)
    est = audit.estimate(values, NH_DESIGN, n_boot=4000, seed=0)
    head = _summary(est, "design_weighted_error", "all")
    assert head["estimate"] == pytest.approx(7 / 30)
    docs = {d: [(w, y) for y in ys if y is not None] for d, _t, w, _r, _m, ys in NH_SPEC}
    exact = []
    for low in itertools.product("AB", repeat=2):
        for mid in itertools.product("CD", repeat=2):
            vals = [v for d in (*low, *mid, "E") for v in docs[d]]
            exact.append(sum(w * y for w, y in vals) / sum(w for w, _ in vals))
    assert len(exact) == 16
    assert head["ci_lo"] == pytest.approx(min(exact)) and min(exact) == 0.0
    assert head["ci_hi"] == pytest.approx(max(exact)) and max(exact) == pytest.approx(7 / 17)
    # The answered-only bootstrap would drop C and could never reach 0 or 7/17.
    assert head["interval"] == "stratified_bootstrap_4000_seed0_percentile"


def test_interval_label_names_the_bootstrap_that_ran():
    est = audit.estimate(_values(HAND), HAND_DESIGN, n_boot=500, seed=7)
    assert _summary(est, "design_weighted_error", "all")["interval"] == "stratified_bootstrap_500_seed7_percentile"
    wide = est.by_field[est.by_field["note"] == "wide"]
    assert set(wide["interval"]) <= {"stratified_bootstrap_500_seed7_percentile"}


def test_flags_come_from_the_risk_values_file(tmp_path):
    values = _values(HAND)
    path = tmp_path / "risk_values_corpus.csv"
    rows = [{"doc_id": d, "field": f, "system": "s5", "n_flags": i % 3}
            for i, (d, f) in enumerate(zip(values["doc_id"], values["field"], strict=True))]
    rows.append({"doc_id": "L1", "field": GROUP_B[0], "system": "other", "n_flags": 4})
    pd.DataFrame(rows).to_csv(path, index=False)
    out = audit.flags_from_risk_values(values, path, "s5")
    assert out["n_flags"].tolist() == [i % 3 for i in range(len(values))]
    with pytest.raises(audit.AuditError, match="no rows of system"):
        audit.flags_from_risk_values(values, path, "missing")
    pd.DataFrame(rows[1:-1]).to_csv(path, index=False)
    with pytest.raises(audit.AuditError, match="1 audited values have no s5 row"):
        audit.flags_from_risk_values(values, path, "s5")


def test_cli_estimate_refuses_a_doc_risk_mismatch(tmp_path, capsys):
    record = _record(LOW, MID, HIGH, (20, 16, 4))
    strata_path = tmp_path / "strata.yaml"
    strata_path.write_text(yaml.safe_dump({"version": 1, "splits": {"audit": record}}), encoding="utf-8")
    design = audit.design_from_record(record)
    pred_path = tmp_path / "predictions_corpus.csv"
    _write_predictions(pred_path, _preds(sorted(design.doc_ids), system="s5"))
    sheet = tmp_path / "audit_sheet.csv"
    base = ["--strata", str(strata_path)]
    assert audit.main([*base, "sheet", "--system", "s5", "--predictions", str(pred_path), "--out", str(sheet)]) == 0
    _write(_fill(label.read_sheet(sheet)), sheet)
    values_path = tmp_path / "risk_values_corpus.csv"
    pd.DataFrame([{"doc_id": d, "field": f, "system": "s5", "n_flags": 1}
                  for d in design.doc_ids for f in GROUP_B]).to_csv(values_path, index=False)
    risk_path = tmp_path / "risk_corpus.csv"
    pd.DataFrame([{"doc_id": d, "doc_risk": 11} for d in design.doc_ids]).to_csv(risk_path, index=False)
    args = [*base, "estimate", "--sheet", str(sheet), "--predictions", str(pred_path),
            "--risk", str(risk_path), "--risk-values", str(values_path),
            "--out-dir", str(tmp_path / "r"), "--n-boot", "20"]
    assert audit.main(args) == 0
    pd.DataFrame([{"doc_id": d, "doc_risk": 10} for d in design.doc_ids]).to_csv(risk_path, index=False)
    capsys.readouterr()
    assert audit.main(args) == 1
    assert "estimate refused" in capsys.readouterr().err


def test_estimator_no_error_branch_reports_wilson_upper_bounds():
    spec = [(d, t, w, r, m, [0 if y is not None else None for y in ys]) for d, t, w, r, m, ys in HAND]
    est = audit.estimate(_values(spec), HAND_DESIGN, n_boot=100)
    head = _summary(est, "design_weighted_error", "all")
    assert head["estimate"] == 0.0
    assert math.isnan(head["ci_lo"]) and math.isnan(head["ci_hi"])
    assert head["interval"] == "none"
    for t, n in (("low", 3), ("mid", 4), ("high", 4)):
        row = _summary(est, "wilson_upper_bound", f"tertile:{t}")
        assert row["ci_hi"] == pytest.approx(wilson_ci(0, n)[1])
        assert row["n_answered"] == n
    by_field = est.by_field.set_index("field")
    assert by_field.loc[GROUP_B[0], "note"].startswith("no error")
    assert math.isnan(by_field.loc[GROUP_B[0], "ci_hi"])


def test_per_field_estimate_is_weighted_and_labeled_wide():
    est = audit.estimate(_values(HAND), HAND_DESIGN, n_boot=200)
    f0 = est.by_field.set_index("field").loc[GROUP_B[0]]
    # Field 0: L1 0 (w10), L2 0 (w10), M1 1 (w4), M2 0 (w4), H1 1 (w2), H2 1 (w2) -> 8 / 32.
    assert f0["error_weighted"] == pytest.approx(8 / 32)
    assert f0["note"] == "wide"
    assert f0["ci_lo"] <= f0["error_weighted"] <= f0["ci_hi"]
    f1 = est.by_field.set_index("field").loc[GROUP_B[1]]
    # Field 1: L2 abstained; L1 0 (w10), M1 0, M2 0 (w4), H1 1, H2 0 (w2) -> 2 / 22.
    assert f1["error_weighted"] == pytest.approx(2 / 22)
    assert f1["abstention_rate_weighted"] == pytest.approx(10 / 32)
    assert len(est.by_field) == 11


def test_minutes_by_mode():
    est = audit.estimate(_values(HAND), HAND_DESIGN, n_boot=50)
    assert _summary(est, "minutes_per_value", "blind")["estimate"] == 2.0
    assert _summary(est, "minutes_per_value", "shown")["estimate"] == 1.0
    assert _summary(est, "minutes_per_value", "all")["estimate"] == pytest.approx((6 * 2 + 6 * 1) / 12)


# ----------------------------------------------------------------------------- flags and curve


def test_error_by_n_flags_and_review_curve_unweighted():
    values = _values([("D1", "low", 1.0, "OCC", "blind", [1, 0, 1, 0])])
    values["n_flags"] = [2, 1, 1, 0]
    curve = audit.review_curve(values)
    knots = curve[curve["point"] == "knot"]
    assert knots["share_reviewed"].tolist() == pytest.approx([0.0, 0.25, 0.75, 1.0])
    assert knots["residual_error"].tolist() == pytest.approx([0.5, 0.25, 0.0, 0.0])
    ops = curve[curve["point"] == "operating_point"].set_index("share_reviewed")
    assert ops.loc[0.10, "residual_error"] == pytest.approx(0.4)
    assert ops.loc[0.25, "residual_error"] == pytest.approx(0.25)
    assert ops.loc[0.50, "residual_error"] == pytest.approx(0.125)
    assert ops["n_flags_block"].tolist() == [2, 2, 1]
    design = audit.AuditDesign({"D1": "low"}, {"low": 1, "mid": 1, "high": 1}, {"low": 1, "mid": 1, "high": 1})
    est = audit.estimate(values, design, n_boot=20)
    assert _summary(est, "error_by_n_flags", "n_flags=1")["estimate"] == 0.5
    assert _summary(est, "error_by_n_flags", "n_flags=2")["estimate"] == 1.0


def test_review_curve_uses_design_weights_and_skips_abstentions():
    values = _values([
        ("A", "low", 3.0, "OCC", "blind", [1]),
        ("B", "high", 1.0, "OCC", "shown", [1, None]),
    ])
    values["n_flags"] = [1, 0, 0]
    curve = audit.review_curve(values)
    knots = curve[curve["point"] == "knot"]
    assert knots["share_reviewed"].tolist() == pytest.approx([0.0, 0.75, 1.0])
    assert knots["residual_error"].tolist() == pytest.approx([1.0, 0.25, 0.0])
    ops = curve[curve["point"] == "operating_point"].set_index("share_reviewed")
    assert ops.loc[0.50, "residual_error"] == pytest.approx(0.5)


def test_add_flags_restricts_disagreement_to_the_pair(design):
    doc = HIGH[0]
    rows = _fill(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM))
    preds = _preds(design.doc_ids)
    rules = _preds([doc], system="rules", primary_product="small_farm")
    other = _preds([doc], system="llm:ollama:other", avg_ltd_ratio_pct="10.0")
    preds = [
        p.model_copy(update={"notes": "locator_miss"}) if (p.doc_id == doc and p.field == "n_assessment_areas") else p
        for p in preds
    ]
    values = audit.score_audit(rows, preds + rules + other, design, SYSTEM)
    grounding = {(doc, "total_assets_usd", SYSTEM): "quote_not_found", (doc, "n_assessment_areas", SYSTEM): None}
    flagged = audit.add_flags(values, preds + rules + other, SYSTEM, grounding, compare_system="rules")
    one = flagged[flagged["doc_id"] == doc].set_index("field")["n_flags"]
    assert one["primary_product"] == 1          # S1 disagrees
    assert one["avg_ltd_ratio_pct"] == 0        # a third system is not part of the pair
    assert one["total_assets_usd"] == 1         # grounding failure
    assert one["n_assessment_areas"] == 1       # locator miss
    assert flagged[flagged["doc_id"] != doc]["n_flags"].sum() == 0


# ----------------------------------------------------------------------------- CLI


def _write_predictions(path: Path, preds: list[FieldValue]) -> None:
    cols = [*FieldValue.model_fields, "grounding_failure", "locator_miss"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for p in preds:
            row = {k: ("" if v is None else v) for k, v in p.model_dump(mode="json").items()}
            row.update(grounding_failure="", locator_miss="0")
            w.writerow(row)


def test_cli_sheet_validate_estimate(tmp_path, capsys):
    record = _record(LOW, MID, HIGH, (20, 16, 4))
    strata_path = tmp_path / "strata.yaml"
    strata_path.write_text(yaml.safe_dump({"version": 1, "splits": {"audit": record}}), encoding="utf-8")
    design = audit.design_from_record(record)
    pred_path = tmp_path / "predictions_corpus.csv"
    _write_predictions(pred_path, _preds(sorted(design.doc_ids), primary_product="small_farm")
                       + _preds(sorted(design.doc_ids), system="rules"))
    sheet = tmp_path / "audit_sheet.csv"
    base = ["--strata", str(strata_path)]

    assert audit.main([*base, "sheet", "--system", SYSTEM, "--predictions", str(pred_path), "--out", str(sheet)]) == 0
    assert audit.main([*base, "sheet", "--system", SYSTEM, "--predictions", str(pred_path), "--out", str(sheet)]) == 1
    assert audit.main([*base, "validate", str(sheet)]) == 1  # unlabeled sheet

    _write(_fill(label.read_sheet(sheet)), sheet)
    assert audit.main([*base, "validate", str(sheet)]) == 0
    out_dir = tmp_path / "results"
    code = audit.main([
        *base, "estimate", "--system", SYSTEM, "--sheet", str(sheet), "--predictions", str(pred_path),
        "--risk", str(tmp_path / "missing.csv"), "--risk-values", str(tmp_path / "missing_values.csv"),
        "--out-dir", str(out_dir), "--n-boot", "50",
    ])
    assert code == 0
    for name in ("audit_summary.csv", "audit_by_field.csv", "audit_by_tertile.csv", "review_curve_group_b.csv"):
        assert (out_dir / name).exists()
    summary = pd.read_csv(out_dir / "audit_summary.csv")
    head = summary[summary["quantity"] == "design_weighted_error"].iloc[0]
    # One wrong value (primary_product) on the first sorted document, FDIC_101 (low, w=10),
    # of 88 answered values: 10 / (2*11*10 + 4*11*4 + 2*11*2) = 10 / 440.
    assert sorted(design.doc_ids)[0] == "FDIC_101_20240110"
    assert head["estimate"] == pytest.approx(10 / 440, abs=1e-5)  # written with 5 decimals
    curve = pd.read_csv(out_dir / "review_curve_group_b.csv")
    assert curve.iloc[0]["residual_error"] == pytest.approx(head["estimate"], abs=1e-5)
    # The disagreeing value carries one flag, so reviewing the top 10 percent clears it.
    ops = curve[curve["point"] == "operating_point"].set_index("share_reviewed")
    assert ops.loc[0.1, "residual_error"] == pytest.approx(0.0, abs=1e-6)
    assert "design-weighted Group B error" in capsys.readouterr().out


def test_cli_estimate_refuses_invalid_sheet(tmp_path, capsys):
    record = _record(LOW, MID, HIGH, (20, 16, 4))
    strata_path = tmp_path / "strata.yaml"
    strata_path.write_text(yaml.safe_dump({"splits": {"audit": record}}), encoding="utf-8")
    design = audit.design_from_record(record)
    pred_path = tmp_path / "p.csv"
    _write_predictions(pred_path, _preds(design.doc_ids))
    sheet = _write(audit.build_sheet(design, _preds(design.doc_ids), SYSTEM), tmp_path / "s.csv")
    code = audit.main([
        "--strata", str(strata_path), "estimate", "--system", SYSTEM, "--sheet", str(sheet),
        "--predictions", str(pred_path), "--out-dir", str(tmp_path / "r"),
    ])
    assert code == 1
    assert "estimate refused" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()
