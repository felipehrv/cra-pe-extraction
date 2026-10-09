"""Split assignment: draw the dev, test and audit documents and record how.

The gold set and the audit are samples from the manifest (design sections 5.1 and
5.3). This module draws them, writes the ``split`` column of ``config/corpus.csv`` in
place, and records every draw in ``config/strata.yaml`` so the sample can be
reproduced and its design weights computed: the seed, the strata and their sizes,
the selection probability of every drawn document, the drawn doc_ids, the sorted
doc_id list hash, and the date the draw was made (passed on the command line, never
computed, so a rerun reproduces the file byte for byte).

Draws

- ``dev`` (10 documents): the eight fixture sample PEs under ``tests/fixtures/samples``
  are fixed members. The remaining slots are filled, in this order, by one document
  for every (regulator, method) cell the fixed members leave uncovered, by one FRB
  document from a Reserve District other than New York and Kansas City, and by
  documents drawn at random from the eligible pool. The district is not in the
  manifest; it is approximated by the bank's state in ``data/index/frb.csv`` (a
  state outside ``NY_KC_STATES``), and the approximation is recorded.
- ``test`` (30 documents): three strata drawn in sequence from the pool that remains
  after dev. First, every (regulator, method) cell with at least ``min_cell``
  documents in the manifest gets at least ``per_cell`` documents, and the slots left
  after the two fixed strata go one each to the largest cells. Second, ``n_rating``
  documents rated NI or SN. Third, ``n_multi_aa`` multi-assessment-area documents:
  with ``--aa-counts`` (doc_id, n_aa_sections, written by the run stage) a document
  qualifies with at least ``aa_min`` sections; without it, the stated proxy is a
  total_assets_usd in the top decile of the document's cell. The rule used is
  recorded.
- ``audit`` (30 documents): ten per document-risk tertile from the corpus split, with
  the risk read from ``--risk`` (doc_id, doc_risk). Tertiles are rank thirds of the
  pool with ties broken at random, so the three strata are equal in size.

Tuning documents (``config/tuning_documents.yaml``): every corpus document cited as evidence
in a tuning configuration (``config/headings.yaml``, ``config/review_templates.yaml``) or
recorded there as read while tuning. The plan (Leakage) allows tuning only on dev, the
locator check and corpus-wide index agreement, never on test or audit documents, so the test
and audit pools drop the banks of these documents; the draw records their count and hash.
``tuning-docs`` lists the cited documents that the file does not hold yet and ``--write``
adds them. The CLI refuses ``draw-test`` and ``draw-audit`` while a non-evaluable row (no
sha256, or ``text_extractable`` not true) is outside split ``excluded`` (plan, Exclusions).

Every draw uses one ``numpy.random.default_rng(seed)`` and draws one document at a
time; after a document is drawn every other document of the same bank leaves the
pool, so a bank never sits in two splits (design 6.8) and never twice in one. The
pool of a draw always excludes the documents and the banks of the other gold splits.

CLI::

    python -m cra_extract.strata draw-dev --seed 20261007 --date 2026-10-07
    python -m cra_extract.strata draw-test --seed 20261013 --date 2026-10-13 [--aa-counts PATH]
    python -m cra_extract.strata draw-audit --risk PATH --seed 20261016 --date 2026-10-16
    python -m cra_extract.strata show
    python -m cra_extract.strata tuning-docs [--write]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
from collections import Counter
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from cra_extract.config import (
    CORPUS_PATH,
    HEADINGS_PATH,
    INDEX_DIR,
    STRATA_PATH,
    TEMPLATES_PATH,
    TUNING_DOCS_PATH,
)
from cra_extract.label import split_hash
from cra_extract.models import DocRecord, make_doc_id
from cra_extract.overrides import non_evaluable_unexcluded

Cell = tuple[str, str]

GOLD_SPLITS = ("dev", "test", "audit")

# The eight sample PEs under tests/fixtures/samples: stem -> (regulator, bank_id, exam date).
# Kept in sync with tests/conftest.py SAMPLE_META by tests/test_strata.py.
FIXTURE_SAMPLES: dict[str, tuple[str, str, date]] = {
    "OCC_703395_GNBank_2023_NI": ("OCC", "703395", date(2023, 3, 27)),
    "OCC_11715_LemontNB_2024_SN": ("OCC", "11715", date(2024, 4, 8)),
    "OCC_13682_FirstNeighborBank_2024_ISB_O": ("OCC", "13682", date(2024, 8, 19)),
    "FDIC_12299_SouthStar_2022_ISB_NI_fairlending": ("FDIC", "12299", date(2022, 9, 12)),
    "FDIC_26816_LibertyBank_2022_SM_SN": ("FDIC", "26816", date(2022, 8, 9)),
    "FDIC_6078_OneAmericanBank_2024_ISB_NI": ("FDIC", "6078", date(2024, 8, 5)),
    "FRB_3664588_FieldPoint_2024_ISB_NI": ("FRB", "3664588", date(2024, 1, 16)),
    "FRB_663656_SmallBusinessBank_2024_SM_NI": ("FRB", "663656", date(2024, 11, 18)),
}
SAMPLE_DOC_IDS: list[str] = sorted(
    make_doc_id(reg, bank, exam) for reg, bank, exam in FIXTURE_SAMPLES.values()
)

# States served by the New York (2nd) and Kansas City (10th) Reserve Districts; an FRB
# document from any other state stands in for "another district".
NY_KC_STATES = frozenset({"NY", "NJ", "CT", "KS", "MO", "NE", "OK", "CO", "WY", "NM"})
FRB_INDEX_PATH = INDEX_DIR / "frb.csv"

LOW_RATINGS = ("NI", "SN")
DEFAULTS = {
    "n_dev": 10,
    "n_test": 30,
    "per_cell": 3,
    "min_cell": 50,
    "n_rating": 5,
    "n_multi_aa": 4,
    "aa_min": 3,
    "n_per_tertile": 10,
    "assets_quantile": 0.9,
}


# Splits whose pool drops the banks of tuning documents (plan, Leakage).
TUNING_FREE_SPLITS = ("test", "audit")
# Configuration files whose cited doc_ids are tuning documents.
TUNING_SOURCES: tuple[Path, ...] = (HEADINGS_PATH, TEMPLATES_PATH)
DOC_ID_PATTERN = re.compile(r"\b(?:OCC|FDIC|FRB)_[0-9A-Za-z]+_\d{8}\b")


class DrawError(ValueError):
    """A draw cannot satisfy its rules on this manifest."""


# ----------------------------------------------------------------------------- records


@dataclass
class DrawnDoc:
    """One drawn document and why it was drawn."""

    doc_id: str
    bank_id: str
    cell: str
    stratum: str
    reason: str
    pool_size: int
    selection_probability: float


@dataclass
class Stratum:
    """One stratum of a draw with its allocation and selection probability."""

    name: str
    population: int
    pool: int
    drawn: int
    selection_probability: float
    weight: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.selection_probability = round(self.selection_probability, 6)
        if self.weight is not None:
            self.weight = round(self.weight, 6)


@dataclass
class Draw:
    """The reproducible record of one split, as written to ``config/strata.yaml``."""

    split: str
    seed: int
    drawn_at: str
    n: int
    rules: dict[str, Any]
    strata: list[Stratum]
    docs: list[DrawnDoc]
    doc_ids: list[str]
    doc_id_hash: str
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ----------------------------------------------------------------------------- manifest i/o


def read_rows(path: Path = CORPUS_PATH) -> tuple[list[str], list[dict[str, str]]]:
    """Read the manifest as text rows, keeping the column order and every cell verbatim."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def write_rows(columns: list[str], rows: list[dict[str, str]], path: Path = CORPUS_PATH) -> None:
    """Write manifest rows atomically with the layout ``index`` and ``fetch`` produce."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def to_records(rows: list[dict[str, str]]) -> list[DocRecord]:
    """Manifest rows as ``DocRecord`` (empty cells become None, unknown columns ignored)."""
    out = []
    for r in rows:
        cleaned = {k: (None if v == "" else v) for k, v in r.items() if k in DocRecord.model_fields}
        out.append(DocRecord.model_validate(cleaned))
    return out


def cell_of(doc: DocRecord) -> Cell:
    return (doc.regulator, doc.method)


def cell_name(cell: Cell) -> str:
    return f"{cell[0]}/{cell[1]}"


def bank_key(doc: DocRecord) -> tuple[str, str]:
    """(regulator, bank_id without leading zeros): the unit of split exclusivity."""
    return (doc.regulator, doc.bank_id.lstrip("0"))


def frb_states(path: Path = FRB_INDEX_PATH) -> dict[tuple[str, str], str]:
    """(bank_id, exam_date) -> state from the FRB index written by the index stage."""
    if not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {
            (r["bank_id"].lstrip("0"), r["exam_date"]): r["state"].strip().upper()
            for r in csv.DictReader(fh)
            if r.get("state")
        }


def state_of(doc: DocRecord, states: dict[tuple[str, str], str]) -> str | None:
    if doc.exam_date is None:
        return None
    return states.get((doc.bank_id.lstrip("0"), doc.exam_date.isoformat()))


def read_two_column_csv(path: Path, value_col: str, cast: type) -> dict[str, Any]:
    """doc_id -> value for a CSV with ``doc_id`` and ``value_col`` columns."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or {"doc_id", value_col} - set(reader.fieldnames):
            raise DrawError(f"{path} needs columns doc_id and {value_col}")
        out: dict[str, Any] = {}
        for r in reader:
            if r[value_col] not in ("", None):
                out[r["doc_id"]] = cast(r[value_col])
        return out


