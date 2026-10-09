"""Tests for S5 routing (src/cra_extract/routing.py) and the shared index truth of group_a."""

from __future__ import annotations

import inspect

import pandas as pd
import pytest
import yaml

from cra_extract import corpus_scoring, group_a, routing
from cra_extract.config import field_spec
from cra_extract.evaluate import values_match

MANIFEST_COLUMNS = [
    "doc_id", "regulator", "bank_id", "bank_name", "exam_date", "release_date", "method",
    "overall_rating", "lending_test_rating", "cd_test_rating", "sha256", "text_extractable", "split",
    "prior_evaluation_date_index", "prior_rating_index",
]


def doc(regulator: str, n: int, method: str = "small", split: str = "corpus", **kw: str) -> dict[str, str]:
    """One manifest row; ``kw`` overrides any column."""
    row = {
        "doc_id": f"{regulator}_{n}_20240101", "regulator": regulator, "bank_id": str(n),
        "bank_name": f"Bank {n}", "exam_date": "2024-01-01", "release_date": "2024-05-01",
        "method": method, "overall_rating": "S", "lending_test_rating": "", "cd_test_rating": "",
        "sha256": "ab" * 32, "text_extractable": "True", "split": split,
        "prior_evaluation_date_index": "2021-01-04", "prior_rating_index": "S",
    }
    row.update(kw)
    return row


def manifest(rows: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(dict.fromkeys([*MANIFEST_COLUMNS, *(k for r in rows for k in r)])))


def preds(rows: list[tuple[str, str, str, str, str]]) -> pd.DataFrame:
    """rows: (doc_id, field, system, status, value)."""
    return pd.DataFrame(rows, columns=["doc_id", "field", "system", "status", "value"])


def s1_rows(m: pd.DataFrame, fields: list[str] | None = None) -> list[tuple[str, str, str, str, str]]:
    """S1 rows equal to the index for every Group A field."""
    out = []
    for r in m.to_dict("records"):
        values = {
            "regulator": r["regulator"], "bank_name": r["bank_name"], "bank_id": r["bank_id"],
            "exam_date": r["exam_date"], "exam_method": r["method"], "overall_rating": r["overall_rating"],
            "prior_evaluation_date": r["prior_evaluation_date_index"], "prior_rating": r["prior_rating_index"],
        }
        for f, v in values.items():
            if fields is None or f in fields:
                out.append((r["doc_id"], f, "rules", "value", v))
    return out


# ----------------------------------------------------------------------------- index truth


def test_fdic_exam_date_has_no_truth_without_source_column():
    m = manifest([doc("FDIC", 1), doc("OCC", 2), doc("FRB", 3)])
    t = group_a.index_truth(m, ["exam_date"]).set_index("doc_id")
    assert t.loc["FDIC_1_20240101", "truth"] == "none"
    assert "file-id" in t.loc["FDIC_1_20240101", "truth_reason"]
    assert t.loc["OCC_2_20240101", "truth"] == "value"
    assert t.loc["FRB_3_20240101", "truth"] == "value"


def test_exam_date_source_column_decides_when_present():
    m = manifest([
        doc("FDIC", 1, exam_date_source="fdic_file_id"),
        doc("FDIC", 2, exam_date_source="override"),
        doc("FDIC", 3, exam_date_source=""),
        doc("OCC", 4, exam_date_source="index_exam_date"),
        doc("OCC", 5, exam_date_source=""),
    ])
    t = group_a.index_truth(m, ["exam_date"]).set_index("doc_id")["truth"].to_dict()
    # Without the gate no FDIC row has exam_date truth, the hand override included, so the
    # exam_date route rests on OCC and FRB documents only.
    assert t == {
        "FDIC_1_20240101": "none", "FDIC_2_20240101": "none", "FDIC_3_20240101": "none",
        "OCC_4_20240101": "value", "OCC_5_20240101": "value",
    }
    gated = group_a.index_truth(m, ["exam_date"], fdic_exam_date_truth=True).set_index("doc_id")["truth"].to_dict()
    assert gated == dict.fromkeys(t, "value")
    m2 = manifest([doc("OCC", 6, exam_date_source="file_id")])
    assert group_a.index_truth(m2, ["exam_date"])["truth"].tolist() == ["none"]


