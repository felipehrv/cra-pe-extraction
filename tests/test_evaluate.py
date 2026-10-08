"""Tests for cra_extract.evaluate: scoring matrix, tolerances, metrics, errors, risk,
the test-split guard and the CLI round trip on tiny synthetic files."""

from __future__ import annotations

import math
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from cra_extract import evaluate
from cra_extract.config import field_spec
from cra_extract.models import FieldValue, LabelRow


def gold(field: str, value=None, null_reason=None, doc_id="FDIC_1_20240101") -> LabelRow:
    return LabelRow(doc_id=doc_id, field=field, value=value, null_reason=null_reason)


def pred(field: str, value=None, status="value", system="llm", doc_id="FDIC_1_20240101", quote=None, notes=None) -> FieldValue:
    return FieldValue(doc_id=doc_id, field=field, system=system, status=status, value=value, quote=quote, notes=notes)


# ---------------------------------------------------------- scoring matrix


@pytest.mark.parametrize(
    "gold_row,pred_row,cell",
    [
        (gold("overall_rating", "S"), pred("overall_rating", "S"), "correct"),
        (gold("overall_rating", "S"), pred("overall_rating", "NI"), "wrong"),
        (gold("overall_rating", "S"), pred("overall_rating", status="absent"), "fabricated_absence"),
        (gold("overall_rating", "S"), pred("overall_rating", status="abstain"), "abstention"),
        (gold("cd_test_rating", null_reason="absent_from_document"), pred("cd_test_rating", "S"), "fabrication"),
        (gold("cd_test_rating", null_reason="absent_from_document"), pred("cd_test_rating", status="absent"), "correct_null"),
        (gold("cd_test_rating", null_reason="absent_from_document"), pred("cd_test_rating", status="abstain"), "abstention"),
        (gold("n_full_scope", null_reason="not_applicable"), pred("n_full_scope", 1), "fabrication"),
        (gold("n_full_scope", null_reason="not_applicable"), pred("n_full_scope", status="absent"), "correct_null"),
        (gold("n_full_scope", null_reason="not_applicable"), pred("n_full_scope", status="abstain"), "abstention"),
        (gold("overall_rating", "S"), None, "abstention"),
    ],
)
def test_score_cell_matrix(gold_row, pred_row, cell):
    assert evaluate.score_cell(gold_row, pred_row, field_spec(gold_row.field)) == cell


# -------------------------------------------------------------- tolerances


@pytest.mark.parametrize(
    "field,g,p,ok",
    [
        ("overall_rating", "S", "S", True),
        ("overall_rating", "S", "Satisfactory", True),
        ("overall_rating", "NI", "Needs to Improve", True),
        ("overall_rating", "S", "O", False),
        ("exam_date", "2024-01-16", "2024-01-16", True),
        ("exam_date", "2024-01-16", "January 16, 2024", True),
        ("exam_date", "2024-01-16", "2024-01-17", False),
        ("bank_id", "0012299", "12299", True),
        ("bank_id", "12299", "12298", False),
        ("bank_name", "SouthStar Bank, S.S.B.", "SouthStar Bank S.S.B", True),
        ("bank_name", "SouthStar Bank, S.S.B.", "Liberty Bank", False),
        ("n_assessment_areas", "5", 5, True),
        ("n_assessment_areas", 5, 6, False),
        ("inside_aa_share_number_pct", 88.5, 88.55, True),
        ("inside_aa_share_number_pct", 88.5, 88.7, False),
        ("avg_ltd_ratio_pct", "81.6", "81.6", True),
        ("loan_products_evaluated", "home_mortgage|small_business", ["small_business", "home_mortgage"], True),
        ("loan_products_evaluated", "home_mortgage|small_business", ["home_mortgage"], False),
        ("finding_present", "true", True, True),
        ("finding_present", "false", "True", False),
        ("primary_product", "home_mortgage", "HOME_MORTGAGE", True),
    ],
)
def test_values_match(field, g, p, ok):
    assert evaluate.values_match(g, p, field_spec(field)) is ok