# ----------------------------------------------------------------------------- exclusivity


def exclusivity_problems(manifest: list[DocRecord]) -> list[str]:
    """Banks present in two gold splits, as messages. Empty when the splits are clean."""
    banks: dict[tuple[str, str], set[str]] = {}
    for d in manifest:
        if d.split in GOLD_SPLITS:
            banks.setdefault(bank_key(d), set()).add(d.split)
    return [
        f"{reg} {bid} is in {' and '.join(sorted(splits))}"
        for (reg, bid), splits in sorted(banks.items())
        if len(splits) > 1
    ]


def doc_id_bank_key(doc_id: str) -> tuple[str, str]:
    """Bank key ``(regulator, bank_id without leading zeros)`` read from a doc_id."""
    regulator, bank_id, _date = doc_id.split("_")
    return regulator, bank_id.lstrip("0")


def eligible_pool(
    manifest: list[DocRecord], split: str, tuning: Collection[str] = ()
) -> list[DocRecord]:
    """Documents a draw for ``split`` may use, sorted by doc_id so draws are deterministic.

    The pool excludes ``excluded`` rows and every document of a bank that sits in a split
    drawn earlier in the order dev, test, audit. Splits drawn later do not shrink the
    pool, so redrawing a split with its seed reproduces it; a clash with a later split
    is caught by ``commit_draw`` and means the later split must be redrawn. For the test
    and audit splits the banks of the ``tuning`` doc_ids leave the pool too (plan, Leakage).
    """
    earlier = GOLD_SPLITS[: GOLD_SPLITS.index(split)]
    taken = {bank_key(d) for d in manifest if d.split in earlier}
    if split in TUNING_FREE_SPLITS:
        taken |= {doc_id_bank_key(d) for d in tuning}
    return sorted(
        (d for d in manifest if d.split != "excluded" and bank_key(d) not in taken),
        key=lambda d: d.doc_id,
    )