def _gold(path, rows):
    pd.DataFrame(rows, columns=["doc_id", "field", "value", "null_reason"]).to_csv(path, index=False)


def test_fdic_exam_date_gate(tmp_path):
    m = manifest([
        doc("FDIC", 1, split="dev"), doc("FDIC", 2, split="test", exam_date="2024-01-01"),
        doc("OCC", 3, split="test"), doc("FDIC", 4),
    ])
    status, table = group_a.fdic_exam_date_gate(m, tmp_path)
    assert status == "pending" and table.empty
    _gold(tmp_path / "labels_dev.csv", [("FDIC_1_20240101", "exam_date", "January 1, 2024", "")])
    status, table = group_a.fdic_exam_date_gate(m, tmp_path)
    assert status == "pending" and table["match"].tolist() == [True]
    _gold(tmp_path / "labels_test.csv", [("FDIC_2_20240101", "exam_date", "2024-01-01", ""),
                                         ("OCC_3_20240101", "exam_date", "1999-01-01", "")])
    status, table = group_a.fdic_exam_date_gate(m, tmp_path)
    assert status == "pass" and table["doc_id"].tolist() == ["FDIC_1_20240101", "FDIC_2_20240101"]
    _gold(tmp_path / "labels_test.csv", [("FDIC_2_20240101", "exam_date", "2023-12-28", "")])
    status, table = group_a.fdic_exam_date_gate(m, tmp_path)
    assert status == "fail" and table["match"].tolist() == [True, False]


def test_apply_triage_on_a_filtered_frame_edits_the_right_row():
    m = manifest([doc("OCC", i) for i in range(1, 4)] + [doc("FRB", i) for i in range(1, 4)])
    truth = group_a.index_truth(m, ["overall_rating"])
    filtered = truth[truth["regulator"] == "FRB"]  # labels 3, 4, 5
    tri = pd.DataFrame([{"doc_id": "FRB_3_20240101", "field": "overall_rating",
                         "category": "source_error", "rule_value": "NI"}])
    out = group_a.apply_triage(filtered, tri).set_index("doc_id")
    assert out.loc["FRB_3_20240101", "index_value"] == "NI"
    assert out.loc["FRB_2_20240101", "index_value"] == "S"
    assert out.loc["FRB_1_20240101", "truth_reason"] == "index"


def test_component_truth_by_cell_and_prior_rows():
    m = manifest([
        doc("FRB", 1, "isb", lending_test_rating="S", cd_test_rating="O"),
        doc("FRB", 2, "small"),
        doc("OCC", 3, "small"),
        doc("FDIC", 4, "isb", prior_evaluation_date_index="", prior_rating_index=""),
    ])
    t = group_a.index_truth(m).set_index(["doc_id", "field"])
    assert t.loc[("FRB_1_20240101", "cd_test_rating"), ["truth", "index_value"]].tolist() == ["value", "O"]
    assert t.loc[("FRB_2_20240101", "lending_test_rating"), "truth"] == "absent_from_document"
    assert t.loc[("OCC_3_20240101", "cd_test_rating"), "truth"] == "absent_from_document"
    # OCC Small lending is printed (equals overall) but not in the index
    assert t.loc[("OCC_3_20240101", "lending_test_rating"), "truth"] == "none"
    assert t.loc[("FDIC_4_20240101", "lending_test_rating"), "truth"] == "none"
    assert t.loc[("FDIC_4_20240101", "prior_rating"), "truth_reason"] == "no previous index row"


def test_compare_uses_field_tolerance():
    m = manifest([doc("OCC", 7, bank_id="0007", bank_name="First Bank, N.A.")])
    t = group_a.index_truth(m, ["bank_name", "bank_id", "exam_date", "overall_rating"])
    p = preds([
        ("OCC_7_20240101", "bank_name", "rules", "value", "First Bank NA"),
        ("OCC_7_20240101", "bank_id", "rules", "value", "7"),
        ("OCC_7_20240101", "exam_date", "rules", "value", "January 1, 2024"),
        ("OCC_7_20240101", "overall_rating", "rules", "value", "Satisfactory"),
    ])
    c = group_a.compare_with_index(p, t, ["rules"]).set_index("field")
    assert (c["cell"] == "correct").all()
    for f, v in [("bank_name", "First Bank NA"), ("bank_id", "7"), ("exam_date", "January 1, 2024")]:
        idx = c.loc[f, "index_value"]
        assert values_match(idx, v, field_spec(f))


