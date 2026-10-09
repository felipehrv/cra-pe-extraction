"""Tests for Group A corpus scoring and the corpus risk writer (src/cra_extract/corpus_scoring.py)."""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from cra_extract import corpus_scoring as cs
from cra_extract import evaluate, routing, strata
from cra_extract.config import load_fields

S3 = "llm:gemini:test"
SYSTEMS = routing.Systems(s1="rules", s3=S3)
COLUMNS = [
    "doc_id", "regulator", "bank_id", "bank_name", "exam_date", "release_date", "method",
    "overall_rating", "lending_test_rating", "cd_test_rating", "sha256", "text_extractable", "split",
    "prior_evaluation_date_index", "prior_rating_index",
]


def doc(regulator: str, n: int, method: str = "small", year: str = "2024", split: str = "corpus",
        **kw: str) -> dict[str, str]:
    row = {
        "doc_id": f"{regulator}_{n}_20240101", "regulator": regulator, "bank_id": str(n),
        "bank_name": f"Bank {n}", "exam_date": "2024-01-01", "release_date": f"{year}-05-01",
        "method": method, "overall_rating": "S", "lending_test_rating": "", "cd_test_rating": "",
        "sha256": "ab" * 32, "text_extractable": "True", "split": split,
        "prior_evaluation_date_index": "2021-01-04", "prior_rating_index": "S",
    }
    row.update(kw)
    return row