def assign(rows: list[dict[str, str]], split: str, doc_ids: list[str]) -> None:
    """Set ``split`` on ``doc_ids`` and return every other row of that split to corpus."""
    wanted = set(doc_ids)
    seen = set()
    for r in rows:
        if r["doc_id"] in wanted:
            r["split"] = split
            seen.add(r["doc_id"])
        elif r["split"] == split:
            r["split"] = "corpus"
    missing = wanted - seen
    if missing:
        raise DrawError(f"doc_ids not in manifest: {sorted(missing)}")


# ----------------------------------------------------------------------------- sampler


class Sampler:
    """Sequential draws without replacement at bank level from a document pool."""

    def __init__(self, pool: list[DocRecord], seed: int) -> None:
        self.rng = np.random.default_rng(seed)
        self.pool: list[DocRecord] = sorted(pool, key=lambda d: d.doc_id)
        self.drawn: list[DrawnDoc] = []
        self.banks_removed = 0

    def remove_bank(self, key: tuple[str, str]) -> int:
        before = len(self.pool)
        self.pool = [d for d in self.pool if bank_key(d) != key]
        removed = before - len(self.pool)
        self.banks_removed += removed
        return removed

    def take(self, doc: DocRecord, stratum: str, reason: str, pool_size: int, prob: float) -> None:
        self.drawn.append(
            DrawnDoc(
                doc_id=doc.doc_id,
                bank_id=doc.bank_id,
                cell=cell_name(cell_of(doc)),
                stratum=stratum,
                reason=reason,
                pool_size=pool_size,
                selection_probability=round(prob, 6),
            )
        )
        self.remove_bank(bank_key(doc))

    def draw(self, candidates: list[DocRecord], n: int, stratum: str, reason: str) -> list[DocRecord]:
        """Draw ``n`` documents one at a time from ``candidates`` (a subset of the pool).

        The selection probability recorded for every document of the stratum is
        ``n / len(candidates)`` at the start of the stratum; documents of a bank drawn
        earlier in the same stratum leave the candidates, which is recorded in
        ``banks_removed`` rather than in the probability.
        """
        if n <= 0:
            return []
        start = len(candidates)
        if start < n:
            raise DrawError(f"{stratum}: {n} wanted, {start} eligible")
        prob = n / start
        out: list[DocRecord] = []
        remaining = sorted(candidates, key=lambda d: d.doc_id)
        for _ in range(n):
            if not remaining:
                raise DrawError(f"{stratum}: pool exhausted by bank exclusivity")
            doc = remaining[int(self.rng.integers(len(remaining)))]
            self.take(doc, stratum, reason, start, prob)
            key = bank_key(doc)
            remaining = [d for d in remaining if bank_key(d) != key]
            out.append(doc)
        return out


