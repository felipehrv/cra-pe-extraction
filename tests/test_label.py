"""Tests for cra_extract.label on a synthetic manifest and label sheet.

No network, no PDFs. The manifest covers every (regulator, method) cell once in dev
plus two test documents; the valid sheet is derived from ``init_sheet`` and filled
with values that satisfy every rule, then one cell is broken per test.
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import pytest

from cra_extract import label
from cra_extract.config import load_fields
from cra_extract.models import DocRecord, make_doc_id


def _doc(regulator: str, bank_id: str, exam: date, method: str, split: str, **kw) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(regulator, bank_id, exam),
        regulator=regulator,
        bank_id=bank_id,
        bank_name=f"{regulator} Bank {bank_id}",
        exam_date=exam,
        release_date=exam,
        method=method,
        overall_rating=kw.pop("overall_rating", "S"),
        url=f"https://example.invalid/{bank_id}.pdf",
        split=split,
        **kw,
    )


@pytest.fixture
def manifest() -> list[DocRecord]:
    return [
        _doc("OCC", "1001", date(2024, 3, 4), "small", "dev"),
        _doc("OCC", "1002", date(2024, 5, 6), "isb", "dev", overall_rating="O"),
        _doc("FDIC", "2001", date(2023, 7, 8), "small", "dev", overall_rating="SN"),
        _doc("FDIC", "2002", date(2023, 9, 10), "isb", "dev", overall_rating="NI"),
        _doc("FRB", "3001", date(2024, 11, 12), "small", "dev"),
        _doc(
            "FRB", "3002", date(2024, 1, 2), "isb", "dev",
            lending_test_rating="NI", cd_test_rating="S", overall_rating="NI",
        ),
        _doc("OCC", "1003", date(2025, 2, 3), "small", "test"),
        _doc("FDIC", "2003", date(2025, 4, 5), "isb", "test"),
        _doc("FRB", "3003", date(2025, 6, 7), "small", "corpus"),
    ]


# A value per field that satisfies every rule once the sheet is complete.
FILL = {
    "prior_evaluation_date": ("2019-01-01", ""),
    "prior_rating": ("S", ""),
    "total_assets_usd": ("100000000", ""),
    "total_assets_asof": ("2023-12-31", ""),
    "lending_data_period_start": ("2021-01-01", ""),
    "lending_data_period_end": ("2022-12-31", ""),
    "n_assessment_areas": ("2", ""),
    "n_full_scope": ("1", ""),
    "loan_products_evaluated": ("home_mortgage|small_business", ""),
    "primary_product": ("home_mortgage", ""),
    "inside_aa_share_number_pct": ("75.2", ""),
    "inside_aa_share_product": ("home_mortgage", ""),
    "avg_ltd_ratio_pct": ("85.9", ""),
    "finding_present": ("false", ""),
    "finding_type": ("", "not_applicable"),
    "rating_effect": ("", "not_applicable"),
    "rating_lowered": ("false", ""),
    "rating_lowered_level": ("", "not_applicable"),
    "rating_before": ("", "not_applicable"),
    "rating_after": ("", "not_applicable"),
    "laws_cited": ("", "not_applicable"),
    "practice_basis": ("", "not_applicable"),
    "finding_source": ("", "not_applicable"),
    "distractor_laws_named": ("false", ""),
}


def _complete(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fill an initialised sheet so that it validates cleanly."""
    for r in rows:
        if not r["value"] and not r["null_reason"]:
            if r["field"] in FILL:
                r["value"], r["null_reason"] = FILL[r["field"]]
            elif r["field"] in ("lending_test_rating", "cd_test_rating"):
                # Applicable cells not covered by the manifest: equal to overall for OCC Small.
                overall = next(x["value"] for x in rows if x["doc_id"] == r["doc_id"] and x["field"] == "overall_rating")
                r["value"] = overall
        if r["value"]:
            r["evidence_page"] = "1"
            r["quote"] = f"quote for {r['field']}"
        r["minutes"] = "0.5"
        r["labeler"] = "fh"
        r["labeled_at"] = "2026-10-08T10:00:00"
    return rows


@pytest.fixture
def sheet(manifest, tmp_path) -> tuple[list[dict[str, str]], Path]:
    rows = _complete(label.init_sheet(manifest, "dev"))
    path = tmp_path / "labels_dev.csv"
    label.write_sheet(rows, path)
    return rows, path