def test_within_1pct_same_quarter():
    spec = field_spec("total_assets_usd")
    assert evaluate.values_match(1_000_000_000, 1_009_000_000, spec, "2022-06-30", "2022-06-30")
    assert not evaluate.values_match(1_000_000_000, 1_011_000_000, spec, "2022-06-30", "2022-06-30")
    # Same number, different quarter: not a match.
    assert not evaluate.values_match(1_000_000_000, 1_000_000_000, spec, "2022-06-30", "2022-09-30")
    # Missing as-of on either side: numbers only.
    assert evaluate.values_match(1_000_000_000, 1_000_000_000, spec, "2022-06-30", None)
    assert evaluate.values_match("1,000,000,000", "$1000000000", spec)


# ------------------------------------------------------------ metrics table


@pytest.fixture
def synthetic():
    """Six documents; every matrix cell, every metric type, both systems."""
    labels, preds = [], []
    docs = [f"OCC_{i}_20240101" for i in range(1, 7)]
    # accuracy field: overall_rating (gold always value)
    for d, g in zip(docs, ["S", "S", "S", "NI", "O", "S"], strict=True):
        labels.append(gold("overall_rating", g, doc_id=d))
    preds += [
        pred("overall_rating", "S", doc_id=docs[0], quote="rated Satisfactory"),
        pred("overall_rating", "NI", doc_id=docs[1], quote="rated Satisfactory"),           # wrong
        pred("overall_rating", status="absent", doc_id=docs[2]),                          # fabricated absence
        pred("overall_rating", status="abstain", doc_id=docs[3]),                         # abstention
        pred("overall_rating", "O", doc_id=docs[4], quote="rated Outstanding"),
        # docs[5]: no prediction row -> abstention
    ]
    # fabrication probe: cd_test_rating ABS for OCC small
    for d in docs[:4]:
        labels.append(gold("cd_test_rating", null_reason="absent_from_document", doc_id=d))
    preds += [
        pred("cd_test_rating", "S", doc_id=docs[0], quote="x"),                            # fabrication
        pred("cd_test_rating", status="absent", doc_id=docs[1]),                          # correct null
        pred("cd_test_rating", status="absent", doc_id=docs[2]),
        pred("cd_test_rating", status="abstain", doc_id=docs[3]),
    ]
    # binary_pr: finding_present, two gold positives
    for d, g in zip(docs[:5], [True, True, False, False, False], strict=True):
        labels.append(gold("finding_present", g, doc_id=d))
    preds += [
        pred("finding_present", True, doc_id=docs[0]),    # tp
        pred("finding_present", False, doc_id=docs[1]),   # fn
        pred("finding_present", True, doc_id=docs[2]),    # fp
        pred("finding_present", False, doc_id=docs[3]),   # tn
        pred("finding_present", status="abstain", doc_id=docs[4]),
    ]
    # set_pr: loan_products_evaluated
    labels.append(gold("loan_products_evaluated", "home_mortgage|small_business", doc_id=docs[0]))
    labels.append(gold("loan_products_evaluated", "home_mortgage", doc_id=docs[1]))
    preds += [
        pred("loan_products_evaluated", "home_mortgage|small_farm", doc_id=docs[0]),   # tp 1, fp 1, fn 1
        pred("loan_products_evaluated", "home_mortgage", doc_id=docs[1]),              # exact
    ]
    # counts: finding_type
    labels.append(gold("finding_type", "discrimination", doc_id=docs[0]))
    preds.append(pred("finding_type", "other_illegal_credit_practice", doc_id=docs[0]))
    # not_scored field must be ignored
    labels.append(gold("distractor_laws_named", True, doc_id=docs[0]))
    preds.append(pred("distractor_laws_named", False, doc_id=docs[0]))
    # a second system that only answers overall_rating, disagreeing on docs[0]
    preds.append(pred("overall_rating", "NI", system="rules", doc_id=docs[0]))
    preds.append(pred("overall_rating", "S", system="rules", doc_id=docs[1]))
    return labels, preds


def _row(df: pd.DataFrame, field: str, system: str = "llm") -> pd.Series:
    sub = df[(df["field"] == field) & (df["system"] == system)]
    assert len(sub) == 1, (field, system)
    return sub.iloc[0]


