"""Page-wise text extraction from Performance Evaluation PDFs.

For each PDF the module writes two files under ``data/text/``:

- ``{doc_id}.pages.json``: a list of :class:`cra_extract.models.Page` dicts
  (``number``, ``text``, ``chars``, plus the scope fields at their defaults, which
  the locator fills in later).
- ``{doc_id}.txt``: the page texts joined with a form feed (``"\\f"``).

Offset convention
-----------------
Everything downstream (the locator's ``Section.char_start`` and ``char_end``, and the
groundedness check's quote offsets) uses character offsets into the string returned by
:func:`doc_text`, which is byte-for-byte the content of ``{doc_id}.txt``::

    doc_text(pages) == "\\f".join(p.text for p in pages)

Page ``k`` (1-based) therefore starts at ``sum(len(p.text) + 1 for p in pages[:k - 1])``
and ends at that start plus ``len(pages[k - 1].text)``. :func:`page_offsets` returns these
spans and :func:`page_of_offset` maps an offset back to a page number. Page texts are stored
verbatim as pymupdf returns them (no stripping or normalisation) so that slices round-trip
and a quote located in the document string can be shown on its page.

``text_extractable`` is false when the mean characters per page is below
:data:`MIN_MEAN_CHARS_PER_PAGE` or more than :data:`MAX_SPARSE_PAGE_SHARE` of the pages have
fewer than :data:`MIN_CHARS_SPARSE_PAGE` characters. Such documents are scanned images that
would need OCR (design.md section 2); the thresholds are far below the 1,800 to 2,200 mean
characters per page of the eight born-digital samples.

CLI: ``python -m cra_extract.parse --manifest config/corpus.csv [--doc-id ID] [--force]``
parses every manifest row whose PDF is present and fills the manifest columns ``pages``,
``chars_per_page`` and ``text_extractable`` in place.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pymupdf

from cra_extract.config import CORPUS_PATH, PDF_DIR, TEXT_DIR
from cra_extract.fetch import read_manifest, write_manifest
from cra_extract.models import Page

log = logging.getLogger(__name__)

PAGE_SEPARATOR = "\f"
MIN_MEAN_CHARS_PER_PAGE = 200
MIN_CHARS_SPARSE_PAGE = 50
MAX_SPARSE_PAGE_SHARE = 0.5

MANIFEST_TEXT_COLUMNS = ("pages", "chars_per_page", "text_extractable")


class TextStats(NamedTuple):
    """Document-level text statistics written to the manifest."""

    pages: int
    chars_per_page: float
    text_extractable: bool


# ----------------------------------------------------------------------------- extraction


def extract_pages(pdf_path: Path) -> list[Page]:
    """Extract the text of every page of ``pdf_path`` in reading order."""
    pages: list[Page] = []
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text()
            pages.append(Page(number=i, text=text, chars=len(text)))
    return pages


def doc_text(pages: Sequence[Page]) -> str:
    """The document string that all character offsets refer to (see module docstring)."""
    return PAGE_SEPARATOR.join(p.text for p in pages)


def page_offsets(pages: Sequence[Page]) -> list[tuple[int, int]]:
    """``(start, end)`` of each page's text within :func:`doc_text`, in page order."""
    spans: list[tuple[int, int]] = []
    start = 0
    for p in pages:
        end = start + len(p.text)
        spans.append((start, end))
        start = end + len(PAGE_SEPARATOR)
    return spans


def page_of_offset(pages: Sequence[Page], offset: int) -> int:
    """1-based page number containing ``offset`` of :func:`doc_text`.

    An offset that lands on a form feed separator is assigned to the page before it.
    """
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    for p, (_, end) in zip(pages, page_offsets(pages), strict=True):
        if offset <= end:
            return p.number
    raise ValueError(f"offset {offset} is beyond the end of the document")


# ----------------------------------------------------------------------------- statistics


def is_text_extractable(pages: Sequence[Page]) -> bool:
    """Apply the thresholds from the module docstring. An empty document is not extractable."""
    if not pages:
        return False
    mean_chars = sum(p.chars for p in pages) / len(pages)
    sparse_share = sum(p.chars < MIN_CHARS_SPARSE_PAGE for p in pages) / len(pages)
    return mean_chars >= MIN_MEAN_CHARS_PER_PAGE and sparse_share <= MAX_SPARSE_PAGE_SHARE