def test_compare_missing_row_is_abstention_and_absent_is_fabricated_absence():
    m = manifest([doc("OCC", 1), doc("OCC", 2)])
    t = group_a.index_truth(m, ["overall_rating"])
    p = preds([("OCC_1_20240101", "overall_rating", "rules", "absent", "")])
    c = group_a.compare_with_index(p, t, ["rules"]).set_index("doc_id")
    assert c.loc["OCC_1_20240101", "cell"] == "fabricated_absence"
    assert c.loc["OCC_2_20240101", "cell"] == "abstention"


def test_join_agree_uses_values_match():
    m = manifest([doc("OCC", 7, bank_id="0007")]).assign(source_file_id="x")
    rules = preds([("OCC_7_20240101", "exam_date", "rules", "value", "January 1, 2024")]).assign(
        quote="", page="", notes="")
    j = group_a.join(rules, m)
    assert bool(j.iloc[0]["agree"])


# ----------------------------------------------------------------------------- compute


def test_population_excludes_dev_test_excluded_and_non_evaluable():
    m = manifest([
        doc("OCC", 1), doc("OCC", 2, split="dev"), doc("OCC", 3, split="test"),
        doc("OCC", 4, split="excluded"), doc("OCC", 5, sha256=""), doc("OCC", 6, text_extractable="False"),
        doc("OCC", 8, split="audit"),
    ])
    assert routing.routing_population(m)["doc_id"].tolist() == ["OCC_1_20240101"]
    table = routing.compute(m, preds(s1_rows(m))).set_index("field")
    assert table.loc["overall_rating", "n_index"] == 1


def _rating_case(n_docs: int, n_answered: int, n_wrong: int) -> pd.DataFrame:
    m = manifest([doc("OCC", i) for i in range(1, n_docs + 1)])
    rows = []
    for i in range(1, n_docs + 1):
        if i <= n_answered:
            rows.append((f"OCC_{i}_20240101", "overall_rating", "rules", "value", "NI" if i <= n_wrong else "S"))
        else:
            rows.append((f"OCC_{i}_20240101", "overall_rating", "rules", "abstain", ""))
    return routing.compute(m, preds(rows)).set_index("field").loc["overall_rating"]


@pytest.mark.parametrize(
    ("answered", "wrong", "route"),
    [
        (95, 1, "s1"),    # coverage 0.95 exactly, agreement 94/95 = 0.989
        (94, 0, "s3"),    # coverage 0.94
        (100, 2, "s1"),   # agreement 0.98 exactly
        (100, 3, "s3"),   # agreement 0.97
    ],
)
def test_thresholds_inclusive(answered, wrong, route):
    r = _rating_case(100, answered, wrong)
    assert r["route"] == route
    assert r["n_s1_value"] == answered and r["n_agree"] == answered - wrong


def test_fdic_exam_date_routed_on_other_regulators_and_components_only_frb_isb():
    m = manifest([
        doc("FDIC", 1), doc("FDIC", 2),
        doc("OCC", 3),
        doc("FRB", 4, "isb", lending_test_rating="S", cd_test_rating="S"),
        doc("FDIC", 5, "isb"),
    ])
    rows = s1_rows(m)
    rows += [("FRB_4_20240101", "lending_test_rating", "rules", "value", "S"),
             ("FRB_4_20240101", "cd_test_rating", "rules", "value", "Satisfactory"),
             ("FDIC_5_20240101", "cd_test_rating", "rules", "value", "NI")]
    # FDIC exam dates disagree with the file-id date; they must not count
    rows = [(d, f, s, st, "2023-12-28" if (f == "exam_date" and d.startswith("FDIC")) else v)
            for d, f, s, st, v in rows]
    table = routing.compute(m, preds(rows)).set_index("field")
    assert table.loc["exam_date", "n_index"] == 2
    assert table.loc["exam_date", "n_no_truth"] == 3
    assert table.loc["exam_date", "route"] == "s1"
    assert table.loc["cd_test_rating", "cell"] == "FRB/isb"
    assert table.loc["cd_test_rating", "n_index"] == 1
    assert table.loc["cd_test_rating", "n_agree"] == 1  # "Satisfactory" maps to S
    assert table.loc["cd_test_rating", "route"] == "s1"


