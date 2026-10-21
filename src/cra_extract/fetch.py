"""Download the corpus PDFs listed in the manifest.

``python -m cra_extract.fetch --manifest config/corpus.csv`` downloads every PDF in the
manifest to ``data/pdfs/{doc_id}.pdf`` and fills the manifest columns ``sha256`` and
``pages`` in place. Runs are resumable: a file already on disk whose sha256 matches the
manifest is skipped, and when the manifest has no checksum yet the file's checksum and page
count are recorded without a download. A file whose checksum differs from the manifest is
downloaded again; if the fresh copy still differs the row is logged as ``sha256_mismatch``
and the file on disk is left as it was (clear the manifest checksum to accept a replaced PDF).

Politeness and robustness: one request per second by default, a descriptive User-Agent,
retries with exponential backoff on 429 and 5xx responses and on connection errors
(honoring ``Retry-After``), a content-type and ``%PDF`` magic check, and atomic writes
(temp file in the same directory, then rename). Failures are appended to
``data/index/fetch_failures.csv`` and the run continues; the exit status is 1 when any
row failed so that ``make fetch`` reports it.

The manifest helpers :func:`read_manifest` and :func:`write_manifest` are shared with
``parse.py``. Every cell is read as a string and missing values are the empty string, which
is what ``DocRecord`` expects after :func:`row_record` maps empty strings to ``None``.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pymupdf
import requests

from cra_extract import __version__
from cra_extract.config import CORPUS_PATH, INDEX_DIR, PDF_DIR
from cra_extract.models import DocRecord

log = logging.getLogger(__name__)

USER_AGENT = (
    f"cra-pe-extraction/{__version__} "
    "(independent research project; downloads public CRA Performance Evaluations at 1 request per second)"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"}
DEFAULT_RATE_LIMIT = 1.0  # requests per second
MAX_RETRIES = 5
BACKOFF_BASE_S = 1.0  # delay before retry k is BACKOFF_BASE_S * 2**k unless Retry-After says more
MAX_RETRY_AFTER_S = 300.0
TIMEOUT_S = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PDF_CONTENT_TYPES = ("pdf", "octet-stream")
FAILURES_PATH = INDEX_DIR / "fetch_failures.csv"
FAILURE_COLUMNS = ["logged_at", "doc_id", "url", "reason", "detail", "attempts"]
MANIFEST_FETCH_COLUMNS = ("sha256", "pages")

SleepFn = Callable[[float], None]


class FetchError(Exception):
    """A download that failed for a known reason (recorded in the failures log)."""

    def __init__(self, reason: str, detail: str = "", attempts: int = 1) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.attempts = attempts


@dataclass
class FetchResult:
    """Outcome for one manifest row.

    ``status`` is ``downloaded``, ``present`` (already on disk and verified), ``planned``
    (dry run) or ``failed``.
    """

    doc_id: str
    status: str
    sha256: str | None = None
    pages: int | None = None
    reason: str | None = None
    detail: str | None = None
    attempts: int = 0


# ----------------------------------------------------------------------------- manifest


def read_manifest(path: Path = CORPUS_PATH) -> pd.DataFrame:
    """Read the manifest with every column as text and blanks as empty strings."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def write_manifest(df: pd.DataFrame, path: Path = CORPUS_PATH) -> None:
    """Write the manifest atomically (temp file in the same directory, then rename)."""
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def row_record(row: pd.Series | dict[str, Any]) -> DocRecord:
    """Build a ``DocRecord`` from a manifest row, mapping empty strings to ``None``."""
    cleaned = {k: (None if v == "" else v) for k, v in dict(row).items() if k in DocRecord.model_fields}
    return DocRecord.model_validate(cleaned)


def select_rows(
    df: pd.DataFrame, *, split: str = "all", doc_id: str | None = None
) -> pd.Index:
    """Indices of the rows selected by ``--split`` and ``--doc-id``."""
    mask = pd.Series(True, index=df.index)
    if split != "all":
        mask &= df["split"] == split
    if doc_id is not None:
        mask &= df["doc_id"] == doc_id
    return df.index[mask]


# ----------------------------------------------------------------------------- helpers


