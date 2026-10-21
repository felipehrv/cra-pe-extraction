"""Corpus manifest and per-agency index files from the three agency sources.

The three regulators publish CRA ratings in three different shapes: the OCC behind a
JSON search API, the FDIC behind the CRAPES JSON API (500 rows per query), and the
Federal Reserve as a CSV download that needs a search session cookie. This module
normalizes each source into one index frame per agency (``data/index/{occ,fdic,frb}.csv``),
keeps the FRB state and MSA rows apart (``data/index/frb_state_rows.csv``), and writes the
corpus manifest ``config/corpus.csv`` for Small Bank and Intermediate Small Bank (ISB)
evaluations released inside a date window. Every index row for the same bank is kept
(all methods, back to ``--prior-from``) so that the manifest can carry the previous
evaluation's date and rating as a verification source for ``prior_evaluation_date`` and
``prior_rating``.

Pure functions (``parse_occ``, ``parse_fdic``, ``parse_frb``, ``filter_window``,
``prior_lookup``) are tested offline on the fixtures under ``tests/fixtures/index``; the
``fetch_*`` functions talk to the agencies and are covered by network-marked tests.

Run ``python -m cra_extract.index --help`` for the CLI.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from cra_extract.config import (
    CORPUS_PATH,
    FIXTURES_DIR,
    INDEX_DIR,
    RAW_INDEX_DIR,
    RESULTS_DIR,
    ensure_dirs,
)
from cra_extract.models import RATING_WORDS, DocRecord, make_doc_id
from cra_extract.overrides import default_exam_date_source

log = logging.getLogger(__name__)

USER_AGENT = "cra-pe-extraction/0.1 (independent research project; see README)"
REQUEST_PAUSE_S = 0.4

OCC_SEARCH_URL = "https://apps.occ.gov/Occ.DataServices.WebApi.Public/api/CraEvaluations/Search/Options"
OCC_PDF_BASE = "https://occ.gov/static/cra/craeval"

FDIC_SEARCH_URL = "https://crapes.fdic.gov/api/getSearchResults"
FDIC_PDF_BASE = "https://crapes.fdic.gov/publish/"
FDIC_ROW_CAP = 500
FDIC_METHOD_CODES = ("SM", "IS")

FRB_SEARCH_URL = "https://www.federalreserve.gov/apps/CRAPubWeb/CRA/BankRating"
FRB_RESULT_URL = "https://www.federalreserve.gov/apps/CRAPubWeb/CRA/BankRatingResult"
FRB_CSV_URL = "https://www.federalreserve.gov/apps/CRAPubWeb/CRA/DownloadCSV"
FRB_PDF_URL = "https://www.federalreserve.gov/apps/CRAPubWeb/CRA/DownloadPDF/{rssd}_{ymd}"

OCC_FIXTURE = FIXTURES_DIR / "index" / "occ_cra_eval_2023_2025.json"
FDIC_FIXTURE = FIXTURES_DIR / "index" / "fdic_crapes_release2025.json"
FRB_FIXTURE = FIXTURES_DIR / "index" / "frb_cra_ratings_all.csv"

# One schema for all three agency index files so they can be concatenated for the
# prior-exam lookup. Regulator-specific raw fields are blank for the other agencies.
INDEX_COLUMNS = [
    "regulator",
    "bank_id",
    "bank_name",
    "city",
    "state",
    "exam_date",
    "release_date",
    "method",
    "method_raw",
    "overall_rating",
    "rating_raw",
    "lending_test_rating",
    "cd_test_rating",
    "total_assets_usd",
    "url",
    "source_file_id",
    "fdic_lending_rating_raw",
    "fdic_invest_rating_raw",
    "fdic_service_rating_raw",
    "frb_lending_raw",
    "frb_cd_raw",
    "frb_investment_raw",
    "frb_service_raw",
]

PRIOR_COLUMNS = ["prior_evaluation_date_index", "prior_rating_index"]
# exam_date_source says where the manifest exam_date comes from (see cra_extract.overrides):
# the index exam date (OCC, FRB), the date in the FDIC file id, or a documented override.
MANIFEST_EXTRA_COLUMNS = PRIOR_COLUMNS + ["exam_date_source"]
MANIFEST_COLUMNS = list(DocRecord.model_fields) + MANIFEST_EXTRA_COLUMNS

# Rating codes used by the FRB CSV in addition to the words in models.RATING_WORDS.
_RATING_CODES = {"O": "O", "S": "S", "NI": "NI", "SN": "SN"}
_RATING_EXTRA_WORDS = {
    "sub non compliance": "SN",
    "substantial non compliance": "SN",
    "needs improvement": "NI",
}

_METHOD_LABELS = {
    "small bank": "small",
    "small bank evaluation": "small",
    "intermediate small bank": "isb",
    "intermediate small bank evaluation": "isb",
    "int small bank": "isb",
}


# --------------------------------------------------------------------------- normalizers


def normalize_rating(raw: Any) -> str | None:
    """Map a rating word or code to O, S, NI or SN; anything else (blank, High or Low
    Satisfactory, large-bank codes) returns None so that scale mismatches never leak in."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.upper() in _RATING_CODES:
        return _RATING_CODES[text.upper()]
    key = re.sub(r"[\s\-]+", " ", text.lower())
    if key in RATING_WORDS:
        return RATING_WORDS[key]
    return _RATING_EXTRA_WORDS.get(key)