def _set(rows, doc_id, field, /, **cells):
    for r in rows:
        if r["doc_id"] == doc_id and r["field"] == field:
            r.update(cells)
            return r
    raise KeyError((doc_id, field))


def _rules(problems) -> set[str]:
    return {p.rule for p in problems}


# --------------------------------------------------------------------------- init_sheet


def test_init_sheet_shape(manifest):
    rows = label.init_sheet(manifest, "dev")
    n_fields = len(load_fields()["fields"])
    assert len(rows) == 6 * n_fields
    assert list(rows[0].keys()) == label.SHEET_COLUMNS
    assert [r["doc_id"] for r in rows[:2]] == sorted(r["doc_id"] for r in rows[:2])
    assert rows[0]["pdf_path"] == f"data/pdfs/{rows[0]['doc_id']}.pdf"
    assert {r["field_group"] for r in rows} == {"A", "A_or_B", "B", "C"}


def test_init_sheet_prefills_group_a(manifest):
    rows = label.init_sheet(manifest, "dev")
    doc = "FDIC_2002_20230910"
    got = {r["field"]: r for r in rows if r["doc_id"] == doc}
    assert got["exam_date"]["value"] == "2023-09-10" and got["exam_date"]["prefilled"] == "true"
    assert got["exam_method"]["value"] == "isb"
    assert got["overall_rating"]["value"] == "NI"
    assert got["bank_id"]["value"] == "2002"
    assert got["regulator"]["value"] == "FDIC"
    # Not in the manifest: left for the labeler, not flagged.
    assert got["prior_rating"]["value"] == "" and got["prior_rating"]["prefilled"] == "false"
    assert got["n_assessment_areas"]["value"] == ""
    # FDIC ISB component ratings are not in the manifest and are applicable: empty.
    assert got["lending_test_rating"]["value"] == "" and got["lending_test_rating"]["null_reason"] == ""


def test_init_sheet_prefills_frb_isb_components_from_manifest(manifest):
    rows = label.init_sheet(manifest, "dev")
    got = {r["field"]: r for r in rows if r["doc_id"] == "FRB_3002_20240102"}
    assert got["lending_test_rating"]["value"] == "NI" and got["lending_test_rating"]["prefilled"] == "true"
    assert got["cd_test_rating"]["value"] == "S" and got["cd_test_rating"]["prefilled"] == "true"


def test_init_sheet_prefills_occ_small_lending_from_overall(manifest):
    rows = label.init_sheet(manifest, "dev")
    by = {(r["doc_id"], r["field"]): r for r in rows}
    small = by[("OCC_1001_20240304", "lending_test_rating")]
    assert small["value"] == "S" and small["prefilled"] == "true" and small["null_reason"] == ""
    # OCC ISB and FDIC ISB lending ratings are not derivable: left empty.
    assert by[("OCC_1002_20240506", "lending_test_rating")]["value"] == ""
    assert by[("FDIC_2002_20230910", "lending_test_rating")]["value"] == ""


@pytest.mark.parametrize(
    "doc_id,lending,cd",
    [
        ("OCC_1001_20240304", "", "absent_from_document"),
        ("OCC_1002_20240506", "", ""),
        ("FDIC_2001_20230708", "absent_from_document", "absent_from_document"),
        ("FDIC_2002_20230910", "", ""),
        ("FRB_3001_20241112", "absent_from_document", "absent_from_document"),
        ("FRB_3002_20240102", "", ""),
    ],
)
def test_init_sheet_applicability_defaults(manifest, doc_id, lending, cd):
    rows = label.init_sheet(manifest, "dev")
    got = {r["field"]: r for r in rows if r["doc_id"] == doc_id}
    assert got["lending_test_rating"]["null_reason"] == lending
    assert got["cd_test_rating"]["null_reason"] == cd
    for f, expected in (("lending_test_rating", lending), ("cd_test_rating", cd)):
        if expected:
            assert got[f]["prefilled"] == "true" and got[f]["value"] == ""


def test_init_sheet_writes_csv_and_test_split(manifest, tmp_path):
    out = tmp_path / "labels_test.csv"
    rows = label.init_sheet(manifest, "test", out)
    assert out.exists()
    assert {r["doc_id"] for r in rows} == {"OCC_1003_20250203", "FDIC_2003_20250405"}
    assert label.read_sheet(out)[0]["doc_id"] == "FDIC_2003_20250405"