def test_no_index_value_routes_to_s3():
    m = manifest([doc("FDIC", 1, prior_evaluation_date_index="", prior_rating_index="")])
    table = routing.compute(m, preds(s1_rows(m))).set_index("field")
    assert table.loc["prior_rating", "n_index"] == 0
    assert table.loc["prior_rating", "route"] == "s3"
    assert pd.isna(table.loc["prior_rating", "coverage"])


def test_other_systems_ignored():
    m = manifest([doc("OCC", 1)])
    rows = s1_rows(m) + [("OCC_1_20240101", "overall_rating", "llm:x:y", "value", "NI")]
    assert routing.compute(m, preds(rows)).set_index("field").loc["overall_rating", "n_agree"] == 1


def test_write_outputs_and_markdown(tmp_path):
    m = manifest([doc("OCC", 1), doc("OCC", 2, split="dev")])
    p = preds(s1_rows(m)).assign(rules_version="0.2.1")
    table = routing.compute(m, p)
    version, source = routing.rules_version_of(p)
    assert (version, source) == ("0.2.1", "predictions")
    record = routing.routing_record(table, m, pd.Timestamp("2026-10-13").date(), version, source, routing.Systems())
    paths = routing.write_outputs(table, record, tmp_path / "r.csv", tmp_path / "r.yaml", tmp_path / "r.md")
    loaded = yaml.safe_load(paths["yaml"].read_text())
    assert loaded["computed_on"] == "2026-10-13"
    assert loaded["rules_version"] == "0.2.1"
    assert loaded["population"]["n_documents"] == 1
    assert loaded["population"]["n_by_split"] == {"corpus": 1, "dev": 1}
    assert loaded["population"]["test_drawn"] is False
    assert loaded["fields"]["overall_rating"]["n_index"] == 1
    assert loaded["fields"]["overall_rating"]["route"] == "s1"
    md = paths["md"].read_text()
    assert md.splitlines()[0] == "| field | n with index value | S1 coverage | S1 agreement | route |"
    assert "| overall_rating | 1 | 100.0 (1 of 1) | 100.0 (1 of 1) | S1 |" in md
    assert "| lending_test_rating (FRB/isb) | 0 | n/a (0) | n/a (0) | S3 |" in md
    assert chr(0x2014) not in md  # no em dash
    routes, rec = routing.load_routing(paths["yaml"])
    assert routes["overall_rating"] == "s1" and rec["s3_system"] == routing.S3_DEFAULT
    assert len(pd.read_csv(paths["csv"])) == len(routing.routed_fields())


def test_rules_version_conflict_raises():
    p = preds([("a", "regulator", "rules", "value", "OCC"), ("b", "regulator", "rules", "value", "OCC")])
    p["rules_version"] = ["0.2.0", "0.2.1"]
    with pytest.raises(ValueError):
        routing.rules_version_of(p)


# ----------------------------------------------------------------------------- apply


S3 = "llm:gemini:test"
SYSTEMS = routing.Systems(s1="rules", s3=S3)


def _routes(**overrides: str) -> dict[str, str]:
    routes = {f: "s3" for f in routing.routed_fields()}
    routes.update(overrides)
    return routes