def normalize_method(raw: Any) -> str:
    """Map an agency's exam-method label to ``small``, ``isb`` or ``other``."""
    key = re.sub(r"\s+", " ", str(raw or "").strip().lower())
    return _METHOD_LABELS.get(key, "other")


def parse_mdy(text: Any) -> date | None:
    """Parse ``MM/DD/YYYY``; blanks and placeholders such as ``Not available`` give None."""
    s = str(text or "").strip()
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_thousands_usd(text: Any) -> int | None:
    """Parse an amount printed in thousands of dollars (``$449,175.00``) into whole dollars."""
    s = re.sub(r"[^\d.]", "", str(text or ""))
    if not s:
        return None
    try:
        return int(round(float(s) * 1000))
    except ValueError:
        return None


# --------------------------------------------------------------------------- per-agency parsing


def _empty_index() -> pd.DataFrame:
    return pd.DataFrame(columns=INDEX_COLUMNS)


def _finish_index(rows: list[dict[str, Any]], regulator: str) -> pd.DataFrame:
    """Order columns, sort, and drop exact duplicate (bank_id, exam_date) keys."""
    if not rows:
        return _empty_index()
    df = pd.DataFrame(rows).reindex(columns=INDEX_COLUMNS)
    df["bank_id"] = df["bank_id"].astype(str).str.strip().str.lstrip("0")
    df = df.sort_values(["bank_id", "exam_date", "release_date"], na_position="first")
    dup = df.duplicated(["bank_id", "exam_date"], keep="last")
    if dup.any():
        # Same exam listed more than once (the OCC re-releases a PE under a new folder);
        # keep the latest release, which is the document the agency currently serves.
        for _, r in df[dup].iterrows():
            log.info(
                "%s: dropped duplicate listing of %s exam %s released %s (kept latest release)",
                regulator, r["bank_id"], r["exam_date"], r["release_date"],
            )
        df = df[~dup]
    return df.reset_index(drop=True)