def test_metrics_table(synthetic):
    labels, preds = synthetic
    ground = {("OCC_1_20240101", "overall_rating", "llm"): None, ("OCC_2_20240101", "overall_rating", "llm"): "quote_does_not_support",
              ("OCC_5_20240101", "overall_rating", "llm"): "quote_not_found", ("OCC_1_20240101", "cd_test_rating", "llm"): "quote_out_of_scope"}
    scored = evaluate.score_predictions(labels, preds, grounding_column=ground)
    assert "distractor_laws_named" not in set(scored["field"])
    t = evaluate.metrics_table(scored)

    r = _row(t, "overall_rating")
    assert r["n_gold"] == 6 and r["n_answered"] == 3 and r["n_abstain"] == 2
    assert r["n_correct"] == 2 and r["n_wrong"] == 1 and r["n_fabricated_absence"] == 1
    assert r["accuracy"] == pytest.approx(2 / 3)
    assert r["accuracy_lo"] < 2 / 3 < r["accuracy_hi"]
    assert r["abstention_rate"] == pytest.approx(2 / 6)
    assert r["fabrication_denominator"] == 0 and math.isnan(r["fabrication_rate"])
    # groundedness: 3 answered, 1 grounded, one does_not_support, one not_found
    assert r["n_grounding_checked"] == 3
    assert r["quote_not_found_rate"] == pytest.approx(1 / 3)
    assert r["quote_does_not_support_rate"] == pytest.approx(1 / 3)
    assert r["quote_out_of_scope_rate"] == 0
    assert (r["correct_grounded"], r["correct_misgrounded"], r["wrong_grounded"], r["wrong_misgrounded"]) == (1, 1, 0, 1)
    # hallucination = wrong (1) + fabricated absence (1) + correct but misgrounded (1) over 4 non-abstained
    assert r["hallucination_rate"] == pytest.approx(3 / 4)

    r = _row(t, "cd_test_rating")
    assert r["fabrication_denominator"] == 4
    assert r["fabrication_rate"] == pytest.approx(1 / 4)
    assert r["n_correct_null"] == 2 and r["n_abstain"] == 1
    assert r["hallucination_rate"] == pytest.approx(1 / 3)

    r = _row(t, "finding_present")
    assert r["metric_type"] == "binary_pr"
    assert (r["tp"], r["fp"], r["fn"], r["n_positive"]) == (1, 1, 1, 2)
    assert r["precision"] == 0.5 and r["recall"] == 0.5 and r["recall_overall"] == 0.5
    assert r["precision_lo"] < 0.5 < r["precision_hi"]
    assert r["n_abstain"] == 1
    # no quotes at all for this field: groundedness is not applicable
    assert math.isnan(r["grounded_rate"])

    r = _row(t, "loan_products_evaluated")
    assert r["metric_type"] == "set_pr"
    assert (r["tp"], r["fp"], r["fn"]) == (2, 1, 1)
    assert r["precision"] == pytest.approx(2 / 3) and r["recall"] == pytest.approx(2 / 3)
    assert r["n_correct"] == 1 and r["n_wrong"] == 1

    r = _row(t, "finding_type")
    assert r["metric_type"] == "counts"
    assert r["n_wrong"] == 1 and math.isnan(r["accuracy"]) and math.isnan(r["hallucination_rate"])

    r = _row(t, "overall_rating", "rules")
    assert r["n_answered"] == 2 and r["n_correct"] == 1 and r["n_abstain"] == 4
    # Rows ordered as in fields.yaml, then by system.
    assert list(t["field"]).index("overall_rating") < list(t["field"]).index("finding_present")


def test_error_table(synthetic):
    labels, preds = synthetic
    scored = evaluate.score_predictions(labels, preds)
    errs = evaluate.error_table(scored)
    assert set(errs["cell"]) <= {"wrong", "fabrication", "fabricated_absence", "abstention"}
    assert "correct" not in set(errs["cell"])
    assert list(errs.columns) == ["doc_id", "field", "system", "gold_status", "gold_value", "pred_status", "pred_value", "cell", "grounding_failure", "cause"]
    assert (errs["cause"] == "").all()
    wrong = errs[(errs["field"] == "overall_rating") & (errs["doc_id"] == "OCC_2_20240101") & (errs["system"] == "llm")]
    assert wrong.iloc[0]["gold_value"] == "S" and wrong.iloc[0]["pred_value"] == "NI"


# -------------------------------------------------------------- risk flags


