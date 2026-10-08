"""Tests for cra_extract.index: offline parsing of the three agency snapshots, FRB dedupe,
normalizers, the release window filter, the prior-exam lookup and the manifest round trip.

Expected values were checked by hand against the fixtures and the sample PEs; the
citations (file and line) are next to each assertion. Network tests run only when
``CRA_NETWORK_TESTS=1`` is set.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pandas as pd
import pytest

from cra_extract import index as ix
from cra_extract.models import DocRecord

OCC_FIXTURE = ix.OCC_FIXTURE
FDIC_FIXTURE = ix.FDIC_FIXTURE
FRB_FIXTURE = ix.FRB_FIXTURE
WINDOW = (date(2023, 1, 1), date(2025, 12, 31))


@pytest.fixture(scope="module")
def occ_rows() -> list[dict]:
    return json.loads(OCC_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fdic_payload() -> dict:
    return json.loads(FDIC_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def frb_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    return ix.frb_index(FRB_FIXTURE)


def _by_id(records: list[DocRecord], doc_id: str) -> DocRecord:
    found = [r for r in records if r.doc_id == doc_id]
    assert len(found) == 1, f"{doc_id}: {len(found)} matches"
    return found[0]


# --------------------------------------------------------------------------- normalizers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Outstanding", "O"),
        ("Satisfactory", "S"),
        ("Needs to Improve", "NI"),
        ("Substantial Noncompliance", "SN"),  # OCC wording (occ fixture, charter 11715)
        ("Sub Non Compliance", "SN"),  # FRB wording
        ("Substantial Non-Compliance", "SN"),
        ("O", "O"),
        ("ni", "NI"),
        ("SN", "SN"),
        ("High Satisfactory", None),  # large-bank scale; never mapped
        ("Low Satisfactory", None),
        ("HS", None),
        ("LS", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_rating(raw, expected):
    assert ix.normalize_rating(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Small Bank", "small"),
        ("Small Bank      ", "small"),  # FRB pads the label with spaces
        ("Small Bank Evaluation", "small"),
        ("Intermediate Small Bank", "isb"),
        ("Intermediate Small Bank Evaluation", "isb"),
        ("Int Small Bank", "isb"),
        ("Large Bank", "other"),
        ("Wholesale", "other"),
        ("Strategic Plan Evaluation", "other"),
        ("Limited Purpose Bank", "other"),
        ("", "other"),
    ],
)
def test_normalize_method(raw, expected):
    assert ix.normalize_method(raw) == expected


def test_date_and_amount_helpers():
    assert ix.parse_mdy("08/05/2024") == date(2024, 8, 5)
    assert ix.parse_mdy("Not available") is None
    assert ix.parse_mdy("") is None
    assert ix.fdic_exam_date_candidate("2024/6078_240805.PDF") == date(2024, 8, 5)
    assert ix.fdic_exam_date_candidate("2025/30332_250909.PDF") == date(2025, 9, 9)
    assert ix.fdic_exam_date_candidate("garbage") is None
    # Thousands of dollars: FDIC "$463,949.00" is "$463 million" in the PE
    # (FDIC_6078_OneAmericanBank_2024_ISB_NI.txt line 104); FRB "$94,182" is "$94.2 million"
    # (FRB_663656_SmallBusinessBank_2024_SM_NI.txt line 91).
    assert ix.parse_thousands_usd("$463,949.00") == 463_949_000
    assert ix.parse_thousands_usd("$94,182") == 94_182_000
    assert ix.parse_thousands_usd("") is None


def test_month_ranges():
    months = ix.month_ranges(date(2024, 11, 15), date(2025, 2, 1))
    assert months == [
        (date(2024, 11, 1), date(2024, 11, 30)),
        (date(2024, 12, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 1, 31)),
        (date(2025, 2, 1), date(2025, 2, 28)),
    ]


# --------------------------------------------------------------------------- OCC


def test_parse_occ_fixture(occ_rows):
    assert len(occ_rows) == 746
    frame = ix.occ_index(occ_rows)
    # 746 listings, 5 of which duplicate an (charter, exam date) already listed under an
    # earlier release folder (charters 703912, 13802, 6171 twice, 10913).
    assert len(frame) == 741
    assert not frame.duplicated(["bank_id", "exam_date"]).any()
    # Fixture Exam labels: Small Bank 342, Intermediate Small Bank 245, other methods 159;
    # the 5 duplicates are 4 Small Bank and 1 ISB listings.
    assert frame["method"].value_counts().to_dict() == {"small": 338, "isb": 244, "other": 159}

    records = ix.parse_occ(occ_rows)
    assert len(records) == 338 + 244
    assert {r.method for r in records} == {"small", "isb"}
    assert all(r.regulator == "OCC" for r in records)

    # GN Bank: fixture row Charter 703395, eDte 03/27/2023, pDte 07/17/2023, pdf /Jul23/703395.pdf;
    # cover page of OCC_703395_GNBank_2023_NI.txt lines 1-7 (March 27, 2023, Charter Number 703395).
    gn = _by_id(records, "OCC_703395_20230327")
    assert gn.bank_name == "GN Bank"
    assert gn.exam_date == date(2023, 3, 27)
    assert gn.release_date == date(2023, 7, 17)
    assert gn.method == "small"
    assert gn.overall_rating == "NI"
    assert gn.url == "https://occ.gov/static/cra/craeval/Jul23/703395.pdf"
    assert gn.lending_test_rating is None and gn.cd_test_rating is None
    assert gn.total_assets_usd is None  # the OCC feed carries no assets

    lemont = _by_id(records, "OCC_11715_20240408")
    assert lemont.overall_rating == "SN"  # "Substantial Noncompliance" in the feed
    neighbor = _by_id(records, "OCC_13682_20240819")
    assert neighbor.method == "isb" and neighbor.overall_rating == "O"
    assert neighbor.url == "https://occ.gov/static/cra/craeval/Mar25/13682.pdf"

    # The pdf path is used verbatim: folder case varies ("/apr19/9183.pdf" for charter 9183).
    first = _by_id(records, "OCC_9183_20230213")
    assert first.url.endswith("/apr19/9183.pdf")


def test_occ_duplicate_listing_keeps_latest_release(occ_rows):
    records = ix.parse_occ(occ_rows)
    # Charter 6171 exam 06/01/2023 is listed three times (releases 01/03/2024, 02/29/2024,
    # 04/30/2024); the latest release wins and its folder is used.
    r = _by_id(records, "OCC_6171_20230601")
    assert r.release_date == date(2024, 4, 30)
    assert r.url.endswith("/Apr24/6171.pdf")


def test_occ_blank_release_rows_are_kept_in_index_and_excluded_by_window(occ_rows):
    records = ix.parse_occ(occ_rows)
    blank = sorted(r.bank_id for r in records if r.release_date is None)
    assert blank == ["14510", "25088"]  # the two blank pDte rows in the fixture
    kept, excluded = ix.filter_window(records, *WINDOW)
    reasons = excluded.groupby("reason")["bank_id"].apply(sorted).to_dict()
    assert reasons["missing_release_date"] == ["14510", "25088"]
    assert "release_before_window" not in reasons  # the fixture was pulled for 2023+ exams
    assert len(excluded[excluded["reason"] == "release_after_window"]) == 107  # 2026 releases
    assert len(kept) + len(excluded) == len(records)
    assert all(WINDOW[0] <= r.release_date <= WINDOW[1] for r in kept)


# --------------------------------------------------------------------------- FDIC


def test_parse_fdic_fixture(fdic_payload):
    assert fdic_payload["status"] == "success"
    rows = fdic_payload["data"]
    assert len(rows) == 500  # a single capped response; fine as a parsing fixture
    frame = ix.fdic_index(fdic_payload)
    assert len(frame) == 500
    assert frame["method"].value_counts().to_dict() == {"small": 266, "isb": 167, "other": 67}
    # Raw CRAPES component ratings are stored but never normalized into the ISB scale.
    assert frame["fdic_lending_rating_raw"].value_counts().to_dict() == {
        "High Satisfactory": 190, "Low Satisfactory": 25, "Outstanding": 9,
    }
    assert frame["lending_test_rating"].isna().all()
    assert frame["cd_test_rating"].isna().all()

    records = ix.parse_fdic(fdic_payload)
    assert len(records) == 433
    assert ix.parse_fdic(rows) == records  # a bare list is accepted too

    # One American Bank: fixture row ORG_CERT_NUM 6078, SYS_EXTRN_FILE_ID 2024/6078_240805.PDF,
    # EXM_CRA_PUB_DTE 08/01/2025, LENDING_RATING "Low Satisfactory". The PE
    # (FDIC_6078_OneAmericanBank_2024_ISB_NI.txt) prints August 5, 2024 on the cover (line 4)
    # and "The Lending Test is rated Needs to Improve." (line 57): the feed's component
    # rating is on the wrong scale, which is why it is not copied into the record.
    oab = _by_id(records, "FDIC_6078_20240805")
    assert oab.bank_name == "One American Bank"
    assert oab.exam_date == date(2024, 8, 5)
    assert oab.release_date == date(2025, 8, 1)
    assert oab.method == "isb"
    assert oab.overall_rating == "NI"
    assert oab.lending_test_rating is None
    assert oab.cd_test_rating is None
    assert oab.total_assets_usd == 463_949_000
    assert oab.url == "https://crapes.fdic.gov/publish/2024/6078_240805.PDF"
    assert oab.source_file_id == "2024/6078_240805.PDF"


def test_fdic_rows_rejects_failed_status():
    with pytest.raises(ValueError):
        ix.fdic_rows({"status": "error", "data": []})


def test_fdic_exam_date_after_release_is_flagged_not_dropped(fdic_payload):
    # Seen in the live 2024 pull: cert 445 is listed as 2024/445_280408.PDF (released
    # 09/01/2024), so the file-name date parses as 2028-04-08. The row stays in the index
    # with its verbatim URL and the anomaly is written to ``notes``.
    template = dict(fdic_payload["data"][0])
    template.update(
        ORG_CERT_NUM="445", INST_FIN_LGL_NME="Mercer County State Bank",
        EXM_CRA_PUB_DTE="09/01/2024", SYS_EXTRN_FILE_ID="2024/445_280408.PDF",
        EXM_ACT_PROC_DESC="Small Bank Evaluation", EXM_RTG_FULL_DESC="Satisfactory",
    )
    records = ix.parse_fdic([template])
    assert len(records) == 1
    assert records[0].doc_id == "FDIC_445_20280408"
    assert records[0].exam_date == date(2028, 4, 8)
    assert records[0].url == "https://crapes.fdic.gov/publish/2024/445_280408.PDF"
    assert records[0].notes == ix.EXAM_AFTER_RELEASE_NOTE
    # Ordinary rows carry no note.
    assert all(r.notes is None for r in ix.parse_fdic(fdic_payload))


# --------------------------------------------------------------------------- FRB


def test_parse_frb_fixture(frb_frames):
    main, state_rows = frb_frames
    raw = pd.read_csv(FRB_FIXTURE, dtype=str, keep_default_na=False)
    assert len(raw) == 10399
    assert len(main) + len(state_rows) == len(raw)
    assert len(main) == 7673
    assert len(state_rows) == 2726
    assert not main.duplicated(["bank_id", "exam_date"]).any()
    assert main["method"].value_counts().to_dict() == {"small": 4517, "other": 1832, "isb": 1324}
    assert set(main["overall_rating"].dropna()) == {"O", "S", "NI", "SN"}
    # Old rows have "Not available" as public date and parse to a missing release date.
    assert main["release_date"].isna().sum() == 2104  # of 2258 "Not available" rows in the CSV
    assert (main.loc[main["release_date"].isna(), "exam_date"].map(lambda d: d.year) <= 2002).all()

    records = ix.parse_frb(FRB_FIXTURE)
    assert len(records) == 4517 + 1324
    assert ix.parse_frb(FRB_FIXTURE.read_text(encoding="utf-8-sig")) == records  # text input

    # Fieldpoint 2024 exam: CSV main row has Lending NI and Community Dev S, assets $1,656,434
    # (thousands). The PE (FRB_3664588_FieldPoint_2024_ISB_NI.txt) says "January 16, 2024"
    # (line 3), "The Lending Test is rated: Needs to Improve." (line 99), "The Community
    # Development Test is rated: Satisfactory." (line 100) and "total assets of $1.7 billion"
    # (line 151).
    fp = _by_id(records, "FRB_3664588_20240116")
    assert fp.bank_name == "FIELDPOINT PRIVATE BANK & TRUST"
    assert fp.method == "isb"
    assert fp.exam_date == date(2024, 1, 16)
    assert fp.release_date == date(2024, 8, 18)
    assert fp.overall_rating == "NI"
    assert fp.lending_test_rating == "NI"
    assert fp.cd_test_rating == "S"
    assert fp.total_assets_usd == 1_656_434_000
    assert fp.url == "https://www.federalreserve.gov/apps/CRAPubWeb/CRA/DownloadPDF/3664588_20240116"

    # Small Bank rows have no component ratings.
    sbb = _by_id(records, "FRB_663656_20241118")
    assert sbb.method == "small" and sbb.overall_rating == "NI"
    assert sbb.lending_test_rating is None and sbb.cd_test_rating is None
    assert sbb.total_assets_usd == 94_182_000


def test_frb_dedupe_on_fixture(frb_frames):
    main, state_rows = frb_frames
    # Fieldpoint 01/16/2024 has one main row and four state rows (CT, FL, GA, NY).
    fp_state = state_rows[(state_rows["IDRSSD"] == "3664588") & (state_rows["Exam Date"] == "01/16/2024")]
    assert sorted(fp_state["MSA/State"]) == ["Connecticut", "Florida", "Georgia", "New York"]
    assert (fp_state["Overall Rating"] == "").all()  # state rows carry no overall rating
    assert len(main[(main["bank_id"] == "3664588") & (main["exam_date"] == date(2024, 1, 16))]) == 1
    # Every state row belongs to an exam that has a main row.
    state_keys = set(zip(state_rows["IDRSSD"], state_rows["Exam Date"], strict=True))
    main_keys = set(zip(main["bank_id"], main["exam_date"].map(lambda d: f"{d:%m/%d/%Y}"), strict=True))
    assert state_keys <= main_keys


def test_frb_dedupe_synthetic():
    header = (
        '"IDRSSD","Bank Name","City","State","Exam Method","Exam Date","Public Date","Overall Rating",'
        '"Lending(Large, Int Small)","Community Dev(Int Small)","Investment(Large)","Service(Large)",'
        '"Asset Size(In Thousands)","MSA/State","MSA/State Rating","MSA/State Lending(Large,Int Small)",'
        '"MSA/State Community Dev(Int Small)","MSA/State Investment(Large)","MSA/State Service(Large)"'
    )
    rows = [
        '"1","TEST BANK   ","X","TX","Int Small Bank  ","01/16/2024","08/18/2024","Satisfactory","S","O","","","$10,000","","","","","",""',
        '"1","TEST BANK   ","X","TX","Int Small Bank  ","01/16/2024","08/18/2024","","","","","","$10,000","Texas","S","S","O","",""',
        '"1","TEST BANK   ","X","TX","Int Small Bank  ","01/16/2024","08/18/2024","","","","","","$10,000","Dallas, TX","O","O","O","",""',
        '"1","TEST BANK   ","X","TX","Large Bank      ","02/01/2019","Not available","Outstanding","HS","","O","HS","$9,000","","","","","",""',
    ]
    main, state = ix.frb_index("\n".join([header, *rows]) + "\n")
    assert len(main) == 2 and len(state) == 2
    assert sorted(state["MSA/State"]) == ["Dallas, TX", "Texas"]
    isb = main[main["method"] == "isb"].iloc[0]
    assert isb["bank_name"] == "TEST BANK"
    assert isb["lending_test_rating"] == "S" and isb["cd_test_rating"] == "O"
    assert isb["total_assets_usd"] == 10_000_000
    large = main[main["method"] == "other"].iloc[0]
    assert pd.isna(large["lending_test_rating"])  # HS is not an ISB-scale rating
    assert pd.isna(large["release_date"])
    records = ix.records_from_index(main)
    assert [r.doc_id for r in records] == ["FRB_1_20240116"]


def test_frb_missing_columns_raise():
    with pytest.raises(ValueError, match="missing columns"):
        ix.frb_index('"IDRSSD","Bank Name"\n"1","X"\n')


# --------------------------------------------------------------------------- window and priors


def _rec(regulator: str, bank_id: str, exam: date, release: date | None, **kw) -> DocRecord:
    return DocRecord(
        doc_id=f"{regulator}_{bank_id}_{exam:%Y%m%d}",
        regulator=regulator,
        bank_id=bank_id,
        bank_name="Bank " + bank_id,
        exam_date=exam,
        release_date=release,
        method=kw.pop("method", "small"),
        url="https://example.invalid/x.pdf",
        **kw,
    )


def test_filter_window_synthetic():
    records = [
        _rec("OCC", "1", date(2022, 10, 1), date(2022, 12, 31)),  # day before window
        _rec("OCC", "2", date(2022, 10, 1), date(2023, 1, 1)),  # first day
        _rec("OCC", "3", date(2025, 9, 1), date(2025, 12, 31)),  # last day
        _rec("OCC", "4", date(2025, 9, 1), date(2026, 1, 1)),  # day after
        _rec("OCC", "5", date(2025, 9, 1), None),
    ]
    kept, excluded = ix.filter_window(records, *WINDOW)
    assert [r.bank_id for r in kept] == ["2", "3"]
    assert excluded.set_index("bank_id")["reason"].to_dict() == {
        "1": "release_before_window",
        "4": "release_after_window",
        "5": "missing_release_date",
    }
    assert list(excluded.columns) == ["doc_id", "regulator", "bank_id", "exam_date", "release_date", "method", "reason"]


def test_prior_lookup_synthetic():
    manifest = pd.DataFrame(
        {
            "doc_id": ["FRB_10_20240116", "FRB_10_20200301", "OCC_10_20240116", "FDIC_7_20240101"],
            "regulator": ["FRB", "FRB", "OCC", "FDIC"],
            "bank_id": ["10", "10", "10", "7"],
            "exam_date": ["2024-01-16", "2020-03-01", "2024-01-16", "2024-01-01"],
        }
    )
    index = pd.DataFrame(
        {
            "regulator": ["FRB", "FRB", "FRB", "FRB", "OCC", "FDIC"],
            "bank_id": ["0010", "10", "10", "10", "10", "7"],
            "exam_date": [
                date(2018, 2, 5), date(2020, 3, 1), date(2024, 1, 16), date(2025, 8, 25),
                date(2024, 1, 16),  # same date as the OCC manifest row: not strictly earlier
                date(2024, 1, 1),
            ],
            "overall_rating": ["S", "NI", "NI", "O", "S", "S"],
            "method": ["other", "isb", "isb", "isb", "small", "small"],
        }
    )
    out = ix.prior_lookup(manifest, index)
    assert list(out.columns) == ["prior_evaluation_date_index", "prior_rating_index"]
    assert out["prior_evaluation_date_index"].tolist() == ["2020-03-01", "2018-02-05", None, None]
    assert out["prior_rating_index"].tolist() == ["NI", "S", None, None]
    # Leading zeros in bank ids and index rows of other methods are both matched.


def test_prior_lookup_on_fixtures(frb_frames):
    main, _ = frb_frames
    records = ix.records_from_index(main)
    kept, _ = ix.filter_window(records, *WINDOW)
    manifest = ix.records_to_frame(kept)
    priors = ix.prior_lookup(manifest, main)
    row = priors[manifest["doc_id"] == "FRB_663656_20241118"].iloc[0]
    # FRB_663656_SmallBusinessBank_2024_SM_NI.txt line 125: "The bank was rated Satisfactory
    # under the CRA at its March 23, 2020 performance evaluation"; CSV row for 663656 03/23/2020.
    assert row["prior_evaluation_date_index"] == "2020-03-23"
    assert row["prior_rating_index"] == "S"
    row = priors[manifest["doc_id"] == "FRB_3664588_20240116"].iloc[0]
    assert row["prior_evaluation_date_index"] == "2018-02-05"  # CSV row 3664588 02/05/2018, S
    assert row["prior_rating_index"] == "S"


# --------------------------------------------------------------------------- manifest


def test_manifest_round_trip(tmp_path, occ_rows):
    records = ix.parse_occ(occ_rows)[:50]
    frame = ix.records_to_frame(records)
    assert list(frame.columns) == list(DocRecord.model_fields)
    path = tmp_path / "corpus.csv"
    frame.to_csv(path, index=False)
    back = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert ix.frame_to_records(back) == records
    assert "." not in back["pages"].iloc[0]  # nullable ints are not written as floats


def test_build_manifest_from_fixtures(occ_rows, fdic_payload, frb_frames, monkeypatch):
    occ = ix.occ_index(occ_rows)
    fdic = ix.fdic_index(fdic_payload)
    frb, _ = frb_frames
    manifest, exclusions = ix.build_manifest(occ, fdic, frb, WINDOW)
    assert list(manifest.columns) == ix.MANIFEST_COLUMNS
    assert manifest["doc_id"].is_unique
    assert set(manifest["method"]) == {"small", "isb"}
    assert set(manifest["regulator"]) == {"OCC", "FDIC", "FRB"}
    assert manifest["release_date"].between("2023-01-01", "2025-12-31").all()
    assert len(manifest) == 1324
    assert exclusions["reason"].value_counts().to_dict() == {
        "release_before_window": 3673, "missing_release_date": 1643, "release_after_window": 216,
    }
    counts = ix.corpus_counts(manifest)
    assert list(counts.columns) == ["regulator", "method", "release_year", "n_docs"]
    assert counts["n_docs"].sum() == len(manifest)
    fdic_counts = counts[counts["regulator"] == "FDIC"].set_index("method")["n_docs"].to_dict()
    assert fdic_counts == {"isb": 167, "small": 266}  # the fixture is a 2025-release pull
    # Components only for FRB ISB rows (the FDIC feed's are deliberately not copied).
    has_lending = manifest[manifest["lending_test_rating"].isin(["O", "S", "NI", "SN"])]
    assert set(has_lending["regulator"]) == {"FRB"} and set(has_lending["method"]) == {"isb"}
    # Manifest rows that match the sample PEs
    sample_ids = {
        "OCC_703395_20230327", "OCC_11715_20240408", "OCC_13682_20240819",
        "FDIC_6078_20240805", "FRB_3664588_20240116", "FRB_663656_20241118",
    }
    assert sample_ids <= set(manifest["doc_id"])
    # Every manifest row is a valid DocRecord again.
    assert len(ix.frame_to_records(manifest)) == len(manifest)


def test_side_outputs_follow_out(tmp_path):
    # The repo manifest keeps the repo side-output dirs; any other --out gets its own.
    assert ix.side_output_dirs(ix.CORPUS_PATH, None, None) == (ix.INDEX_DIR, ix.RESULTS_DIR)
    assert ix.side_output_dirs(tmp_path / "x.csv", None, None) == (tmp_path / "index", tmp_path / "results")
    assert ix.side_output_dirs(tmp_path / "x.csv", tmp_path / "i", None) == (tmp_path / "i", tmp_path / "results")


def test_cli_offline_refuses_repo_index_dir(tmp_path):
    out = tmp_path / "corpus.csv"
    with pytest.raises(SystemExit):
        ix.main(["--offline-from-fixtures", "--out", str(out), "--index-dir", str(ix.INDEX_DIR)])
    assert not out.exists()


def test_cli_offline_writes_outputs(tmp_path):
    # No monkeypatching: with --out outside the repo the side outputs land next to it.
    out = tmp_path / "corpus.csv"
    rc = ix.main(["--window", "2024-01-01", "2024-12-31", "--prior-from", "2018-01-01",
                  "--offline-from-fixtures", "--out", str(out)])
    assert rc == 0
    for name in ("occ", "fdic", "frb", "frb_state_rows", "manifest_exclusions"):
        assert (tmp_path / "index" / f"{name}.csv").exists()
    manifest = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert manifest["release_date"].str[:4].eq("2024").all()
    assert "prior_evaluation_date_index" in manifest.columns
    counts = pd.read_csv(tmp_path / "results" / "corpus_counts.csv")
    assert counts["release_year"].eq(2024).all()
    assert counts["n_docs"].sum() == len(manifest)


def test_cli_from_raw_rebuilds_without_network(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(ix, "RAW_INDEX_DIR", raw)
    out = tmp_path / "corpus.csv"
    argv = ["--window", "2024-01-01", "2024-12-31", "--out", str(out), "--from-raw", "20260101"]
    with pytest.raises(FileNotFoundError, match="occ_cra_eval_20260101.json"):
        ix.main(argv)
    for src, dst in zip((OCC_FIXTURE, FDIC_FIXTURE, FRB_FIXTURE), ix.raw_paths("20260101"), strict=True):
        dst.write_bytes(src.read_bytes())
    assert ix.main(argv) == 0
    manifest = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert manifest["release_date"].str[:4].eq("2024").all()
    assert "FRB_3664588_20240116" in set(manifest["doc_id"])


# --------------------------------------------------------------------------- network

network = pytest.mark.skipif(
    os.environ.get("CRA_NETWORK_TESTS") != "1", reason="set CRA_NETWORK_TESTS=1 to call the agencies"
)


@network
@pytest.mark.network
def test_fetch_occ_live():
    rows = ix.fetch_occ(date(2025, 1, 1), date(2025, 1, 31))
    assert rows and {"Charter", "BankName", "Exam", "eDte", "pDte", "pdf"} <= set(rows[0])
    assert ix.parse_occ(rows)


@network
@pytest.mark.network
def test_fetch_fdic_live():
    rows, queries = ix.fetch_fdic(date(2025, 8, 1), date(2025, 8, 31))
    assert len(queries) == 2 and all(q["n_rows"] < ix.FDIC_ROW_CAP for q in queries)
    assert rows and "SYS_EXTRN_FILE_ID" in rows[0]
    assert ix.parse_fdic(rows)


@network
@pytest.mark.network
def test_fetch_frb_live():
    text = ix.fetch_frb()
    main, state_rows = ix.frb_index(text)
    assert len(main) > 7000 and len(state_rows) > 2000