def _finish(
    split: str,
    seed: int,
    drawn_at: str,
    rules: dict[str, Any],
    strata: list[Stratum],
    sampler: Sampler,
    manifest: list[DocRecord],
    notes: list[str],
) -> Draw:
    doc_ids = sorted(d.doc_id for d in sampler.drawn)
    if len(set(doc_ids)) != len(doc_ids):
        raise DrawError(f"{split}: a document was drawn twice")
    # The hash is over the manifest as it will be after assignment: the drawn set.
    shadow = [d.model_copy(update={"split": split if d.doc_id in doc_ids else "corpus"}) for d in manifest]
    return Draw(
        split=split,
        seed=seed,
        drawn_at=drawn_at,
        n=len(doc_ids),
        rules=rules,
        strata=strata,
        docs=sorted(sampler.drawn, key=lambda d: d.doc_id),
        doc_ids=doc_ids,
        doc_id_hash=split_hash(shadow, split),
        notes=notes + [f"documents removed from the pool by bank exclusivity during the draw: {sampler.banks_removed}"],
    )


# ----------------------------------------------------------------------------- dev


def draw_dev(
    manifest: list[DocRecord],
    seed: int,
    drawn_at: str,
    states: dict[tuple[str, str], str],
    fixed_doc_ids: list[str] | None = None,
    n_dev: int = DEFAULTS["n_dev"],
) -> Draw:
    """Draw the dev split: the fixed sample documents plus random fills for coverage.

    Order of fills after the fixed members: one document per uncovered (regulator,
    method) cell, then an FRB document whose state lies outside the New York and
    Kansas City districts when none is present, then random draws up to ``n_dev``.
    """
    fixed = sorted(SAMPLE_DOC_IDS if fixed_doc_ids is None else fixed_doc_ids)
    by_id = {d.doc_id: d for d in manifest}
    missing = [i for i in fixed if i not in by_id]
    if missing:
        raise DrawError(f"fixed dev documents not in manifest: {missing}")
    if len(fixed) > n_dev:
        raise DrawError(f"{len(fixed)} fixed documents exceed n_dev={n_dev}")

    pool = eligible_pool(manifest, "dev")
    pool_ids = {d.doc_id for d in pool}
    blocked = [i for i in fixed if i not in pool_ids]
    if blocked:
        raise DrawError(f"fixed dev documents belong to a bank in another split: {blocked}")
    sampler = Sampler(pool, seed)
    cells = sorted({cell_of(d) for d in manifest})
    strata: list[Stratum] = []

    for doc_id in fixed:
        sampler.take(by_id[doc_id], "fixed", "fixture_sample", len(fixed), 1.0)
    strata.append(Stratum("fixed", len(fixed), len(fixed), len(fixed), 1.0))

    def covered(cell: Cell) -> bool:
        return any(cell_name(cell) == d.cell for d in sampler.drawn)

    for cell in cells:
        if covered(cell):
            continue
        cands = [d for d in sampler.pool if cell_of(d) == cell]
        sampler.draw(cands, 1, f"cell_coverage:{cell_name(cell)}", "uncovered_cell")
        strata.append(Stratum(f"cell_coverage:{cell_name(cell)}", len(cands), len(cands), 1, 1 / len(cands)))

    def other_district(doc: DocRecord) -> bool:
        st = state_of(doc, states)
        return doc.regulator == "FRB" and st is not None and st not in NY_KC_STATES

    if not any(other_district(by_id[d.doc_id]) for d in sampler.drawn):
        cands = [d for d in sampler.pool if other_district(d)]
        drawn = sampler.draw(cands, 1, "frb_other_district", "frb_state_outside_ny_kc")
        strata.append(
            Stratum(
                "frb_other_district",
                sum(1 for d in manifest if other_district(d)),
                len(cands),
                1,
                1 / len(cands),
                detail={"state": state_of(drawn[0], states)},
            )
        )

    n_fill = n_dev - len(sampler.drawn)
    if n_fill < 0:
        raise DrawError(f"coverage draws exceed n_dev={n_dev}")
    if n_fill:
        cands = list(sampler.pool)
        sampler.draw(cands, n_fill, "random_fill", "random_fill")
        strata.append(Stratum("random_fill", len(manifest), len(cands), n_fill, n_fill / len(cands)))

    rules = {
        "n_dev": n_dev,
        "fixed_doc_ids": fixed,
        "fill_order": ["uncovered_cell", "frb_other_district", "random_fill"],
        "frb_district_approximation": (
            "district is not in the manifest; an FRB document counts as outside the New York "
            "and Kansas City districts when its state in data/index/frb.csv is not one of "
            + ", ".join(sorted(NY_KC_STATES))
        ),
    }
    notes = ["dev is a development set, not an inference sample; probabilities are recorded for completeness"]
    return _finish("dev", seed, drawn_at, rules, strata, sampler, manifest, notes)


# ----------------------------------------------------------------------------- test