def test_apply_takes_routed_system_and_s3_elsewhere():
    m = manifest([doc("FRB", 1, "isb"), doc("FDIC", 2, "isb")])
    rows = []
    for d in ("FRB_1_20240101", "FDIC_2_20240101"):
        rows += [(d, "overall_rating", "rules", "value", "S"), (d, "overall_rating", S3, "value", "O"),
                 (d, "cd_test_rating", "rules", "value", "S"), (d, "cd_test_rating", S3, "value", "NI"),
                 (d, "bank_name", "rules", "value", "Bank"), (d, "bank_name", S3, "value", "Bank S3"),
                 (d, "avg_ltd_ratio_pct", "rules", "value", "80.0"), (d, "avg_ltd_ratio_pct", S3, "value", "81.0")]
    p = preds(rows).assign(grounding_failure="", locator_miss="0", notes="")
    p.loc[(p["system"] == S3) & (p["field"] == "avg_ltd_ratio_pct"), "grounding_failure"] = "quote_not_found"
    s5 = routing.apply(p, _routes(overall_rating="s1", cd_test_rating="s1"), m, SYSTEMS)
    by = s5.set_index(["doc_id", "field"])
    assert set(s5["system"]) == {"s5"}
    assert by.loc[("FDIC_2_20240101", "overall_rating"), "value"] == "S"           # routed to S1
    assert by.loc[("FDIC_2_20240101", "overall_rating"), "reference_system"] == S3
    assert by.loc[("FDIC_2_20240101", "bank_name"), "value"] == "Bank S3"          # routed to S3
    assert by.loc[("FRB_1_20240101", "cd_test_rating"), "value"] == "S"            # FRB/isb: S1
    assert by.loc[("FDIC_2_20240101", "cd_test_rating"), "value"] == "NI"          # other cell: S3
    ltd = by.loc[("FDIC_2_20240101", "avg_ltd_ratio_pct")]
    assert ltd["value"] == "81.0" and ltd["source_system"] == S3 and ltd["reference_system"] == "rules"
    assert ltd["grounding_failure"] == "quote_not_found"
    assert ltd["notes"].startswith(f"s5_from={S3}")
    # a field without a source row abstains
    assert by.loc[("FDIC_2_20240101", "finding_present"), "status"] == "abstain"
    assert set(s5.columns) >= set(routing.PREDICTION_COLUMNS)


def test_apply_requires_every_route_and_known_docs():
    m = manifest([doc("OCC", 1)])
    p = preds([("OCC_1_20240101", "overall_rating", S3, "value", "S")])
    routes = _routes()
    del routes["bank_id"]
    with pytest.raises(ValueError, match="bank_id"):
        routing.apply(p, routes, m, SYSTEMS)
    with pytest.raises(ValueError, match="manifest"):
        routing.apply(preds([("OCC_9_20240101", "overall_rating", S3, "value", "S")]), _routes(), m, SYSTEMS)


def test_ensure_s5_adds_rows_only_where_missing(tmp_path):
    m = manifest([doc("OCC", 1), doc("OCC", 2)])
    table = routing.compute(m, preds(s1_rows(m)))
    record = routing.routing_record(table, m, pd.Timestamp("2026-10-13").date(), "0.2.1", "code", SYSTEMS)
    paths = routing.write_outputs(table, record, tmp_path / "r.csv", tmp_path / "r.yaml", tmp_path / "r.md")
    p = preds(s1_rows(m) + [("OCC_1_20240101", "overall_rating", S3, "value", "O"),
                            ("OCC_2_20240101", "overall_rating", S3, "value", "O"),
                            ("OCC_2_20240101", "overall_rating", "s5", "value", "S")])
    out = routing.ensure_s5(p, m, SYSTEMS, paths["yaml"])
    s5 = out[out["system"] == "s5"]
    assert sorted(s5["doc_id"].unique()) == ["OCC_1_20240101", "OCC_2_20240101"]
    assert len(s5[s5["doc_id"] == "OCC_2_20240101"]) == 1  # existing S5 rows kept as they are
    assert routing.ensure_s5(p, m, SYSTEMS, tmp_path / "missing.yaml").equals(routing.normalize_predictions(p))


def test_cli_compute_writes_files(tmp_path, monkeypatch):
    m = manifest([doc("OCC", 1), doc("FRB", 2), doc("OCC", 3, split="test")])
    mp = tmp_path / "corpus.csv"
    m.to_csv(mp, index=False)
    from cra_extract.rules import RULES_VERSION

    rp = tmp_path / "rules.csv"
    preds(s1_rows(m)).assign(rules_version=RULES_VERSION).to_csv(rp, index=False)
    out = tmp_path / "out"
    code = routing.main(["compute", "--predictions", str(rp), "--manifest", str(mp), "--date", "2026-10-13",
                         "--out-csv", str(out / "a.csv"), "--out-yaml", str(out / "a.yaml"),
                         "--out-md", str(out / "a.md")])
    assert code == 0 and (out / "a.yaml").exists()
    record = yaml.safe_load((out / "a.yaml").read_text(encoding="utf-8"))
    assert record["provisional"] is False
    assert record["exam_date_index_by_regulator"] == {"OCC": 1, "FDIC": 0, "FRB": 1}
    assert "OCC (1) and FRB (1)" in record["notes"][0]
    preds(s1_rows(m)).assign(rules_version="0.0.1").to_csv(rp, index=False)
    assert routing.main(["compute", "--predictions", str(rp), "--manifest", str(mp),
                         "--out-csv", str(out / "b.csv"), "--out-yaml", str(out / "b.yaml"),
                         "--out-md", str(out / "b.md")]) == 1