def manifest(rows: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def frame(rows: list[tuple[str, ...]]) -> pd.DataFrame:
    """rows: (doc_id, field, system, status, value[, grounding_failure[, locator_miss]])."""
    out = []
    for r in rows:
        d, f, s, st, v, *rest = r
        out.append({"doc_id": d, "field": f, "system": s, "status": st, "value": v,
                    "grounding_failure": rest[0] if rest else "", "locator_miss": rest[1] if len(rest) > 1 else "0",
                    "notes": ""})
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------- score


@pytest.fixture()
def scored() -> pd.DataFrame:
    m = manifest([
        doc("OCC", 1, "isb", "2023"), doc("OCC", 2, "isb", "2023"), doc("OCC", 3, "isb", "2023"),
        doc("OCC", 4, "small", "2025", exam_date="2024-01-01"),
        doc("FRB", 5, "small", "2024"),
        doc("FRB", 6, "isb", "2024", lending_test_rating="S", cd_test_rating="O"),
        doc("FDIC", 7, "small", "2024"),
        doc("FDIC", 8, "small", "2024", split="excluded"),
        doc("OCC", 9, "isb", "2024", split="test"),
    ])
    p = frame([
        ("OCC_1_20240101", "overall_rating", "rules", "value", "S"),
        ("OCC_2_20240101", "overall_rating", "rules", "value", "NI"),
        ("OCC_3_20240101", "overall_rating", "rules", "abstain", ""),
        ("OCC_1_20240101", "overall_rating", S3, "value", "Satisfactory"),
        ("OCC_4_20240101", "exam_date", "rules", "value", "2023-12-01"),
        ("OCC_4_20240101", "exam_date", S3, "value", "2024-01-01"),
        ("OCC_1_20240101", "exam_method", "rules", "value", "isb"),
        ("OCC_1_20240101", "exam_method", S3, "value", "small"),
        ("FRB_5_20240101", "cd_test_rating", "rules", "absent", ""),
        ("FRB_5_20240101", "cd_test_rating", S3, "value", "S"),
        ("FRB_6_20240101", "cd_test_rating", "rules", "value", "O"),
        ("FRB_6_20240101", "cd_test_rating", S3, "value", "S"),
        ("FDIC_7_20240101", "exam_date", "rules", "value", "2023-12-28"),
        ("FDIC_8_20240101", "overall_rating", "rules", "value", "S"),
        ("OCC_9_20240101", "overall_rating", "rules", "value", "S"),
    ])
    triage = pd.DataFrame([
        {"doc_id": "OCC_4_20240101", "field": "exam_date", "category": "source_error",
         "rule_value": "2023-12-01", "correct_value": "2023-12-01"},
        {"doc_id": "OCC_1_20240101", "field": "exam_method", "category": "definitional",
         "rule_value": "isb", "correct_value": "mixed"},
        {"doc_id": "OCC_2_20240101", "field": "overall_rating", "category": "our_error",
         "rule_value": "NI", "correct_value": "S"},
    ])
    return cs.score(m, p, systems=SYSTEMS, triage=triage)


def _row(t: pd.DataFrame, **kw: str) -> pd.Series:
    sel = pd.Series(True, index=t.index)
    defaults = {"truth": "raw", "scope": "all", "release_year": cs.POOLED}
    for k, v in {**defaults, **kw}.items():
        sel &= t[k] == v
    rows = t[sel]
    assert len(rows) == 1, kw
    return rows.iloc[0]


def test_accuracy_among_non_abstained_with_truth(scored):
    r = _row(scored, system="rules", field="overall_rating", regulator="OCC", method="isb")
    assert (r["n_correct"], r["n_wrong"], r["n_abstain"]) == (2, 1, 1)  # OCC_9 (test) is evaluable
    assert r["accuracy_n"] == 3 and r["accuracy"] == pytest.approx(2 / 3)
    assert r["abstention_rate"] == pytest.approx(1 / 4)
    assert 0 < r["accuracy_lo"] < r["accuracy"] < r["accuracy_hi"] <= 1
    # S3 has a row for one OCC isb document only: the others are abstentions
    s3 = _row(scored, system=S3, field="overall_rating", regulator="OCC", method="isb")
    assert s3["n_correct"] == 1 and s3["n_abstain"] == 3


def test_abs_cell_fabrication_and_component_truth(scored):
    small = _row(scored, system=S3, field="cd_test_rating", regulator="FRB", method="small")
    assert small["n_fabrication"] == 1 and small["fabrication_denominator"] == 1
    assert small["fabrication_rate"] == 1.0
    assert pd.isna(small["accuracy"])
    s1 = _row(scored, system="rules", field="cd_test_rating", regulator="FRB", method="small")
    assert s1["n_correct_null"] == 1 and s1["fabrication_rate"] == 0.0
    isb = _row(scored, system=S3, field="cd_test_rating", regulator="FRB", method="isb")
    assert isb["n_wrong"] == 1 and isb["fabrication_denominator"] == 0
    occ = _row(scored, system="rules", field="cd_test_rating", regulator="OCC", method="isb")
    assert occ["n_truth_value"] == 0 and occ["n_no_truth"] == 4


def test_fdic_exam_date_no_truth_and_excluded_rows_dropped(scored):
    r = _row(scored, system="rules", field="exam_date", regulator="FDIC", method="small")
    assert r["n_truth_value"] == 0 and r["n_no_truth"] == 1 and r["n_docs"] == 1
    assert pd.isna(r["accuracy"])
    ov = _row(scored, system="rules", field="overall_rating", regulator="FDIC", method="small")
    assert ov["n_docs"] == 1  # FDIC_8 is excluded


def test_cells_18_plus_pooled(scored):
    block = scored[(scored["truth"] == "raw") & (scored["scope"] == "all") & (scored["system"] == "rules")
                   & (scored["field"] == "overall_rating")]
    years = block[block["release_year"] != cs.POOLED]
    assert len(years) == 18
    assert sorted(years["release_year"].unique()) == ["2023", "2024", "2025"]
    assert len(block[block["release_year"] == cs.POOLED]) == 6
    cell = _row(scored, system="rules", field="overall_rating", regulator="OCC", method="isb", release_year="2023")
    assert cell["n_truth_value"] == 3


def test_triaged_truth(scored):
    raw = _row(scored, system="rules", field="exam_date", regulator="OCC", method="small")
    tri = _row(scored, truth="triaged", system="rules", field="exam_date", regulator="OCC", method="small")
    assert raw["n_wrong"] == 1 and tri["n_correct"] == 1        # source_error: printed value is truth
    # The triage covers S1 disagreements only, so the triaged block is S1's alone.
    assert set(scored.loc[scored["truth"] == "triaged", "system"]) == {"rules"}
    assert scored.loc[scored["truth"] == "triaged", "note"].str.contains("S1-triaged").all()
    assert scored["in_sample"].all()
    raw_m = _row(scored, system="rules", field="exam_method", regulator="OCC", method="isb")
    tri_m = _row(scored, truth="triaged", system="rules", field="exam_method", regulator="OCC", method="isb")
    assert raw_m["n_truth_value"] - tri_m["n_truth_value"] == 1  # definitional dropped
    ours = _row(scored, truth="triaged", system="rules", field="overall_rating", regulator="OCC", method="isb")
    assert ours["n_wrong"] == 1                                 # our_error keeps the index truth


def test_test_scope_and_consistency_flag(scored):
    t = _row(scored, scope="test", system="rules", field="overall_rating", regulator="OCC", method="isb")
    assert t["n_correct"] == 1 and t["n_docs"] == 1
    assert _row(scored, system=S3, field="exam_method", regulator="OCC", method="isb")["consistency_check"]
    assert not _row(scored, system="rules", field="exam_method", regulator="OCC", method="isb")["consistency_check"]
    assert not _row(scored, system=S3, field="overall_rating", regulator="OCC", method="isb")["consistency_check"]


def test_s5_consistency_check_follows_the_routed_source():
    m = manifest([doc("OCC", 1)])
    rows = [("OCC_1_20240101", "exam_method", "s5", "value", "small"),
            ("OCC_1_20240101", "regulator", "s5", "value", "OCC")]
    p = frame(rows)
    p["notes"] = ["s5_from=rules", f"s5_from={S3}"]
    t = cs.score(m, p, systems=SYSTEMS)
    assert not _row(t, system="s5", field="exam_method", regulator="OCC", method="small")["consistency_check"]
    assert _row(t, system="s5", field="regulator", regulator="OCC", method="small")["consistency_check"]


def test_score_without_triage_has_raw_only():
    m = manifest([doc("OCC", 1)])
    t = cs.score(m, frame([("OCC_1_20240101", "overall_rating", "rules", "value", "S")]), systems=SYSTEMS)
    assert set(t["truth"]) == {"raw"} and set(t["system"]) == {"rules"} and set(t["scope"]) == {"all"}


# ----------------------------------------------------------------------------- risk


def _risk_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    m = manifest([doc("OCC", 1, "isb"), doc("OCC", 2, "isb"), doc("OCC", 3, "isb")])
    rows = []
    for d in ("OCC_1_20240101", "OCC_2_20240101", "OCC_3_20240101"):
        rows += [(d, "overall_rating", "rules", "value", "S"), (d, "overall_rating", S3, "value", "S"),
                 (d, "n_assessment_areas", "rules", "value", "2"), (d, "n_assessment_areas", S3, "value", "2")]
    rows += [
        # within 1 percent of S3: no disagreement; 2 percent: disagreement
        ("OCC_1_20240101", "total_assets_usd", "rules", "value", "1005000"),
        ("OCC_1_20240101", "total_assets_usd", S3, "value", "1000000", "", "0"),
        ("OCC_2_20240101", "total_assets_usd", "rules", "value", "1020000"),
        ("OCC_2_20240101", "total_assets_usd", S3, "value", "1000000", "quote_does_not_support", "1"),
        ("OCC_3_20240101", "total_assets_usd", S3, "value", "1000000", "", "1"),
        # consistency on the system's own values
        ("OCC_1_20240101", "n_full_scope", S3, "value", "3"),
        ("OCC_1_20240101", "lending_data_period_end", S3, "value", "2024-06-30"),
        ("OCC_2_20240101", "lending_data_period_end", S3, "value", "2023-12-31"),
        ("OCC_2_20240101", "avg_ltd_ratio_pct", S3, "abstain", "", "", "1"),
        # a Group A disagreement, not in doc_risk
        ("OCC_3_20240101", "bank_name", "rules", "value", "Bank 3"),
        ("OCC_3_20240101", "bank_name", S3, "value", "Other Name"),
    ]
    p = frame(rows)
    sections = pd.DataFrame([
        # OCC_1: institution sections missing -> locator_miss although the column says 0
        {"doc_id": "OCC_1_20240101", "section": "description_of_institution", "found": "False"},
        {"doc_id": "OCC_1_20240101", "section": "scope_of_evaluation", "found": "False"},
        {"doc_id": "OCC_1_20240101", "section": "cover", "found": "True"},
        # OCC_2: found -> no locator_miss although the column says 1
        {"doc_id": "OCC_2_20240101", "section": "description_of_institution", "found": "True"},
        {"doc_id": "OCC_2_20240101", "section": "ltd_ratio", "found": "True"},
        # OCC_3 is not in the sections file: the column decides
    ])
    routes = {f: "s3" for f in routing.routed_fields()}
    routes["overall_rating"] = "s1"
    p = pd.concat([routing.normalize_predictions(p), routing.apply(p, routes, m, SYSTEMS)], ignore_index=True)
    return m, p, sections


def test_risk_flags_for_s5():
    m, p, sections = _risk_inputs()
    risk = cs.risk_values(p, SYSTEMS, "s5", sections).set_index(["doc_id", "field"])
    a1 = risk.loc[("OCC_1_20240101", "total_assets_usd")]
    assert bool(a1["locator_miss"]) and not bool(a1["disagreement"]) and a1["reference_system"] == "rules"
    a2 = risk.loc[("OCC_2_20240101", "total_assets_usd")]
    assert not bool(a2["locator_miss"]) and bool(a2["disagreement"]) and bool(a2["groundedness_fail"])
    assert a2["n_flags"] == 2
    a3 = risk.loc[("OCC_3_20240101", "total_assets_usd")]
    assert bool(a3["locator_miss"]) and not bool(a3["disagreement"])  # S1 did not answer
    assert bool(risk.loc[("OCC_1_20240101", "n_full_scope"), "consistency_fail"])
    assert bool(risk.loc[("OCC_1_20240101", "lending_data_period_end"), "consistency_fail"])
    assert not bool(risk.loc[("OCC_2_20240101", "lending_data_period_end"), "consistency_fail"])
    abst = risk.loc[("OCC_2_20240101", "avg_ltd_ratio_pct")]
    assert not bool(abst["answered"]) and abst["n_flags"] == 0
    # S1-routed field: reference is S3
    assert risk.loc[("OCC_1_20240101", "overall_rating"), "reference_system"] == S3
    assert bool(risk.loc[("OCC_3_20240101", "bank_name"), "disagreement"])


def test_risk_documents_sum_group_b_only():
    _, p, sections = _risk_inputs()
    docs = cs.risk_documents(cs.risk_values(p, SYSTEMS, "s5", sections)).set_index("doc_id")
    # four answered institution-group values with locator_miss, plus two consistency flags
    assert docs.loc["OCC_1_20240101", "doc_risk"] == 6
    assert docs.loc["OCC_1_20240101", "n_locator_miss"] == 4
    assert docs.loc["OCC_2_20240101", "doc_risk"] == 2  # disagreement + groundedness
    assert docs.loc["OCC_3_20240101", "doc_risk"] == 1  # bank_name disagreement is Group A
    assert docs.loc["OCC_2_20240101", "n_abstain"] >= 1
    assert docs.loc["OCC_1_20240101", "n_consistency_fail"] == 2


def test_reference_for_s1_and_s3():
    _, p, _ = _risk_inputs()
    r3 = cs.risk_values(p, SYSTEMS, S3).set_index(["doc_id", "field"])
    assert set(r3["reference_system"]) == {"rules"}
    assert bool(r3.loc[("OCC_2_20240101", "total_assets_usd"), "disagreement"])
    r1 = cs.risk_values(p, SYSTEMS, "rules").set_index(["doc_id", "field"])
    assert set(r1["reference_system"]) == {S3}
    # without a sections file the predictions column decides
    assert bool(r3.loc[("OCC_2_20240101", "total_assets_usd"), "locator_miss"])
    with pytest.raises(ValueError):
        cs.risk_values(p, SYSTEMS, "nobody")


def test_write_risk_files_and_strata_reader(tmp_path):
    m, p, sections = _risk_inputs()
    paths = cs.write_risk(p, SYSTEMS, "s5", sections, m, None,
                          tmp_path / "risk.csv", tmp_path / "values.csv", tmp_path / "val.csv")
    docs = pd.read_csv(paths["risk"])
    assert docs.columns[:2].tolist() == ["doc_id", "doc_risk"]
    assert docs.columns.tolist() == cs.RISK_DOC_COLUMNS
    risk = strata.read_two_column_csv(paths["risk"], "doc_risk", float)
    assert risk == {"OCC_1_20240101": 6.0, "OCC_2_20240101": 2.0, "OCC_3_20240101": 1.0}
    val = pd.read_csv(paths["validation"])
    assert set(val["truth"]) == {"raw"}
    by = val[val["by"] == "n_flags"].set_index("level")
    # n_flags 1: OCC_2 overall_rating (correct; cover and rating not in its sections rows, so
    # locator_miss) and OCC_3 bank_name (wrong, disagreement); n_flags 0: two correct ratings
    assert by.loc[1, "n"] == 2 and by.loc[1, "n_error"] == 1
    assert by.loc[0, "n"] == 2 and by.loc[0, "n_error"] == 0
    flag = val[(val["by"] == "flag:disagreement") & (val["level"] == 1)].iloc[0]
    assert flag["error_rate"] == 1.0


def test_risk_flags_match_evaluate_consistency():
    # the consistency flag is evaluate.consistency_fail, not a copy
    spec = load_fields()["fields"]["n_full_scope"]
    assert evaluate.consistency_fail("n_full_scope", "3", "OCC_1_20240101", {"n_assessment_areas": "2"}.get, spec)


def test_section_misses_per_field_group():
    sections = pd.DataFrame([
        {"doc_id": "D", "section": "cover", "found": "True"},
        {"doc_id": "D", "section": "aa_concentration", "found": "False"},
    ])
    miss = cs.section_misses(sections)
    assert miss[("D", "overall_rating")] is False
    assert miss[("D", "avg_ltd_ratio_pct")] is True
    assert miss[("D", "finding_present")] is True


def test_cli_score_and_risk(tmp_path):
    m, p, sections = _risk_inputs()
    mp, pp, sp = tmp_path / "corpus.csv", tmp_path / "pred.csv", tmp_path / "sections.csv"
    m.to_csv(mp, index=False)
    p.to_csv(pp, index=False)
    sections.to_csv(sp, index=False)
    common = ["--predictions", str(pp), "--manifest", str(mp), "--routing", str(tmp_path / "none.yaml"),
              "--triage", str(tmp_path / "none.csv"), "--s3", S3,
              "--gold-dir", str(tmp_path / "gold"), "--gate-out", str(tmp_path / "gate.csv")]
    assert cs.main(["score", *common, "--out", str(tmp_path / "g.csv")]) == 0
    assert (tmp_path / "gate.csv").exists()
    assert set(pd.read_csv(tmp_path / "g.csv")["system"]) == {"rules", S3, "s5"}
    assert cs.main(["risk", *common, "--sections", str(sp), "--out", str(tmp_path / "r.csv"),
                    "--values-out", str(tmp_path / "v.csv"), "--validation-out", str(tmp_path / "w.csv")]) == 0
    assert len(pd.read_csv(tmp_path / "r.csv")) == 3


def test_no_function_accepts_gold_labels():
    for module in (cs, routing):
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if fn.__module__ != module.__name__:
                continue
            params = set(inspect.signature(fn).parameters)
            assert not {x for x in params if "label" in x or "gold" in x}, f"{name}: {params}"


def test_risk_values_never_passes_labels(monkeypatch):
    seen = {}
    original = cs.risk_table

    def spy(scored, preds, labels=None):
        seen["labels"] = labels
        return original(scored, preds, labels)

    monkeypatch.setattr(cs, "risk_table", spy)
    _, p, sections = _risk_inputs()
    cs.risk_values(p, SYSTEMS, "s5", sections)
    assert seen["labels"] is None