def cell_allocation(
    manifest: list[DocRecord], n_cells_total: int, per_cell: int, min_cell: int
) -> dict[Cell, int]:
    """Documents per eligible cell: ``per_cell`` each, the remainder one each to the
    largest cells (ties by cell name). Cells smaller than ``min_cell`` get none."""
    sizes = Counter(cell_of(d) for d in manifest)
    eligible = sorted((c for c, n in sizes.items() if n >= min_cell), key=lambda c: (-sizes[c], c))
    base = per_cell * len(eligible)
    if n_cells_total < base:
        raise DrawError(f"{n_cells_total} cell slots cannot give {per_cell} to each of {len(eligible)} cells")
    alloc = dict.fromkeys(eligible, per_cell)
    for i in range(n_cells_total - base):
        alloc[eligible[i % len(eligible)]] += 1
    return alloc


def assets_thresholds(manifest: list[DocRecord], quantile: float) -> dict[Cell, float]:
    """Per-cell total_assets_usd value at ``quantile`` over documents that report assets."""
    by_cell: dict[Cell, list[int]] = {}
    for d in manifest:
        if d.total_assets_usd is not None:
            by_cell.setdefault(cell_of(d), []).append(d.total_assets_usd)
    return {c: float(np.quantile(v, quantile)) for c, v in by_cell.items()}


def draw_test(
    manifest: list[DocRecord],
    seed: int,
    drawn_at: str,
    aa_counts: dict[str, int] | None = None,
    n_test: int = DEFAULTS["n_test"],
    per_cell: int = DEFAULTS["per_cell"],
    min_cell: int = DEFAULTS["min_cell"],
    n_rating: int = DEFAULTS["n_rating"],
    n_multi_aa: int = DEFAULTS["n_multi_aa"],
    aa_min: int = DEFAULTS["aa_min"],
    assets_quantile: float = DEFAULTS["assets_quantile"],
    tuning: Collection[str] = (),
) -> Draw:
    """Draw the test split by cells, then low ratings, then multi-assessment-area documents.

    ``tuning`` lists the tuning documents whose banks leave the pool (plan, Leakage)."""
    pool = eligible_pool(manifest, "test", tuning)
    sampler = Sampler(pool, seed)
    sizes = Counter(cell_of(d) for d in manifest)
    strata: list[Stratum] = []
    notes: list[str] = []

    alloc = cell_allocation(manifest, n_test - n_rating - n_multi_aa, per_cell, min_cell)
    for cell, n in sorted(alloc.items()):
        cands = [d for d in sampler.pool if cell_of(d) == cell]
        sampler.draw(cands, n, f"cell:{cell_name(cell)}", "regulator_x_method")
        strata.append(Stratum(f"cell:{cell_name(cell)}", sizes[cell], len(cands), n, n / len(cands)))

    cands = [d for d in sampler.pool if d.overall_rating in LOW_RATINGS]
    sampler.draw(cands, n_rating, "low_rating", "overall_rating_NI_or_SN")
    strata.append(
        Stratum(
            "low_rating",
            sum(1 for d in manifest if d.overall_rating in LOW_RATINGS),
            len(cands),
            n_rating,
            n_rating / len(cands),
        )
    )

    if aa_counts is not None:
        rule = "aa_counts"
        is_multi = {d.doc_id for d in manifest if aa_counts.get(d.doc_id, 0) >= aa_min}
        detail: dict[str, Any] = {"aa_min": aa_min, "documents_with_counts": sum(1 for d in manifest if d.doc_id in aa_counts)}
        notes.append(f"multi-AA stratum from parsed section counts (n_aa_sections >= {aa_min})")
    else:
        rule = "assets_top_decile_proxy"
        thresholds = assets_thresholds(manifest, assets_quantile)
        is_multi = {
            d.doc_id
            for d in manifest
            if d.total_assets_usd is not None and d.total_assets_usd >= thresholds[cell_of(d)]
        }
        detail = {
            "assets_quantile": assets_quantile,
            "thresholds_usd": {cell_name(c): int(t) for c, t in sorted(thresholds.items())},
            "cells_without_assets": [cell_name(c) for c in sorted(sizes) if c not in thresholds],
        }
        notes.append(
            "multi-AA stratum drawn by proxy: no --aa-counts file, so documents whose "
            "total_assets_usd is in the top decile of their cell stand in for multi-AA documents; "
            "cells without assets in the index (OCC) are not eligible for this stratum"
        )
    cands = [d for d in sampler.pool if d.doc_id in is_multi]
    sampler.draw(cands, n_multi_aa, "multi_aa", rule)
    strata.append(Stratum("multi_aa", len(is_multi), len(cands), n_multi_aa, n_multi_aa / len(cands), detail={"rule": rule, **detail}))

    rules = {
        "n_test": n_test,
        "per_cell": per_cell,
        "min_cell": min_cell,
        "n_rating": n_rating,
        "n_multi_aa": n_multi_aa,
        "multi_aa_rule": rule,
        "cell_allocation": {cell_name(c): n for c, n in sorted(alloc.items())},
        "order": ["cell", "low_rating", "multi_aa"],
        **tuning_record(manifest, "test", tuning),
    }
    notes.append(
        "strata are drawn in sequence; a document eligible for a later stratum that was not "
        "drawn earlier keeps that later chance, so its inclusion probability exceeds the "
        "recorded stage probability; gold metrics are reported unweighted, corpus estimates "
        "come from the audit (design 5.1)"
    )
    return _finish("test", seed, drawn_at, rules, strata, sampler, manifest, notes)


