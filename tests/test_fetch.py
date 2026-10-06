"""Tests for ``cra_extract.fetch`` with a scripted fake HTTP session (no network).

The fake serves the OCC GN Bank sample (17 pages, verified with pymupdf) so that the
checksum, resume, retry, magic-check and manifest-update paths are all exercised on a
real PDF.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pandas as pd
import pytest
import requests

from cra_extract import fetch
from cra_extract.models import DocRecord

SAMPLES = Path(__file__).parent / "fixtures" / "samples"

SAMPLE_PDF = SAMPLES / "OCC_703395_GNBank_2023_NI.pdf"
SAMPLE_PAGES = 17
DOC_ID = "OCC_703395_20230327"
URL = "https://example.invalid/craeval/GNBank.pdf"


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Returns the scripted responses in order; raises scripted exceptions."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None):
        self.calls.append((url, headers or {}))
        if not self.script:
            raise AssertionError(f"unexpected request to {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def pdf_response() -> FakeResponse:
    return FakeResponse(200, SAMPLE_PDF.read_bytes(), {"Content-Type": "application/pdf"})


@pytest.fixture
def sample_sha() -> str:
    return hashlib.sha256(SAMPLE_PDF.read_bytes()).hexdigest()


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    """A one-row manifest for the GN Bank sample with empty fetch columns."""
    row = {
        "doc_id": DOC_ID,
        "regulator": "OCC",
        "bank_id": "703395",
        "bank_name": "GN Bank",
        "exam_date": "2023-03-27",
        "release_date": "",
        "method": "small",
        "overall_rating": "NI",
        "url": URL,
        "sha256": "",
        "pages": "",
        "chars_per_page": "",
        "text_extractable": "",
        "split": "dev",
    }
    path = tmp_path / "corpus.csv"
    pd.DataFrame([row]).to_csv(path, index=False)
    return path


@pytest.fixture
def sleeps() -> list[float]:
    return []


def run(manifest: Path, tmp_path: Path, session: FakeSession, sleeps: list[float], **kw):
    return fetch.fetch_manifest(
        manifest,
        session=session,
        sleep=sleeps.append,
        pdf_dir=tmp_path / "pdfs",
        failures_path=tmp_path / "fetch_failures.csv",
        **kw,
    )


def set_manifest(manifest: Path, **values: str) -> None:
    df = fetch.read_manifest(manifest)
    for k, v in values.items():
        df.at[0, k] = v
    fetch.write_manifest(df, manifest)


# ----------------------------------------------------------------------------- download path


def test_download_verifies_sha256_and_updates_manifest(manifest, tmp_path, sample_sha, sleeps):
    session = FakeSession([pdf_response()])
    results = run(manifest, tmp_path, session, sleeps)

    assert [r.status for r in results] == ["downloaded"]
    dest = tmp_path / "pdfs" / f"{DOC_ID}.pdf"
    assert fetch.sha256_of(dest) == sample_sha == results[0].sha256
    assert results[0].pages == SAMPLE_PAGES
    assert not list((tmp_path / "pdfs").glob("*.part")), "temp file must be renamed away"
    assert not (tmp_path / "fetch_failures.csv").exists()

    df = fetch.read_manifest(manifest)
    assert df.at[0, "sha256"] == sample_sha
    assert df.at[0, "pages"] == str(SAMPLE_PAGES)
    record = fetch.row_record(df.loc[0])
    assert isinstance(record, DocRecord) and record.pages == SAMPLE_PAGES
    assert record.release_date is None, "empty cells map to None"

    url, headers = session.calls[0]
    assert url == URL
    assert headers["User-Agent"].startswith("cra-pe-extraction/")


def test_sha256_mismatch_is_logged_and_file_not_written(manifest, tmp_path, sleeps):
    set_manifest(manifest, sha256="0" * 64)
    results = run(manifest, tmp_path, FakeSession([pdf_response()]), sleeps)

    assert results[0].status == "failed" and results[0].reason == "sha256_mismatch"
    assert not (tmp_path / "pdfs" / f"{DOC_ID}.pdf").exists()
    failures = pd.read_csv(tmp_path / "fetch_failures.csv", dtype=str)
    assert failures["reason"].tolist() == ["sha256_mismatch"]
    assert failures["doc_id"].tolist() == [DOC_ID]
    assert fetch.read_manifest(manifest).at[0, "sha256"] == "0" * 64, "manifest untouched"


# ----------------------------------------------------------------------------- resume


def test_resume_skips_file_with_matching_sha256(manifest, tmp_path, sample_sha, sleeps):
    set_manifest(manifest, sha256=sample_sha)
    (tmp_path / "pdfs").mkdir()
    shutil.copy(SAMPLE_PDF, tmp_path / "pdfs" / f"{DOC_ID}.pdf")
    session = FakeSession([])

    results = run(manifest, tmp_path, session, sleeps)
    assert [r.status for r in results] == ["present"]
    assert session.calls == []
    assert fetch.read_manifest(manifest).at[0, "pages"] == str(SAMPLE_PAGES)


def test_resume_fills_missing_sha256_without_download(manifest, tmp_path, sample_sha, sleeps):
    (tmp_path / "pdfs").mkdir()
    shutil.copy(SAMPLE_PDF, tmp_path / "pdfs" / f"{DOC_ID}.pdf")
    session = FakeSession([])

    results = run(manifest, tmp_path, session, sleeps)
    assert results[0].status == "present" and results[0].sha256 == sample_sha
    assert session.calls == []
    df = fetch.read_manifest(manifest)
    assert df.at[0, "sha256"] == sample_sha and df.at[0, "pages"] == str(SAMPLE_PAGES)


def test_resume_redownloads_when_existing_file_differs(manifest, tmp_path, sample_sha, sleeps):
    set_manifest(manifest, sha256=sample_sha)
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / f"{DOC_ID}.pdf").write_bytes(b"%PDF-1.4 truncated")
    session = FakeSession([pdf_response()])

    results = run(manifest, tmp_path, session, sleeps)
    assert results[0].status == "downloaded"
    assert len(session.calls) == 1
    assert fetch.sha256_of(tmp_path / "pdfs" / f"{DOC_ID}.pdf") == sample_sha


# ----------------------------------------------------------------------------- retries


def test_retry_on_429_honors_retry_after_then_succeeds(manifest, tmp_path, sleeps):
    session = FakeSession([FakeResponse(429, b"slow down", {"Retry-After": "3"}), pdf_response()])
    results = run(manifest, tmp_path, session, sleeps)

    assert results[0].status == "downloaded"
    assert len(session.calls) == 2
    assert sleeps == [3.0], "Retry-After of 3s beats the 1s backoff"


def test_connection_errors_back_off_exponentially_then_give_up(sleeps):
    session = FakeSession([requests.ConnectionError("boom")] * 3)
    with pytest.raises(fetch.FetchError) as excinfo:
        fetch.download(session, URL, max_retries=2, sleep=sleeps.append)
    assert excinfo.value.reason == "retries_exhausted"
    assert excinfo.value.attempts == 3
    assert sleeps == [1.0, 2.0]


def test_5xx_is_retried_but_404_is_not(sleeps):
    session = FakeSession([FakeResponse(503), pdf_response()])
    assert fetch.download(session, URL, sleep=sleeps.append) == SAMPLE_PDF.read_bytes()
    assert len(session.calls) == 2

    session = FakeSession([FakeResponse(404)])
    with pytest.raises(fetch.FetchError) as excinfo:
        fetch.download(session, URL, sleep=sleeps.append)
    assert excinfo.value.reason == "http_error" and len(session.calls) == 1


def test_retry_after_parsing():
    assert fetch.retry_after_seconds({"Retry-After": "7"}) == 7.0
    assert fetch.retry_after_seconds({}) is None
    assert fetch.retry_after_seconds({"Retry-After": "soon"}) is None
    assert fetch.retry_after_seconds({"Retry-After": "99999"}) == fetch.MAX_RETRY_AFTER_S
    # an HTTP date in the past means no extra wait
    assert fetch.retry_after_seconds({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0


# ----------------------------------------------------------------------------- content checks


def test_html_body_fails_magic_check_and_is_logged(manifest, tmp_path, sleeps):
    html = FakeResponse(200, b"<html><body>Not found</body></html>", {"Content-Type": "text/html"})
    results = run(manifest, tmp_path, FakeSession([html]), sleeps)

    assert results[0].status == "failed" and results[0].reason == "not_pdf"
    assert "text/html" in results[0].detail
    assert not (tmp_path / "pdfs" / f"{DOC_ID}.pdf").exists()
    failures = pd.read_csv(tmp_path / "fetch_failures.csv", dtype=str)
    assert failures["reason"].tolist() == ["not_pdf"]
    assert fetch.read_manifest(manifest).at[0, "sha256"] == ""


def test_octet_stream_with_pdf_magic_is_accepted(sleeps):
    resp = FakeResponse(200, SAMPLE_PDF.read_bytes(), {"Content-Type": "application/octet-stream"})
    assert fetch.download(FakeSession([resp]), URL, sleep=sleeps.append) == SAMPLE_PDF.read_bytes()


def test_looks_like_pdf():
    assert fetch.looks_like_pdf(b"%PDF-1.7\n")
    assert fetch.looks_like_pdf(b"\n\n junk %PDF-1.4")
    assert not fetch.looks_like_pdf(b"<html>")


# ----------------------------------------------------------------------------- options


def test_dry_run_touches_nothing(manifest, tmp_path, sleeps):
    before = manifest.read_text()
    session = FakeSession([])
    results = run(manifest, tmp_path, session, sleeps, dry_run=True)

    assert [r.status for r in results] == ["planned"]
    assert session.calls == []
    assert not (tmp_path / "pdfs").exists()
    assert manifest.read_text() == before


def test_split_doc_id_and_limit_filters(tmp_path, sleeps):
    rows = []
    for i, split in enumerate(["dev", "test", "corpus"]):
        rows.append(
            {
                "doc_id": f"FDIC_{i}_20240101",
                "regulator": "FDIC",
                "bank_id": str(i),
                "bank_name": f"Bank {i}",
                "method": "small",
                "url": f"https://example.invalid/{i}.pdf",
                "sha256": "",
                "pages": "",
                "split": split,
            }
        )
    manifest = tmp_path / "corpus.csv"
    pd.DataFrame(rows).to_csv(manifest, index=False)

    results = run(manifest, tmp_path, FakeSession([pdf_response()]), sleeps, split="test")
    assert [r.doc_id for r in results] == ["FDIC_1_20240101"]

    results = run(manifest, tmp_path, FakeSession([pdf_response()]), sleeps, doc_id="FDIC_2_20240101")
    assert [r.doc_id for r in results] == ["FDIC_2_20240101"]

    # rows 1 and 2 are now present; limit=1 stops after the first row that needs a download
    session = FakeSession([pdf_response()])
    results = run(manifest, tmp_path, session, sleeps, limit=1)
    assert [r.status for r in results] == ["downloaded"]
    assert len(session.calls) == 1


def test_rate_limiter_spaces_requests():
    clock = {"t": 100.0}
    sleeps: list[float] = []
    limiter = fetch.RateLimiter(2.0, sleep=sleeps.append, clock=lambda: clock["t"])
    limiter.wait()
    assert sleeps == [], "first call never waits"
    clock["t"] += 0.1
    limiter.wait()
    assert sleeps == [pytest.approx(0.4)]
    clock["t"] += 5.0
    limiter.wait()
    assert len(sleeps) == 1, "no wait when the interval has already passed"


def test_cli_dry_run_exit_code(manifest, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "PDF_DIR", tmp_path / "pdfs")
    assert fetch.main(["--manifest", str(manifest), "--dry-run"]) == 0
