"""Keyword baseline (system S0, design section 4) for the illegal-credit-practices finding.

This is what an analyst would run before building anything: sentence-level regular
expressions over the located review section, with negation handling. A sentence is a hit
when it contains one of the triggers

- "lowered",
- "violation(s) of",
- "identified ... discriminatory / illegal credit practice(s)" (either order, the reverse
  form requiring "was/were identified"),
- "substantive violation(s)",

and the hit is negated when a negator ("has not", "did not", "no evidence", "not identified",
"was not", "were not", ...) occurs earlier in the same sentence. ``finding_present`` is true
when at least one hit is not negated; ``rating_lowered`` is true when a non-negated hit has
the "lowered" trigger. The quote is the triggering sentence.

False positives on boilerplate are the point of reporting this system separately. The phrase
"discriminatory or other illegal credit practices" appears three times in every OCC review
section (OCC_703395_GNBank_2023_NI.txt lines 186 to 204): once in "the OCC considers evidence
of ..." (no trigger), once in "The OCC has not identified that this bank ... has engaged in
..." (a hit, negated by "has not"), and once in the forward-looking "The OCC will consider any
information ... identified by or provided to the OCC" (no trigger because the reverse form
needs "was identified"). The FRB "No evidence of ... was identified" paragraph is a negated
hit. :func:`scan` returns every hit with its negation flag so the census can count how often
the baseline is saved by the negation rule, per regulator.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from cra_extract.config import CORPUS_PATH, RESULTS_DIR, TEXT_DIR
from cra_extract.locate import load_pages_json, locate_section, pages_from_text
from cra_extract.models import FieldValue, Page, Section
from cra_extract.rules import Doc, Sentence, section_sentences, split_sentences

SYSTEM = "baseline"
BASELINE_VERSION = "0.1.0"

PRACTICE = r"(?:discriminatory|illegal\s+credit\s+practices?)"
TRIGGERS: list[tuple[str, re.Pattern[str]]] = [
    ("lowered", re.compile(r"\blowered\b", re.IGNORECASE)),
    ("violation_of", re.compile(r"\bviolations?\s+of\b", re.IGNORECASE)),
    ("identified_practice", re.compile(r"\bidentified\b.{0,200}?\b" + PRACTICE, re.IGNORECASE | re.DOTALL)),
    (
        "practice_identified",
        re.compile(PRACTICE + r"\b.{0,200}?\b(?:was|were|has\s+been|have\s+been)\s+identified\b", re.IGNORECASE | re.DOTALL),
    ),
    ("substantive_violation", re.compile(r"\bsubstantive\s+violations?\b", re.IGNORECASE)),
]
NEGATOR_RE = re.compile(
    r"\b(?:has\s+not|have\s+not|had\s+not|did\s+not|does\s+not|do\s+not|no\s+evidence|not\s+identified|was\s+not|were\s+not)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Hit:
    """One trigger match in one sentence of the review section."""

    sentence: Sentence
    trigger: str
    negated: bool
    negator: str | None


def _hits(sentences: list[Sentence]) -> list[Hit]:
    hits: list[Hit] = []
    for s in sentences:
        n = s.norm
        for name, pat in TRIGGERS:
            m = pat.search(n)
            if not m:
                continue
            neg = NEGATOR_RE.search(n[: m.start()])
            hits.append(Hit(s, name, neg is not None, neg.group(0) if neg else None))
    return hits


def scan(text: str) -> list[Hit]:
    """Hits in a plain review-section text (offsets relative to the text)."""
    return _hits(split_sentences(text))


def _fv(doc_id: str, field: str, value: Any, hit: Hit | None, section: Section | None, notes: str) -> FieldValue:
    return FieldValue(
        doc_id=doc_id,
        field=field,
        system=SYSTEM,
        status="value",
        value=value,
        quote=hit.sentence.quote if hit else None,
        page=hit.sentence.page if hit else (section.page_start if section else None),
        scope=section.scope if section else None,
        char_start=hit.sentence.start if hit else None,
        char_end=hit.sentence.end if hit else None,
        notes=notes,
    )


def detect(doc_id: str, review: Section | None, pages: list[Page], regulator: str = "") -> list[FieldValue]:
    """``finding_present`` and ``rating_lowered`` for one document from its review section.

    Both abstain when the review section was not located (a locator miss, not a negative).
    """
    if review is None:
        note = "review section not located"
        return [
            FieldValue(doc_id=doc_id, field=f, system=SYSTEM, status="abstain", notes=note)
            for f in ("finding_present", "rating_lowered")
        ]
    doc = Doc(doc_id, regulator, pages, {"illegal_credit_practices_review": review})
    hits = _hits(section_sentences(review, doc))
    positive = [h for h in hits if not h.negated]
    lowered = [h for h in positive if h.trigger == "lowered"]
    n_neg = sum(h.negated for h in hits)
    note = f"hits={len(hits)} negated={n_neg} triggers=" + ",".join(h.trigger + ("!" if h.negated else "") for h in hits)
    return [
        _fv(doc_id, "finding_present", bool(positive), positive[0] if positive else None, review, note),
        _fv(doc_id, "rating_lowered", bool(lowered), lowered[0] if lowered else None, review, note),
    ]


# --------------------------------------------------------------------------- census report


def census_row(doc_id: str, regulator: str, pages: list[Page]) -> dict[str, Any]:
    """One row per document: baseline verdicts plus hit and negated-hit counts."""
    review = locate_section("illegal_credit_practices_review", doc_id, pages, regulator)
    values = {fv.field: fv for fv in detect(doc_id, review, pages, regulator)}
    present, lowered = values["finding_present"], values["rating_lowered"]
    hits = _hits(section_sentences(review, Doc(doc_id, regulator, pages, {}))) if review else []
    return {
        "doc_id": doc_id,
        "regulator": regulator,
        "located": review is not None,
        "finding_present": present.value if present.status == "value" else None,
        "rating_lowered": lowered.value if lowered.status == "value" else None,
        "n_hits": len(hits),
        "n_negated_hits": sum(h.negated for h in hits),
        "triggers": ";".join(h.trigger + ("!" if h.negated else "") for h in hits),
        "quote": (present.quote or "")[:300],
    }


def census(docs: list[tuple[str, str, list[Page]]]) -> pd.DataFrame:
    """Baseline census over (doc_id, regulator, pages) triples."""
    return pd.DataFrame([census_row(d, r, p) for d, r, p in docs])


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m cra_extract.baseline --samples-dir tests/fixtures/samples --out results/baseline.csv``."""
    ap = argparse.ArgumentParser(description="Keyword baseline (S0) for the illegal-credit-practices finding.")
    ap.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    ap.add_argument("--text-dir", type=Path, default=TEXT_DIR, help="folder with {doc_id}.pages.json")
    ap.add_argument("--samples-dir", type=Path, help="run on pdftotext .txt files instead of the manifest")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "baseline.csv")
    args = ap.parse_args(argv)

    docs: list[tuple[str, str, list[Page]]] = []
    if args.samples_dir:
        for txt in sorted(args.samples_dir.glob("*.txt")):
            docs.append((txt.stem, txt.stem.split("_")[0], pages_from_text(txt.read_text(encoding="utf-8"))))
    elif args.manifest.exists():
        with open(args.manifest, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                path = args.text_dir / f"{row['doc_id']}.pages.json"
                if path.exists():
                    docs.append((row["doc_id"], row["regulator"], load_pages_json(path)))
    else:
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    if not docs:
        print("no documents with page text found", file=sys.stderr)
        return 1
    table = census(docs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False)
    n_pos = int(table["finding_present"].fillna(False).astype(bool).sum())
    print(f"baseline {BASELINE_VERSION}: {len(table)} documents, {n_pos} positives; written to {args.out}")
    by_reg = table.groupby("regulator")[["n_hits", "n_negated_hits"]].sum()
    print("hits and negated hits by regulator (negated hits are the boilerplate the negation rule absorbed):")
    print(by_reg.to_string())
    print(table[["doc_id", "located", "finding_present", "rating_lowered", "n_hits", "n_negated_hits", "triggers"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
