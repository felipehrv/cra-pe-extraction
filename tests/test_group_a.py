"""Tests for the Group A comparison of the rules with the agency index (src/cra_extract/group_a.py)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from cra_extract import group_a
from cra_extract.config import CONFIG_DIR

MANIFEST = pd.DataFrame(
    [
        # doc_id, regulator, bank_id, exam_date, method, overall_rating, source_file_id, release_date
        ("FDIC_10159_20231206", "FDIC", "10159", "2023-12-06", "small", "S", "2023/10159_231206.PDF", "2024-04-01"),
        ("FDIC_14134_20240205", "FDIC", "14134", "2024-02-05", "isb", "O", "2024/14134_240205.PDF", "2024-06-01"),
        ("OCC_10180_20231031", "OCC", "10180", "2023-10-31", "isb", "S", "/Mar24/10180.pdf", "2024-03-21"),
        ("FRB_1_20240101", "FRB", "0001", "2024-01-01", "small", "S", "1_20240101", "2024-03-01"),
    ],
    columns=["doc_id", "regulator", "bank_id", "exam_date", "method", "overall_rating", "source_file_id", "release_date"],
)


def _rules(rows: list[tuple[str, str, str, str]], version: str) -> pd.DataFrame:
    """rows: (doc_id, field, status, value); quotes are 'q:<value>' on page 1."""
    return pd.DataFrame(
        [
            {"doc_id": d, "field": f, "status": s, "value": v, "quote": f"q:{v}" if s == "value" else "",
             "page": "1" if s == "value" else "", "notes": "", "rules_version": version}
            for d, f, s, v in rows
        ]
    )


BEFORE = _rules(
    [
        ("FDIC_10159_20231206", "exam_date", "value", "2023-11-30"),
        ("FDIC_10159_20231206", "exam_method", "value", "small"),
        ("FDIC_14134_20240205", "exam_method", "value", "small"),
        ("FDIC_14134_20240205", "bank_id", "value", "14134"),
        ("OCC_10180_20231031", "exam_date", "value", "2022-10-31"),
        ("OCC_10180_20231031", "overall_rating", "abstain", ""),
        ("FRB_1_20240101", "bank_id", "value", "1"),
        ("FRB_1_20240101", "regulator", "value", "FRB"),
    ],
    "0.1.0",
)
AFTER = _rules(
    [
        ("FDIC_10159_20231206", "exam_date", "value", "2023-11-30"),
        ("FDIC_10159_20231206", "exam_method", "value", "small"),
        ("FDIC_14134_20240205", "exam_method", "value", "isb"),
        ("FDIC_14134_20240205", "bank_id", "value", "14134"),
        ("OCC_10180_20231031", "exam_date", "value", "2022-10-31"),
        ("OCC_10180_20231031", "overall_rating", "value", "S"),
        ("FRB_1_20240101", "bank_id", "value", "1"),
        ("FRB_1_20240101", "regulator", "value", "FRB"),
    ],
    "0.2.0",
)


def test_join_strips_leading_zeros_and_maps_columns():
    j = group_a.join(AFTER, MANIFEST)
    row = j[(j["doc_id"] == "FRB_1_20240101") & (j["field"] == "bank_id")].iloc[0]
    assert row["index_value"] == "1" and bool(row["agree"])
    method = j[(j["doc_id"] == "FDIC_14134_20240205") & (j["field"] == "exam_method")].iloc[0]
    assert method["index_value"] == "isb" and bool(method["agree"])
    assert j[j["field"] == "regulator"]["index_value"].tolist() == ["FRB"]


def test_agreement_table_counts_answered_and_abstentions():
    t = group_a.agreement_table(group_a.join(BEFORE, MANIFEST), "0.1.0").set_index(["field", "regulator"])
    assert t.loc[("exam_date", "all"), "answered"] == 2
    assert t.loc[("exam_date", "all"), "agree"] == 0
    assert t.loc[("overall_rating", "OCC"), "abstention"] == 1.0
    assert t.loc[("exam_method", "FDIC"), "agreement"] == 0.5
    lo, hi = t.loc[("exam_method", "FDIC"), ["agreement_lo", "agreement_hi"]]
    assert 0 < lo < 0.5 < hi < 1


def test_auto_triage_our_error_and_fdic_definitional():
    tri = group_a.triage(group_a.join(BEFORE, MANIFEST), group_a.join(AFTER, MANIFEST), hand={})
    by = tri.set_index(["doc_id", "field"])
    fixed = by.loc[("FDIC_14134_20240205", "exam_method")]
    assert fixed["category"] == "our_error" and fixed["correct_value"] == "isb"
    assert fixed["evidence_quote"] == "q:isb" and fixed["triage"] == "auto"
    fdic = by.loc[("FDIC_10159_20231206", "exam_date")]
    assert fdic["category"] == "definitional"
    assert "6 days after the printed date" in fdic["note"]
    # an OCC date disagreement has no automatic rule
    assert by.loc[("OCC_10180_20231031", "exam_date"), "category"] == "untriaged"


def test_hand_entry_overrides_and_summary():
    hand = {("OCC_10180_20231031", "exam_date"): {
        "category": "source_error", "error_side": "index", "correct_value": "2022-10-31", "note": "index year +1",
    }}
    tri = group_a.triage(group_a.join(BEFORE, MANIFEST), group_a.join(AFTER, MANIFEST), hand=hand)
    row = tri.set_index(["doc_id", "field"]).loc[("OCC_10180_20231031", "exam_date")]
    assert row["category"] == "source_error" and row["evidence_quote"] == "q:2022-10-31"
    summary = group_a.triage_summary(tri).set_index(["field", "regulator"])
    assert summary.loc[("exam_date", "OCC"), "source_error"] == 1
    assert summary.loc[("exam_date", "FDIC"), "definitional"] == 1
    assert summary["untriaged"].sum() == 0


def test_fdic_date_lag_buckets():
    lag = group_a.fdic_date_lag(group_a.join(AFTER, MANIFEST)).set_index("bucket")
    assert lag.loc["1-7", "n"] == 1
    assert lag.loc["total", "n"] == 1
    assert lag.attrs["median_lag_days"] == 6


def test_quote_on_page(tmp_path):
    pages = [{"number": 1, "text": "PUBLIC DISCLOSURE\nCertificate Number:  34450\n"},
             {"number": 2, "text": "INSTITUTION’S CRA RATING: This institution is rated\nOutstanding.\n"}]
    (tmp_path / "D_1_20240101.pages.json").write_text(json.dumps(pages), encoding="utf-8")
    assert group_a.quote_on_page("D_1_20240101", "Certificate Number: 34450", 1, tmp_path) is True
    assert group_a.quote_on_page("D_1_20240101", "Certificate Number: 34450", 2, tmp_path) is False
    assert group_a.quote_on_page("D_1_20240101", "INSTITUTION'S CRA RATING: This institution is rated Outstanding.", 2, tmp_path)
    assert group_a.quote_on_page("missing", "x", 1, tmp_path) is None


def test_main_exits_1_on_untriaged(tmp_path):
    MANIFEST.to_csv(tmp_path / "corpus.csv", index=False)
    BEFORE.to_csv(tmp_path / "before.csv", index=False)
    AFTER.to_csv(tmp_path / "after.csv", index=False)
    (tmp_path / "triage.yaml").write_text("entries: []\n", encoding="utf-8")
    args = ["--rules", str(tmp_path / "after.csv"), "--before", str(tmp_path / "before.csv"),
            "--manifest", str(tmp_path / "corpus.csv"), "--triage", str(tmp_path / "triage.yaml"),
            "--text-dir", str(tmp_path), "--out-dir", str(tmp_path / "out"), "--abstention-sample", "0"]
    assert group_a.main(args) == 1
    (tmp_path / "triage.yaml").write_text(
        "entries:\n  - {doc_id: OCC_10180_20231031, field: exam_date, category: source_error, error_side: index}\n",
        encoding="utf-8",
    )
    assert group_a.main(args) == 0
    agreement = pd.read_csv(tmp_path / "out" / "group_a_agreement.csv")
    assert set(agreement["rules_version"]) == {"0.1.0", "0.2.0"}
    assert (tmp_path / "out" / "group_a_triage.csv").exists()


def test_hand_triage_file_is_well_formed():
    hand = group_a.load_hand_triage(CONFIG_DIR / "group_a_triage.yaml")
    assert len(hand) >= 30
    for (doc_id, field), e in hand.items():
        assert field in group_a.TRIAGE_FIELDS, doc_id
        assert e["category"] in group_a.CATEGORIES
        assert e.get("error_side") in {"index", "document", "rules", "none"}, doc_id
        assert e.get("note"), f"{doc_id} {field}: hand entry without a note"
        if e["category"] == "source_error":
            assert e["error_side"] in {"index", "document"}


def test_unknown_category_rejected(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("entries:\n  - {doc_id: X, field: bank_id, category: typo}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        group_a.load_hand_triage(path)