def test_risk_flags_and_document_risk():
    doc = "OCC_9_20240301"
    labels = [
        gold("overall_rating", "S", doc_id=doc),
        gold("exam_method", "small", doc_id=doc),
        gold("exam_date", "2024-03-01", doc_id=doc),
        gold("lending_test_rating", "S", doc_id=doc),
        gold("lending_data_period_end", "2023-12-31", doc_id=doc),
        gold("n_assessment_areas", 2, doc_id=doc),
        gold("n_full_scope", 1, doc_id=doc),
        gold("total_assets_usd", 100, doc_id=doc),
        gold("rating_after", "S", doc_id=doc),
        gold("rating_lowered_level", "overall", doc_id=doc),
    ]
    preds = [
        pred("overall_rating", "S", doc_id=doc),
        pred("exam_method", "small", doc_id=doc),
        pred("exam_date", "2024-03-01", doc_id=doc),
        pred("lending_test_rating", "NI", doc_id=doc),                   # consistency: != overall for OCC small
        pred("lending_data_period_end", "2024-06-30", doc_id=doc),       # consistency: after exam date
        pred("n_assessment_areas", 2, doc_id=doc),
        pred("n_full_scope", 3, doc_id=doc, notes="locator_miss"),       # consistency + locator miss
        pred("total_assets_usd", 100, doc_id=doc),
        pred("total_assets_usd", 150, doc_id=doc, system="rules"),       # disagreement with llm
        pred("rating_after", "NI", doc_id=doc),                          # != overall when level overall
        pred("rating_lowered_level", "overall", doc_id=doc),
    ]
    ground = {(doc, "total_assets_usd", "llm"): "quote_not_found"}
    scored = evaluate.score_predictions(labels, preds, grounding_column=ground)
    risk = evaluate.risk_table(scored, preds, labels)
    r = risk.set_index(["field", "system"])
    assert r.loc[("lending_test_rating", "llm"), "consistency_fail"]
    assert r.loc[("lending_data_period_end", "llm"), "consistency_fail"]
    assert r.loc[("n_full_scope", "llm"), "consistency_fail"] and r.loc[("n_full_scope", "llm"), "locator_miss"]
    assert r.loc[("rating_after", "llm"), "consistency_fail"]
    assert not r.loc[("overall_rating", "llm"), "consistency_fail"]
    assert r.loc[("total_assets_usd", "llm"), "disagreement"] and r.loc[("total_assets_usd", "rules"), "disagreement"]
    assert r.loc[("total_assets_usd", "llm"), "groundedness_fail"]
    assert r.loc[("total_assets_usd", "llm"), "n_flags"] == 2
    assert r.loc[("n_full_scope", "llm"), "n_flags"] == 2
    # Document risk sums Group B values only: period_end (1) + n_full_scope (2) + total_assets (2) = 5
    assert r.loc[("overall_rating", "llm"), "doc_risk"] == 5
    assert r.loc[("total_assets_usd", "rules"), "doc_risk"] == 1
    calib = evaluate.calibration_table(risk, n_boot=20)
    assert set(calib["n_flags"]) == {0, 1, 2}
    assert calib["ece"].notna().all()


def test_lending_consistency_only_for_occ_small():
    doc = "FDIC_9_20240301"
    labels = [gold("overall_rating", "S", doc_id=doc), gold("exam_method", "small", doc_id=doc), gold("lending_test_rating", "NI", doc_id=doc)]
    preds = [pred("lending_test_rating", "NI", doc_id=doc)]
    risk = evaluate.risk_table(evaluate.score_predictions(labels, preds), preds, labels)
    assert not risk.set_index("field").loc["lending_test_rating", "consistency_fail"]


# ------------------------------------------------------------- test guard


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "results").mkdir()
    return tmp_path


def test_guard_ignores_dev(repo):
    evaluate.check_test_guard("dev", root=repo, env={})
    assert not (repo / "results" / "test_invocations.log").exists()


def test_guard_requires_tag_or_env(repo):
    with pytest.raises(evaluate.TestGuardError, match="plan-v1.0"):
        evaluate.check_test_guard("test", root=repo, env={})
    evaluate.check_test_guard("test", root=repo, env={"CRA_ALLOW_TEST_EVAL": "1"}, replay_only=True)
    log = (repo / "results" / "test_invocations.log").read_text().splitlines()
    assert len(log) == 1 and "\treplay\t" in log[0]


