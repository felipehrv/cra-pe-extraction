"""Tests for cra_extract.overrides: schema, pure apply, file steps and the CLI.

The synthetic manifest has one FDIC row with an impossible file-id date (renamed by an
exam_date override), an OCC and an FRB row (exclusions), a dev row and a test row
(protected), and a row whose doc_id the rename would collide with in one test. The
portfolio tests build a PDF portfolio with pymupdf. The live tests run the real overrides
file against a temporary copy of ``config/corpus.csv``.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import pymupdf
import pytest
import yaml

from cra_extract import index as ix
from cra_extract import overrides as ovr
from cra_extract import strata
from cra_extract.config import CORPUS_PATH, TEXT_DIR

COLUMNS = [
    "doc_id", "regulator", "bank_id", "bank_name", "exam_date", "release_date", "method",
    "url", "sha256", "pages", "chars_per_page", "text_extractable", "split", "source_file_id", "notes",
]
ANOMALY = "index anomaly: exam date is after the release date"


def _row(doc_id: str, split: str = "corpus", **kw: str) -> dict[str, str]:
    reg, bank, exam = ovr.parse_doc_id(doc_id)
    row = {c: "" for c in COLUMNS}
    row.update(
        doc_id=doc_id, regulator=reg, bank_id=bank, bank_name=f"Bank {bank}", exam_date=exam.isoformat(),
        release_date="2024-09-01", method="small", url=f"https://example.invalid/{doc_id}.pdf",
        sha256="a" * 64, pages="10", chars_per_page="2000.0", text_extractable="True", split=split,
    )
    row.update(kw)
    return row


def _manifest() -> list[dict[str, str]]:
    return [
        _row("FDIC_445_20280408", notes=ANOMALY, source_file_id="2024/445_280408.PDF"),
        _row("FDIC_12299_20220912", split="dev"),
        _row("FRB_614948_20250902", sha256="", pages="", chars_per_page="", text_extractable=""),
        _row("OCC_13682_20240819", split="test"),
        _row("OCC_9183_20230213"),
    ]


def _evidence(doc: str = "FDIC_445_20280408") -> list[dict]:
    return [{"doc": doc, "page": 1, "quote": "April 8, 2024"}, {"log": "data/index/fdic.csv", "detail": "file id"}]


def _rename(**kw) -> dict:
    entry = {
        "doc_id": "FDIC_445_20280408", "field": "exam_date", "old": "2028-04-08", "new": "2024-04-08",
        "new_doc_id": "FDIC_445_20240408", "reason": "fdic_file_id_date_impossible",
        "evidence": _evidence(), "date": "2026-10-08",
    }
    entry.update(kw)
    return entry


def _exclusion(doc_id: str, reason: str = "wrong_pdf_link") -> dict:
    return {"doc_id": doc_id, "reason": reason, "evidence": [{"log": "x.csv", "detail": "y"}], "date": "2026-10-08"}


def _data(**kw) -> dict:
    data = {
        "version": 1,
        "updated": "2026-10-08",
        "field_overrides": [
            _rename(),
            {
                "doc_id": "FDIC_445_20280408", "field": "notes", "old": ANOMALY, "new": "corrected; alias FDIC_445_20280408",
                "reason": "fdic_file_id_date_impossible", "evidence": [{"log": "l", "detail": "d"}], "date": "2026-10-08",
            },
        ],
        "exclusions": [_exclusion("OCC_9183_20230213"), _exclusion("FRB_614948_20250902", "pdf_unavailable_http_500")],
    }
    data.update(kw)
    return data


def _ov(**kw) -> ovr.Overrides:
    return ovr.parse_overrides(_data(**kw))


# ----------------------------------------------------------------------------- schema


def test_schema_accepts_the_synthetic_file():
    ov = _ov()
    assert ov.aliases() == {"FDIC_445_20280408": "FDIC_445_20240408"}
    assert ov.resolve("FDIC_445_20280408") == "FDIC_445_20240408"
    assert ov.resolve("OCC_9183_20230213") == "OCC_9183_20230213"
    assert len(ov.entries_for("FDIC_445_20240408")) == 2
    assert len(ov.entries_for("FDIC_445_20280408")) == 2


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["field_overrides"][0].update(old=date(2028, 4, 8)), "valid string"),
        (lambda d: d["field_overrides"][0].update(evidence=[]), "at least 1"),
        (lambda d: d["field_overrides"][0].pop("evidence"), "evidence"),
        (lambda d: d["field_overrides"][0].update(field="split"), "managed by the overrides module"),
        (lambda d: d["field_overrides"][0].update(field="exam_date_source"), "managed by the overrides module"),
        (lambda d: d["field_overrides"][0].update(reason="Bad Reason"), "snake_case"),
        (lambda d: d["field_overrides"][0].update(new_doc_id="FDIC_445_20240409"), "new_doc_id must be FDIC_445_20240408"),
        (lambda d: d["field_overrides"][0].pop("new_doc_id"), "new_doc_id must be FDIC_445_20240408"),
        (lambda d: d["field_overrides"][1].update(new_doc_id="FDIC_445_20240408"), "only goes on regulator"),
        (lambda d: d["field_overrides"][0].update(new="2024-04-31"), "not an ISO date"),
        (lambda d: d["field_overrides"][0].update(new="2028-04-08"), "old and new are equal"),
        (lambda d: d["field_overrides"][0].update(doc_id="FDIC-445"), "malformed doc_id"),
        (lambda d: d["field_overrides"][0].update(colour="red"), "Extra inputs"),
        (lambda d: d["field_overrides"].append(copy.deepcopy(d["field_overrides"][1])), "two overrides of"),
        (lambda d: d["exclusions"].append(_exclusion("OCC_9183_20230213")), "excluded twice"),
        (lambda d: d["exclusions"].append(_exclusion("FDIC_445_20240408")), "entries use the original doc_id"),
        (lambda d: d["field_overrides"][0]["evidence"].append({"doc": "FDIC_445_20280408", "page": 1}), "page' and a non-empty 'quote'"),
        (lambda d: d["field_overrides"][0]["evidence"].append({"doc": "FDIC_445_20280408", "log": "x", "detail": "y"}), "exactly one of"),
        (lambda d: d["field_overrides"][0]["evidence"].append({"log": "x"}), "non-empty 'detail'"),
        (lambda d: d["field_overrides"][0]["evidence"].append({"log": "x", "detail": "y", "page": 2}), "takes no 'page'"),
        (lambda d: d.update(version=0), "version"),
        (lambda d: d.pop("updated"), "updated"),
    ],
)
def test_schema_rejects(mutate, message):
    data = _data()
    mutate(data)
    with pytest.raises(ovr.OverrideError, match=message):
        ovr.parse_overrides(data)


def test_schema_rejects_non_mapping_and_yaml_dates():
    with pytest.raises(ovr.OverrideError, match="mapping"):
        ovr.parse_overrides(["not", "a", "mapping"])
    # An unquoted YAML date is read as a date and refused, so cells are compared verbatim.
    text = yaml.safe_dump(_data()).replace("old: '2028-04-08'", "old: 2028-04-08")
    assert "old: 2028-04-08" in text
    with pytest.raises(ovr.OverrideError, match="valid string"):
        ovr.parse_overrides(yaml.safe_load(text))


def test_schema_pdf_replacement_needs_matching_sha_override():
    rep = {
        "doc_id": "FRB_785473_20220418", "method": "portfolio_embedded_file", "embedded_file": "pe.pdf",
        "original_sha256": "1" * 64, "sha256": "2" * 64, "reason": "pdf_portfolio_extracted",
        "evidence": [{"log": "l", "detail": "d"}], "date": "2026-10-08",
    }
    with pytest.raises(ovr.OverrideError, match="needs a sha256 field override"):
        _ov(pdf_replacements=[rep])
    sha = {
        "doc_id": "FRB_785473_20220418", "field": "sha256", "old": "1" * 64, "new": "2" * 64,
        "reason": "pdf_portfolio_extracted", "evidence": [{"log": "l", "detail": "d"}], "date": "2026-10-08",
    }
    ov = _ov(pdf_replacements=[rep], field_overrides=[*_data()["field_overrides"], sha])
    assert ov.pdf_replacements[0].embedded_file == "pe.pdf"
    with pytest.raises(ovr.OverrideError, match="lowercase hex"):
        _ov(pdf_replacements=[{**rep, "sha256": "XYZ"}], field_overrides=[sha])
    with pytest.raises(ovr.OverrideError, match="both excluded and given a replacement"):
        _ov(pdf_replacements=[rep], field_overrides=[sha], exclusions=[_exclusion("FRB_785473_20220418")])


# ----------------------------------------------------------------------------- pure apply


def test_apply_renames_records_alias_and_excludes():
    rows = _manifest()
    before = copy.deepcopy(rows)
    out = ovr.apply_overrides(rows, _ov())
    assert rows == before, "apply_overrides must not change its input"
    assert [r["doc_id"] for r in out] == [
        "FDIC_445_20240408", "FDIC_12299_20220912", "FRB_614948_20250902", "OCC_13682_20240819", "OCC_9183_20230213",
    ]
    renamed = out[0]
    assert renamed["exam_date"] == "2024-04-08"
    assert renamed["notes"] == "corrected; alias FDIC_445_20280408"
    assert renamed["source_file_id"] == "2024/445_280408.PDF", "the agency file id is kept verbatim"
    assert {r["doc_id"]: r["split"] for r in out} == {
        "FDIC_445_20240408": "corpus", "FDIC_12299_20220912": "dev", "FRB_614948_20250902": "excluded",
        "OCC_13682_20240819": "test", "OCC_9183_20230213": "excluded",
    }
    assert {r["doc_id"]: r["exam_date_source"] for r in out} == {
        "FDIC_445_20240408": "override", "FDIC_12299_20220912": "fdic_file_id",
        "FRB_614948_20250902": "index_exam_date", "OCC_13682_20240819": "index_exam_date",
        "OCC_9183_20230213": "index_exam_date",
    }
    assert list(out[0]) == [*COLUMNS, "exam_date_source"]
    changes = ovr.changed_cells(rows, out)
    assert {(c.doc_id, c.column) for c in changes if c.column != "exam_date_source"} == {
        ("FDIC_445_20240408", "doc_id"), ("FDIC_445_20240408", "exam_date"), ("FDIC_445_20240408", "notes"),
        ("FRB_614948_20250902", "split"), ("OCC_9183_20230213", "split"),
    }
    assert sum(c.column == "exam_date_source" for c in changes) == len(rows)


def test_apply_is_idempotent_and_keeps_column_position():
    ov = _ov()
    once = ovr.apply_overrides(_manifest(), ov)
    twice = ovr.apply_overrides(once, ov)
    assert twice == once
    assert ovr.changed_cells(once, twice) == []
    # An existing exam_date_source column keeps its position.
    cols = ["doc_id", "exam_date_source", *[c for c in COLUMNS if c != "doc_id"]]
    rows = [{c: r.get(c, "") for c in cols} for r in _manifest()]
    assert list(ovr.apply_overrides(rows, ov)[0]) == cols


def test_apply_resolves_alias_for_later_entries():
    # An exclusion of a renamed document names its original id and reaches the renamed row.
    ov = _ov(exclusions=[_exclusion("FDIC_445_20280408"), *_data()["exclusions"]])
    out = ovr.apply_overrides(_manifest(), ov)
    assert {r["doc_id"]: r["split"] for r in out}["FDIC_445_20240408"] == "excluded"
    again = ovr.apply_overrides(out, ov)
    assert again == out


def test_apply_refuses_dev_and_test_rows():
    with pytest.raises(ovr.OverrideError, match="dev split; its exclusion is refused"):
        ovr.apply_overrides(_manifest(), _ov(exclusions=[_exclusion("FDIC_12299_20220912")]))
    with pytest.raises(ovr.OverrideError, match="test split; its exclusion is refused"):
        ovr.apply_overrides(_manifest(), _ov(exclusions=[_exclusion("OCC_13682_20240819")]))
    move_dev = _rename(doc_id="FDIC_12299_20220912", old="2022-09-12", new="2022-09-13", new_doc_id="FDIC_12299_20220913")
    with pytest.raises(ovr.OverrideError, match="dev split; an override that changes its doc_id is refused"):
        ovr.apply_overrides(_manifest(), _ov(field_overrides=[move_dev]))
    # An override that keeps the doc_id is allowed on a gold row.
    keep = {
        "doc_id": "OCC_13682_20240819", "field": "bank_name", "old": "Bank 13682", "new": "First Neighbor Bank, N.A.",
        "reason": "index_name_typo", "evidence": [{"log": "l", "detail": "d"}], "date": "2026-10-08",
    }
    out = ovr.apply_overrides(_manifest(), _ov(field_overrides=[keep]))
    row = next(r for r in out if r["doc_id"] == "OCC_13682_20240819")
    assert row["bank_name"] == "First Neighbor Bank, N.A." and row["split"] == "test"
    assert row["exam_date_source"] == "index_exam_date"


def test_apply_refuses_conflicts_unknowns_and_collisions():
    rows = _manifest()
    rows[0]["exam_date"] = "2028-04-09"
    with pytest.raises(ovr.OverrideError, match="manifest has '2028-04-09'"):
        ovr.apply_overrides(rows, _ov())
    rows = [r for r in _manifest() if r["doc_id"] != "OCC_9183_20230213"]
    with pytest.raises(ovr.OverrideError, match="OCC_9183_20230213: not in the manifest"):
        ovr.apply_overrides(rows, _ov())
    bad_col = {**_data()["field_overrides"][1], "field": "colour"}
    with pytest.raises(ovr.OverrideError, match="no manifest column 'colour'"):
        ovr.apply_overrides(_manifest(), _ov(field_overrides=[_rename(), bad_col]))
    rows = [*_manifest(), _row("FDIC_445_20240408")]
    with pytest.raises(ovr.OverrideError, match="already in the manifest"):
        ovr.apply_overrides(rows, _ov())
    rows = [*_manifest(), _row("FDIC_445_20240408")]
    rows[0]["doc_id"] = rows[-1]["doc_id"]
    with pytest.raises(ovr.OverrideError, match="duplicate doc_id"):
        ovr.apply_overrides(rows, _ov())


def test_apply_refuses_undocumented_exclusion():
    rows = _manifest()
    rows.append(_row("FRB_785473_20220418", split="excluded"))
    with pytest.raises(ovr.OverrideError, match="no exclusion entry.*FRB_785473_20220418"):
        ovr.apply_overrides(rows, _ov())


@pytest.mark.parametrize("cells", [
    {"sha256": "", "text_extractable": "False"},
    {"sha256": "", "text_extractable": "True"},
    {"text_extractable": "False"},
    {"text_extractable": ""},
])
def test_apply_refuses_non_evaluable_rows_left_in_the_pool(cells):
    # Plan, Exclusions: every non-evaluable row is set to excluded before draw-test.
    rows = _manifest()
    rows.append(_row("FDIC_10159_20231206", **cells))
    with pytest.raises(ovr.OverrideError, match="non-evaluable rows.*FDIC_10159_20231206"):
        ovr.apply_overrides(rows, _ov())
    assert ovr.non_evaluable_unexcluded(rows) == ["FRB_614948_20250902", "FDIC_10159_20231206"]


def test_apply_accepts_non_evaluable_rows_once_excluded():
    rows = _manifest()
    rows.append(_row("FDIC_10159_20231206", sha256="", text_extractable="False"))
    data = _data()
    data["exclusions"].append(_exclusion("FDIC_10159_20231206", "pdf_unavailable_http_404"))
    out = ovr.apply_overrides(rows, ovr.parse_overrides(data))
    assert {r["doc_id"]: r["split"] for r in out}["FDIC_10159_20231206"] == "excluded"
    assert ovr.non_evaluable_unexcluded(out) == []


def test_default_exam_date_source_and_index_columns():
    assert ovr.default_exam_date_source("FDIC") == "fdic_file_id"
    assert ovr.default_exam_date_source("OCC") == "index_exam_date"
    assert ovr.default_exam_date_source("FRB") == "index_exam_date"
    assert set(ovr.EXAM_DATE_SOURCES) == {"index_exam_date", "fdic_file_id", "override"}
    assert ix.MANIFEST_COLUMNS[-1] == ovr.EXAM_DATE_SOURCE_COLUMN


def test_index_build_manifest_fills_exam_date_source(occ_rows, fdic_payload, frb_frames):
    manifest, _ = ix.build_manifest(
        ix.occ_index(occ_rows), ix.fdic_index(fdic_payload), frb_frames[0],
        (date(2023, 1, 1), date(2025, 12, 31)),
    )
    expected = manifest["regulator"].map({"FDIC": "fdic_file_id", "OCC": "index_exam_date", "FRB": "index_exam_date"})
    assert (manifest["exam_date_source"] == expected).all()
    assert list(manifest.columns) == ix.MANIFEST_COLUMNS


@pytest.fixture
def occ_rows(index_fixtures_dir):
    return json.loads((index_fixtures_dir / "occ_cra_eval_2023_2025.json").read_text(encoding="utf-8"))


@pytest.fixture
def fdic_payload(index_fixtures_dir):
    return json.loads((index_fixtures_dir / "fdic_crapes_release2025.json").read_text(encoding="utf-8"))


@pytest.fixture
def frb_frames(index_fixtures_dir):
    return ix.frb_index(index_fixtures_dir / "frb_cra_ratings_all.csv")


# ----------------------------------------------------------------------------- files


def _pe_pdf(path: Path, pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=9)
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return data


def _portfolio(path: Path, name: str, embedded: bytes) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "For the best experience, open this PDF portfolio in Acrobat X.")
    doc.embfile_add(name, embedded, filename=name)
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return data


def _pe_pages() -> list[str]:
    cover = "PUBLIC DISCLOSURE\nApril 18, 2022\nCOMMUNITY REINVESTMENT ACT\nPERFORMANCE EVALUATION\nFirst Savings Bank\n"
    body = "\n".join(["The evaluation period covers the period since the previous evaluation."] * 10)
    return [cover + body, body]


def _replacement(original: bytes, embedded: bytes, name: str = "First Savings Bank PE.pdf") -> ovr.PdfReplacement:
    return ovr.PdfReplacement(
        doc_id="FRB_785473_20220418", method="portfolio_embedded_file", embedded_file=name,
        original_sha256=hashlib.sha256(original).hexdigest(), sha256=hashlib.sha256(embedded).hexdigest(),
        reason="pdf_portfolio_extracted", evidence=[{"log": "l", "detail": "d"}], date="2026-10-08",
    )


def test_replace_portfolio_extracts_keeps_original_and_parses(tmp_path):
    pdf_dir, text_dir = tmp_path / "pdfs", tmp_path / "text"
    pdf_dir.mkdir()
    embedded = _pe_pdf(tmp_path / "pe.pdf", _pe_pages())
    original = _portfolio(pdf_dir / "FRB_785473_20220418.pdf", "First Savings Bank PE.pdf", embedded)
    rep = _replacement(original, embedded)
    assert ovr.embedded_pdf(pdf_dir / "FRB_785473_20220418.pdf", rep.embedded_file) == embedded

    assert ovr.replace_portfolio(rep, rep.doc_id, pdf_dir, text_dir, dry_run=True) == ("extracted", None)
    assert (pdf_dir / "FRB_785473_20220418.pdf").read_bytes() == original

    status, stats = ovr.replace_portfolio(rep, rep.doc_id, pdf_dir, text_dir)
    assert status == "extracted"
    assert (pdf_dir / "FRB_785473_20220418.pdf").read_bytes() == embedded
    assert (pdf_dir / "FRB_785473_20220418.portfolio.pdf").read_bytes() == original
    assert stats.pages == 2 and stats.text_extractable
    pages = json.loads((text_dir / "FRB_785473_20220418.pages.json").read_text(encoding="utf-8"))
    assert "April 18, 2022" in pages[0]["text"]
    row = {"doc_id": rep.doc_id, "pages": "2", "chars_per_page": f"{stats.chars_per_page:.1f}", "text_extractable": "True"}
    assert ovr.check_replacement_stats(row, stats) == []
    assert ovr.check_replacement_stats({**row, "pages": "1"}, stats) == [f"{rep.doc_id} pages: manifest '1', parsed '2'"]

    status, again = ovr.replace_portfolio(rep, rep.doc_id, pdf_dir, text_dir)
    assert status == "present" and again == stats


def test_replace_portfolio_reparses_stale_text_and_refuses_unknown_files(tmp_path):
    pdf_dir, text_dir = tmp_path / "pdfs", tmp_path / "text"
    pdf_dir.mkdir()
    embedded = _pe_pdf(pdf_dir / "FRB_785473_20220418.pdf", _pe_pages())
    rep = _replacement(b"portfolio bytes", embedded)
    # Text left over from the portfolio (one page) is re-extracted from the PE.
    text_dir.mkdir()
    (text_dir / "FRB_785473_20220418.pages.json").write_text(
        json.dumps([{"number": 1, "text": "portfolio notice", "chars": 16}]), encoding="utf-8"
    )
    status, stats = ovr.replace_portfolio(rep, rep.doc_id, pdf_dir, text_dir)
    assert status == "present" and stats.pages == 2

    assert ovr.replace_portfolio(rep, "FRB_1_20220418", pdf_dir, text_dir) == ("missing", None)
    (pdf_dir / "FRB_2_20220418.pdf").write_bytes(b"%PDF-other")
    with pytest.raises(ovr.OverrideError, match="neither the portfolio"):
        ovr.replace_portfolio(rep, "FRB_2_20220418", pdf_dir, text_dir)
    original = _portfolio(pdf_dir / "FRB_3_20220418.pdf", "other.pdf", embedded)
    with pytest.raises(ovr.OverrideError, match="no embedded file 'First Savings Bank PE.pdf'"):
        ovr.replace_portfolio(_replacement(original, embedded), "FRB_3_20220418", pdf_dir, text_dir)
    plain = _pe_pdf(pdf_dir / "FRB_4_20220418.pdf", ["no attachments"])
    with pytest.raises(ovr.OverrideError, match="has no embedded files"):
        ovr.replace_portfolio(_replacement(plain, embedded), "FRB_4_20220418", pdf_dir, text_dir)


def test_rename_doc_files(tmp_path):
    pdf_dir, text_dir = tmp_path / "pdfs", tmp_path / "text"
    pdf_dir.mkdir()
    text_dir.mkdir()
    for p in ovr.doc_files("FDIC_445_20280408", pdf_dir, text_dir)[::2]:
        p.write_text(p.name, encoding="utf-8")
    planned = ovr.rename_doc_files("FDIC_445_20280408", "FDIC_445_20240408", pdf_dir, text_dir, dry_run=True)
    assert [d.name for _, d in planned] == ["FDIC_445_20240408.pdf", "FDIC_445_20240408.txt"]
    assert not (pdf_dir / "FDIC_445_20240408.pdf").exists()
    moved = ovr.rename_doc_files("FDIC_445_20280408", "FDIC_445_20240408", pdf_dir, text_dir)
    assert moved == planned
    assert (text_dir / "FDIC_445_20240408.txt").read_text(encoding="utf-8") == "FDIC_445_20280408.txt"
    assert ovr.rename_doc_files("FDIC_445_20280408", "FDIC_445_20240408", pdf_dir, text_dir) == []
    (pdf_dir / "FDIC_445_20280408.pdf").write_text("different", encoding="utf-8")
    with pytest.raises(ovr.OverrideError, match="exist and differ"):
        ovr.rename_doc_files("FDIC_445_20280408", "FDIC_445_20240408", pdf_dir, text_dir)


def test_check_evidence_resolves_aliases(tmp_path):
    ov = _ov()
    problems, skipped = ovr.check_evidence(ov, tmp_path)
    assert problems == [] and skipped == ["FDIC_445_20280408 evidence FDIC_445_20280408 p1: no page text on disk"]
    pages = [{"number": 1, "text": "PUBLIC DISCLOSURE \n\nApril 8,\n 2024 \n"}]
    (tmp_path / "FDIC_445_20240408.pages.json").write_text(json.dumps(pages), encoding="utf-8")
    assert ovr.check_evidence(ov, tmp_path) == ([], [])
    pages[0]["text"] = "April 9, 2024"
    (tmp_path / "FDIC_445_20240408.pages.json").write_text(json.dumps(pages), encoding="utf-8")
    problems, _ = ovr.check_evidence(ov, tmp_path)
    assert problems == ["FDIC_445_20280408 evidence FDIC_445_20280408 p1: quote 'April 8, 2024' not on the page"]
    ov2 = _ov(field_overrides=[_rename(evidence=[{"doc": "FDIC_445_20280408", "page": 3, "quote": "x"}])])
    problems, _ = ovr.check_evidence(ov2, tmp_path)
    assert problems == ["FDIC_445_20280408 evidence FDIC_445_20280408 p3: the document has 1 pages"]


# ----------------------------------------------------------------------------- CLI


def _setup_cli(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    manifest, yml = tmp_path / "corpus.csv", tmp_path / "overrides.yaml"
    pdf_dir, text_dir = tmp_path / "pdfs", tmp_path / "text"
    pdf_dir.mkdir()
    text_dir.mkdir()
    strata.write_rows(COLUMNS, _manifest(), manifest)
    yml.write_text(yaml.safe_dump(_data(), sort_keys=False), encoding="utf-8")
    (pdf_dir / "FDIC_445_20280408.pdf").write_bytes(b"%PDF-445")
    (text_dir / "FDIC_445_20280408.pages.json").write_text(
        json.dumps([{"number": 1, "text": "PUBLIC DISCLOSURE April 8, 2024"}]), encoding="utf-8"
    )
    (text_dir / "FDIC_445_20280408.txt").write_text("PUBLIC DISCLOSURE April 8, 2024", encoding="utf-8")
    return manifest, yml, pdf_dir, text_dir


def _cli(yml: Path, manifest: Path, pdf_dir: Path, text_dir: Path, *extra: str) -> int:
    return ovr.main(
        ["--overrides", str(yml), "apply", "--manifest", str(manifest),
         "--pdf-dir", str(pdf_dir), "--text-dir", str(text_dir), *extra]
    )


def test_cli_round_trip(tmp_path, capsys):
    manifest, yml, pdf_dir, text_dir = _setup_cli(tmp_path)
    original = manifest.read_bytes()

    assert _cli(yml, manifest, pdf_dir, text_dir, "--dry-run") == 0
    assert manifest.read_bytes() == original
    assert (pdf_dir / "FDIC_445_20280408.pdf").exists()
    assert "would change" in capsys.readouterr().out

    assert _cli(yml, manifest, pdf_dir, text_dir) == 0
    out = capsys.readouterr().out
    assert "FDIC_445_20240408 doc_id: 'FDIC_445_20280408' -> 'FDIC_445_20240408'" in out
    assert "OCC_9183_20230213 split: 'corpus' -> 'excluded'" in out
    assert sorted(p.name for p in pdf_dir.iterdir()) == ["FDIC_445_20240408.pdf"]
    assert sorted(p.name for p in text_dir.iterdir()) == ["FDIC_445_20240408.pages.json", "FDIC_445_20240408.txt"]

    cols, rows = strata.read_rows(manifest)
    assert cols == [*COLUMNS, "exam_date_source"]
    changed = {("FDIC_445_20240408", "doc_id"), ("FDIC_445_20240408", "exam_date"), ("FDIC_445_20240408", "notes"),
               ("FRB_614948_20250902", "split"), ("OCC_9183_20230213", "split")}
    for before, after in zip(_manifest(), rows, strict=True):
        for col in COLUMNS:
            if (after["doc_id"], col) not in changed:
                assert after[col] == before[col], (after["doc_id"], col)
    # The rewrite keeps the csv layout: the original lines reappear with one cell appended.
    lines_before = original.decode().splitlines()
    lines_after = manifest.read_text(encoding="utf-8").splitlines()
    assert lines_after[0] == lines_before[0] + ",exam_date_source"
    assert lines_after[2] == lines_before[2] + ",fdic_file_id"

    # Second run: nothing to do, the file is byte-identical, the evidence check passes.
    applied = manifest.read_bytes()
    assert _cli(yml, manifest, pdf_dir, text_dir) == 0
    assert "already up to date" in capsys.readouterr().out
    assert manifest.read_bytes() == applied
    assert ovr.main(["--overrides", str(yml), "check", "--text-dir", str(text_dir)]) == 0

    # The drawn splits survive and the strata pool drops the excluded rows.
    records = strata.to_records(rows)
    pool = {d.doc_id for d in strata.eligible_pool(records, "test")}
    assert "OCC_9183_20230213" not in pool and "FRB_614948_20250902" not in pool
    assert "FDIC_445_20240408" in pool


def test_cli_refuses_and_writes_nothing(tmp_path, capsys):
    manifest, yml, pdf_dir, text_dir = _setup_cli(tmp_path)
    data = _data(exclusions=[_exclusion("FDIC_12299_20220912")])
    yml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    original = manifest.read_bytes()
    assert _cli(yml, manifest, pdf_dir, text_dir) == 1
    assert "dev split; its exclusion is refused" in capsys.readouterr().err
    assert manifest.read_bytes() == original
    assert (pdf_dir / "FDIC_445_20280408.pdf").exists(), "no file is renamed when the apply is refused"
    yml.write_text("version: 1\nupdated: 2026-10-08\nfield_overrides: [{doc_id: X}]\n", encoding="utf-8")
    assert _cli(yml, manifest, pdf_dir, text_dir) == 1
    assert "manifest overrides:" in capsys.readouterr().err


# ----------------------------------------------------------------------------- live files


def test_live_overrides_file_is_valid():
    ov = ovr.load_overrides()
    assert ov.aliases() == {"FDIC_445_20280408": "FDIC_445_20240408"}
    assert {e.doc_id: e.reason for e in ov.exclusions} == {
        "OCC_9183_20230213": "wrong_pdf_link",
        "FRB_614948_20250902": "pdf_unavailable_http_500",
    }
    assert [r.doc_id for r in ov.pdf_replacements] == ["FRB_785473_20220418"]
    assert "FRB_785473_20220418" not in {e.doc_id for e in ov.exclusions}


@pytest.mark.skipif(not (TEXT_DIR / "FDIC_445_20240408.pages.json").exists()
                    and not (TEXT_DIR / "FDIC_445_20280408.pages.json").exists(),
                    reason="corpus text not present")
def test_live_evidence_quotes():
    problems, _skipped = ovr.check_evidence(ovr.load_overrides())
    # The portfolio quote is on the page only after the embedded PE has been parsed.
    if not (TEXT_DIR.parent / "pdfs" / "FRB_785473_20220418.portfolio.pdf").exists():
        problems = [p for p in problems if not p.startswith("FRB_785473_20220418")]
    assert problems == []


def test_live_manifest_apply_on_copy(tmp_path):
    mpath = tmp_path / "corpus.csv"
    shutil.copy(CORPUS_PATH, mpath)
    with open(CORPUS_PATH, newline="", encoding="utf-8") as fh:
        original = list(csv.DictReader(fh))
    assert ovr.main(["apply", "--manifest", str(mpath), "--no-files"]) == 0
    _cols, rows = strata.read_rows(mpath)
    assert len(rows) == len(original)
    by_id = {r["doc_id"]: r for r in rows}
    assert "FDIC_445_20280408" not in by_id
    assert by_id["FDIC_445_20240408"]["exam_date"] == "2024-04-08"
    assert by_id["FDIC_445_20240408"]["exam_date_source"] == "override"
    assert by_id["OCC_9183_20230213"]["split"] == "excluded"
    assert by_id["FRB_614948_20250902"]["split"] == "excluded"
    assert by_id["FRB_785473_20220418"]["split"] != "excluded"
    assert by_id["FRB_785473_20220418"]["text_extractable"] == "True"
    assert sum(r["split"] == "excluded" for r in rows) == 2
    assert sum(r["split"] == "dev" for r in rows) == 10
    for r in rows:
        if r["doc_id"] != "FDIC_445_20240408":
            assert r["exam_date_source"] == ovr.default_exam_date_source(r["regulator"])
    touched = {"FDIC_445_20280408": {"doc_id", "exam_date", "notes"},
               "OCC_9183_20230213": {"split"}, "FRB_614948_20250902": {"split"},
               "FRB_785473_20220418": {"sha256", "pages", "chars_per_page", "text_extractable"}}
    for before, after in zip(original, rows, strict=True):
        for col, val in before.items():
            if col == "exam_date_source" or col in touched.get(before["doc_id"], set()):
                continue
            assert after[col] == val, (before["doc_id"], col)
    applied = mpath.read_bytes()
    assert ovr.main(["apply", "--manifest", str(mpath), "--no-files"]) == 0
    assert mpath.read_bytes() == applied
    assert strata.exclusivity_problems(strata.to_records(rows)) == []