def test_cli_compute_refuses_before_the_test_draw(tmp_path, monkeypatch):
    m = manifest([doc("OCC", 1), doc("FRB", 2)])
    mp = tmp_path / "corpus.csv"
    m.to_csv(mp, index=False)
    from cra_extract.rules import RULES_VERSION

    rp = tmp_path / "rules.csv"
    preds(s1_rows(m)).assign(rules_version=RULES_VERSION).to_csv(rp, index=False)
    monkeypatch.setattr(routing, "PROVISIONAL_DIR", tmp_path / "prov")
    assert routing.main(["compute", "--predictions", str(rp), "--manifest", str(mp)]) == 1
    assert routing.main(["compute", "--predictions", str(rp), "--manifest", str(mp), "--provisional"]) == 0
    record = yaml.safe_load((tmp_path / "prov" / "s5_routing.yaml").read_text(encoding="utf-8"))
    assert record["provisional"] is True
    code = routing.main(["compute", "--predictions", str(rp), "--manifest", str(mp), "--provisional",
                         "--out-yaml", str(routing.CONFIG_DIR / "elsewhere.yaml"),
                         "--out-csv", str(tmp_path / "c.csv"), "--out-md", str(tmp_path / "c.md")])
    assert code == 1 and not (routing.CONFIG_DIR / "elsewhere.yaml").exists()
    # apply refuses a provisional table
    pp = tmp_path / "p.csv"
    preds([("OCC_1_20240101", "regulator", "llm:gemini:gemini-3.8-flash", "value", "OCC")]).to_csv(pp, index=False)
    assert routing.main(["apply", "--predictions", str(pp), "--manifest", str(mp),
                         "--routing", str(tmp_path / "prov" / "s5_routing.yaml"), "--out", str(tmp_path / "s5.csv")]) == 1


def test_s1_sourced_s5_values_are_not_graded_and_append_to_a_predictions_file(tmp_path):
    from cra_extract import evaluate

    m = manifest([doc("OCC", 1)])
    s3 = "llm:gemini:gemini-3.8-flash"
    rows = [*s1_rows(m), *((d, f, s3, st, v) for d, f, _s, st, v in s1_rows(m)),
            ("OCC_1_20240101", "avg_ltd_ratio_pct", s3, "value", "80.0")]
    p = preds(rows).assign(grounding_failure="", locator_miss="0", quote="q", page="1")
    routes = dict.fromkeys(routing.routed_fields(), "s1")
    s5 = routing.apply(p, routes, m)
    by = s5.set_index("field")
    assert by.loc["overall_rating", "grounding_failure"] == evaluate.GROUNDING_NOT_GRADED
    assert by.loc["avg_ltd_ratio_pct", "grounding_failure"] == ""
    path = tmp_path / "predictions_test.csv"
    full = routing.normalize_predictions(p)[routing.PREDICTION_COLUMNS]
    full.to_csv(path, index=False)
    assert routing.append_s5(path, s5) == len(s5)
    assert routing.append_s5(path, s5) == len(s5)  # replaces, never duplicates
    back = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert list(back.columns) == routing.PREDICTION_COLUMNS
    assert (back["system"] == "s5").sum() == len(s5)
    fvs, column = evaluate.read_predictions(path)
    import math

    assert math.isnan(column[("OCC_1_20240101", "overall_rating", "s5")])
    assert column[("OCC_1_20240101", "avg_ltd_ratio_pct", "s5")] is None


# ----------------------------------------------------------------------------- no gold


@pytest.mark.parametrize("module", [routing, corpus_scoring])
def test_no_public_function_accepts_gold_labels(module):
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if fn.__module__ != module.__name__:
            continue
        params = set(inspect.signature(fn).parameters)
        assert not {p for p in params if "label" in p or "gold" in p}, f"{module.__name__}.{name}: {params}"