# ----------------------------------------------------------------------------- audit


def risk_tertiles(
    pool: list[DocRecord], risk: dict[str, float], rng: np.random.Generator
) -> dict[str, list[DocRecord]]:
    """Rank thirds of the pool by document risk, ties broken at random with ``rng``."""
    ordered = sorted(pool, key=lambda d: d.doc_id)
    keys = rng.random(len(ordered))
    order = sorted(range(len(ordered)), key=lambda i: (risk[ordered[i].doc_id], keys[i]))
    parts = np.array_split(np.array(order), 3)
    return {name: [ordered[i] for i in part] for name, part in zip(("low", "mid", "high"), parts, strict=True)}


def draw_audit(
    manifest: list[DocRecord],
    risk: dict[str, float],
    seed: int,
    drawn_at: str,
    n_per_tertile: int = DEFAULTS["n_per_tertile"],
    tuning: Collection[str] = (),
) -> Draw:
    """Draw the audit split: ``n_per_tertile`` documents per document-risk tertile.

    ``tuning`` lists the tuning documents whose banks leave the pool (plan, Leakage)."""
    pool = eligible_pool(manifest, "audit", tuning)
    with_risk = [d for d in pool if d.doc_id in risk]
    n_missing = len(pool) - len(with_risk)
    if not with_risk:
        raise DrawError("no eligible document has a doc_risk value")
    sampler = Sampler(with_risk, seed)
    tertiles = risk_tertiles(with_risk, risk, sampler.rng)
    strata: list[Stratum] = []
    for name in ("low", "mid", "high"):
        members = tertiles[name]
        values = [risk[d.doc_id] for d in members]
        member_ids = {d.doc_id for d in members}
        cands = [d for d in sampler.pool if d.doc_id in member_ids]
        sampler.draw(cands, n_per_tertile, f"tertile:{name}", "doc_risk_tertile")
        strata.append(
            Stratum(
                f"tertile:{name}",
                len(members),
                len(cands),
                n_per_tertile,
                n_per_tertile / len(cands),
                weight=len(members) / n_per_tertile,
                detail={"doc_risk_min": min(values), "doc_risk_max": max(values)},
            )
        )
    rules = {
        "n_per_tertile": n_per_tertile,
        "tertiles": "rank thirds of the eligible corpus documents by doc_risk, ties broken at random",
        "weight": "N_h / n_h at document level (design 5.3)",
        **tuning_record(manifest, "audit", tuning),
    }
    notes = [f"eligible corpus documents without a doc_risk value, excluded from the pool: {n_missing}"]
    return _finish("audit", seed, drawn_at, rules, strata, sampler, manifest, notes)


# ----------------------------------------------------------------------------- tuning documents


def tuning_record(manifest: list[DocRecord], split: str, tuning: Collection[str]) -> dict[str, Any]:
    """Draw-record entries for the tuning documents (empty when there are none)."""
    if not tuning:
        return {}
    ids = sorted(set(tuning))
    keys = {doc_id_bank_key(d) for d in ids}
    earlier = GOLD_SPLITS[: GOLD_SPLITS.index(split)]
    taken = {bank_key(d) for d in manifest if d.split in earlier}
    removed = sum(1 for d in manifest if d.split != "excluded" and bank_key(d) in keys and bank_key(d) not in taken)
    return {
        "tuning_documents": len(ids),
        "tuning_documents_hash": hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest(),
        "tuning_documents_removed_from_pool": removed,
    }


def cited_doc_ids(paths: Collection[Path] = TUNING_SOURCES) -> dict[str, list[str]]:
    """doc_id -> sorted file names of the tuning configuration files that cite it."""
    out: dict[str, set[str]] = {}
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        for doc_id in DOC_ID_PATTERN.findall(text):
            out.setdefault(doc_id, set()).add(Path(path).name)
    return {d: sorted(v) for d, v in sorted(out.items())}


def load_tuning_documents(path: Path = TUNING_DOCS_PATH) -> dict[str, dict[str, Any]]:
    """doc_id -> entry (``source``, optional ``note``) of ``config/tuning_documents.yaml``;
    empty when the file does not exist."""
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: dict[str, dict[str, Any]] = {}
    for entry in data.get("documents") or []:
        doc_id = entry["doc_id"]
        if not DOC_ID_PATTERN.fullmatch(doc_id):
            raise DrawError(f"{path}: malformed doc_id {doc_id!r}")
        if doc_id in out:
            raise DrawError(f"{path}: {doc_id} listed twice")
        out[doc_id] = entry
    return out