def test_guard_logs_and_refuses_second_run_without_deviation(repo):
    subprocess.run(["git", "tag", "plan-v1.0"], cwd=repo, check=True)
    t0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    evaluate.check_test_guard("test", root=repo, env={}, now=t0, note="--split test")
    log = repo / "results" / "test_invocations.log"
    assert log.read_text().startswith("2026-10-15T09:00:00+00:00\trun\t--split test")
    with pytest.raises(evaluate.TestGuardError, match="deviations"):
        evaluate.check_test_guard("test", root=repo, env={}, now=t0 + timedelta(hours=1))
    # A stale deviation entry (before the first run) does not unlock.
    dev = repo / "docs" / "deviations.md"
    dev.write_text("# Deviations\n\n## 2026-10-12 label correction\n\ntext\n")
    with pytest.raises(evaluate.TestGuardError):
        evaluate.check_test_guard("test", root=repo, env={}, now=t0 + timedelta(hours=1))
    # An entry dated on or after the first run unlocks exactly one more run.
    dev.write_text("# Deviations\n\n## 2026-10-12 old\n\n- 2026-10-15: rerun after fixing a label in doc X\n")
    evaluate.check_test_guard("test", root=repo, env={}, now=t0 + timedelta(hours=2))
    assert len(log.read_text().splitlines()) == 2
    # Replay-only regenerations are always allowed and logged.
    evaluate.check_test_guard("test", root=repo, env={}, now=t0 + timedelta(hours=3), replay_only=True)
    assert len(log.read_text().splitlines()) == 3


def test_deviation_dates_parsing(tmp_path):
    p = tmp_path / "deviations.md"
    p.write_text("# Deviations\n## 2026-10-16: x\n- 2026-10-17 y\n1. 2026-10-18 z\nplain 2026-10-19 text\n")
    assert [d.isoformat() for d in evaluate.deviation_dates(p)] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    assert evaluate.deviation_dates(tmp_path / "missing.md") == []


# ------------------------------------------------------------- CLI round trip


LABELS_CSV = """doc_id,field,value,null_reason,evidence_page,quote,scope,minutes
FDIC_1_20240101,overall_rating,NI,,1,This institution is rated Needs to Improve.,institution,0.5
FDIC_1_20240101,cd_test_rating,S,,1,The Community Development Test is rated: Satisfactory.,institution,0.5
FDIC_1_20240101,total_assets_usd,463000000,,2,total assets of $463 million,institution,1
FDIC_1_20240101,total_assets_asof,2024-03-31,,2,"As of March 31, 2024",institution,1
FDIC_1_20240101,n_full_scope,,not_applicable,,,,0.2
FDIC_1_20240101,finding_present,false,,3,Examiners did not identify any evidence,institution,1
"""

PREDICTIONS_CSV = """doc_id,field,system,status,value,quote,page,scope,char_start,char_end,confidence,notes
FDIC_1_20240101,overall_rating,llm,value,NI,This institution is rated Needs to Improve.,1,,,,0.9,
FDIC_1_20240101,cd_test_rating,llm,value,S,The Lending Test is rated: Needs to Improve.,1,,,,0.9,
FDIC_1_20240101,total_assets_usd,llm,value,463000000,"As of March 31, 2024, the institution reported total assets of $463 million",2,,,,0.8,
FDIC_1_20240101,total_assets_asof,llm,value,2024-03-31,"As of March 31, 2024, the institution reported total assets of $463 million",2,,,,0.8,
FDIC_1_20240101,n_full_scope,llm,value,1,one full-scope review,2,,,,0.3,
FDIC_1_20240101,finding_present,llm,value,false,Examiners did not identify any evidence of discriminatory or other illegal credit practices,3,,,,0.9,
FDIC_1_20240101,overall_rating,rules,value,NI,,1,,,,,
FDIC_1_20240101,cd_test_rating,rules,abstain,,,,,,,,
"""

DOC_TEXT = (
    "PUBLIC DISCLOSURE\nINSTITUTION'S CRA RATING: This institution is rated Needs to Improve.\n"
    "The Lending Test is rated: Needs to Improve.\nThe Community Development Test is rated: Satisfactory.\n"
    "\f"
    "DESCRIPTION OF INSTITUTION\nAs of March 31, 2024, the institution reported total assets of $463 million.\n"
    "Examiners conducted one full-scope review.\n"
    "\f"
    "DISCRIMINATORY OR OTHER ILLEGAL CREDIT PRACTICES REVIEW\n"
    "Examiners did not identify any evidence of discriminatory or other illegal credit practices; therefore, "
    "this consideration did not affect the institution's overall CRA rating.\n"
)


