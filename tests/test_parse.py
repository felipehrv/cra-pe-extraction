"""Tests for ``cra_extract.parse`` on the eight sample PDFs plus synthetic documents."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pymupdf
import pytest

from cra_extract import parse
from cra_extract.models import Page

SAMPLES = Path(__file__).parent / "fixtures" / "samples"

# Verified with pymupdf on 2026-10-05 (doc.page_count); they agree with the values
# given in the assignment.
EXPECTED_PAGES = {
    "OCC_703395_GNBank_2023_NI": 17,
    "OCC_11715_LemontNB_2024_SN": 20,
    "OCC_13682_FirstNeighborBank_2024_ISB_O": 26,
    "FDIC_12299_SouthStar_2022_ISB_NI_fairlending": 45,
    "FDIC_26816_LibertyBank_2022_SM_SN": 14,
    "FDIC_6078_OneAmericanBank_2024_ISB_NI": 23,
    "FRB_3664588_FieldPoint_2024_ISB_NI": 95,
    "FRB_663656_SmallBusinessBank_2024_SM_NI": 21,
}


@pytest.fixture(scope="module")
def sample_pages() -> dict[str, list[Page]]:
    """Extract each sample once per module (FieldPoint has 95 pages)."""
    return {stem: parse.extract_pages(SAMPLES / f"{stem}.pdf") for stem in EXPECTED_PAGES}


# ----------------------------------------------------------------------------- samples


def test_page_counts_match_known_values(sample, sample_pages):
    pages = sample_pages[sample["stem"]]
    assert len(pages) == EXPECTED_PAGES[sample["stem"]]
    assert [p.number for p in pages] == list(range(1, len(pages) + 1))
    assert all(p.chars == len(p.text) for p in pages)


def test_all_samples_are_text_extractable(sample, sample_pages):
    stats = parse.text_stats(sample_pages[sample["stem"]])
    assert stats.text_extractable
    assert stats.pages == EXPECTED_PAGES[sample["stem"]]
    assert stats.chars_per_page > parse.MIN_MEAN_CHARS_PER_PAGE
    assert parse.is_text_extractable(sample_pages[sample["stem"]])


def test_sample_text_contains_cover_heading(sample, sample_pages):
    # every PE opens with "PUBLIC DISCLOSURE" (headings.yaml, cover section)
    assert "PUBLIC DISCLOSURE" in sample_pages[sample["stem"]][0].text


# ----------------------------------------------------------------------------- offsets


def test_offsets_round_trip_through_doc_text(sample, sample_pages):
    pages = sample_pages[sample["stem"]]
    text = parse.doc_text(pages)
    spans = parse.page_offsets(pages)
    assert len(spans) == len(pages)
    for (a, b), page in zip(spans, pages, strict=True):
        assert text[a:b] == page.text
        assert parse.page_of_offset(pages, a) == page.number
        assert parse.page_of_offset(pages, max(a, b - 1)) == page.number
    for a, _ in spans[1:]:
        assert text[a - 1] == parse.PAGE_SEPARATOR
    assert spans[-1][1] == len(text)
    assert text.count(parse.PAGE_SEPARATOR) >= len(pages) - 1


def test_page_of_offset_rejects_out_of_range():
    pages = [Page(number=1, text="abc", chars=3), Page(number=2, text="de", chars=2)]
    assert parse.page_of_offset(pages, 3) == 1, "the separator belongs to the page before it"
    assert parse.page_of_offset(pages, 4) == 2
    assert parse.page_of_offset(pages, 6) == 2
    with pytest.raises(ValueError):
        parse.page_of_offset(pages, 7)
    with pytest.raises(ValueError):
        parse.page_of_offset(pages, -1)


def test_write_and_load_round_trip(tmp_path, sample_pages):
    stem = "FDIC_26816_LibertyBank_2022_SM_SN"
    pages = sample_pages[stem]
    json_path, txt_path = parse.write_pages("FDIC_26816_20220809", pages, tmp_path)
    assert json_path.exists() and txt_path.exists()

    loaded = parse.load_pages("FDIC_26816_20220809", tmp_path)
    assert loaded == pages
    with open(txt_path, encoding="utf-8", newline="") as fh:
        assert fh.read() == parse.doc_text(loaded)

    with pytest.raises(FileNotFoundError):
        parse.load_pages("missing", tmp_path)


# ----------------------------------------------------------------------------- thresholds


def test_blank_pdf_is_not_extractable(tmp_path):
    pdf = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        for _ in range(3):
            doc.new_page()
        doc.save(pdf)

    stats = parse.parse_pdf(pdf, "blank", tmp_path)
    assert stats == parse.TextStats(pages=3, chars_per_page=0.0, text_extractable=False)
    assert parse.load_pages("blank", tmp_path)[0].text == ""


def test_sparse_page_rule():
    rich = Page(number=1, text="x" * 500, chars=500)
    sparse = Page(number=2, text="x" * 10, chars=10)
    assert parse.is_text_extractable([rich, rich, sparse, sparse]), "half sparse is allowed"
    assert not parse.is_text_extractable([rich, rich, sparse, sparse, sparse]), "3 of 5 sparse"
    assert not parse.is_text_extractable([Page(number=1, text="x" * 150, chars=150)] * 4), "mean 150"
    assert not parse.is_text_extractable([])
    assert parse.text_stats([]) == parse.TextStats(0, 0.0, False)


# ----------------------------------------------------------------------------- manifest CLI


def test_parse_manifest_updates_columns_and_skips_missing_pdfs(tmp_path, caplog):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    shutil.copy(SAMPLES / "OCC_703395_GNBank_2023_NI.pdf", pdf_dir / "OCC_703395_20230327.pdf")
    shutil.copy(SAMPLES / "FDIC_26816_LibertyBank_2022_SM_SN.pdf", pdf_dir / "FDIC_26816_20220809.pdf")
    rows = [
        {"doc_id": "OCC_703395_20230327", "regulator": "OCC", "url": "u", "split": "dev"},
        {"doc_id": "FDIC_26816_20220809", "regulator": "FDIC", "url": "u", "split": "dev"},
        {"doc_id": "FRB_1_20240101", "regulator": "FRB", "url": "u", "split": "corpus"},
    ]
    manifest = tmp_path / "corpus.csv"
    pd.DataFrame(rows).to_csv(manifest, index=False)

    df = parse.parse_manifest(manifest, pdf_dir=pdf_dir, text_dir=tmp_path / "text")
    assert df["pages"].tolist() == ["17", "14", ""]
    assert df["text_extractable"].tolist() == ["True", "True", ""]
    assert float(df.at[0, "chars_per_page"]) > parse.MIN_MEAN_CHARS_PER_PAGE
    assert (tmp_path / "text" / "OCC_703395_20230327.pages.json").exists()
    assert (tmp_path / "text" / "FDIC_26816_20220809.txt").exists()
    assert "FRB_1_20240101" in caplog.text

    on_disk = pd.read_csv(manifest, dtype=str, keep_default_na=False)
    assert on_disk["pages"].tolist() == ["17", "14", ""]

    # --doc-id restricts the run; existing text is reused unless --force
    json_path, _ = parse.text_paths("OCC_703395_20230327", tmp_path / "text")
    mtime = json_path.stat().st_mtime_ns
    parse.parse_manifest(manifest, doc_id="OCC_703395_20230327", pdf_dir=pdf_dir, text_dir=tmp_path / "text")
    assert json_path.stat().st_mtime_ns == mtime
    parse.parse_manifest(manifest, doc_id="OCC_703395_20230327", force=True, pdf_dir=pdf_dir, text_dir=tmp_path / "text")
    assert json_path.stat().st_mtime_ns >= mtime


def test_parse_pdf_reuses_existing_text_unless_forced(tmp_path):
    pdf = SAMPLES / "OCC_703395_GNBank_2023_NI.pdf"
    fake = [Page(number=1, text="x" * 300, chars=300)]
    parse.write_pages("doc", fake, tmp_path)
    assert parse.parse_pdf(pdf, "doc", tmp_path).pages == 1
    assert parse.parse_pdf(pdf, "doc", tmp_path, force=True).pages == 17


def test_cli_main(tmp_path, monkeypatch):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    shutil.copy(SAMPLES / "OCC_703395_GNBank_2023_NI.pdf", pdf_dir / "OCC_703395_20230327.pdf")
    manifest = tmp_path / "corpus.csv"
    pd.DataFrame([{"doc_id": "OCC_703395_20230327", "url": "u"}]).to_csv(manifest, index=False)
    monkeypatch.setattr(parse, "PDF_DIR", pdf_dir)
    monkeypatch.setattr(parse, "TEXT_DIR", tmp_path / "text")

    assert parse.main(["--manifest", str(manifest)]) == 0
    df = pd.read_csv(manifest, dtype=str, keep_default_na=False)
    assert df.at[0, "pages"] == "17"
    assert (tmp_path / "text" / "OCC_703395_20230327.txt").exists()