def write_tuning_documents(entries: dict[str, dict[str, Any]], path: Path = TUNING_DOCS_PATH) -> None:
    """Write ``config/tuning_documents.yaml`` sorted by doc_id."""
    docs = [{"doc_id": d, **{k: v for k, v in entries[d].items() if k != "doc_id"}} for d in sorted(entries)]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "# Corpus documents read while tuning rules, locator scope rules, templates or thresholds\n"
            "# (plan, Leakage). Their banks leave the test and audit pools (strata.eligible_pool).\n"
            "# Entries with source config files are cited there as evidence and are added by\n"
            "# `python -m cra_extract.strata tuning-docs --write`; entries with a note were read\n"
            "# while tuning without being cited. Fix this list before draw-test; it is recorded\n"
            "# in the test and audit draws by count and hash.\n"
        )
        yaml.safe_dump({"version": 1, "documents": docs}, fh, sort_keys=False, allow_unicode=True, width=100)


def missing_tuning_documents(
    recorded: Collection[str], cited: dict[str, list[str]] | None = None
) -> list[str]:
    """Cited doc_ids (:func:`cited_doc_ids`) that the tuning file does not list."""
    cited = cited_doc_ids() if cited is None else cited
    have = set(recorded)
    return [d for d in cited if d not in have]


def evaluability_problems(rows: list[dict[str, str]]) -> list[str]:
    """Non-evaluable rows outside split ``excluded`` (plan, Exclusions: none may remain
    before ``draw-test``); see :func:`cra_extract.overrides.non_evaluable_unexcluded`."""
    return non_evaluable_unexcluded(rows)


# ----------------------------------------------------------------------------- strata.yaml


def load_strata(path: Path = STRATA_PATH) -> dict[str, Any]:
    """Current ``config/strata.yaml`` content, or an empty record when it does not exist."""
    if not Path(path).exists():
        return {"version": 1, "manifest": "config/corpus.csv", "splits": {}}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("version", 1)
    data.setdefault("manifest", "config/corpus.csv")
    data.setdefault("splits", {})
    return data


def save_draw(draw: Draw, path: Path = STRATA_PATH) -> None:
    """Replace the record of ``draw.split`` in ``config/strata.yaml``."""
    data = load_strata(path)
    data["splits"][draw.split] = draw.to_dict()
    data["splits"] = {s: data["splits"][s] for s in GOLD_SPLITS if s in data["splits"]}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Written by python -m cra_extract.strata; do not edit by hand.\n")
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=100)


def commit_draw(
    draw: Draw, columns: list[str], rows: list[dict[str, str]], manifest_path: Path, strata_path: Path
) -> list[DocRecord]:
    """Assign the split in the manifest rows, verify exclusivity, write both files."""
    assign(rows, draw.split, draw.doc_ids)
    records = to_records(rows)
    problems = exclusivity_problems(records)
    if problems:
        raise DrawError(
            "bank in two splits after the draw (redraw the later split): " + "; ".join(problems)
        )
    if split_hash(records, draw.split) != draw.doc_id_hash:
        raise DrawError("doc_id hash of the assigned split differs from the draw record")
    write_rows(columns, rows, manifest_path)
    save_draw(draw, strata_path)
    return records


# ----------------------------------------------------------------------------- show


def summary(manifest: list[DocRecord], strata: dict[str, Any]) -> str:
    """Human-readable state of the splits: counts per split and cell, draws on record."""
    lines = []
    counts = Counter(d.split for d in manifest)
    lines.append("split counts: " + ", ".join(f"{s}={counts.get(s, 0)}" for s in ("dev", "test", "audit", "corpus", "excluded")))
    cells = sorted({cell_of(d) for d in manifest})
    for split in GOLD_SPLITS:
        per_cell = Counter(cell_of(d) for d in manifest if d.split == split)
        if per_cell:
            lines.append(f"{split} by cell: " + ", ".join(f"{cell_name(c)}={per_cell.get(c, 0)}" for c in cells))
    for split, rec in strata.get("splits", {}).items():
        lines.append(
            f"{split}: n={rec['n']} seed={rec['seed']} drawn_at={rec['drawn_at']} hash={rec['doc_id_hash'][:16]}"
        )
        for s in rec["strata"]:
            w = f" weight={s['weight']:.3f}" if s.get("weight") else ""
            lines.append(f"  {s['name']}: N={s['population']} pool={s['pool']} n={s['drawn']} p={s['selection_probability']:.4f}{w}")
    problems = exclusivity_problems(manifest)
    lines.append("bank exclusivity: " + ("ok" if not problems else "; ".join(problems)))
    return "\n".join(lines)


# ----------------------------------------------------------------------------- CLI