def test_init_sheet_refuses_bank_overlap(manifest):
    manifest.append(_doc("OCC", "1001", date(2025, 8, 9), "small", "test"))
    with pytest.raises(ValueError, match="1001"):
        label.init_sheet(manifest, "dev")
    # Other splits are not guarded.
    assert label.init_sheet(manifest, "corpus")


# --------------------------------------------------------------------------- validate


def test_valid_sheet_has_no_problems(sheet):
    _, path = sheet
    assert label.validate(path) == []


def test_validate_rejects_missing_columns(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("doc_id,field\nX,regulator\n", encoding="utf-8")
    assert _rules(label.validate(p)) == {"columns"}


@pytest.mark.parametrize(
    "doc_id,field,cells,rule",
    [
        ("OCC_1001_20240304", "regulator", {"field": "bogus"}, "unknown_field"),
        ("OCC_1001_20240304", "overall_rating", {"value": "Great"}, "bad_value"),
        ("OCC_1001_20240304", "loan_products_evaluated", {"value": "home_mortgage|credit_card"}, "bad_value"),
        ("OCC_1001_20240304", "loan_products_evaluated", {"value": "home_mortgage|"}, "bad_value"),
        ("OCC_1001_20240304", "finding_present", {"value": "yes"}, "bad_value"),
        ("OCC_1001_20240304", "n_assessment_areas", {"value": "two"}, "bad_value"),
        ("OCC_1001_20240304", "inside_aa_share_number_pct", {"value": "120"}, "bad_value"),
        ("OCC_1001_20240304", "avg_ltd_ratio_pct", {"value": "-1"}, "bad_value"),
        ("OCC_1001_20240304", "total_assets_usd", {"value": "0"}, "bad_value"),
        ("OCC_1001_20240304", "exam_date", {"value": "2024-13-01"}, "bad_date"),
        ("OCC_1001_20240304", "prior_evaluation_date", {"value": "1/1/2019"}, "bad_date"),
        ("OCC_1001_20240304", "prior_rating", {"value": "", "null_reason": "missing"}, "bad_null_reason"),
        ("OCC_1001_20240304", "prior_rating", {"null_reason": "absent_from_document"}, "value_and_null"),
        ("OCC_1001_20240304", "prior_rating", {"value": "", "quote": ""}, "empty"),
        ("OCC_1001_20240304", "overall_rating", {"value": "", "null_reason": "absent_from_document"}, "not_nullable"),
        ("OCC_1001_20240304", "prior_rating", {"minutes": ""}, "missing_minutes"),
        ("OCC_1001_20240304", "prior_rating", {"minutes": "fast"}, "missing_minutes"),
        ("OCC_1001_20240304", "prior_rating", {"evidence_page": ""}, "missing_evidence_page"),
        ("OCC_1001_20240304", "prior_rating", {"evidence_page": "p3"}, "bad_evidence_page"),
        ("OCC_1001_20240304", "prior_rating", {"quote": ""}, "missing_quote"),
        ("OCC_1001_20240304", "prior_rating", {"scope": "nation"}, "bad_scope"),
        ("OCC_1001_20240304", "prior_rating", {"labeled_at": "yesterday"}, "bad_labeled_at"),
    ],
)
def test_validate_row_rules(sheet, doc_id, field, cells, rule):
    rows, path = sheet
    _set(rows, doc_id, field, **cells)
    label.write_sheet(rows, path)
    assert rule in _rules(label.validate(path))


def test_validate_duplicate_row(sheet):
    rows, path = sheet
    rows.append(dict(rows[0]))
    label.write_sheet(rows, path)
    assert "duplicate_row" in _rules(label.validate(path))


def test_consistency_lte_exam_date(sheet):
    rows, path = sheet
    _set(rows, "OCC_1001_20240304", "lending_data_period_end", value="2024-03-05")
    label.write_sheet(rows, path)
    probs = [p for p in label.validate(path) if p.rule == "lte_exam_date"]
    assert len(probs) == 1 and probs[0].field == "lending_data_period_end"
    # The day of the exam itself is allowed.
    _set(rows, "OCC_1001_20240304", "lending_data_period_end", value="2024-03-04")
    label.write_sheet(rows, path)
    assert "lte_exam_date" not in _rules(label.validate(path))


def test_consistency_lte_exam_date_falls_back_to_doc_id(sheet):
    rows, path = sheet
    _set(rows, "OCC_1001_20240304", "exam_date", value="", null_reason="", quote="", evidence_page="")
    _set(rows, "OCC_1001_20240304", "lending_data_period_end", value="2024-06-30")
    label.write_sheet(rows, path)
    assert "lte_exam_date" in _rules(label.validate(path))


def test_consistency_lte_n_assessment_areas(sheet):
    rows, path = sheet
    _set(rows, "FDIC_2002_20230910", "n_full_scope", value="3")
    label.write_sheet(rows, path)
    assert "lte_n_assessment_areas" in _rules(label.validate(path))


def test_consistency_n_full_scope_na_when_single_aa(sheet):
    rows, path = sheet
    _set(rows, "FDIC_2002_20230910", "n_assessment_areas", value="1")
    label.write_sheet(rows, path)
    assert "na_when_single_aa" in _rules(label.validate(path))
    _set(rows, "FDIC_2002_20230910", "n_full_scope", value="", null_reason="not_applicable", quote="", evidence_page="")
    label.write_sheet(rows, path)
    assert label.validate(path) == []
    _set(rows, "FDIC_2002_20230910", "n_assessment_areas", value="3")
    label.write_sheet(rows, path)
    assert "na_when_single_aa" in _rules(label.validate(path))


def test_consistency_equals_overall_for_occ_small(sheet):
    rows, path = sheet
    _set(rows, "OCC_1001_20240304", "lending_test_rating", value="O")  # overall is S
    label.write_sheet(rows, path)
    assert "equals_overall_for_OCC_small" in _rules(label.validate(path))
    # The same disagreement is allowed for an OCC ISB document.
    _set(rows, "OCC_1001_20240304", "lending_test_rating", value="S")
    _set(rows, "OCC_1002_20240506", "lending_test_rating", value="S")  # overall is O
    label.write_sheet(rows, path)
    assert "equals_overall_for_OCC_small" not in _rules(label.validate(path))


def test_consistency_equals_overall_when_level_overall(sheet):
    rows, path = sheet
    doc = "FDIC_2002_20230910"  # overall NI
    _set(rows, doc, "finding_present", value="true")
    _set(rows, doc, "finding_type", value="discrimination", null_reason="", evidence_page="12", quote="q")
    _set(rows, doc, "rating_effect", value="substantive_lowered", null_reason="", evidence_page="12", quote="q")
    _set(rows, doc, "rating_lowered", value="true")
    _set(rows, doc, "rating_lowered_level", value="overall", null_reason="", evidence_page="12", quote="q")
    _set(rows, doc, "rating_before", value="S", null_reason="", evidence_page="12", quote="q")
    _set(rows, doc, "rating_after", value="S", null_reason="", evidence_page="12", quote="q")
    label.write_sheet(rows, path)
    assert "equals_overall_when_level_overall" in _rules(label.validate(path))
    _set(rows, doc, "rating_after", value="NI")
    label.write_sheet(rows, path)
    assert label.validate(path) == []
    # At a non-overall level the rule does not apply.
    _set(rows, doc, "rating_after", value="S")
    _set(rows, doc, "rating_lowered_level", value="state_or_msa")
    label.write_sheet(rows, path)
    assert "equals_overall_when_level_overall" not in _rules(label.validate(path))


def test_problem_str_and_cli_validate(sheet, capsys):
    rows, path = sheet
    _set(rows, "OCC_1001_20240304", "prior_rating", minutes="")
    label.write_sheet(rows, path)
    assert label.main(["validate", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[missing_minutes]" in out and "1 problem(s)" in out
    _set(rows, "OCC_1001_20240304", "prior_rating", minutes="1")
    label.write_sheet(rows, path)
    assert label.main(["validate", str(path)]) == 0


# --------------------------------------------------------------------------- load_labels


def test_load_labels_coerces_types(sheet):
    _, path = sheet
    labels = label.load_labels(path)
    by = {(r.doc_id, r.field): r for r in labels}
    r = by[("FDIC_2002_20230910", "n_assessment_areas")]
    assert r.value == 2 and r.evidence_page == 1 and r.minutes == 0.5 and r.labeler == "fh"
    assert r.labeled_at is not None and r.labeled_at.year == 2026
    assert by[("FDIC_2002_20230910", "exam_date")].value == date(2023, 9, 10)
    assert by[("FDIC_2002_20230910", "loan_products_evaluated")].value == ["home_mortgage", "small_business"]
    assert by[("FDIC_2002_20230910", "finding_present")].value is False
    assert by[("FDIC_2002_20230910", "inside_aa_share_number_pct")].value == 75.2
    na = by[("FDIC_2002_20230910", "laws_cited")]
    assert na.value is None and na.null_reason == "not_applicable" and na.is_null
    abs_ = by[("FDIC_2001_20230708", "cd_test_rating")]
    assert abs_.null_reason == "absent_from_document"


def test_load_labels_strict_raises(sheet):
    rows, path = sheet
    _set(rows, "OCC_1001_20240304", "overall_rating", value="Great")
    label.write_sheet(rows, path)
    with pytest.raises(ValueError, match="bad_value"):
        label.load_labels(path)
    loose = label.load_labels(path, strict=False)
    assert any(r.value == "Great" for r in loose)


# --------------------------------------------------------------------------- hashes


def test_split_hash_is_deterministic(manifest):
    h1 = label.split_hash(manifest, "dev")
    shuffled = list(manifest)
    random.Random(7).shuffle(shuffled)
    assert label.split_hash(shuffled, "dev") == h1
    assert len(h1) == 64
    assert label.split_hash(manifest, "test") != h1
    # Adding a document to the split changes the hash; changing another split does not.
    manifest.append(_doc("FRB", "3009", date(2025, 1, 1), "small", "test"))
    assert label.split_hash(manifest, "dev") == h1
    assert label.split_hash(manifest, "test") != label.split_hash(manifest[:-1], "test")


def test_labels_hash_tracks_content(sheet):
    rows, path = sheet
    h1 = label.labels_hash(path)
    assert h1 == label.labels_hash(path)
    _set(rows, "OCC_1001_20240304", "prior_rating", notes="changed")
    label.write_sheet(rows, path)
    assert label.labels_hash(path) != h1


def test_record_hashes_appends_markdown(tmp_path, manifest, sheet):
    _, labels = sheet
    out = tmp_path / "hashes.md"
    label.record_hashes("test", 2, label.split_hash(manifest, "test"), None, None, out)
    label.record_hashes("dev", 6, label.split_hash(manifest, "dev"), "labels_dev.csv", label.labels_hash(labels), out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Committed hashes")
    lines = [ln for ln in text.splitlines() if ln.startswith("| 20")]
    assert len(lines) == 2 and "| test | 2 |" in lines[0] and "labels_dev.csv" in lines[1]


# --------------------------------------------------------------------------- overlap and CLI


def test_bank_overlap(manifest):
    assert label.bank_overlap(manifest) == []
    manifest.append(_doc("FDIC", "0002001", date(2025, 8, 9), "isb", "test"))  # same bank, leading zeros
    assert label.bank_overlap(manifest) == [("FDIC", "2001")]
    # Same number under another regulator is a different bank.
    manifest.append(_doc("OCC", "2002", date(2025, 8, 9), "isb", "test"))
    assert label.bank_overlap(manifest) == [("FDIC", "2001")]


def _write_manifest(manifest: list[DocRecord], path: Path) -> Path:
    import csv

    cols = list(DocRecord.model_fields)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for d in manifest:
            w.writerow({c: ("" if getattr(d, c) is None else getattr(d, c)) for c in cols})
    return path


def test_cli_init_hash_overlap(manifest, tmp_path, capsys):
    mpath = _write_manifest(manifest, tmp_path / "corpus.csv")
    out = tmp_path / "labels_dev.csv"
    assert label.main(["--manifest", str(mpath), "init", "--split", "dev", "--out", str(out)]) == 0
    assert "6 dev documents" in capsys.readouterr().out
    assert len(label.read_sheet(out)) == 6 * len(load_fields()["fields"])

    hashes = tmp_path / "hashes.md"
    assert label.main(["--manifest", str(mpath), "hash", "--split", "test", "--out", str(hashes)]) == 0
    assert label.split_hash(manifest, "test") in hashes.read_text(encoding="utf-8")

    assert label.main(["--manifest", str(mpath), "overlap"]) == 0
    manifest.append(_doc("OCC", "1001", date(2025, 8, 9), "small", "test"))
    _write_manifest(manifest, mpath)
    assert label.main(["--manifest", str(mpath), "overlap"]) == 1
    assert "OCC 1001" in capsys.readouterr().out
