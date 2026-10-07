"""Template-residual census of the illegal-credit-practices review section (design 5.2).

``config/review_templates.yaml`` holds the "no evidence" boilerplate paragraphs per regulator
(FRB per Reserve Bank district). :func:`residual` normalizes a located review section, removes
every template paragraph that matches with rapidfuzz ``partial_ratio`` >= 90, and flags the
section for hand reading when what is left is longer than 120 characters or when no template
reaches 85. The two positive samples (SouthStar, Liberty) are flagged on both criteria; the six
negative samples are not flagged.

Normalization: curly quotes straightened, page furniture dropped (bare page numbers,
"Page x of y", "Charter Number: n", running headers with column gaps, the review heading
itself), whitespace collapsed, lowercased. Furniture is dropped before matching because an
OCC paragraph can straddle a page break (GN Bank, lines 199 to 203 of the sample text).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from functools import cache
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel
from rapidfuzz import fuzz

from cra_extract.config import CORPUS_PATH, RESULTS_DIR, TEMPLATES_PATH, TEXT_DIR, load_headings
from cra_extract.locate import load_pages_json, locate_section, normalize_line, pages_from_text

MATCH_THRESHOLD = 90.0  # a template at or above this score is removed from the section
FLAG_SCORE = 85.0  # best score below this flags the section
FLAG_RESIDUAL_CHARS = 120  # residual longer than this flags the section

_FURNITURE = [
    re.compile(r"^\d{1,3}$"),
    re.compile(r"^page \d+ of \d+$", re.IGNORECASE),
    re.compile(r"^charter number:? \d+$", re.IGNORECASE),
    re.compile(r"\S {3,}\S"),  # running header with column gap
]


class Template(BaseModel):
    """One boilerplate paragraph from ``config/review_templates.yaml``."""

    id: str
    regulator: str
    text: str
    district: str | None = None
    source: str | None = None
    distractor_laws_named: bool = False

    @property
    def normalized(self) -> str:
        return normalize(self.text)


class TemplateMatch(BaseModel):
    """Result of :func:`residual` for one review section."""

    template_id: str | None
    score: float
    residual_text: str
    flagged: bool
    removed: list[str] = []


@cache
def load_templates() -> tuple[Template, ...]:
    """Parsed template library, in file order."""
    with open(TEMPLATES_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return tuple(Template(**t) for t in data.get("templates", []))


def templates_for(regulator: str) -> list[Template]:
    return [t for t in load_templates() if t.regulator == regulator]


def _heading_patterns(regulator: str) -> list[re.Pattern[str]]:
    spec = load_headings()["sections"]["illegal_credit_practices_review"]
    return [re.compile(p, re.IGNORECASE) for p in spec.get(regulator, [])]


def normalize(text: str, regulator: str | None = None) -> str:
    """Lowercased, whitespace-collapsed text with straight quotes and page furniture removed."""
    headings = _heading_patterns(regulator) if regulator else []
    kept = []
    for raw in text.split("\n"):
        line = normalize_line(raw)
        if not line or any(p.search(line) for p in _FURNITURE):
            continue
        if any(p.search(line) for p in headings):
            continue
        kept.append(line)
    return re.sub(r"\s+", " ", " ".join(kept)).strip().lower()


def residual(section_text: str, regulator: str) -> TemplateMatch:
    """Remove matching boilerplate from a review section and decide whether to flag it."""
    current = normalize(section_text, regulator)
    best_id: str | None = None
    best_score = 0.0
    removed: list[str] = []
    for template in templates_for(regulator):
        if not current:
            break
        aligned = fuzz.partial_ratio_alignment(template.normalized, current)
        score = float(aligned.score) if aligned else 0.0
        if score > best_score:
            best_id, best_score = template.id, score
        if score >= MATCH_THRESHOLD and aligned is not None:
            current = (current[: aligned.dest_start] + " " + current[aligned.dest_end :]).strip()
            current = re.sub(r"\s+", " ", current)
            removed.append(template.id)
    flagged = len(current) > FLAG_RESIDUAL_CHARS or best_score < FLAG_SCORE
    return TemplateMatch(
        template_id=best_id,
        score=round(best_score, 1),
        residual_text=current,
        flagged=flagged,
        removed=removed,
    )


# --------------------------------------------------------------------------- census


def census_row(doc_id: str, regulator: str, section_text: str | None) -> dict[str, object]:
    """One census row; a missing review section is a locator miss and is flagged."""
    if section_text is None:
        return {
            "doc_id": doc_id,
            "regulator": regulator,
            "located": False,
            "template_id": None,
            "score": None,
            "residual_chars": None,
            "flagged": True,
            "residual_text": "",
        }
    match = residual(section_text, regulator)
    return {
        "doc_id": doc_id,
        "regulator": regulator,
        "located": True,
        "template_id": match.template_id,
        "score": match.score,
        "residual_chars": len(match.residual_text),
        "flagged": match.flagged,
        "residual_text": match.residual_text[:500],
    }


def census(docs: list[tuple[str, str, str | None]]) -> pd.DataFrame:
    """Census table over (doc_id, regulator, review section text or None) triples."""
    return pd.DataFrame([census_row(d, r, t) for d, r, t in docs])


def _review_text(doc_id: str, regulator: str, pages) -> str | None:
    section = locate_section("illegal_credit_practices_review", doc_id, pages, regulator)
    return section.text if section else None


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m cra_extract.templates --manifest config/corpus.csv --out results/census.csv``."""
    ap = argparse.ArgumentParser(description="Template-residual census of review sections.")
    ap.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    ap.add_argument("--text-dir", type=Path, default=TEXT_DIR, help="folder with {doc_id}.pages.json")
    ap.add_argument("--samples-dir", type=Path, help="run on pdftotext .txt files instead of the manifest")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "census.csv")
    args = ap.parse_args(argv)

    docs: list[tuple[str, str, str | None]] = []
    if args.samples_dir:
        for txt in sorted(args.samples_dir.glob("*.txt")):
            regulator = txt.stem.split("_")[0]
            pages = pages_from_text(txt.read_text(encoding="utf-8"))
            docs.append((txt.stem, regulator, _review_text(txt.stem, regulator, pages)))
    elif args.manifest.exists():
        with open(args.manifest, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                path = args.text_dir / f"{row['doc_id']}.pages.json"
                if path.exists():
                    pages = load_pages_json(path)
                    docs.append((row["doc_id"], row["regulator"], _review_text(row["doc_id"], row["regulator"], pages)))
    else:
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    if not docs:
        print("no documents with page text found", file=sys.stderr)
        return 1
    table = census(docs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    print(f"{len(table)} documents; {int(table['flagged'].sum())} flagged; written to {args.out}")
    print(table[["doc_id", "located", "template_id", "score", "residual_chars", "flagged"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