def test_cli_round_trip(tmp_path: Path, capsys):
    gold_dir = tmp_path / "data" / "gold"
    (gold_dir / "text").mkdir(parents=True)
    (tmp_path / "results").mkdir()
    (gold_dir / "labels_dev.csv").write_text(LABELS_CSV)
    (tmp_path / "results" / "predictions_dev.csv").write_text(PREDICTIONS_CSV)
    (gold_dir / "text" / "FDIC_1_20240101.txt").write_text(DOC_TEXT)

    rc = evaluate.main(["--split", "dev", "--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "table1" in out

    table1 = pd.read_csv(tmp_path / "results" / "table1_dev.csv")
    errors = pd.read_csv(tmp_path / "results" / "errors_dev.csv", keep_default_na=False)
    risk = pd.read_csv(tmp_path / "results" / "risk_dev.csv")
    assert (tmp_path / "results" / "calibration_dev.csv").exists()

    llm = table1[table1["system"] == "llm"].set_index("field")
    assert llm.loc["overall_rating", "n_correct"] == 1 and llm.loc["overall_rating", "grounded_rate"] == 1.0
    # cd_test_rating: value S is correct but the quote (found, in scope) is about the Lending
    # Test and never says Satisfactory, so it is misgrounded.
    assert llm.loc["cd_test_rating", "n_correct"] == 1
    assert llm.loc["cd_test_rating", "quote_does_not_support_rate"] == 1.0
    assert llm.loc["cd_test_rating", "correct_misgrounded"] == 1
    assert llm.loc["cd_test_rating", "hallucination_rate"] == 1.0
    assert llm.loc["total_assets_usd", "n_correct"] == 1 and llm.loc["total_assets_usd", "grounded_rate"] == 1.0
    assert llm.loc["n_full_scope", "n_fabrication"] == 1 and llm.loc["n_full_scope", "fabrication_rate"] == 1.0
    assert llm.loc["finding_present", "tp"] == 0 and llm.loc["finding_present", "n_correct"] == 1
    assert llm.loc["finding_present", "grounded_rate"] == 1.0

    rules = table1[table1["system"] == "rules"].set_index("field")
    assert rules.loc["cd_test_rating", "n_abstain"] == 1
    assert rules.loc["total_assets_usd", "n_abstain"] == 1  # no row -> abstention
    assert math.isnan(rules.loc["overall_rating", "grounded_rate"])  # rules give no quotes

    assert set(errors["cell"]) == {"fabrication", "abstention"}
    assert "cause" in errors.columns
    fab = errors[errors["cell"] == "fabrication"].iloc[0]
    assert fab["field"] == "n_full_scope" and fab["gold_status"] == "not_applicable"

    cd = risk[(risk["field"] == "cd_test_rating") & (risk["system"] == "llm")].iloc[0]
    assert cd["groundedness_fail"] and cd["n_flags"] == 1

    # --systems restricts the output; deterministic regeneration.
    before = (tmp_path / "results" / "table1_dev.csv").read_bytes()
    assert evaluate.main(["--split", "dev", "--root", str(tmp_path)]) == 0
    assert (tmp_path / "results" / "table1_dev.csv").read_bytes() == before
    assert evaluate.main(["--split", "dev", "--root", str(tmp_path), "--systems", "llm", "--out-dir", str(tmp_path / "only")]) == 0
    only = pd.read_csv(tmp_path / "only" / "table1_dev.csv")
    assert set(only["system"]) == {"llm"}


def test_cli_refuses_test_split_without_tag(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.delenv("CRA_ALLOW_TEST_EVAL", raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    rc = evaluate.main(["--split", "test", "--root", str(tmp_path)])
    assert rc == 2
    assert "refused" in capsys.readouterr().err


def test_predictions_grounding_column_is_used(tmp_path: Path):
    csv = "doc_id,field,system,status,value,quote,grounding_failure,locator_miss\n" \
          "FDIC_1_20240101,overall_rating,llm,value,NI,some quote,quote_out_of_scope,1\n" \
          "FDIC_1_20240101,cd_test_rating,llm,value,S,some quote,,0\n"
    p = tmp_path / "p.csv"
    p.write_text(csv)
    preds, column = evaluate.read_predictions(p)
    assert column == {("FDIC_1_20240101", "overall_rating", "llm"): "quote_out_of_scope", ("FDIC_1_20240101", "cd_test_rating", "llm"): None}
    assert "locator_miss" in preds[0].notes and not preds[1].notes
