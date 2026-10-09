"""Template-residual census of the illegal-credit-practices review section (design 5.2).

``config/review_templates.yaml`` holds the "no evidence" boilerplate paragraphs per regulator
(FRB per Reserve Bank district). :func:`residual` normalizes a located review section, removes
every template paragraph that matches with rapidfuzz ``partial_ratio`` >= 90, and flags the
section for hand reading when what is left is longer than 120 characters or when no template
reaches 85. A third criterion, added on 2026-10-08 with the template variants, can only add
flags: a residual that still holds an asserting sentence (:func:`asserting_sentences`) is
flagged whatever its length, so that a short finding sentence next to a boilerplate paragraph
("However, a violation of Regulation B was identified.") cannot hide under the 120-character
rule. The two positive samples (SouthStar, Liberty) are flagged on all criteria; the six
negative samples are not flagged.

Normalization: curly quotes straightened, page furniture dropped (bare page numbers,
"Page x of y", "Charter Number: n", security markings, running headers with column gaps,
the document's running headers and footers from ``locate.running_lines``, the review heading
itself), whitespace collapsed, lowercased. Furniture is dropped before matching because an
OCC paragraph can straddle a page break (GN Bank, lines 199 to 203 of the sample text) and an
FRB paragraph can be followed by the next page's header (bank name, report title, date).

Template admission rule (design 5.2, 2026-10-08): a paragraph enters the library only when it
asserts no finding. :func:`asserting_sentences` returns the sentences of a template that
contain a finding predicate ("lowered", "violation", "identified", "substantive", "downgrade",
"adversely", "consent order", a law name) without a negation that governs it; it must be
empty for every template, which ``tests/test_templates.py`` checks.
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
from cra_extract.locate import (
    COLUMN_GAP,
    is_furniture_line,
    load_pages_json,
    locate_section,
    normalize_line,
    pages_from_text,
    running_lines,
)

MATCH_THRESHOLD = 90.0  # a template at or above this score is removed from the section
FLAG_SCORE = 85.0  # best score below this flags the section
FLAG_RESIDUAL_CHARS = 120  # residual longer than this flags the section
TIE_POINTS = 1.0  # templates this close to the best score count as tied; the longest wins


class Template(BaseModel):
    """One boilerplate paragraph from ``config/review_templates.yaml``."""

    id: str
    regulator: str
    text: str
    district: str | None = None
    source: str | None = None
    distractor_laws_named: bool = False
    evidence: list[str] = []  # corpus doc_ids whose review section the template must match
    note: str | None = None

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
    asserting: list[str] = []  # sentences of the residual that assert a finding


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


def normalize(
    text: str, regulator: str | None = None, furniture: frozenset[str] = frozenset()
) -> str:
    """Lowercased, whitespace-collapsed text with straight quotes and page furniture removed.

    ``furniture`` holds the document's running headers and footers (normalized lines, as
    returned by ``locate.running_lines``); they are dropped like the other page furniture.
    The review heading is dropped also when it is wrapped over two lines ("DISCRIMINATORY OR
    OTHER ILLEGAL CREDIT PRACTICES" then "REVIEW", FDIC_10474_20250428 p17).
    """
    headings = _heading_patterns(regulator) if regulator else []
    kept = []
    for raw in text.split("\n"):
        line = normalize_line(raw)
        if not line or line in furniture or is_furniture_line(line) or COLUMN_GAP.search(line):
            continue
        if any(p.search(line) for p in headings):
            continue
        if kept and any(p.search(f"{kept[-1]} {line}") for p in headings):
            kept.pop()
            continue
        kept.append(line)
    return re.sub(r"\s+", " ", " ".join(kept)).strip().lower()


def residual(
    section_text: str, regulator: str, furniture: frozenset[str] = frozenset()
) -> TemplateMatch:
    """Remove matching boilerplate from a review section and decide whether to flag it.

    Templates are removed best score first, one at a time, until no template reaches
    ``MATCH_THRESHOLD`` against what is left. Removing the best match first matters when two
    templates are near-identical variants (the OCC 2023 and 2024 "Pursuant" paragraphs): the
    weaker variant would otherwise align past the differing prefix and leave a stub behind.
    ``score`` is the best score against the full section, which is what the flag rule uses.
    Among templates within ``TIE_POINTS`` of the best score the longest is removed first: a
    one-sentence template that is also the closing sentence of a longer paragraph must not be
    removed first and leave the rest of that paragraph below the match threshold
    (FRB_185859_20230905 p18, where the long St. Louis paragraph scores 99.7 because of an en
    dash and its closing sentence scores 100).

    A template is removed only when the span it aligns with asserts nothing
    (:func:`asserting_sentences`): partial_ratio 90 tolerates a dropped "not", so "Examiners
    identified evidence of discriminatory ... practices" would otherwise be removed as the
    "did not identify any evidence" paragraph. A rejected template is not tried again on the
    same section.
    """
    current = normalize(section_text, regulator, furniture)
    candidates = list(templates_for(regulator))
    best_id: str | None = None
    best_score = 0.0
    removed: list[str] = []
    while current and candidates:
        scored = [(fuzz.partial_ratio_alignment(t.normalized, current), t) for t in candidates]
        top = max(st[0].score if st[0] else 0.0 for st in scored)
        aligned, template = max(
            (st for st in scored if st[0] and st[0].score >= top - TIE_POINTS),
            key=lambda st: len(st[1].normalized),
            default=scored[0],
        )
        score = float(aligned.score) if aligned else 0.0
        if not removed and score > best_score:
            best_id, best_score = template.id, score
        if score < MATCH_THRESHOLD or aligned is None or aligned.dest_end <= aligned.dest_start:
            break
        if asserting_sentences(current[aligned.dest_start : aligned.dest_end]):
            candidates.remove(template)
            continue
        current = (current[: aligned.dest_start] + " " + current[aligned.dest_end :]).strip()
        current = re.sub(r"\s+", " ", current)
        removed.append(template.id)
    asserting = asserting_sentences(current)
    flagged = len(current) > FLAG_RESIDUAL_CHARS or best_score < FLAG_SCORE or bool(asserting)
    return TemplateMatch(
        template_id=best_id,
        score=round(best_score, 1),
        residual_text=current,
        flagged=flagged,
        removed=removed,
        asserting=asserting,
    )


# --------------------------------------------------------------------------- admission rule

# Finding predicates. A strong predicate asserts a finding unless a negation precedes it in
# the same sentence ("did not identify ... was identified" is negated by "no evidence"); a weak
# predicate (a law name, "substantive") is also neutral inside a sentence that only says what
# the review covered ("compliance with ... the Fair Housing Act was reviewed").
STRONG_PREDICATES = re.compile(
    r"\b(lowered|lowering|violations?|violated|identified|downgraded?|downgrade|adversely|"
    r"consent orders?)\b"
)
WEAK_PREDICATES = re.compile(
    r"\b(substantive|equal credit opportunity act|fair housing act|regulation b|"
    r"federal trade commission act|home mortgage disclosure act|udaa?p|ecoa|fha)\b"
)
NEGATION = re.compile(r"\b(not|no|nor|never|without|any information)\b")
# "examiners identified no evidence", "found no violations": the negation right after the verb
NEGATED_OBJECT = re.compile(r"^ (no|none)\b")
SCOPE_PHRASE = re.compile(
    r"\b((was|were) (reviewed|considered|evaluated)|reviewed|evaluated|considers?|consults?|"
    r"in compliance with|analysis performed under|determine compliance with|"
    r"compliance with (the )?(laws|anti-?discrimination laws)|including)\b"
)


CLAUSE_BREAK = re.compile(r"\b(?:but|however|although|though|while|whereas)\b")


def _clause_asserts(clause: str) -> bool:
    for strong in STRONG_PREDICATES.finditer(clause):
        negation = NEGATION.search(clause, 0, strong.start())
        if negation is None and not NEGATED_OBJECT.search(clause[strong.end() :]):
            return True
    return bool(
        WEAK_PREDICATES.search(clause)
        and not STRONG_PREDICATES.search(clause)
        and not NEGATION.search(clause)
        and not SCOPE_PHRASE.search(clause)
    )


def asserting_sentences(text: str) -> list[str]:
    """Sentences of ``text`` that assert a finding: the admission test for a template.

    Sentences are split on ".", ";" and ":", and each sentence into clauses at "but",
    "however", "although", "while" and "whereas". A clause asserts when one of its strong
    predicates ("lowered", "violation", "identified", "downgrade", "adversely", "consent
    order") has no negation before it in the clause and no "no" right after it ("identified no
    evidence"), or when it has a weak predicate (a law name, "substantive") and neither a
    negation nor a scope phrase ("was reviewed", "including", "in compliance with"). Every
    template must return an empty list: "examiners identified a violation of the Fair Housing
    Act" can never be boilerplate, while "examiners did not identify any discriminatory or
    other illegal credit practices" and "compliance with the Fair Housing Act was reviewed"
    can.
    """
    norm = re.sub(r"\s+", " ", text.lower())
    out = []
    for sentence in (s.strip() for s in re.split(r"(?<=[.;:])\s+", norm)):
        if sentence and any(_clause_asserts(c) for c in CLAUSE_BREAK.split(sentence)):
            out.append(sentence)
    return out


# --------------------------------------------------------------------------- census


def census_row(
    doc_id: str,
    regulator: str,
    section_text: str | None,
    furniture: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """One census row; a missing review section is a locator miss and is flagged."""
    if section_text is None:
        return {
            "doc_id": doc_id,
            "regulator": regulator,
            "located": False,
            "template_id": None,
            "score": None,
            "residual_chars": None,
            "asserts_finding": None,
            "flagged": True,
            "residual_text": "",
        }
    match = residual(section_text, regulator, furniture)
    return {
        "doc_id": doc_id,
        "regulator": regulator,
        "located": True,
        "template_id": match.template_id,
        "score": match.score,
        "residual_chars": len(match.residual_text),
        "asserts_finding": bool(match.asserting),
        "flagged": match.flagged,
        "residual_text": match.residual_text[:500],
    }


def census(docs: list[tuple]) -> pd.DataFrame:
    """Census table over (doc_id, regulator, review section text or None[, furniture]) tuples."""
    return pd.DataFrame([census_row(*doc) for doc in docs])


def review_section(
    doc_id: str, regulator: str, pages
) -> tuple[str | None, frozenset[str]]:
    """Located review-section text (None on a locator miss) and the running lines of the document."""
    section = locate_section("illegal_credit_practices_review", doc_id, pages, regulator)
    return (section.text if section else None), running_lines(pages)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m cra_extract.templates --manifest config/corpus.csv --out results/census.csv``."""
    ap = argparse.ArgumentParser(description="Template-residual census of review sections.")
    ap.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    ap.add_argument(
        "--text-dir", type=Path, default=TEXT_DIR, help="folder with {doc_id}.pages.json"
    )
    ap.add_argument(
        "--samples-dir", type=Path, help="run on pdftotext .txt files instead of the manifest"
    )
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "census.csv")
    args = ap.parse_args(argv)

    docs: list[tuple] = []
    if args.samples_dir:
        for txt in sorted(args.samples_dir.glob("*.txt")):
            regulator = txt.stem.split("_")[0]
            pages = pages_from_text(txt.read_text(encoding="utf-8"))
            docs.append((txt.stem, regulator, *review_section(txt.stem, regulator, pages)))
    elif args.manifest.exists():
        with open(args.manifest, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                path = args.text_dir / f"{row['doc_id']}.pages.json"
                if path.exists():
                    pages = load_pages_json(path)
                    docs.append(
                        (
                            row["doc_id"],
                            row["regulator"],
                            *review_section(row["doc_id"], row["regulator"], pages),
                        )
                    )
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
    print(
        table[["doc_id", "located", "template_id", "score", "residual_chars", "flagged"]].to_string(
            index=False
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
