"""Tests for the report stage (``cra_extract.report``).

Every test builds a small synthetic workspace under ``tmp_path`` (manifest, FRB index,
prices, label sheets, traces and the ``results/`` inputs) so nothing reads the real corpus.
Table 1 inputs are produced by ``evaluate.score_predictions`` and ``evaluate.metrics_table``
so the report is tested against the evaluator's real column contract.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from cra_extract import cli, evaluate, report
from cra_extract.models import FieldValue, LabelRow, Trace

GEMINI = "llm:gemini:gemini-3.8-flash"
HAIKU = "llm:anthropic:claude-haiku-4-5"
MAKEFILE = Path(__file__).parent.parent / "Makefile"

MANIFEST_COLUMNS = [
    "doc_id", "regulator", "bank_id", "bank_name", "exam_date", "release_date", "method", "overall_rating",
    "lending_test_rating", "cd_test_rating", "total_assets_usd", "url", "sha256", "pages", "chars_per_page",
    "text_extractable", "split", "source_file_id", "notes", "prior_evaluation_date_index", "prior_rating_index",
]
# doc_id, regulator, bank_id, name, exam, release, method, overall, prior date, prior rating, extractable, split
DOCS = [
    ("OCC_1_20240101", "OCC", "1", "Alpha Bank", "2024-01-01", "2024-05-15", "small", "S", "2021-01-01", "S", "True", "test"),
    ("OCC_2_20240201", "OCC", "2", "Beta Bank", "2024-02-01", "2024-06-10", "isb", "NI", "2022-02-01", "S", "True", "test"),
    ("FDIC_3_20240301", "FDIC", "3", "Gamma Bank", "2024-03-01", "2024-08-01", "small", "SN", "2023-03-01", "NI", "True", "test"),
    ("FDIC_4_20240401", "FDIC", "4", "Delta Bank", "2024-04-01", "2024-03-01", "isb", "S", "", "", "True", "corpus"),
    ("FRB_5_20240501", "FRB", "5", "Epsilon Bank", "2024-05-01", "2024-08-20", "isb", "NI", "2020-05-01", "S", "True", "corpus"),
    ("FRB_6_20240601", "FRB", "6", "Zeta Bank", "2024-06-01", "2024-09-01", "isb", "S", "2019-06-01", "O", "True", "corpus"),
    ("FRB_7_20240701", "FRB", "7", "Eta Bank", "2024-07-01", "2024-10-01", "small", "NI", "2021-07-01", "S", "False", "excluded"),
]
FRB_INDEX = [
    # bank_id, exam_date, method, overall, lending, cd
    ("5", "2024-05-01", "isb", "NI", "S", "S"),
    ("6", "2024-06-01", "isb", "S", "S", "O"),
    ("7", "2024-07-01", "small", "NI", "", ""),
]


def _mkparent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def _labels(doc_id: str, regulator: str, method: str) -> list[LabelRow]:
    small = method == "small"
    rows = [
        LabelRow(doc_id=doc_id, field="regulator", value=regulator, minutes=0.5),
        LabelRow(doc_id=doc_id, field="overall_rating", value="S", minutes=0.5),
        LabelRow(doc_id=doc_id, field="n_assessment_areas", value="2", minutes=3.0),
        LabelRow(doc_id=doc_id, field="loan_products_evaluated", value="home_mortgage|small_business", minutes=4.0),
        LabelRow(doc_id=doc_id, field="finding_present", value="false", minutes=1.0),
        LabelRow(doc_id=doc_id, field="laws_cited", null_reason="not_applicable", minutes=0.5),
    ]
    if small:
        rows.append(LabelRow(doc_id=doc_id, field="cd_test_rating", null_reason="absent_from_document", minutes=0.5))
    else:
        rows.append(LabelRow(doc_id=doc_id, field="cd_test_rating", value="S", minutes=0.5))
    return rows


def _preds(doc_id: str, regulator: str, method: str) -> list[FieldValue]:
    out = []
    for system in ("rules", GEMINI):
        q = "quote" if system == GEMINI else None
        out += [
            FieldValue(doc_id=doc_id, field="regulator", system=system, value=regulator, quote=q),
            FieldValue(doc_id=doc_id, field="overall_rating", system=system, value="S" if doc_id != "FDIC_3_20240301" else "NI", quote=q),
            FieldValue(doc_id=doc_id, field="n_assessment_areas", system=system, value="2", quote=q),
            FieldValue(doc_id=doc_id, field="loan_products_evaluated", system=system, value="home_mortgage", quote=q),
            FieldValue(doc_id=doc_id, field="finding_present", system=system, value="false", quote=q),
            FieldValue(doc_id=doc_id, field="laws_cited", system=system, status="absent"),
        ]
        if system == GEMINI:
            # Fabricated CD rating on the Small documents (probe), correct on ISB.
            out.append(FieldValue(doc_id=doc_id, field="cd_test_rating", system=system, value="S", quote=q))
        elif method == "isb":
            out.append(FieldValue(doc_id=doc_id, field="cd_test_rating", system=system, value="S"))
        else:
            out.append(FieldValue(doc_id=doc_id, field="cd_test_rating", system=system, status="absent"))
    return out


def _trace(doc_id: str, group: str, backend: str, model: str, tin: int, tout: int, cost: float, i: int) -> str:
    t = Trace(
        trace_id=f"t{i:04d}", doc_id=doc_id, field_group=group, backend=backend, model=model, prompt_hash=f"h{i}",
        input_tokens=tin, output_tokens=tout, cost_usd=cost, called_at=datetime(2026, 10, 15, 12, 0, 0),
    )
    return t.model_dump_json()


@pytest.fixture()
def ws(tmp_path: Path) -> report.ReportPaths:
    """A complete synthetic workspace; returns its ReportPaths."""
    root = tmp_path / "ws"
    paths = report.ReportPaths.under(root)
    manifest_rows = []
    for d in DOCS:
        manifest_rows.append({
            "doc_id": d[0], "regulator": d[1], "bank_id": d[2], "bank_name": d[3], "exam_date": d[4],
            "release_date": d[5], "method": d[6], "overall_rating": d[7], "url": "u", "sha256": "abc",
            "text_extractable": d[10], "split": d[11], "prior_evaluation_date_index": d[8], "prior_rating_index": d[9],
        })
    _write_csv(paths.manifest, MANIFEST_COLUMNS, manifest_rows)
    _write_csv(paths.frb_index, ["regulator", "bank_id", "exam_date", "method", "overall_rating", "lending_test_rating", "cd_test_rating"],
               [{"regulator": "FRB", "bank_id": b, "exam_date": e, "method": m, "overall_rating": o,
                 "lending_test_rating": lt, "cd_test_rating": c} for b, e, m, o, lt, c in FRB_INDEX])
    paths.prices.write_text(
        "anthropic:\n  retrieved: 2026-09-25\n  models:\n    claude-haiku-4-5: { input: 1.00, output: 5.00 }\n"
        "gemini:\n  retrieved: null\n  models:\n    gemini-3.8-flash: { input: null, output: null }\n"
        "manual_baseline:\n  wage_usd_per_hour: null\n",
        encoding="utf-8",
    )
    test_docs = [d for d in DOCS if d[11] == "test"]
    labels = [lab for d in test_docs for lab in _labels(d[0], d[1], d[6])]
    preds = [p for d in test_docs for p in _preds(d[0], d[1], d[6])]
    grounding = {(p.doc_id, p.field, p.system): None for p in preds if p.status == "value"}
    scored = evaluate.score_predictions(labels, preds, grounding_column=grounding)
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    evaluate.metrics_table(scored).to_csv(paths.inp("table1_test.csv"), index=False, float_format="%.5f")
    scored.to_csv(paths.inp("scored_test.csv"), index=False)
    gold = pd.DataFrame([{"doc_id": lab.doc_id, "field": lab.field, "minutes": lab.minutes} for lab in labels])
    paths.gold_dir.mkdir(parents=True, exist_ok=True)
    gold.to_csv(paths.gold_dir / "labels_test.csv", index=False)
    # audit.py summary rows (minutes per value by mode).
    pd.DataFrame({"quantity": ["design_weighted_error", "minutes_per_value", "minutes_per_value", "minutes_per_value"],
                  "stratum": ["all", "blind", "shown", "all"], "estimate": [0.1, 2.5, 1.5, 2.0]}).to_csv(
        paths.inp("audit_summary.csv"), index=False)

    # Review-curve inputs in the formats audit.py and corpus_scoring.py write, calibration, reliability.
    curve = report.review_curve([2, 2, 1, 0], [1, 0, 1, 0], [1.0, 1.0, 2.0, 4.0])
    knots = curve.rename(columns={"n_flags": "n_flags_block"}).assign(point="knot", note="")
    ops = pd.DataFrame({"point": "operating_point", "share_reviewed": [0.1, 0.25, 0.5],
                        "residual_error": [0.99, 0.99, 0.99], "n_flags_block": [2, 2, 1], "note": ""})
    pd.concat([knots, ops])[["point", "share_reviewed", "residual_error", "n_flags_block", "note"]].to_csv(
        paths.inp("review_curve_group_b.csv"), index=False, float_format="%.5f")
    pd.DataFrame({"truth": ["raw", "raw", "raw", "triaged"], "system": "s5", "by": ["n_flags", "n_flags", "flag:disagreement", "n_flags"],
                  "level": [0, 1, 1, 0], "n": [90, 10, 5, 90], "n_error": [1, 4, 2, 0]}).to_csv(
        paths.inp("risk_validation_group_a.csv"), index=False)
    pd.DataFrame({"system": [GEMINI, GEMINI], "n_flags": [0, 2], "n": [20, 5], "error_rate": [0.05, 0.6],
                  "ece": [0.1, 0.1], "ece_se": [0.02, 0.02]}).to_csv(_mkparent(paths.calibration("test")), index=False)
    pd.DataFrame({"field": ["n_assessment_areas", "avg_ltd_ratio_pct", "overall_rating"], "n": [8, 12, 8],
                  "n_agree": [7, 12, 8]}).to_csv(paths.inp("reliability.csv"), index=False)

    # Run stage outputs: traces, run log, predictions with locator_miss.
    tdir = paths.trace_dir / "test"
    tdir.mkdir(parents=True, exist_ok=True)
    lines, i = [], 0
    for d in test_docs:
        for group, tin in (("cover_and_rating", 1000), ("institution", 3000), ("lending_tables", 2000), ("review", 1500)):
            i += 1
            whole = d[0] == "OCC_1_20240101" and group == "lending_tables"
            lines.append(_trace(d[0], group, "gemini", "gemini-3.8-flash", 16000 if whole else tin, 200, 0.0, i))
    # A superseded earlier call of one pair: counts as spent, not in tokens per document.
    i += 1
    lines.insert(0, _trace("OCC_1_20240101", "review", "gemini", "gemini-3.8-flash", 99999, 1, 0.5, i))
    (tdir / "gemini.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    hl = []
    for d in test_docs[:2]:
        for group in ("cover_and_rating", "institution", "lending_tables", "review"):
            i += 1
            hl.append(_trace(d[0], group, "anthropic", "claude-haiku-4-5", 1000, 100, 0.0015, i))
    (tdir / "anthropic.jsonl").write_text("\n".join(hl) + "\n", encoding="utf-8")
    log = []
    for d in test_docs:
        log.append({"doc_id": d[0], "system": "rules", "seconds": 0.5, "error": None})
        log.append({"doc_id": d[0], "system": GEMINI, "seconds": 10.0, "error": None, "n_locator_fallbacks": 0})
    log.append({"doc_id": "OCC_1_20240101", "system": GEMINI, "seconds": 12.0, "error": None, "n_locator_fallbacks": 1})
    paths.inp("run_log_test.jsonl").write_text("\n".join(json.dumps(e) for e in log) + "\n", encoding="utf-8")
    pred_rows = []
    groups = {"cover_and_rating": "overall_rating", "institution": "n_assessment_areas",
              "lending_tables": "avg_ltd_ratio_pct", "review": "finding_present"}
    for d in test_docs:
        for system in (GEMINI, HAIKU):
            for group, field in groups.items():
                miss = int(system == GEMINI and d[0] == "OCC_1_20240101" and group == "lending_tables")
                pred_rows.append({"doc_id": d[0], "field": field, "system": system, "locator_miss": miss})
    _write_csv(paths.inp("predictions_test.csv"), ["doc_id", "field", "system", "locator_miss"], pred_rows)

    _write_csv(paths.inp("data_quality_findings.csv"), report.TABLE2_COLUMNS, [
        {"source": "Z source", "field": "f1", "n_affected": "3", "example_doc": "X", "root_cause": "source_error",
         "recommendation": "Fix | carefully", "found_on": "2026-10-05"},
        {"source": "A source", "field": "f2", "n_affected": "1", "example_doc": "Y", "root_cause": "source_error",
         "recommendation": "r", "found_on": "2026-10-05"},
        {"source": "M source", "field": "f3", "n_affected": "", "example_doc": "", "root_cause": "definitional",
         "recommendation": "line\nbreak", "found_on": "2026-10-06"},
    ])
    _write_csv(paths.inp("census_positives.csv"), ["doc_id", "finding_type", "rating_effect", "laws_cited"], [
        {"doc_id": "FDIC_3_20240301", "finding_type": "discrimination", "rating_effect": "substantive_lowered", "laws_cited": "FHA"},
        {"doc_id": "FRB_6_20240601", "finding_type": "other_illegal_credit_practice", "rating_effect": "substantive_lowered",
         "laws_cited": "FTC_s5_UDAP"},
        {"doc_id": "OCC_1_20240101", "finding_type": "discrimination", "rating_effect": "technical_only_noted", "laws_cited": ""},
    ])
    paths.matrix_floor_triage.write_text("FRB_5_20240501:\n  root_cause: source_error\n  note: synthetic\n", encoding="utf-8")
    return paths


def _csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


# ----------------------------------------------------------------------------- formatting


def test_cell_and_pct_formatting() -> None:
    assert report.cell(None) == ""
    assert report.cell(math.nan) == ""
    assert report.cell(True) == "true"
    assert report.cell(3) == "3"
    assert report.cell(-0.0) == "0.0000"
    assert report.cell(0.123456) == "0.1235"
    assert report.pct(0.9333) == "93.3"
    assert report.pct_ci(29 / 30, 0.8333, 0.9941) == "96.7 [83.3, 99.4]"
    assert report.pct_ci(0.5, math.nan, 0.6) == "50.0"
    assert report.pct("") == ""


def test_md_table_escapes_pipes_and_newlines() -> None:
    md = report.md_table(["a", "b"], [["x|y", "1\n2"]])
    assert md.splitlines()[2] == "| x\\|y | 1 2 |"


# ----------------------------------------------------------------------------- review curve


def test_review_curve_unweighted_blocks() -> None:
    # 10 values: 2 with 2 flags (1 error), 3 with 1 flag (1 error), 5 with 0 flags (1 error).
    flags = [2, 2, 1, 1, 1, 0, 0, 0, 0, 0]
    err = [1, 0, 1, 0, 0, 1, 0, 0, 0, 0]
    c = report.review_curve(flags, err)
    assert c["share_reviewed"].tolist() == pytest.approx([0.0, 0.2, 0.5, 1.0])
    assert c["residual_error"].tolist() == pytest.approx([0.3, 0.2, 0.1, 0.0])
    # Linear inside a block: halfway through the 1-flag block.
    assert report.residual_at(c, 0.35) == pytest.approx(0.15)
    assert report.residual_at(c, 0.10) == pytest.approx(0.25)


def test_review_curve_design_weights() -> None:
    c = report.review_curve([1, 0], [1, 1], weights=[3.0, 1.0])
    assert c["share_reviewed"].tolist() == pytest.approx([0.0, 0.75, 1.0])
    assert c["residual_error"].tolist() == pytest.approx([1.0, 0.25, 0.0])
    assert report.review_curve([], []).empty


def test_load_curve_frame_reads_audit_and_corpus_scoring_formats(ws: report.ReportPaths) -> None:
    curves, missing = report.load_curves(ws)
    assert missing == []
    b = next(c for c in curves if c.group == "B")
    assert len(b.knots) == 4  # operating-point rows of audit.py are not knots
    assert report.residual_at(b.knots, 0.25) == pytest.approx(0.25)  # 3/8 at 0, 1/8 removed by the 2-flag block
    a = next(c for c in curves if c.group == "A")
    assert a.system == "s5"
    assert a.knots["residual_error"].tolist() == pytest.approx([0.05, 0.01, 0.0])


def test_load_curve_frame_schemas() -> None:
    pre = pd.DataFrame({"share_reviewed": ["0.5", "0", "1"], "residual_error": ["0.1", "0.2", "0"]})
    (c,) = report.load_curve_frame(pre, "B")
    assert c.knots["share_reviewed"].tolist() == [0.0, 0.5, 1.0]
    counts = pd.DataFrame({"system": ["s1", "s1", "s2"], "n_flags": ["0", "1", "0"], "n": ["90", "10", "4"],
                           "n_error": ["1", "4", "2"]})
    curves = report.load_curve_frame(counts, "A")
    assert [x.system for x in curves] == ["s1", "s2"]
    assert curves[0].knots["residual_error"].tolist() == pytest.approx([0.05, 0.01, 0.0])
    with pytest.raises(ValueError, match="expected"):
        report.load_curve_frame(pd.DataFrame({"x": [1]}), "A")


# ----------------------------------------------------------------------------- matrix floor


def test_isb_floor_follows_appendix_a_paragraph_d3() -> None:
    # Appendix A to 12 CFR part 345, (d)(3): both tests at least satisfactory -> an overall
    # NI or SN is not explained by the tests (floor S; outstanding is optional); any test
    # below satisfactory -> NI or SN both compatible (floor SN).
    assert len(report.ISB_OVERALL_FLOOR) == 16
    for (lt, cd), floor in report.ISB_OVERALL_FLOOR.items():
        both_ok = report.RATING_RANK[lt] >= report.RATING_RANK["S"] and report.RATING_RANK[cd] >= report.RATING_RANK["S"]
        assert floor == ("S" if both_ok else "SN")
    assert report.isb_overall_floor("S", "O") == "S"
    assert report.isb_overall_floor("O", "O") == "S"
    assert report.isb_overall_floor("S", "NI") == "SN"
    assert report.isb_overall_floor("", "O") is None
    assert "12 CFR 345.28" in report.isb_overall_floor.__doc__
    assert "verified" in report.isb_overall_floor.__doc__


# ----------------------------------------------------------------------------- Table 1


def test_table1_sections_and_columns(ws: report.ReportPaths) -> None:
    out = report.table1("test", ws)
    t = _csv(out["table1_csv"])
    assert list(t.columns) == report.TABLE1_COLUMNS
    assert set(t["section"]) == {"Group A (smoke test)", "Component ratings", "Group B"}
    assert not t["field"].isin(["finding_present", "laws_cited"]).any()
    # Rows follow fields.yaml order, then system.
    assert t["field"].tolist()[:2] == ["regulator", "regulator"]
    assert t["system"].tolist()[:2] == [GEMINI, "rules"]
    over = t[(t["field"] == "overall_rating") & (t["system"] == "rules")].iloc[0]
    assert over["n"] == "3" and over["accuracy"] == "0.6667"
    # Non-quoting systems: error rate without grounding, never a hallucination rate.
    assert over["hallucination_rate"] == "" and over["grounded_rate"] == ""
    assert over["error_rate_no_grounding"] == "0.3333"
    g = t[(t["field"] == "overall_rating") & (t["system"] == GEMINI)].iloc[0]
    assert g["hallucination_rate"] == "0.3333" and g["grounded_rate"] == "1.0000"
    assert g["error_rate_no_grounding"] == ""
    # Component ratings: fabrication moved to the probe table.
    cd = t[(t["field"] == "cd_test_rating") & (t["system"] == GEMINI)].iloc[0]
    assert cd["fabrication_rate"] == "" and "table_probes" in cd["note"]
    lp = t[(t["field"] == "loan_products_evaluated") & (t["system"] == "rules")].iloc[0]
    assert lp["precision"] == "1.0000" and lp["recall"] == "0.5000" and "element-level" in lp["note"]


def test_table1_probes_appendix_and_grounding(ws: report.ReportPaths) -> None:
    out = report.table1("test", ws)
    p = _csv(out["probes_csv"])
    cd = p[(p["field"] == "cd_test_rating") & (p["system"] == GEMINI)].iloc[0]
    # Two Small test documents (OCC_1, FDIC_3) are the applicability ABS cells.
    assert cd["probe_basis"] == "applicability ABS cells"
    assert (cd["n_probe"], cd["n_fabrication"], cd["n_probe_abstain"]) == ("2", "2", "0")
    assert cd["fabrication_rate"] == "1.0000"
    rules_cd = p[(p["field"] == "cd_test_rating") & (p["system"] == "rules")].iloc[0]
    assert rules_cd["n_fabrication"] == "0"
    laws = p[(p["field"] == "laws_cited") & (p["system"] == GEMINI)].iloc[0]
    assert laws["section"] == "Group C (fabrication probe)" and laws["n_probe"] == "3" and laws["n_fabrication"] == "0"
    # Only rating_before, rating_after and laws_cited are Group C probes (plan, Field groups).
    assert not p["field"].isin(["finding_present"]).any()
    assert laws["probe_basis"] == "documents with gold finding_present false"
    a = _csv(out["appendix_csv"])
    lp = a[(a["field"] == "loan_products_evaluated") & (a["system"] == "rules")].iloc[0]
    assert lp["overall_accuracy"] == "0.0000" and lp["recall_overall"] == "0.5000"
    gr = _csv(out["grounding_csv"])
    assert set(gr["system"]) == {GEMINI}
    md = out["table1_md"].read_text(encoding="utf-8")
    assert "smoke test" in md and "see probe table" in md and "\n\n| Section |" in md


def test_table1_probe_fallback_without_scored(ws: report.ReportPaths) -> None:
    ws.inp("scored_test.csv").unlink()
    out = report.table1("test", ws)
    p = _csv(out["probes_csv"])
    cd = p[(p["field"] == "cd_test_rating") & (p["system"] == GEMINI)].iloc[0]
    assert cd["probe_basis"] == "all gold-null items" and cd["n_probe_abstain"] == ""
    a = _csv(out["appendix_csv"])
    assert "not available" in a[a["field"] == "loan_products_evaluated"].iloc[0]["recall_overall_basis"]


def test_table1_missing_input_raises(ws: report.ReportPaths) -> None:
    with pytest.raises(FileNotFoundError):
        report.table1("dev", ws)


def _scored_with_variants(ws: report.ReportPaths) -> pd.DataFrame:
    """Test scored rows plus a :whole run on OCC_1 and OCC_2 and a :closedbook run."""
    test_docs = [d for d in DOCS if d[11] == "test"]
    labels = [lab for d in test_docs for lab in _labels(d[0], d[1], d[6])]
    preds = [p for d in test_docs for p in _preds(d[0], d[1], d[6])]
    whole = GEMINI + ":whole"
    for d in test_docs[:2]:
        for p in _preds(d[0], d[1], d[6]):
            if p.system == GEMINI:
                v = p.model_copy(update={"system": whole})
                if p.field == "n_assessment_areas" and d[0] == "OCC_1_20240101":
                    v = v.model_copy(update={"value": "3"})  # whole wrong, located right
                preds.append(v)
    closed = GEMINI + ":closedbook"
    for d in test_docs:
        preds.append(FieldValue(doc_id=d[0], field="overall_rating", system=closed,
                                value="S" if d[0] != "FDIC_3_20240301" else "abstain_marker"))
        preds.append(FieldValue(doc_id=d[0], field="finding_present", system=closed, value="false"))
    preds = [p if p.value != "abstain_marker" else p.model_copy(update={"status": "abstain", "value": None})
             for p in preds]
    grounding = {(p.doc_id, p.field, p.system): ("quote_not_found" if p.system == closed else None)
                 for p in preds if p.status == "value"}
    scored = evaluate.score_predictions(labels, preds, grounding_column=grounding)
    evaluate.metrics_table(scored).to_csv(ws.inp("table1_test.csv"), index=False, float_format="%.5f")
    scored.to_csv(ws.inp("scored_test.csv"), index=False)
    return scored


def test_table1_leaves_out_the_whole_and_closed_book_systems(ws: report.ReportPaths) -> None:
    _scored_with_variants(ws)
    out = report.table1("test", ws)
    for key in ("table1_csv", "probes_csv", "appendix_csv", "grounding_csv"):
        systems = set(_csv(out[key])["system"])
        assert not {s for s in systems if s.endswith((":whole", ":closedbook"))}, key


def test_ablation_table_compares_located_and_whole_on_the_same_documents(ws: report.ReportPaths) -> None:
    _scored_with_variants(ws)
    preds = _csv(ws.inp("predictions_test.csv"))
    extra = pd.DataFrame([{"doc_id": d, "field": "n_assessment_areas", "system": GEMINI + ":whole", "locator_miss": "na"}
                          for d in ("OCC_1_20240101", "OCC_2_20240201")])
    pd.concat([preds, extra]).to_csv(ws.inp("predictions_test.csv"), index=False)
    out = report.ablation("test", ws)
    t = _csv(out["csv"]).set_index("field")
    aa = t.loc["n_assessment_areas"]
    assert aa["located_system"] == GEMINI and aa["whole_system"] == GEMINI + ":whole"
    assert aa["n_docs"] == "2"  # the whole run's documents only
    assert (aa["located_correct"], aa["located_n"], aa["whole_correct"], aa["whole_n"]) == ("2", "2", "1", "2")
    assert (aa["n_both_answered"], aa["located_only_correct"], aa["whole_only_correct"]) == ("2", "1", "0")
    assert "Discordant" in out["md"].read_text(encoding="utf-8")


def test_closed_book_table_with_majority_class(ws: report.ReportPaths) -> None:
    _scored_with_variants(ws)
    out = report.closed_book("test", ws)
    t = _csv(out["csv"]).set_index("field")
    ov = t.loc["overall_rating"]
    # Gold overall_rating is S on all three test documents; the closed-book run answers S twice
    # and abstains once.
    assert (ov["n_gold_value"], ov["n_answered"], ov["n_correct"], ov["n_abstain"]) == ("3", "2", "2", "1")
    assert ov["accuracy_answered"] == "1.0000" and ov["accuracy_overall"] == "0.6667"
    assert ov["majority_class"] == "s" and ov["majority_class_rate"] == "1.0000"
    fp = t.loc["finding_present"]
    assert fp["accuracy_overall"] == "1.0000" and fp["majority_class"] == "false"
    written, _skipped = report.build_all("test", ws, figures=False)
    assert ws.out("table_ablation_test.csv") in written and ws.out("table_closed_book_test.csv") in written


def test_group_c_probes_are_restricted_to_finding_present_false(ws: report.ReportPaths) -> None:
    scored = _csv(ws.inp("scored_test.csv"))
    # OCC_1 now has a finding: its laws_cited is not a probe item.
    sel = (scored["doc_id"] == "OCC_1_20240101") & (scored["field"] == "finding_present")
    scored.loc[sel, "gold_value"] = "true"
    scored.to_csv(ws.inp("scored_test.csv"), index=False)
    p = _csv(report.table1("test", ws)["probes_csv"])
    laws = p[(p["field"] == "laws_cited") & (p["system"] == GEMINI)].iloc[0]
    assert laws["n_probe"] == "2"


# ----------------------------------------------------------------------------- Figure 1


def test_figure1_outputs_and_metadata(ws: report.ReportPaths) -> None:
    out = report.figure1("test", ws)
    png = out["png"].read_bytes()
    svg = out["svg"].read_text(encoding="utf-8")
    assert png.startswith(b"\x89PNG") and b"Software" not in png
    assert "<dc:date>" not in svg and "dc:creator" not in svg
    cap = out["caption"].read_text(encoding="utf-8")
    assert "Missing inputs: none." in cap
    assert "calibration inset (test split)" in cap and "manual-review error line" in cap
    assert f"pair: {GEMINI}" in cap
    data = _csv(out["data"])
    ops = data[(data["series"] == "review_curve_group_B") & (data["kind"] == "operating_point")]
    assert ops["x"].tolist() == ["0.100000", "0.250000", "0.500000"]
    manual = data[data["series"] == "manual_review_error"].iloc[0]
    # Pooled over the Group B rows only (overall_rating is Group A): 1 - 19 / 20.
    assert float(manual["y"]) == pytest.approx(1 - 19 / 20)


def test_figure1_missing_inputs_listed(tmp_path: Path) -> None:
    paths = report.ReportPaths.under(tmp_path)
    out = report.figure1("test", paths)
    cap = out["caption"].read_text(encoding="utf-8")
    assert "Drawn: nothing" in cap
    for name in ("review_curve_group_b.csv", "risk_validation_group_a.csv", "calibration_test.csv", "reliability.csv"):
        assert name in cap
    assert out["png"].exists() and out["svg"].exists()


# ----------------------------------------------------------------------------- Table 2


def test_table2_sorted_by_root_cause_then_source(ws: report.ReportPaths) -> None:
    out = report.table2(ws)
    t = _csv(out["csv"])
    assert t["source"].tolist() == ["M source", "A source", "Z source"]
    md = out["md"].read_text(encoding="utf-8")
    assert "Fix \\| carefully" in md and "line break" in md


# ----------------------------------------------------------------------------- Table 3


def test_table3_tokens_fallback_and_cost(ws: report.ReportPaths) -> None:
    out = report.table3("test", ws)
    t = _csv(out["csv"]).set_index("system")
    g = t.loc[GEMINI]
    assert g["n_docs"] == "3" and g["n_calls"] == "12" and g["n_calls_whole_document"] == "1"
    assert g["locator_fallback_rate"] == f"{1 / 12:.4f}"
    assert float(g["input_tokens_per_call_whole_document"]) == 16000
    assert float(g["input_tokens_per_call_located"]) == pytest.approx((3 * 1000 + 3 * 3000 + 2 * 2000 + 3 * 1500) / 11)
    assert float(g["input_tokens_per_doc"]) == pytest.approx((7500 * 2 + 21500) / 3)
    assert g["price_status"] == "price not set" and g["cost_per_doc_usd"] == ""
    assert g["cost_spent_usd"] == "0.5000"  # the superseded call
    assert g["corpus_cost_basis"] == "price not set"
    assert float(g["wall_clock_s"]) == pytest.approx(32.0)  # last run-log entry per document
    h = t.loc[HAIKU]
    assert h["price_status"] == "priced" and h["prices_retrieved"] == "2026-09-25"
    per_doc = 4 * (1000 * 1.0 + 100 * 5.0) / 1e6
    assert float(h["cost_per_doc_usd"]) == pytest.approx(per_doc)
    assert float(h["corpus_cost_usd"]) == pytest.approx(per_doc * 6)  # 6 evaluable documents
    assert h["n_docs"] == "2" and h["n_calls_mode_unknown"] == "0"
    r = t.loc["rules"]
    assert r["price_status"] == "no model calls" and r["n_correct"] != ""
    m = t.loc["manual (gold label timers)"]
    assert m["price_status"] == "wage not set" and m["cost_per_doc_usd"] == "wage not set"
    assert float(m["minutes_per_doc"]) == pytest.approx(10.0)
    rev = _csv(out["review_csv"])
    assert rev["share_reviewed"].tolist() == ["0.1000", "0.2500", "0.5000"]
    assert rev["review_cost_usd"].tolist() == ["wage not set"] * 3
    assert rev["reviewer_minutes_per_value"].iloc[0] == "1.5000"  # shown mode of audit_summary.csv
    assert rev["n_values"].iloc[0] == str(6 * 11)
    curve = report.review_curve([2, 2, 1, 0], [1, 0, 1, 0], [1.0, 1.0, 2.0, 4.0])
    assert float(rev["residual_error"].iloc[1]) == pytest.approx(report.residual_at(curve, 0.25), abs=1e-4)
    md = out["md"].read_text(encoding="utf-8")
    assert "wage not set" in md and "corpus tier: not chosen" in md


def test_table3_with_wage_and_corpus_system(ws: report.ReportPaths) -> None:
    text = ws.prices.read_text(encoding="utf-8").replace("wage_usd_per_hour: null", "wage_usd_per_hour: 30")
    ws.prices.write_text(text, encoding="utf-8")
    out = report.table3("test", ws, corpus_system=HAIKU)
    rev = _csv(out["review_csv"])
    reviewed = round(0.1 * 66)
    review_cost = reviewed * 1.5 / 60 * 30
    extraction = 4 * (1000 * 1.0 + 100 * 5.0) / 1e6 * 6
    assert float(rev["review_cost_usd"].iloc[0]) == pytest.approx(review_cost, abs=1e-4)
    assert float(rev["total_cost_usd"].iloc[0]) == pytest.approx(review_cost + extraction, abs=1e-4)
    m = _csv(out["csv"]).set_index("system").loc["manual (gold label timers)"]
    assert float(m["cost_per_doc_usd"]) == pytest.approx(10.0 / 60 * 30)
    man = _csv(out["manual_csv"])
    assert man["field"].iloc[0] == "regulator"


def test_table3_reads_corpus_traces_as_spent(ws: report.ReportPaths) -> None:
    cdir = ws.trace_dir / "corpus"
    cdir.mkdir(parents=True)
    (cdir / "anthropic.jsonl").write_text(_trace("FDIC_4_20240401", "review", "anthropic", "claude-haiku-4-5", 10, 1, 0.25, 999) + "\n")
    t = _csv(report.table3("test", ws)["csv"]).set_index("system")
    assert t.loc[HAIKU, "corpus_cost_usd"] == "0.2500"
    assert t.loc[HAIKU, "corpus_cost_basis"] == "spent (corpus traces)"


def test_cost_attributes_whole_and_closed_book_traces_to_their_systems(ws: report.ReportPaths) -> None:
    """Interleaved located, whole and closed-book traces of one document and group."""
    def tr(group: str, tin: int, mode: str | None, i: int) -> str:
        t = Trace(trace_id=f"x{i}", doc_id="OCC_1_20240101", field_group=group, backend="gemini",
                  model="gemini-3.8-flash", prompt_hash=f"x{i}", input_tokens=tin, output_tokens=10,
                  cost_usd=0.0, called_at=datetime(2026, 10, 15, 12, 0, 0), mode=mode)
        return t.model_dump_json()

    traces = [Trace.model_validate_json(x) for x in (
        tr("institution", 2000, "located", 1), tr("institution", 40000, "whole", 2), tr("closedbook", 100, None, 3))]
    latest = report.latest_traces(traces)
    got = sorted((t.field_group, t.input_tokens, report.trace_system(t)) for t in latest)
    assert got == [("closedbook", 100, GEMINI + ":closedbook"), ("institution", 2000, GEMINI),
                   ("institution", 40000, GEMINI + ":whole")]
    tdir = ws.trace_dir / "test"
    (tdir / "gemini.jsonl").write_text("\n".join(
        [tr("institution", 2000, "located", 1), tr("institution", 40000, "whole", 2), tr("closedbook", 100, None, 3)])
        + "\n", encoding="utf-8")
    t = _csv(report.table3("test", ws)["csv"]).set_index("system")
    assert float(t.loc[GEMINI, "input_tokens_per_doc"]) == 2000
    assert t.loc[GEMINI + ":whole", "n_calls"] == "1" and t.loc[GEMINI + ":whole", "n_calls_whole_document"] == "1"
    assert float(t.loc[GEMINI + ":closedbook", "input_tokens_per_doc"]) == 100


def test_manual_baseline_includes_blind_audit_minutes(ws: report.ReportPaths) -> None:
    pd.DataFrame([
        {"doc_id": "FDIC_4_20240401", "field": "avg_ltd_ratio_pct", "mode": "blind", "minutes": "4"},
        {"doc_id": "FDIC_4_20240401", "field": "n_assessment_areas", "mode": "blind", "minutes": "2"},
        {"doc_id": "FRB_5_20240501", "field": "n_assessment_areas", "mode": "shown", "minutes": "1"},
    ]).to_csv(ws.gold_dir / "audit_sheet.csv", index=False)
    out = report.table3("test", ws)
    man = _csv(out["manual_csv"])
    audit_rows = man[man["source"] == "audit sheet, blind mode"].set_index("field")
    assert audit_rows.loc["avg_ltd_ratio_pct", "mean_minutes"] == "4.0000"
    assert audit_rows.loc["n_assessment_areas", "n_rows_timed"] == "1"
    assert "audit_sheet.csv (blind mode" in out["md"].read_text(encoding="utf-8")


def test_review_values_use_answered_group_b_values_when_known(ws: report.ReportPaths) -> None:
    pd.DataFrame([{"doc_id": f"d{i}", "field": "avg_ltd_ratio_pct", "group": "B", "answered": i % 4 != 0}
                  for i in range(40)] + [{"doc_id": "d0", "field": "overall_rating", "group": "A", "answered": True}]
                 ).to_csv(ws.inp("risk_values_corpus.csv"), index=False)
    rev = _csv(report.table3("test", ws)["review_csv"])
    assert rev["n_values"].iloc[0] == "30"
    assert "answered Group B values" in rev["note"].iloc[0]


def test_check_rejects_a_bad_table1_name_without_a_traceback(ws: report.ReportPaths, capsys) -> None:
    assert report.main(["--check", str(ws.inp("table1.csv")), "--results-dir", str(ws.results_dir)]) == 1
    assert "expected a path ending in table1_{split}.csv" in capsys.readouterr().err


# ----------------------------------------------------------------------------- descriptive


def test_descriptive_decomposition_and_registry(ws: report.ReportPaths) -> None:
    out = report.descriptive(ws)
    d = _csv(out["decomposition"])
    assert list(d.columns) == report.DECOMP_COLUMNS
    allrow = d[(d["regulator"] == "all") & (d["method"] == "all")].iloc[0]
    # Evaluable: FRB_7 is not text-extractable. Low ratings: OCC_2 NI, FDIC_3 SN, FRB_5 NI.
    assert (allrow["n_evaluable"], allrow["n_low"], allrow["n_ni"], allrow["n_sn"]) == ("6", "3", "2", "1")
    assert allrow["n_compliance_driven"] == "1" and allrow["n_performance_driven"] == "2"
    lo, hi = report.clopper_pearson_ci(1, 3)
    assert allrow["share_lo"] == f"{lo:.4f}" and allrow["share_hi"] == f"{hi:.4f}"
    reg = _csv(out["registry"])
    assert reg["doc_id"].tolist() == ["FDIC_3_20240301", "FRB_6_20240601", "OCC_1_20240101"]
    assert reg.iloc[0]["bank_name"] == "Gamma Bank" and reg.iloc[0]["rating_lowered"] == "true"
    assert reg.iloc[2]["rating_lowered"] == "false"


def test_descriptive_matrix_floor(ws: report.ReportPaths) -> None:
    out = report.descriptive(ws)
    f = _csv(out["matrix_floor"]).set_index("doc_id")
    assert f.index.tolist() == ["FRB_5_20240501", "FRB_6_20240601"]
    assert f.loc["FRB_5_20240501", "floor"] == "S" and f.loc["FRB_5_20240501", "below_floor"] == "true"
    assert f.loc["FRB_5_20240501", "status"] == "below_floor_not_lowered"
    assert f.loc["FRB_5_20240501", "root_cause"] == "source_error"
    assert f.loc["FRB_6_20240601", "status"] == "lowered_not_below_floor"


def test_descriptive_without_census(ws: report.ReportPaths) -> None:
    ws.inp("census_positives.csv").unlink()
    out = report.descriptive(ws)
    assert "registry" not in out
    d = _csv(out["decomposition"])
    assert set(d["note"]) == {"census_positives.csv not available"} and set(d["n_compliance_driven"]) == {""}
    f = _csv(out["matrix_floor"])
    assert set(f["status"]) == {"census_not_available"}
    assert "Missing inputs: census_positives.csv" in out["md"].read_text(encoding="utf-8")


def test_descriptive_transitions_cycle_and_lag(ws: report.ReportPaths) -> None:
    out = report.descriptive(ws)
    t = _csv(out["transitions"])
    s_to_ni = t[(t["regulator"] == "all") & (t["prior_rating"] == "S") & (t["overall_rating"] == "NI")].iloc[0]
    assert s_to_ni["n"] == "3"  # OCC_2, FRB_5, FRB_7 (index facts use the whole manifest)
    none = t[(t["regulator"] == "all") & (t["prior_rating"] == "none") & (t["overall_rating"] == "S")].iloc[0]
    assert none["n"] == "1" and none["row_share"] == "1.0000"
    c = _csv(out["cycle"])
    ni = c[(c["regulator"] == "all") & (c["prior_rating"] == "NI")].iloc[0]
    assert ni["n"] == "1" and float(ni["median_days"]) == 366.0
    lag = _csv(out["lag"])
    fdic = lag[(lag["regulator"] == "FDIC") & (lag["overall_rating"] == "all")].iloc[0]
    assert fdic["median_days"] == "" and fdic["precision"].startswith("month")
    assert fdic["n_negative"] == "1"  # FDIC_4: release month before the exam month
    occ = lag[(lag["regulator"] == "OCC") & (lag["overall_rating"] == "S")].iloc[0]
    assert occ["median_months"] == "4.0000" and occ["median_days"] == "135.0000"


# ----------------------------------------------------------------------------- language, determinism, check


def _all_text_outputs(paths: report.ReportPaths) -> list[Path]:
    return sorted(p for p in paths.out_dir.iterdir() if p.suffix in (".md", ".txt"))


def test_generated_text_has_no_comparative_language(ws: report.ReportPaths) -> None:
    report.build_all("test", ws)
    pattern = re.compile(r"\b(" + "|".join(report.COMPARATIVE_WORDS) + r")\b", re.IGNORECASE)
    for p in _all_text_outputs(ws):
        # Table cells copied from data files (Table 2 recommendations, bank names) are data, not generated text.
        generated = [line for line in p.read_text(encoding="utf-8").splitlines() if not line.startswith("|")]
        for line in generated:
            assert not pattern.search(line), f"{p.name}: {line}"
        assert "\u2014" not in p.read_text(encoding="utf-8")


def test_build_all_is_byte_identical_across_runs(ws: report.ReportPaths, tmp_path: Path) -> None:
    a = dataclasses.replace(ws, out_dir=tmp_path / "a")
    b = dataclasses.replace(ws, out_dir=tmp_path / "b")
    wa, skipped = report.build_all("test", a, corpus_system=GEMINI)
    wb, _ = report.build_all("test", b, corpus_system=GEMINI)
    assert not skipped
    names = sorted(p.name for p in wa)
    assert names == sorted(p.name for p in wb)
    for name in ("table1_test_report.csv", "figure1.png", "figure1.svg", "table3_test.csv", "descriptive.md"):
        assert name in names
    for name in names:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


def test_csv_outputs_have_no_timestamps(ws: report.ReportPaths) -> None:
    written, _ = report.build_all("test", ws, figures=False)
    stamp = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
    for p in written:
        if p.suffix == ".csv":
            assert not stamp.search(p.read_text(encoding="utf-8")), p.name


def test_check_passes_then_detects_a_change(ws: report.ReportPaths) -> None:
    report.build_all("test", ws)
    t1 = ws.inp("table1_test.csv")
    assert report.check(t1, ws) == []
    md = ws.inp("table2.md")
    md.write_text(md.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
    problems = report.check(t1, ws)
    assert len(problems) == 1 and "table2.md" in problems[0] and "differs" in problems[0]
    ws.inp("table3_test.csv").unlink()
    assert any("not committed" in p for p in report.check(t1, ws))
    with pytest.raises(ValueError):
        report.check(ws.inp("table1.csv"), ws)


def test_main_check_exit_codes(ws: report.ReportPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "ReportPaths", lambda: ws)
    args = ["--results-dir", str(ws.results_dir), "--no-figures"]
    assert report.main(["all", "--split", "test", *args]) == 0
    assert report.main(["--check", str(ws.inp("table1_test.csv")), *args]) == 0
    ws.inp("desc_publication_lag.csv").write_text("x\n", encoding="utf-8")
    assert report.main(["--check", str(ws.inp("table1_test.csv")), *args]) == 1
    assert report.main(["table1", "--split", "dev", *args]) == 1


def test_cli_and_makefile_wiring() -> None:
    assert cli.STAGES["report"][0] == "cra_extract.report"
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^report:\n\t\$\(PY\) -m cra_extract\.report --split \$\(REPORT_SPLIT\)", text, re.MULTILINE)