def occ_index(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalize OCC search rows (all methods) into the shared index schema."""
    out = []
    for r in rows:
        out.append(
            {
                "regulator": "OCC",
                "bank_id": r.get("Charter"),
                "bank_name": (r.get("BankName") or "").strip(),
                "city": (r.get("City") or "").strip(),
                "state": (r.get("State") or "").strip(),
                "exam_date": parse_mdy(r.get("eDte")),
                "release_date": parse_mdy(r.get("pDte")),
                "method": normalize_method(r.get("Exam")),
                "method_raw": r.get("Exam"),
                "overall_rating": normalize_rating(r.get("Rating")),
                "rating_raw": r.get("Rating"),
                "url": OCC_PDF_BASE + (r.get("pdf") or ""),
                "source_file_id": r.get("pdf"),
            }
        )
    return _finish_index(out, "OCC")


_FDIC_FILE_ID = re.compile(r"^(?P<year>\d{4})/(?P<cert>\d+)_(?P<ymd>\d{6})\.PDF$", re.IGNORECASE)


def fdic_exam_date_candidate(file_id: str | None) -> date | None:
    """Exam date encoded as YYMMDD in ``SYS_EXTRN_FILE_ID`` (``2024/6078_240805.PDF``)."""
    m = _FDIC_FILE_ID.match(str(file_id or "").strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group("ymd"), "%y%m%d").date()
    except ValueError:
        return None


def fdic_rows(payload: Any) -> list[dict[str, Any]]:
    """Unwrap a CRAPES response (``{"status": ..., "data": [...]}``) or pass a list through."""
    if isinstance(payload, dict):
        if payload.get("status") not in (None, "success"):
            raise ValueError(f"CRAPES response status: {payload.get('status')!r}")
        return list(payload.get("data") or [])
    return list(payload)


def fdic_index(rows: Any) -> pd.DataFrame:
    """Normalize CRAPES rows (all methods) into the shared index schema.

    The component ratings CRAPES reports (High or Low Satisfactory) are on the large-bank
    scale even for ISB rows, so they are stored raw and never copied into
    ``lending_test_rating`` or ``cd_test_rating``.
    """
    out = []
    for r in fdic_rows(rows):
        file_id = r.get("SYS_EXTRN_FILE_ID") or ""
        out.append(
            {
                "regulator": "FDIC",
                "bank_id": r.get("ORG_CERT_NUM"),
                "bank_name": (r.get("INST_FIN_LGL_NME") or "").strip(),
                "city": (r.get("LOCN_PHY_CTY_NME") or "").strip(),
                "state": (r.get("LOCN_ST_ABBV_NME") or "").strip(),
                "exam_date": fdic_exam_date_candidate(file_id),
                "release_date": parse_mdy(r.get("EXM_CRA_PUB_DTE")),
                "method": normalize_method(r.get("EXM_ACT_PROC_DESC")),
                "method_raw": r.get("EXM_ACT_PROC_DESC"),
                "overall_rating": normalize_rating(r.get("EXM_RTG_FULL_DESC")),
                "rating_raw": r.get("EXM_RTG_FULL_DESC"),
                "total_assets_usd": parse_thousands_usd(r.get("EXM_INST_ASET_AMT")),
                "url": FDIC_PDF_BASE + file_id,
                "source_file_id": file_id,
                "fdic_lending_rating_raw": r.get("LENDING_RATING"),
                "fdic_invest_rating_raw": r.get("INVEST_RATING"),
                "fdic_service_rating_raw": r.get("SERVICE_RATING"),
            }
        )
    return _finish_index(out, "FDIC")


FRB_COLUMNS = {
    "IDRSSD": "bank_id",
    "Bank Name": "bank_name",
    "City": "city",
    "State": "state",
    "Exam Method": "method_raw",
    "Exam Date": "exam_date_raw",
    "Public Date": "release_date_raw",
    "Overall Rating": "rating_raw",
    "Lending(Large, Int Small)": "frb_lending_raw",
    "Community Dev(Int Small)": "frb_cd_raw",
    "Investment(Large)": "frb_investment_raw",
    "Service(Large)": "frb_service_raw",
    "Asset Size(In Thousands)": "assets_raw",
    "MSA/State": "msa_state",
}


def _read_frb_csv(source: str | Path) -> pd.DataFrame:
    text = Path(source).read_text(encoding="utf-8-sig") if isinstance(source, Path) else source
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    missing = set(FRB_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"FRB CSV is missing columns: {sorted(missing)}")
    return df.apply(lambda col: col.str.strip())


def frb_index(source: str | Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split the FRB CSV into the main index (all methods) and the state and MSA rows.

    ``source`` is a path or the CSV text. Rows are grouped on (IDRSSD, exam date): the
    row with a blank ``MSA/State`` is the institution-level row; the others carry state
    or MSA ratings and are returned separately with the original columns.
    """
    df = _read_frb_csv(source)
    is_main = df["MSA/State"] == ""
    state_rows = df[~is_main].reset_index(drop=True)
    main = df[is_main].rename(columns=FRB_COLUMNS)

    missing_main = set(zip(state_rows["IDRSSD"], state_rows["Exam Date"], strict=True)) - set(
        zip(main["bank_id"], main["exam_date_raw"], strict=True)
    )
    if missing_main:
        log.warning("FRB: %d (RSSD, exam date) groups have state rows but no main row", len(missing_main))

    out = []
    for r in main.to_dict("records"):
        exam_date = parse_mdy(r["exam_date_raw"])
        method = normalize_method(r["method_raw"])
        out.append(
            {
                "regulator": "FRB",
                "bank_id": r["bank_id"],
                "bank_name": re.sub(r"\s+", " ", r["bank_name"]),
                "city": r["city"],
                "state": r["state"],
                "exam_date": exam_date,
                "release_date": parse_mdy(r["release_date_raw"]),
                "method": method,
                "method_raw": r["method_raw"],
                "overall_rating": normalize_rating(r["rating_raw"]),
                "rating_raw": r["rating_raw"],
                # Component ratings are on the ISB scale only for ISB rows.
                "lending_test_rating": normalize_rating(r["frb_lending_raw"]) if method == "isb" else None,
                "cd_test_rating": normalize_rating(r["frb_cd_raw"]) if method == "isb" else None,
                "total_assets_usd": parse_thousands_usd(r["assets_raw"]),
                "url": FRB_PDF_URL.format(rssd=r["bank_id"], ymd=f"{exam_date:%Y%m%d}") if exam_date else "",
                "source_file_id": f"{r['bank_id']}_{exam_date:%Y%m%d}" if exam_date else None,
                "frb_lending_raw": r["frb_lending_raw"],
                "frb_cd_raw": r["frb_cd_raw"],
                "frb_investment_raw": r["frb_investment_raw"],
                "frb_service_raw": r["frb_service_raw"],
            }
        )
    return _finish_index(out, "FRB"), state_rows


def _none_if_blank(v: Any) -> Any:
    """None for NaN, None and empty strings (CSV and reindex gaps); the value otherwise."""
    if v is None or v == "":
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    return v


EXAM_AFTER_RELEASE_NOTE = "index anomaly: exam date is after the release date"


def records_from_index(index: pd.DataFrame) -> list[DocRecord]:
    """Turn Small Bank and ISB index rows into DocRecords; other methods are dropped and
    their counts logged. Rows without an exam date cannot form a doc_id and are dropped too.

    A row whose exam date falls after its release date is kept (the PDF URL is still the
    agency's verbatim file id) but flagged in ``notes``: the FDIC feed encodes the exam
    date only in the file name, and a typo there (``2024/445_280408.PDF``) would
    otherwise pass silently into the doc_id and the verification of ``exam_date``.
    """
    dropped = Counter()
    flagged = []
    records = []
    for r in index.to_dict("records"):
        if r["method"] not in ("small", "isb"):
            dropped[str(r["method_raw"]).strip()] += 1
            continue
        if not isinstance(r["exam_date"], date):
            dropped["(no exam date)"] += 1
            continue
        assets = _none_if_blank(r["total_assets_usd"])
        release = r["release_date"] if isinstance(r["release_date"], date) else None
        notes = None
        if release is not None and r["exam_date"] > release:
            notes = EXAM_AFTER_RELEASE_NOTE
            flagged.append(f"{r['regulator']} {r['bank_id']} {r['exam_date']} ({r['source_file_id']})")
        records.append(
            DocRecord(
                doc_id=make_doc_id(r["regulator"], r["bank_id"], r["exam_date"]),
                regulator=r["regulator"],
                bank_id=r["bank_id"],
                bank_name=r["bank_name"],
                exam_date=r["exam_date"],
                release_date=release,
                method=r["method"],
                overall_rating=_none_if_blank(r["overall_rating"]),
                lending_test_rating=_none_if_blank(r["lending_test_rating"]),
                cd_test_rating=_none_if_blank(r["cd_test_rating"]),
                total_assets_usd=assets,
                url=r["url"],
                source_file_id=_none_if_blank(r["source_file_id"]),
                notes=notes,
            )
        )
    regulator = index["regulator"].iloc[0] if len(index) else "?"
    if dropped:
        log.info("%s: dropped %d rows by method: %s", regulator, sum(dropped.values()), dict(dropped))
    if flagged:
        log.warning("%s: %d rows with exam date after release date: %s", regulator, len(flagged), flagged)
    return records


def parse_occ(rows: list[dict[str, Any]]) -> list[DocRecord]:
    """OCC search rows to DocRecords (Small Bank and ISB only)."""
    return records_from_index(occ_index(rows))


def parse_fdic(rows: Any) -> list[DocRecord]:
    """CRAPES rows or response payload to DocRecords (Small Bank and ISB only)."""
    return records_from_index(fdic_index(rows))


def parse_frb(source: str | Path) -> list[DocRecord]:
    """FRB CSV (path or text) to DocRecords for the main rows (Small Bank and ISB only)."""
    main, _ = frb_index(source)
    return records_from_index(main)


# --------------------------------------------------------------------------- manifest steps


def records_to_frame(records: list[DocRecord]) -> pd.DataFrame:
    """DocRecords as a frame with ISO dates, ready for CSV."""
    rows = [r.model_dump() for r in records]
    df = pd.DataFrame(rows, columns=list(DocRecord.model_fields))
    for col in ("exam_date", "release_date"):
        df[col] = df[col].map(lambda d: d.isoformat() if isinstance(d, date) else None)
    for col in ("total_assets_usd", "pages"):
        df[col] = df[col].astype("Int64")
    return df


def frame_to_records(df: pd.DataFrame) -> list[DocRecord]:
    """Rows of a manifest frame (as read from CSV) back into validated DocRecords."""
    cols = [c for c in DocRecord.model_fields if c in df.columns]
    records = []
    for row in df[cols].to_dict("records"):
        clean = {k: _none_if_blank(v) for k, v in row.items()}
        records.append(DocRecord(**clean))
    return records


def filter_window(
    records: list[DocRecord], start: date, end: date
) -> tuple[list[DocRecord], pd.DataFrame]:
    """Keep records released inside [start, end]; return the rest as an exclusion log."""
    kept, excluded = [], []
    for r in records:
        if r.release_date is None:
            reason = "missing_release_date"
        elif r.release_date < start:
            reason = "release_before_window"
        elif r.release_date > end:
            reason = "release_after_window"
        else:
            kept.append(r)
            continue
        excluded.append(
            {
                "doc_id": r.doc_id,
                "regulator": r.regulator,
                "bank_id": r.bank_id,
                "exam_date": r.exam_date,
                "release_date": r.release_date,
                "method": r.method,
                "reason": reason,
            }
        )
    log_df = pd.DataFrame(
        excluded,
        columns=["doc_id", "regulator", "bank_id", "exam_date", "release_date", "method", "reason"],
    )
    if len(log_df):
        log.info("window %s..%s: excluded %s", start, end, log_df["reason"].value_counts().to_dict())
    return kept, log_df


def prior_lookup(manifest: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    """For each manifest row, the latest index row of the same regulator and bank with an
    exam date strictly earlier. Returns a frame aligned to ``manifest.index`` with columns
    ``prior_evaluation_date_index`` (ISO string or None) and ``prior_rating_index``."""
    out = pd.DataFrame(
        {c: pd.Series([None] * len(manifest), index=manifest.index, dtype=object)
         for c in PRIOR_COLUMNS}
    )
    if manifest.empty or index.empty:
        return out
    left = pd.DataFrame(
        {
            "_pos": manifest.index,
            "regulator": manifest["regulator"].astype(str),
            "bank_id": manifest["bank_id"].astype(str).str.lstrip("0"),
            "exam_date": pd.to_datetime(manifest["exam_date"]),
        }
    )
    right = pd.DataFrame(
        {
            "regulator": index["regulator"].astype(str),
            "bank_id": index["bank_id"].astype(str).str.lstrip("0"),
            "prior_date": pd.to_datetime(index["exam_date"]),
            "prior_rating": index["overall_rating"],
        }
    ).dropna(subset=["prior_date"])
    merged = left.merge(right, on=["regulator", "bank_id"], how="inner")
    merged = merged[merged["prior_date"] < merged["exam_date"]]
    best = merged.sort_values("prior_date").groupby("_pos").tail(1).set_index("_pos")
    out.loc[best.index, "prior_evaluation_date_index"] = best["prior_date"].dt.strftime("%Y-%m-%d")
    out.loc[best.index, "prior_rating_index"] = best["prior_rating"].where(best["prior_rating"].notna(), None)
    return out


def corpus_counts(manifest: pd.DataFrame) -> pd.DataFrame:
    """Document counts by regulator x method x release year, with totals per regulator."""
    df = manifest.assign(release_year=pd.to_datetime(manifest["release_date"]).dt.year)
    counts = (
        df.groupby(["regulator", "method", "release_year"]).size().rename("n_docs").reset_index()
    )
    return counts.sort_values(["regulator", "method", "release_year"]).reset_index(drop=True)


def build_manifest(
    occ: pd.DataFrame,
    fdic: pd.DataFrame,
    frb: pd.DataFrame,
    window: tuple[date, date],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Combine the three index frames into the manifest frame and the exclusion log."""
    records: list[DocRecord] = []
    for frame in (occ, fdic, frb):
        records.extend(records_from_index(frame))
    kept, exclusions = filter_window(records, *window)
    manifest = records_to_frame(kept)
    dup = manifest["doc_id"].duplicated()
    if dup.any():
        raise ValueError(f"duplicate doc_ids in manifest: {manifest.loc[dup, 'doc_id'].tolist()[:5]}")
    all_index = pd.concat([occ, fdic, frb], ignore_index=True)
    manifest[PRIOR_COLUMNS] = prior_lookup(manifest, all_index)
    manifest["exam_date_source"] = manifest["regulator"].map(default_exam_date_source)
    manifest = manifest.sort_values("doc_id").reset_index(drop=True)
    return manifest, exclusions


# --------------------------------------------------------------------------- network


def _session(session: requests.Session | None = None) -> requests.Session:
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)
    return s


def fetch_occ(from_date: date, to_date: date, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """OCC search rows for exams dated inside [from_date, to_date] (the API filters on
    exam date, not release date; release dates lag by months, so ask from well before the
    release window)."""
    body = {
        "q": "",
        "rating": "",
        "state": "",
        "eFromDate": f"{from_date:%m/%d/%Y}",
        "eToDate": f"{to_date:%m/%d/%Y}",
    }
    r = _session(session).post(OCC_SEARCH_URL, json=body, timeout=120)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise ValueError(f"OCC search returned {type(rows).__name__}, expected a list")
    log.info("OCC: %d rows for exams %s..%s", len(rows), from_date, to_date)
    return rows


def month_ranges(from_date: date, to_date: date) -> list[tuple[date, date]]:
    """Calendar months covering [from_date, to_date], each clipped to whole months."""
    out = []
    y, m = from_date.year, from_date.month
    while date(y, m, 1) <= to_date:
        out.append((date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fetch_fdic(
    from_date: date,
    to_date: date,
    methods: tuple[str, ...] = FDIC_METHOD_CODES,
    session: requests.Session | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """CRAPES rows released inside the window, queried month by month and method by method
    because the API caps every response at 500 rows. Returns (rows, query log); raises if
    any single query hits the cap, since rows would be missing silently."""
    s = _session(session)
    rows: list[dict[str, Any]] = []
    queries = []
    for start, end in month_ranges(from_date, to_date):
        for code in methods:
            body = {"release": f"{start.year}-{start.month}-1:{end.year}-{end.month}-{end.day}", "examCriteria": code}
            r = s.post(FDIC_SEARCH_URL, json=body, timeout=120)
            r.raise_for_status()
            data = fdic_rows(r.json())
            if len(data) >= FDIC_ROW_CAP:
                raise RuntimeError(f"CRAPES query {body} returned {len(data)} rows: hit the {FDIC_ROW_CAP} cap")
            queries.append({"body": body, "n_rows": len(data)})
            rows.extend(data)
            time.sleep(REQUEST_PAUSE_S)
    log.info("FDIC: %d rows from %d queries", len(rows), len(queries))
    return rows, queries


def fetch_frb(session: requests.Session | None = None) -> str:
    """The full FRB ratings CSV: open the search page (session cookie), post an empty
    search so the server stores an all-institutions result, then download the CSV."""
    s = _session(session)
    s.get(FRB_SEARCH_URL, timeout=60).raise_for_status()
    form = {
        "SearchClick": "T", "NameID": "", "CRARating": "0", "City": "", "County": "", "States": "0",
        "AssetSize": "0", "ExamYears": "0", "PublicYears": "0", "ExamMethod": "All", "LendingL": "0",
        "Investment": "0", "Service": "0", "LendingS": "0", "Community": "0", "btnSearch": "Search",
    }
    s.post(FRB_RESULT_URL, data=form, timeout=120).raise_for_status()
    r = s.get(FRB_CSV_URL, timeout=300)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig")
    n_lines = text.count("\n")
    if n_lines < 100:
        raise RuntimeError(f"FRB CSV download has only {n_lines} lines; the search did not register")
    log.info("FRB: CSV with %d lines", n_lines)
    return text


# --------------------------------------------------------------------------- orchestration


def raw_paths(stamp: str) -> tuple[Path, Path, Path]:
    """Where the three raw responses of one pull live under ``data/index/raw/``."""
    return (
        RAW_INDEX_DIR / f"occ_cra_eval_{stamp}.json",
        RAW_INDEX_DIR / f"fdic_crapes_{stamp}.json",
        RAW_INDEX_DIR / f"frb_cra_ratings_{stamp}.csv",
    )


def load_sources(
    window: tuple[date, date], prior_from: date, offline: bool, from_raw: str | None = None
) -> tuple[list[dict[str, Any]], Any, str]:
    """Raw OCC rows, CRAPES payload and FRB CSV text, from the fixtures, a saved pull or
    the agencies. Live responses are saved under ``data/index/raw/`` with today's date in
    the name so that ``from_raw=YYYYMMDD`` can rebuild the same manifest without network."""
    if offline:
        log.info("offline: reading index fixtures from %s", FIXTURES_DIR / "index")
        occ_path, fdic_path, frb_path = OCC_FIXTURE, FDIC_FIXTURE, FRB_FIXTURE
    elif from_raw:
        occ_path, fdic_path, frb_path = raw_paths(from_raw)
        log.info("reading the saved pull %s from %s", from_raw, RAW_INDEX_DIR)
    else:
        ensure_dirs()
        occ_path, fdic_path, frb_path = raw_paths(f"{date.today():%Y%m%d}")
        occ_rows = fetch_occ(prior_from, window[1])
        occ_path.write_text(json.dumps(occ_rows, indent=1), encoding="utf-8")
        fdic_data, queries = fetch_fdic(prior_from, window[1])
        fdic_payload = {"status": "success", "queries": queries, "data": fdic_data}
        fdic_path.write_text(json.dumps(fdic_payload, indent=1), encoding="utf-8")
        frb_path.write_text(fetch_frb(), encoding="utf-8")
    missing = [p for p in (occ_path, fdic_path, frb_path) if not p.exists()]
    if missing:
        raise FileNotFoundError(f"index sources not found: {[str(p) for p in missing]}")
    return (
        json.loads(occ_path.read_text(encoding="utf-8")),
        json.loads(fdic_path.read_text(encoding="utf-8")),
        frb_path.read_text(encoding="utf-8-sig"),
    )


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)


def side_output_dirs(out: Path, index_dir: Path | None, results_dir: Path | None) -> tuple[Path, Path]:
    """Where the per-agency index files and the counts table go.

    Explicit directories win. Otherwise the repo paths are used only when the manifest is
    written to its repo location (``CORPUS_PATH``); a manifest written elsewhere gets its
    side outputs next to it (``<out dir>/index/`` and ``<out dir>/results/``), so trying the
    stage with ``--out /tmp/x.csv`` or ``--offline-from-fixtures`` never overwrites the live
    pull under ``data/index/`` or ``results/corpus_counts.csv``.
    """
    out = Path(out)
    if out.resolve() == Path(CORPUS_PATH).resolve():
        default_index, default_results = INDEX_DIR, RESULTS_DIR
    else:
        default_index, default_results = out.parent / "index", out.parent / "results"
    return (Path(index_dir) if index_dir else default_index, Path(results_dir) if results_dir else default_results)


def run(
    window: tuple[date, date],
    prior_from: date,
    offline: bool,
    out: Path,
    from_raw: str | None = None,
    index_dir: Path | None = None,
    results_dir: Path | None = None,
) -> pd.DataFrame:
    """Build and write the index files, manifest, exclusion log and counts; return the manifest.
    Side outputs go to ``side_output_dirs(out, index_dir, results_dir)``."""
    occ_rows, fdic_payload, frb_text = load_sources(window, prior_from, offline, from_raw)
    index_out, results_out = side_output_dirs(out, index_dir, results_dir)
    if offline and index_out.resolve() == Path(INDEX_DIR).resolve():
        raise ValueError(
            "--offline-from-fixtures would overwrite the live index files under "
            f"{INDEX_DIR}; pass --out outside the repo or an explicit --index-dir"
        )
    occ = occ_index(occ_rows)
    fdic = fdic_index(fdic_payload)
    frb, frb_state = frb_index(frb_text)
    for name, frame in (("occ", occ), ("fdic", fdic), ("frb", frb), ("frb_state_rows", frb_state)):
        _write_csv(frame, index_out / f"{name}.csv")
        log.info("wrote %s (%d rows)", index_out / f"{name}.csv", len(frame))

    manifest, exclusions = build_manifest(occ, fdic, frb, window)
    _write_csv(manifest[MANIFEST_COLUMNS], out)
    _write_csv(exclusions, index_out / "manifest_exclusions.csv")
    counts = corpus_counts(manifest)
    _write_csv(counts, results_out / "corpus_counts.csv")
    log.info("wrote %s (%d documents); counts:\n%s", out, len(manifest), counts.to_string(index=False))
    return manifest


def _iso(text: str) -> date:
    return date.fromisoformat(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build config/corpus.csv and the agency index files.")
    parser.add_argument("--window", nargs=2, type=_iso, metavar=("START", "END"),
                        default=(date(2023, 1, 1), date(2025, 12, 31)),
                        help="release-date window, inclusive (ISO dates)")
    parser.add_argument("--prior-from", type=_iso, default=date(2018, 1, 1),
                        help="earliest exam date to pull from the OCC and FDIC for the prior-exam lookup")
    parser.add_argument("--offline-from-fixtures", action="store_true",
                        help="use the snapshots under tests/fixtures/index instead of the agencies")
    parser.add_argument("--from-raw", metavar="YYYYMMDD",
                        help="rebuild from the raw responses of an earlier pull in data/index/raw/")
    parser.add_argument("--out", type=Path, default=CORPUS_PATH, help="manifest path")
    parser.add_argument("--index-dir", type=Path, default=None,
                        help="where the per-agency index files go (default: data/index when --out is the "
                             "repo manifest, else <out dir>/index)")
    parser.add_argument("--results-dir", type=Path, default=None,
                        help="where corpus_counts.csv goes (default: results/ when --out is the repo "
                             "manifest, else <out dir>/results)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    start, end = args.window
    if start > end:
        parser.error("window START must not be after END")
    try:
        run((start, end), args.prior_from, args.offline_from_fixtures, args.out, args.from_raw,
            args.index_dir, args.results_dir)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