def _iso_date(s: str) -> str:
    return date.fromisoformat(s).isoformat()


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.strata", description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    p.add_argument("--strata", type=Path, default=STRATA_PATH, help="strata.yaml to write")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(s: argparse.ArgumentParser) -> None:
        s.add_argument("--seed", type=int, required=True)
        s.add_argument("--date", type=_iso_date, required=True, help="drawn-at date (ISO), recorded as given")

    s = sub.add_parser("draw-dev", help="fix the sample PEs and fill dev to 10")
    common(s)
    s.add_argument("--frb-index", type=Path, default=FRB_INDEX_PATH, help="FRB index with the state column")
    s.add_argument("--n-dev", type=int, default=DEFAULTS["n_dev"])

    s = sub.add_parser("draw-test", help="draw the stratified test split")
    common(s)
    s.add_argument("--aa-counts", type=Path, default=None, help="CSV doc_id,n_aa_sections from the run stage")
    s.add_argument("--n-test", type=int, default=DEFAULTS["n_test"])
    s.add_argument("--per-cell", type=int, default=DEFAULTS["per_cell"])
    s.add_argument("--min-cell", type=int, default=DEFAULTS["min_cell"])
    s.add_argument("--n-rating", type=int, default=DEFAULTS["n_rating"])
    s.add_argument("--n-multi-aa", type=int, default=DEFAULTS["n_multi_aa"])

    s = sub.add_parser("draw-audit", help="draw the audit split by document-risk tertile")
    common(s)
    s.add_argument("--risk", type=Path, required=True, help="CSV doc_id,doc_risk")
    s.add_argument("--n-per-tertile", type=int, default=DEFAULTS["n_per_tertile"])

    sub.add_parser("show", help="print split counts and the draws on record")

    s = sub.add_parser("tuning-docs", help="list cited tuning documents missing from the tuning file")
    s.add_argument("--write", action="store_true", help="add the missing documents to the file")

    for name in ("draw-test", "draw-audit"):
        sub.choices[name].add_argument(
            "--tuning", type=Path, default=TUNING_DOCS_PATH, help="tuning documents whose banks leave the pool"
        )

    a = p.parse_args(argv)

    if a.cmd == "tuning-docs":
        recorded = load_tuning_documents(TUNING_DOCS_PATH)
        cited = cited_doc_ids()
        missing = missing_tuning_documents(recorded, cited)
        for d in missing:
            print(f"{d}: cited in {', '.join(cited[d])}")
        if a.write and missing:
            for d in missing:
                recorded[d] = {"doc_id": d, "source": cited[d]}
            write_tuning_documents(recorded, TUNING_DOCS_PATH)
            print(f"wrote {TUNING_DOCS_PATH} ({len(recorded)} documents)")
            return 0
        print(f"{len(recorded)} tuning documents recorded, {len(missing)} cited and missing")
        return 1 if missing else 0

    columns, rows = read_rows(a.manifest)
    manifest = to_records(rows)

    if a.cmd == "show":
        print(summary(manifest, load_strata(a.strata)))
        return 1 if exclusivity_problems(manifest) else 0

    tuning: list[str] = []
    if a.cmd in ("draw-test", "draw-audit"):
        problems = evaluability_problems(rows)
        if problems:
            print(
                "error: non-evaluable rows are not excluded (plan, Exclusions); run "
                "`python -m cra_extract.overrides apply` after documenting them: " + ", ".join(problems),
                file=sys.stderr,
            )
            return 1
        recorded = load_tuning_documents(a.tuning)
        missing = missing_tuning_documents(recorded)
        if missing:
            print(
                "error: tuning documents cited in the config are missing from "
                f"{a.tuning} (run `python -m cra_extract.strata tuning-docs --write`): " + ", ".join(missing),
                file=sys.stderr,
            )
            return 1
        tuning = sorted(recorded)

    try:
        if a.cmd == "draw-dev":
            draw = draw_dev(manifest, a.seed, a.date, frb_states(a.frb_index), n_dev=a.n_dev)
        elif a.cmd == "draw-test":
            aa = read_two_column_csv(a.aa_counts, "n_aa_sections", int) if a.aa_counts else None
            draw = draw_test(
                manifest, a.seed, a.date, aa,
                n_test=a.n_test, per_cell=a.per_cell, min_cell=a.min_cell,
                n_rating=a.n_rating, n_multi_aa=a.n_multi_aa, tuning=tuning,
            )
        else:
            risk = read_two_column_csv(a.risk, "doc_risk", float)
            draw = draw_audit(manifest, risk, a.seed, a.date, n_per_tertile=a.n_per_tertile, tuning=tuning)
        commit_draw(draw, columns, rows, a.manifest, a.strata)
    except DrawError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{draw.split}: {draw.n} documents, seed {draw.seed}, drawn {draw.drawn_at}, hash {draw.doc_id_hash}")
    for s in draw.strata:
        print(f"  {s.name}: N={s.population} pool={s.pool} n={s.drawn} p={s.selection_probability:.4f}")
    print(f"wrote {a.manifest} and {a.strata}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