def text_stats(pages: Sequence[Page]) -> TextStats:
    """Page count, mean characters per page and the extractability flag."""
    n = len(pages)
    mean_chars = sum(p.chars for p in pages) / n if n else 0.0
    return TextStats(pages=n, chars_per_page=mean_chars, text_extractable=is_text_extractable(pages))


# ----------------------------------------------------------------------------- files


def text_paths(doc_id: str, text_dir: Path = TEXT_DIR) -> tuple[Path, Path]:
    """``(pages.json path, txt path)`` for a document."""
    return text_dir / f"{doc_id}.pages.json", text_dir / f"{doc_id}.txt"


def write_pages(doc_id: str, pages: Sequence[Page], text_dir: Path = TEXT_DIR) -> tuple[Path, Path]:
    """Write ``{doc_id}.pages.json`` and ``{doc_id}.txt``; return their paths."""
    text_dir.mkdir(parents=True, exist_ok=True)
    json_path, txt_path = text_paths(doc_id, text_dir)
    json_path.write_text(
        json.dumps([p.model_dump() for p in pages], ensure_ascii=False, indent=0), encoding="utf-8"
    )
    txt_path.write_text(doc_text(pages), encoding="utf-8", newline="")
    return json_path, txt_path


def load_pages(doc_id: str, text_dir: Path = TEXT_DIR) -> list[Page]:
    """Load the pages written by :func:`write_pages`. Raises ``FileNotFoundError`` if absent."""
    json_path, _ = text_paths(doc_id, text_dir)
    with open(json_path, encoding="utf-8") as fh:
        return [Page.model_validate(d) for d in json.load(fh)]


def parse_pdf(
    pdf_path: Path, doc_id: str, text_dir: Path = TEXT_DIR, force: bool = False
) -> TextStats:
    """Extract (or, unless ``force``, reuse) the text of one PDF and return its statistics."""
    json_path, _ = text_paths(doc_id, text_dir)
    if json_path.exists() and not force:
        pages = load_pages(doc_id, text_dir)
    else:
        pages = extract_pages(pdf_path)
        write_pages(doc_id, pages, text_dir)
    return text_stats(pages)


# ----------------------------------------------------------------------------- manifest


def parse_manifest(
    manifest_path: Path = CORPUS_PATH,
    *,
    doc_id: str | None = None,
    force: bool = False,
    pdf_dir: Path = PDF_DIR,
    text_dir: Path = TEXT_DIR,
) -> pd.DataFrame:
    """Parse every manifest row whose PDF exists and update the text columns in place.

    Rows without a PDF on disk are logged and left untouched. Returns the updated manifest.
    """
    df = read_manifest(manifest_path)
    for col in MANIFEST_TEXT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    rows = df.index if doc_id is None else df.index[df["doc_id"] == doc_id]
    parsed = missing = 0
    for idx in rows:
        did = df.at[idx, "doc_id"]
        pdf_path = pdf_dir / f"{did}.pdf"
        if not pdf_path.exists():
            log.warning("%s: no PDF at %s", did, pdf_path)
            missing += 1
            continue
        stats = parse_pdf(pdf_path, did, text_dir, force=force)
        df.at[idx, "pages"] = str(stats.pages)
        df.at[idx, "chars_per_page"] = f"{stats.chars_per_page:.1f}"
        df.at[idx, "text_extractable"] = str(stats.text_extractable)
        parsed += 1
        if not stats.text_extractable:
            log.warning("%s: not text extractable (%.0f chars/page)", did, stats.chars_per_page)
    write_manifest(df, manifest_path)
    n_bad = int((df.loc[rows, "text_extractable"] == "False").sum())
    log.info("parsed %d, missing PDF %d, not extractable %d", parsed, missing, n_bad)
    return df


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    parser.add_argument("--doc-id", help="parse only this document")
    parser.add_argument("--force", action="store_true", help="re-extract even if text exists")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    parse_manifest(
        args.manifest, doc_id=args.doc_id, force=args.force, pdf_dir=PDF_DIR, text_dir=TEXT_DIR
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