def sha256_of(path: Path) -> str:
    """Hex sha256 of a file, streamed."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_like_pdf(data: bytes) -> bool:
    """True when the ``%PDF`` signature appears in the first 1,024 bytes (the spec allows
    leading junk before the header)."""
    return b"%PDF" in data[:1024]


def page_count(path: Path) -> int:
    """Number of pages, via pymupdf. Raises on files pymupdf cannot open."""
    with pymupdf.open(path) as doc:
        return doc.page_count


def retry_after_seconds(headers: Any) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delay or HTTP date), capped at
    :data:`MAX_RETRY_AFTER_S`; ``None`` when absent or unparseable."""
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        delay = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        delay = (when - datetime.now(when.tzinfo or UTC)).total_seconds()
    return min(max(delay, 0.0), MAX_RETRY_AFTER_S)


class RateLimiter:
    """Spaces calls to :meth:`wait` at least ``1 / per_second`` seconds apart."""

    def __init__(
        self,
        per_second: float = DEFAULT_RATE_LIMIT,
        sleep: SleepFn = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()


# ----------------------------------------------------------------------------- download


def download(
    session: Any,
    url: str,
    *,
    max_retries: int = MAX_RETRIES,
    backoff: float = BACKOFF_BASE_S,
    sleep: SleepFn = time.sleep,
    timeout: float = TIMEOUT_S,
) -> bytes:
    """GET ``url`` and return the PDF bytes.

    Retries on 429, 5xx, connection errors and timeouts with exponential backoff, using the
    server's ``Retry-After`` when it asks for a longer wait. Other HTTP errors fail at once.
    Raises :class:`FetchError` with reason ``http_error``, ``retries_exhausted`` or
    ``not_pdf``.
    """
    last_detail = ""
    for attempt in range(max_retries + 1):
        retry_after: float | None = None
        try:
            resp = session.get(url, headers=HEADERS, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as err:
            last_detail = f"{type(err).__name__}: {err}"
        else:
            if resp.status_code == 200:
                return _checked_pdf(resp, attempt + 1)
            last_detail = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRY_STATUSES:
                raise FetchError("http_error", last_detail, attempt + 1)
            retry_after = retry_after_seconds(resp.headers)
        if attempt == max_retries:
            break
        delay = max(backoff * 2**attempt, retry_after or 0.0)
        log.info("%s: %s, retry %d/%d in %.0fs", url, last_detail, attempt + 1, max_retries, delay)
        sleep(delay)
    raise FetchError("retries_exhausted", last_detail, max_retries + 1)


def _checked_pdf(resp: Any, attempts: int) -> bytes:
    content_type = str(resp.headers.get("Content-Type", "")).lower()
    data: bytes = resp.content
    if not looks_like_pdf(data):
        raise FetchError("not_pdf", f"content-type {content_type!r}, starts {data[:24]!r}", attempts)
    if not any(t in content_type for t in PDF_CONTENT_TYPES):
        log.warning("unexpected content-type %r but body is a PDF; accepting", content_type)
    return data


def write_atomic(dest: Path, data: bytes) -> None:
    """Write ``data`` to a temp file next to ``dest`` and rename it into place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- per document


def fetch_one(
    row: pd.Series | dict[str, Any],
    session: Any,
    limiter: RateLimiter,
    *,
    pdf_dir: Path = PDF_DIR,
    dry_run: bool = False,
    sleep: SleepFn = time.sleep,
) -> FetchResult:
    """Make sure ``pdf_dir/{doc_id}.pdf`` exists and is verified; see module docstring."""
    doc_id, url = row["doc_id"], row["url"]
    expected = row.get("sha256") or None
    dest = pdf_dir / f"{doc_id}.pdf"

    if dest.exists():
        actual = sha256_of(dest)
        if expected is None or actual == expected:
            try:
                pages = page_count(dest)
            except Exception as err:  # any pymupdf failure means download again
                log.warning("%s: existing file unreadable (%s); downloading again", doc_id, err)
            else:
                return FetchResult(doc_id, "present", actual, pages)
        else:
            log.warning("%s: checksum of existing file differs from manifest; downloading again", doc_id)

    if not url:
        return FetchResult(doc_id, "failed", reason="no_url")
    if dry_run:
        return FetchResult(doc_id, "planned")

    limiter.wait()
    try:
        data = download(session, url, sleep=sleep)
    except FetchError as err:
        return FetchResult(doc_id, "failed", reason=err.reason, detail=err.detail, attempts=err.attempts)
    except requests.RequestException as err:
        return FetchResult(doc_id, "failed", reason="request_error", detail=f"{type(err).__name__}: {err}")

    actual = hashlib.sha256(data).hexdigest()
    if expected is not None and actual != expected:
        return FetchResult(
            doc_id, "failed", reason="sha256_mismatch", detail=f"manifest {expected[:12]}, got {actual[:12]}"
        )
    write_atomic(dest, data)
    try:
        pages = page_count(dest)
    except Exception as err:  # report whatever pymupdf raised
        dest.unlink(missing_ok=True)
        return FetchResult(doc_id, "failed", reason="unreadable_pdf", detail=f"{type(err).__name__}: {err}")
    return FetchResult(doc_id, "downloaded", actual, pages)


def log_failure(result: FetchResult, url: str, path: Path = FAILURES_PATH) -> None:
    """Append one failed result to the failures CSV, creating it with a header if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = pd.DataFrame(
        [
            {
                "logged_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "doc_id": result.doc_id,
                "url": url,
                "reason": result.reason,
                "detail": result.detail or "",
                "attempts": result.attempts,
            }
        ],
        columns=FAILURE_COLUMNS,
    )
    row.to_csv(path, mode="a", header=not path.exists(), index=False)


# ----------------------------------------------------------------------------- manifest run


def fetch_manifest(
    manifest_path: Path = CORPUS_PATH,
    *,
    limit: int | None = None,
    split: str = "all",
    doc_id: str | None = None,
    dry_run: bool = False,
    rate_limit: float = DEFAULT_RATE_LIMIT,
    session: Any | None = None,
    sleep: SleepFn = time.sleep,
    pdf_dir: Path = PDF_DIR,
    failures_path: Path = FAILURES_PATH,
) -> list[FetchResult]:
    """Fetch every selected manifest row and update the manifest in place.

    ``limit`` stops after that many rows needed network work (files already present and
    verified do not count). A dry run touches neither the network nor any file.
    """
    df = read_manifest(manifest_path)
    for col in MANIFEST_FETCH_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    rows = select_rows(df, split=split, doc_id=doc_id)
    if session is None:
        session = requests.Session()
    limiter = RateLimiter(rate_limit, sleep=sleep)
    results: list[FetchResult] = []
    attempted = 0
    try:
        for idx in rows:
            row = df.loc[idx]
            result = fetch_one(row, session, limiter, pdf_dir=pdf_dir, dry_run=dry_run, sleep=sleep)
            results.append(result)
            if result.status == "failed":
                log.error("%s: %s (%s)", result.doc_id, result.reason, result.detail or "")
                if not dry_run:
                    log_failure(result, row["url"], failures_path)
            elif result.status == "planned":
                log.info("%s: would download %s", result.doc_id, row["url"])
            elif result.sha256 is not None:
                df.at[idx, "sha256"] = result.sha256
                df.at[idx, "pages"] = str(result.pages)
            if result.status != "present":
                attempted += 1
                if limit is not None and attempted >= limit:
                    break
    finally:
        if not dry_run:
            write_manifest(df, manifest_path)
    counts = {s: sum(r.status == s for r in results) for s in ("downloaded", "present", "planned", "failed")}
    log.info("fetch summary: %s", ", ".join(f"{k} {v}" for k, v in counts.items()))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    parser.add_argument("--limit", type=int, help="stop after this many downloads")
    parser.add_argument("--split", default="all", choices=["dev", "test", "corpus", "all"])
    parser.add_argument("--doc-id", help="fetch only this document")
    parser.add_argument("--dry-run", action="store_true", help="report what would be downloaded")
    parser.add_argument(
        "--rate-limit", type=float, default=DEFAULT_RATE_LIMIT, help="requests per second"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    results = fetch_manifest(
        args.manifest,
        limit=args.limit,
        split=args.split,
        doc_id=args.doc_id,
        dry_run=args.dry_run,
        rate_limit=args.rate_limit,
        pdf_dir=PDF_DIR,
        failures_path=FAILURES_PATH,
    )
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
