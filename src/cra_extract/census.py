"""Group C census: random draw, hand-reading sheet, validation, census truth and scoring.

Implements ``docs/analysis_plan.md`` "Group C census" (design 5.2) on top of the
template-residual census of :mod:`cra_extract.templates` (``results/census.csv``: doc_id,
regulator, located, template_id, score, residual_chars, asserts_finding, flagged,
residual_text). The steps:

1. :func:`draw_random_unflagged` draws the simple random sample of unflagged located review
   sections (plan step 4) and :func:`save_record` writes it to ``config/census_sample.yaml``
   (seed, date, pool size and hash, doc_ids) before any section is read.
2. :func:`build_sheet` writes ``data/gold/census_sheet.csv``, one row per section to read:
   every flagged located section (stratum ``flagged``), every locator miss (``locator_miss``,
   hand-located by the reader, plan step 5) and the random sample (``random_unflagged``).
   The sheet is blind: it carries the located text, pages and the template-residual
   columns, never an S0, S3 or S4 output (:data:`SYSTEM_COLUMNS` are refused by
   :func:`validate`).
3. :func:`validate` turns each read sheet row into gold label rows and runs
   :func:`cra_extract.label.validate` on them (the Group C rules are not duplicated here),
   plus the census rules: every row read, strata known, no system column, and (with the
   census and the sample record) every flagged section and every sampled section present.
4. :func:`truth` builds census truth per plan step 7: the hand label where read; an unread
   unflagged section is negative by template; a locator miss is truth as hand-located.
5. :func:`score` reports, with exact (Clopper-Pearson) 95 percent intervals: census sizes,
   positives and rating_lowered counts by regulator and method with the prevalence, the miss
   bound from the random sample, counts of finding_type, practice_basis and laws_cited
   elements among positives, precision and recall of finding_present and rating_lowered per
   system (S0 from ``results/baseline.csv``, every ``llm:`` system of a predictions file),
   the S0 false-positive rate by regulator next to ``n_negated_hits``, false positives by the
   ``distractor_laws_named`` stratum, attribute accuracy among census positives and the
   element-level precision and recall of laws_cited. :func:`positives_registry` is the seed
   of the findings registry (design 7).
6. :func:`draw_double_read`, :func:`build_double_sheet` and :func:`reliability` support the
   reliability check (plan "Reliability"): every census positive plus template-negative
   sections drawn within regulator in proportion to the positives, a second blind sheet, and
   Cohen's kappa per field (:func:`cohen_kappa`).

Census population: manifest rows that are evaluable (split other than ``excluded`` and
``text_extractable`` true, plan "Exclusions") and have a census row. Dev and test documents
are in the census (the plan reads every flagged section) but not in the random-sample pool.

Sheet conventions (one row per section, one column per Group C field): a cell holds a value
(``true``/``false`` for booleans, ``|`` between set elements as in the label sheets) or a gold
null code (``not_applicable``, ``absent_from_document``). A row is read when
``finding_present`` is filled. :func:`complete_row` fills the cells the codebook implies so
a negative row needs only finding_present, distractor_laws_named, evidence_page, quote and
minutes: a blank ``rating_lowered`` is derived from ``rating_effect`` (labeling guide 17c),
the finding attributes are ``not_applicable`` when finding_present is false, and the
downgrade attributes are ``not_applicable`` when rating_lowered is false. A typed cell is
never overwritten, so a contradiction still fails validation. ``evidence_page`` and
``quote`` are per section and apply to every value of the row; ``minutes`` is per section.

CLI::

    python -m cra_extract.census draw --seed SEED --date YYYY-MM-DD [--n 100] [--before-test-draw]
    python -m cra_extract.census sheet [--out data/gold/census_sheet.csv] [--fallback-cap]
    python -m cra_extract.census validate [data/gold/census_sheet.csv]
    python -m cra_extract.census score [--predictions results/predictions_corpus.csv]
    python -m cra_extract.census double --seed SEED --date YYYY-MM-DD [--n-negatives 20]
    python -m cra_extract.census double --compare data/gold/census_double.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from cra_extract import label
from cra_extract.config import (
    CONFIG_DIR,
    CORPUS_PATH,
    GOLD_DIR,
    RESULTS_DIR,
    TEXT_DIR,
    field_names,
    load_fields,
)
from cra_extract.evaluate import read_predictions, score_predictions
from cra_extract.label import Problem
from cra_extract.models import DocRecord, FieldValue, LabelRow, Section
from cra_extract.stats import clopper_pearson_ci, cohen_kappa

CENSUS_PATH = RESULTS_DIR / "census.csv"
SAMPLE_PATH = CONFIG_DIR / "census_sample.yaml"
SHEET_PATH = GOLD_DIR / "census_sheet.csv"
DOUBLE_SHEET_PATH = GOLD_DIR / "census_double.csv"
BASELINE_PATH = RESULTS_DIR / "baseline.csv"
PREDICTIONS_PATH = RESULTS_DIR / "predictions_corpus.csv"
SUMMARY_PATH = RESULTS_DIR / "census_summary.csv"
POSITIVES_PATH = RESULTS_DIR / "census_positives.csv"
RELIABILITY_PATH = RESULTS_DIR / "census_reliability.csv"

STRATA = ("flagged", "locator_miss", "random_unflagged")
# Plan step 3: hand reading starts only when at most READ_CAP sections (flagged plus locator
# misses) are flagged; READ_CAP_FALLBACK under the 10-09 checkpoint fallback.
READ_CAP = 300
READ_CAP_FALLBACK = 200
# Plan step 5: with at least MISS_SAMPLE_THRESHOLD locator misses, a random MISS_SAMPLE_N
# are hand-located and the miss rate is reported.
MISS_SAMPLE_THRESHOLD = 100
MISS_SAMPLE_N = 50
S5_SYSTEM = "s5"  # S5 = the S3 corpus tier for Group C (routing.apply rows)
HELD_OUT_SPLITS = ("dev", "test", "excluded")  # never in the random-sample pool
BASELINE_SYSTEM = "baseline"  # S0, as named by run.py and baseline.py
LLM_PREFIX = "llm:"
REGULATORS = ("OCC", "FDIC", "FRB")
GOLD_NULLS = ("not_applicable", "absent_from_document")
BINARY_FIELDS = ("finding_present", "rating_lowered")
# Attributes that are not_applicable when there is no finding, and when the rating was not lowered.
FINDING_ATTRIBUTES = (
    "finding_type",
    "rating_effect",
    "rating_lowered_level",
    "rating_before",
    "rating_after",
    "laws_cited",
    "practice_basis",
    "finding_source",
)
DOWNGRADE_ATTRIBUTES = ("rating_lowered_level", "rating_before", "rating_after")
# Plan "Group C census": accuracy among census positives, and the count-only attributes.
ACCURACY_FIELDS = ("rating_effect", "rating_lowered_level", "rating_before", "rating_after", "finding_source")
COUNT_FIELDS = ("finding_type", "practice_basis", "laws_cited")

SHEET_INFO_COLUMNS = [
    "doc_id",
    "stratum",
    "regulator",
    "method",
    "bank_name",
    "exam_date",
    "split",
    "pdf_path",
    "overall_rating_index",
    "located",
    "page_start",
    "page_end",
    "template_id",
    "score",
    "residual_chars",
    "residual_text",
    "section_text",
    "rating_pages",
    "rating_text",
    "candidate_pages",
]
LABEL_META_COLUMNS = ["evidence_page", "quote", "minutes", "labeler", "labeled_at", "notes"]
# Columns of a second (reliability) sheet: no stratum, no template output, no first labels.
DOUBLE_INFO_COLUMNS = [
    "doc_id",
    "regulator",
    "method",
    "bank_name",
    "exam_date",
    "pdf_path",
    "page_start",
    "page_end",
    "section_text",
    "rating_pages",
    "rating_text",
    "candidate_pages",
]
# Columns of S0, S1, S3 and S4 outputs (baseline.csv, predictions files). A sheet that carries
# any of them is not blind.
SYSTEM_COLUMNS = frozenset(
    {
        "system",
        "status",
        "value",
        "n_hits",
        "n_negated_hits",
        "triggers",
        "pred_status",
        "pred_value",
        "grounding_failure",
        "locator_miss",
        "confidence",
    }
)
SUMMARY_COLUMNS = ["metric", "system", "field", "stratum", "k", "n", "estimate", "ci_low", "ci_high", "note"]
POSITIVES_COLUMNS = [
    "doc_id",
    "bank_name",
    "regulator",
    "method",
    "exam_date",
    "release_date",
    "finding_type",
    "laws_cited",
    "practice_basis",
    "rating_effect",
    "rating_lowered",
    "rating_lowered_level",
    "rating_before",
    "rating_after",
    "finding_source",
    "stratum",
    "evidence_page",
    "quote",
]
# Words that point the reader of a locator miss to the pages that may hold the review section
# or its restatement on the rating page (labeling guide, Group C intro: SS:63-65, LB:74-76).
CANDIDATE_PATTERN = re.compile(r"illegal\s+credit\s+practice|discriminat|fair\s+lending", re.IGNORECASE)


class CensusError(ValueError):
    """The census input cannot satisfy a rule of the protocol."""


def group_c_fields() -> list[str]:
    """Group C fields in ``fields.yaml`` order (distractor_laws_named included)."""
    return field_names("C")


def sheet_columns() -> list[str]:
    """Canonical column order of the census sheet."""
    return SHEET_INFO_COLUMNS + group_c_fields() + LABEL_META_COLUMNS


def double_sheet_columns() -> list[str]:
    """Canonical column order of the second (reliability) sheet."""
    return DOUBLE_INFO_COLUMNS + group_c_fields() + LABEL_META_COLUMNS


# ----------------------------------------------------------------------------- inputs


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "t"}


def _cell(value: Any) -> str:
    """CSV cell text: '' for None and NaN, ISO for dates, repr-free numbers."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def read_census(path: Path = CENSUS_PATH) -> pd.DataFrame:
    """``results/census.csv`` with ``located`` and ``flagged`` as booleans."""
    df = pd.read_csv(path, dtype={"doc_id": str, "regulator": str, "template_id": str, "residual_text": str})
    return normalize_census(df)


def normalize_census(df: pd.DataFrame) -> pd.DataFrame:
    """Check the census columns and coerce ``located`` and ``flagged`` to booleans."""
    required = {"doc_id", "regulator", "located", "template_id", "score", "residual_chars", "flagged", "residual_text"}
    missing = required - set(df.columns)
    if missing:
        raise CensusError(f"census is missing columns {sorted(missing)}")
    out = df.copy()
    out["located"] = out["located"].map(_as_bool)
    out["flagged"] = out["flagged"].map(_as_bool)
    if out["doc_id"].duplicated().any():
        raise CensusError("census has duplicate doc_ids")
    return out


def is_evaluable(doc: DocRecord) -> bool:
    """Plan "Exclusions": a fetched, text-extractable document whose split is not excluded."""
    return doc.split != "excluded" and doc.text_extractable is True


def missing_from_census(census: pd.DataFrame, manifest: Sequence[DocRecord]) -> list[str]:
    """Evaluable manifest documents without a census row (the census is older than the manifest).

    They are outside the census population until ``make templates`` is rerun; a renamed
    doc_id (``config/manifest_overrides.yaml``) shows up here.
    """
    have = set(census["doc_id"])
    return sorted(d.doc_id for d in manifest if is_evaluable(d) and d.doc_id not in have)


def census_population(census: pd.DataFrame, manifest: Sequence[DocRecord]) -> pd.DataFrame:
    """Census rows of evaluable documents, joined to the manifest columns the census needs.

    Adds ``split``, ``method``, ``bank_name``, ``exam_date``, ``release_date`` and
    ``overall_rating_index``; sorted by doc_id.
    """
    docs = {d.doc_id: d for d in manifest if is_evaluable(d)}
    pop = normalize_census(census)
    pop = pop[pop["doc_id"].isin(docs)].copy()
    pop["split"] = [docs[d].split for d in pop["doc_id"]]
    pop["method"] = [docs[d].method for d in pop["doc_id"]]
    pop["bank_name"] = [docs[d].bank_name for d in pop["doc_id"]]
    pop["exam_date"] = [_cell(docs[d].exam_date) for d in pop["doc_id"]]
    pop["release_date"] = [_cell(docs[d].release_date) for d in pop["doc_id"]]
    pop["overall_rating_index"] = [_cell(docs[d].overall_rating) for d in pop["doc_id"]]
    return pop.sort_values("doc_id").reset_index(drop=True)


def ids_hash(doc_ids: Iterable[str]) -> str:
    """sha256 of the sorted doc_id list, one id per line (``label.split_hash`` convention)."""
    ids = sorted(doc_ids)
    return hashlib.sha256(("\n".join(ids) + "\n").encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- random draw


def random_pool(population: pd.DataFrame) -> list[str]:
    """doc_ids of unflagged located sections outside the dev, test and excluded splits."""
    mask = population["located"] & ~population["flagged"] & ~population["split"].isin(HELD_OUT_SPLITS)
    return sorted(population.loc[mask, "doc_id"])


def draw_random_unflagged(
    census: pd.DataFrame,
    manifest: Sequence[DocRecord],
    seed: int,
    drawn_at: str,
    n: int = 100,
) -> dict[str, Any]:
    """Simple random sample without replacement of ``n`` unflagged sections (plan step 4).

    The pool is :func:`random_pool` over the census population, sorted by doc_id; the draw is
    ``numpy.random.default_rng(seed).choice(len(pool), n, replace=False)``, so the same
    census, manifest and seed always give the same sample. The pool keeps documents of split
    ``audit``, so a redraw after the audit draw reproduces the sample. The record carries
    the splits present at draw time: if the test split is not drawn yet, a later test
    document can be in the sample (``test_split_drawn`` false).
    """
    date.fromisoformat(drawn_at)
    population = census_population(census, manifest)
    pool = random_pool(population)
    if n < 1:
        raise CensusError("n must be at least 1")
    if len(pool) < n:
        raise CensusError(f"random_unflagged: {n} wanted, {len(pool)} in the pool")
    rng = np.random.default_rng(seed)
    picks = rng.choice(len(pool), size=n, replace=False)
    doc_ids = sorted(pool[int(i)] for i in picks)
    split_counts = Counter(d.split for d in manifest)
    by_reg = Counter(population.set_index("doc_id").loc[doc_ids, "regulator"])
    return {
        "seed": int(seed),
        "drawn_at": drawn_at,
        "n": n,
        "pool_rule": (
            "census rows of evaluable documents (split not excluded, text_extractable true) "
            "with located true and flagged false, split not dev, test or excluded; sorted by "
            "doc_id; numpy default_rng(seed).choice without replacement"
        ),
        "held_out_splits": list(HELD_OUT_SPLITS),
        "test_split_drawn": split_counts.get("test", 0) > 0,
        "split_counts": {s: int(split_counts[s]) for s in sorted(split_counts)},
        "population": int(len(population)),
        "evaluable_missing_from_census": missing_from_census(census, manifest),
        "pool": len(pool),
        "pool_doc_id_hash": ids_hash(pool),
        "selection_probability": round(n / len(pool), 6),
        "by_regulator": {r: int(by_reg.get(r, 0)) for r in REGULATORS},
        "doc_ids": doc_ids,
        "doc_id_hash": ids_hash(doc_ids),
    }


def locator_misses(population: pd.DataFrame) -> list[str]:
    """doc_ids of the census population whose review section the locator did not find."""
    return sorted(population.loc[~population["located"], "doc_id"])


def draw_locator_misses(
    census: pd.DataFrame,
    manifest: Sequence[DocRecord],
    seed: int,
    drawn_at: str,
    n: int = MISS_SAMPLE_N,
    threshold: int = MISS_SAMPLE_THRESHOLD,
) -> dict[str, Any] | None:
    """Plan step 5: with at least ``threshold`` locator misses, a simple random sample of
    ``n`` of them (``numpy.random.default_rng(seed)`` over the sorted misses) is hand-located;
    with fewer, every miss is listed and None is returned."""
    date.fromisoformat(drawn_at)
    misses = locator_misses(census_population(census, manifest))
    if len(misses) < threshold:
        return None
    rng = np.random.default_rng(seed)
    picks = rng.choice(len(misses), size=min(n, len(misses)), replace=False)
    doc_ids = sorted(misses[int(i)] for i in picks)
    return {
        "seed": int(seed),
        "drawn_at": drawn_at,
        "n": len(doc_ids),
        "rule": (
            f"at least {threshold} locator misses: simple random sample of {n}, sorted misses, "
            "numpy default_rng(seed).choice without replacement; the miss rate is reported"
        ),
        "misses": len(misses),
        "misses_doc_id_hash": ids_hash(misses),
        "doc_ids": doc_ids,
        "doc_id_hash": ids_hash(doc_ids),
    }


def miss_sample_ids(path: Path = SAMPLE_PATH) -> list[str] | None:
    """doc_ids of the recorded locator-miss sample, or None when none was drawn."""
    rec = load_records(path).get("locator_miss_sample")
    return list(rec["doc_ids"]) if rec else None


def reading_queue(
    population: pd.DataFrame, miss_sample: Sequence[str] | None = None
) -> tuple[list[str], list[str]]:
    """(flagged located sections, locator misses to hand-locate). With at least
    ``MISS_SAMPLE_THRESHOLD`` misses only the recorded sample is read (plan step 5); without
    a sample record that case raises ``CensusError``."""
    flagged = sorted(population.loc[population["located"] & population["flagged"], "doc_id"])
    misses = locator_misses(population)
    if len(misses) >= MISS_SAMPLE_THRESHOLD:
        if miss_sample is None:
            raise CensusError(
                f"{len(misses)} locator misses (at least {MISS_SAMPLE_THRESHOLD}): the plan hand-locates a "
                f"random {MISS_SAMPLE_N}; run draw to record the sample first"
            )
        unknown = sorted(set(miss_sample) - set(misses))
        if unknown:
            raise CensusError(f"sampled locator misses are not misses of the census: {unknown}")
        return flagged, sorted(miss_sample)
    return flagged, misses


def load_records(path: Path = SAMPLE_PATH) -> dict[str, Any]:
    """Content of ``config/census_sample.yaml`` (empty record when the file does not exist)."""
    if not Path(path).exists():
        return {"version": 1}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("version", 1)
    return data


def save_record(key: str, record: Mapping[str, Any], path: Path = SAMPLE_PATH, force: bool = False) -> None:
    """Write one record (``random_unflagged`` or ``double_read``) to the sample file.

    An existing record is replaced only with ``force``: the sample is fixed before reading.
    """
    data = load_records(path)
    if key in data and not force:
        raise CensusError(f"{path} already holds a {key!r} record; pass force to replace it")
    data[key] = dict(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Written by python -m cra_extract.census; do not edit by hand.\n")
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=100)


def sample_ids(path: Path = SAMPLE_PATH) -> list[str]:
    """doc_ids of the recorded random sample (empty when not drawn)."""
    return list(load_records(path).get("random_unflagged", {}).get("doc_ids", []))


# ----------------------------------------------------------------------------- sheet


@dataclass(frozen=True)
class ReviewContext:
    """What the reader needs next to the PDF: the located review section and the rating page."""

    section: Section | None
    rating_pages: str = ""
    rating_text: str = ""
    candidate_pages: str = ""


def _pages_label(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def load_review_context(doc_id: str, regulator: str, text_dir: Path = TEXT_DIR) -> ReviewContext | None:
    """Locate the review and rating sections in ``{doc_id}.pages.json`` (None without text).

    ``candidate_pages`` lists the non-table-of-contents pages that mention illegal credit
    practices, discrimination or fair lending (:data:`CANDIDATE_PATTERN`): on the two
    positive samples the rating page restates the finding (FDIC_26816_20220809 p3, LB:74-75;
    FDIC_12299_20220912 p3), so the reader of a locator miss is pointed there too.
    """
    from cra_extract import locate

    path = Path(text_dir) / f"{doc_id}.pages.json"
    if not path.exists():
        return None
    pages = locate.load_pages_json(path)
    section = locate.locate_section("illegal_credit_practices_review", doc_id, pages, regulator)
    rating = locate.locate_section("rating", doc_id, pages, regulator)
    tagged = locate.tag_pages(pages, regulator)
    candidates = [p.number for p in tagged if not p.is_toc and CANDIDATE_PATTERN.search(p.text)]
    return ReviewContext(
        section=section,
        rating_pages=_pages_label(rating.page_start, rating.page_end) if rating else "",
        rating_text=rating.text if rating else "",
        candidate_pages=";".join(str(n) for n in candidates),
    )


def build_sheet(
    census: pd.DataFrame,
    manifest: Sequence[DocRecord],
    sample: Sequence[str],
    loader: Callable[[str, str], ReviewContext | None] = load_review_context,
    miss_sample: Sequence[str] | None = None,
    cap: int = READ_CAP,
) -> list[dict[str, str]]:
    """Rows of the census sheet: flagged, locator_miss, then random_unflagged, each by doc_id.

    Every label column is empty. Only manifest, locator and template-residual columns are
    filled; no system output is read, so the sheet is blind to S0, S3 and S4. Plan step 3:
    hand reading starts only when at most ``cap`` sections are flagged (flagged located
    sections plus the locator misses to hand-locate; 300, or 200 under the checkpoint
    fallback), so a larger queue raises ``CensusError`` (add template variants and rerun
    the census). Plan step 5: :func:`reading_queue` lists every miss, or the recorded sample
    of 50 when there are 100 or more.
    """
    population = census_population(census, manifest)
    flagged_ids, miss_ids = reading_queue(population, miss_sample)
    if len(flagged_ids) + len(miss_ids) > cap:
        raise CensusError(
            f"{len(flagged_ids)} flagged sections and {len(miss_ids)} locator misses exceed the reading cap "
            f"of {cap} (plan step 3): add template variants and rerun the census before reading"
        )
    by_id = population.set_index("doc_id", drop=False)
    unknown = sorted(set(sample) - set(by_id.index))
    if unknown:
        raise CensusError(f"sampled doc_ids not in the census population: {unknown}")
    bad = [d for d in sample if not by_id.at[d, "located"] or by_id.at[d, "flagged"]]
    if bad:
        raise CensusError(f"sampled doc_ids are not unflagged located sections: {sorted(bad)}")
    plan = [
        ("flagged", flagged_ids),
        ("locator_miss", miss_ids),
        ("random_unflagged", sorted(sample)),
    ]
    columns = sheet_columns()
    rows: list[dict[str, str]] = []
    for stratum, doc_ids in plan:
        for doc_id in doc_ids:
            c = by_id.loc[doc_id]
            ctx = loader(doc_id, str(c["regulator"]))
            section = ctx.section if ctx else None
            row = dict.fromkeys(columns, "")
            row.update(
                doc_id=doc_id,
                stratum=stratum,
                regulator=str(c["regulator"]),
                method=str(c["method"]),
                bank_name=str(c["bank_name"]),
                exam_date=str(c["exam_date"]),
                split=str(c["split"]),
                pdf_path=label.pdf_path_for(doc_id),
                overall_rating_index=str(c["overall_rating_index"]),
                located="true" if c["located"] else "false",
                template_id=_cell(c["template_id"]),
                score=_cell(c["score"]),
                residual_chars=_cell(c["residual_chars"]),
                residual_text=_cell(c["residual_text"]),
                rating_pages=ctx.rating_pages if ctx else "",
                rating_text=ctx.rating_text if ctx else "",
                candidate_pages=ctx.candidate_pages if ctx else "",
            )
            if section is not None and c["located"]:
                row.update(page_start=str(section.page_start), page_end=str(section.page_end), section_text=section.text)
            elif c["located"]:
                row["notes"] = "census located the section but it was not found again in data/text"
            rows.append(row)
    return rows


def write_csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str], path: Path) -> None:
    """Write rows with the given column order (missing cells empty)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: _cell(r.get(c, "")) for c in columns})


def read_sheet(path: Path) -> list[dict[str, str]]:
    """A census or reliability sheet as string dicts (``label.read_sheet``)."""
    return label.read_sheet(path)


# ----------------------------------------------------------------------------- completion


def is_read(row: Mapping[str, str]) -> bool:
    """A sheet row is read once its ``finding_present`` cell is filled."""
    return bool((row.get("finding_present") or "").strip())


def complete_row(row: Mapping[str, str]) -> dict[str, str]:
    """Fill the cells the codebook implies, never overwriting a typed cell (module docstring)."""
    out = {k: (v or "").strip() for k, v in row.items()}
    finding = out.get("finding_present", "").lower()
    if finding == "false":
        for f in FINDING_ATTRIBUTES:
            if not out.get(f):
                out[f] = "not_applicable"
        if not out.get("rating_lowered"):
            out["rating_lowered"] = "false"
    effect = out.get("rating_effect", "")
    if not out.get("rating_lowered") and effect and effect not in GOLD_NULLS:
        out["rating_lowered"] = "true" if effect == "substantive_lowered" else "false"
    if out.get("rating_lowered", "").lower() == "false":
        for f in DOWNGRADE_ATTRIBUTES:
            if not out.get(f):
                out[f] = "not_applicable"
    return out


def label_rows(
    rows: Sequence[Mapping[str, str]], overall: Mapping[str, str] | None = None
) -> tuple[list[dict[str, str]], list[int]]:
    """Long label rows (``label.SHEET_COLUMNS``) for the read sheet rows, with their sheet row numbers.

    Each Group C cell becomes one (doc_id, field) row: a gold null code goes to
    ``null_reason``, anything else to ``value``. evidence_page, quote, labeler, labeled_at
    and notes are copied to every field of the section; the section's minutes go on the
    finding_present row and 0 on the others. With ``overall`` (doc_id -> index rating), an
    ``overall_rating`` context row is added so the rule rating_after == overall_rating when
    the lowered level is overall can run; its row number is 0.
    """
    fields = group_c_fields()
    out: list[dict[str, str]] = []
    src: list[int] = []
    for i, raw in enumerate(rows, start=1):
        if not is_read(raw):
            continue
        r = complete_row(raw)
        doc_id = r.get("doc_id", "")
        for f in fields:
            cell = r.get(f, "")
            out.append(
                {
                    "doc_id": doc_id,
                    "field": f,
                    "field_group": "C",
                    "value": "" if cell in GOLD_NULLS else cell,
                    "null_reason": cell if cell in GOLD_NULLS else "",
                    "evidence_page": r.get("evidence_page", ""),
                    "quote": r.get("quote", ""),
                    "minutes": r.get("minutes", "") if f == "finding_present" else "0",
                    "labeler": r.get("labeler", ""),
                    "labeled_at": r.get("labeled_at", ""),
                    "notes": r.get("notes", ""),
                }
            )
            src.append(i)
        rating = (overall or {}).get(doc_id, "")
        if rating:
            out.append(
                {
                    "doc_id": doc_id,
                    "field": "overall_rating",
                    "field_group": "A",
                    "value": rating,
                    "evidence_page": "1",
                    "quote": "agency index",
                    "minutes": "0",
                }
            )
            src.append(0)
    return out, src


# ----------------------------------------------------------------------------- validate


# Rules of label.validate that concern the section as a whole, reported once per sheet row.
_ROW_RULES = {
    "missing_evidence_page": "evidence_page",
    "bad_evidence_page": "evidence_page",
    "missing_quote": "quote",
    "missing_minutes": "minutes",
    "bad_labeled_at": "labeled_at",
}


def validate(
    path: Path,
    census: pd.DataFrame | None = None,
    manifest: Sequence[DocRecord] | None = None,
    sample: Sequence[str] | None = None,
    miss_sample: Sequence[str] | None = None,
) -> list[Problem]:
    """Check a census sheet; an empty list means it may be used.

    Census rules: the sheet has the doc_id, stratum, Group C and label metadata columns and
    no system column; one row per doc_id; stratum in :data:`STRATA`; every row read. The
    Group C rules of :func:`cra_extract.label.validate` run on :func:`label_rows` (value
    or null per cell, allowed values, nulls only on nullable fields, evidence page and quote
    for every value, minutes, rating_lowered derived from rating_effect, attributes
    not_applicable exactly when there is no finding, downgrade attributes exactly when not
    lowered, rating_after equal to the index overall rating when the lowered level is
    overall). With ``census`` and ``manifest``, every flagged section and locator miss of the
    census population must have a row (only the recorded ``miss_sample`` of the misses when
    there are 100 or more), and with ``sample`` every sampled doc_id must have a
    ``random_unflagged`` row; a row outside these sets is reported.
    """
    rows = read_sheet(path)
    problems: list[Problem] = []
    present = set(rows[0].keys()) if rows else set()
    required = {"doc_id", "stratum", *group_c_fields(), "evidence_page", "quote", "minutes"}
    missing = required - present
    if not rows or missing:
        problems.append(Problem(0, "", "", "columns", f"missing columns {sorted(missing)}" if rows else "empty sheet"))
        return problems
    leaked = sorted(present & SYSTEM_COLUMNS) + sorted(c for c in present if c.startswith("pred_"))
    if leaked:
        problems.append(Problem(0, "", "", "not_blind", f"system output columns in the sheet: {leaked}"))

    first_row: dict[str, int] = {}
    for i, r in enumerate(rows, start=1):
        doc_id = r["doc_id"]
        if doc_id in first_row:
            problems.append(Problem(i, doc_id, "", "duplicate_row", f"second row for {doc_id}"))
        first_row.setdefault(doc_id, i)
        if r["stratum"] not in STRATA:
            problems.append(Problem(i, doc_id, "stratum", "bad_stratum", f"{r['stratum']!r} not in {list(STRATA)}"))
        if not is_read(r):
            problems.append(Problem(i, doc_id, "finding_present", "unread", "section not read yet (finding_present empty)"))

    overall: dict[str, str] = {}
    if manifest is not None:
        overall = {d.doc_id: d.overall_rating for d in manifest if d.overall_rating}
    long_rows, src = label_rows(rows, overall)
    if long_rows:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "census_long.csv"
            label.write_sheet(long_rows, tmp_path)
            label_problems = label.validate(tmp_path)
        seen: set[tuple[int, str, str]] = set()
        for p in label_problems:
            if p.field == "overall_rating" or p.rule == "columns":
                continue
            sheet_row = src[p.row - 1] if 0 < p.row <= len(src) else 0
            if sheet_row == 0:
                sheet_row = first_row.get(p.doc_id, 0)
            fld = _ROW_RULES.get(p.rule, p.field)
            key = (sheet_row, p.rule, fld)
            if p.rule in _ROW_RULES and key in seen:
                continue
            seen.add(key)
            problems.append(Problem(sheet_row, p.doc_id, fld, p.rule, p.message))

    if census is not None and manifest is not None:
        population = census_population(census, manifest)
        try:
            flagged_ids, miss_ids = reading_queue(population, miss_sample)
        except CensusError as err:
            problems.append(Problem(0, "", "", "locator_miss_sample", str(err)))
            flagged_ids, miss_ids = reading_queue(population.assign(located=True), None)[0], []
        expected = {"flagged": set(flagged_ids), "locator_miss": set(miss_ids)}
        if sample is not None:
            expected["random_unflagged"] = set(sample)
        stratum_of = {r["doc_id"]: r["stratum"] for r in rows}
        for stratum, ids in expected.items():
            for doc_id in sorted(ids):
                if doc_id not in stratum_of:
                    problems.append(Problem(0, doc_id, "", "missing_row", f"{stratum} section has no sheet row"))
                elif stratum_of[doc_id] != stratum:
                    problems.append(
                        Problem(first_row[doc_id], doc_id, "stratum", "wrong_stratum", f"expected {stratum}, sheet says {stratum_of[doc_id]}")
                    )
        if sample is not None:
            allowed = set().union(*expected.values())
            for doc_id in sorted(set(stratum_of) - allowed):
                problems.append(Problem(first_row[doc_id], doc_id, "", "unexpected_row", "not flagged, not a locator miss and not sampled"))
    return sorted(problems, key=lambda p: (p.row, p.doc_id, p.field, p.rule))


# ----------------------------------------------------------------------------- truth


def template_distractors() -> dict[str, bool]:
    """template_id -> ``distractor_laws_named`` from ``config/review_templates.yaml``."""
    from cra_extract.templates import load_templates

    return {t.id: t.distractor_laws_named for t in load_templates()}


def truth(
    sheet: Sequence[Mapping[str, str]],
    census: pd.DataFrame,
    manifest: Sequence[DocRecord],
    distractors: Mapping[str, bool] | None = None,
) -> pd.DataFrame:
    """Census truth, one row per document of the census population (plan step 7).

    ``truth_source`` is ``hand`` for a read sheet row (cells completed by
    :func:`complete_row`), ``template`` for an unflagged section without a read row
    (negative by template: finding_present and rating_lowered false, every finding attribute
    not_applicable, distractor_laws_named from the best-matching template) and ``unread``
    for a flagged section or locator miss whose row is missing or unread (all cells empty;
    excluded from every metric and counted). ``stratum`` is the sheet stratum, or
    ``unflagged`` for a section that is not on the sheet.
    """
    distractors = template_distractors() if distractors is None else distractors
    population = census_population(census, manifest)
    read = {r["doc_id"]: complete_row(r) for r in sheet if is_read(r)}
    on_sheet = {r["doc_id"]: r.get("stratum", "") for r in sheet}
    fields = group_c_fields()
    out: list[dict[str, Any]] = []
    for c in population.to_dict("records"):
        doc_id = c["doc_id"]
        row: dict[str, Any] = {
            k: c[k] for k in ("doc_id", "regulator", "method", "split", "bank_name", "exam_date", "release_date")
        }
        needs_reading = (not c["located"]) or c["flagged"]
        default_stratum = ("locator_miss" if not c["located"] else "flagged") if needs_reading else "unflagged"
        row["stratum"] = on_sheet.get(doc_id) or default_stratum
        if doc_id in read:
            r = read[doc_id]
            row["truth_source"] = "hand"
            row.update({f: r.get(f, "") for f in fields})
            row["evidence_page"] = r.get("evidence_page", "")
            row["quote"] = r.get("quote", "")
        elif needs_reading:
            row["truth_source"] = "unread"
            row.update(dict.fromkeys(fields, ""))
            row["evidence_page"] = row["quote"] = ""
        else:
            row["truth_source"] = "template"
            row.update(dict.fromkeys(fields, "not_applicable"))
            row["finding_present"] = row["rating_lowered"] = "false"
            tid = _cell(c["template_id"])
            row["distractor_laws_named"] = "true" if distractors.get(tid, False) else "false"
            row["evidence_page"] = row["quote"] = ""
        out.append(row)
    return pd.DataFrame(out, columns=[
        "doc_id", "regulator", "method", "split", "bank_name", "exam_date", "release_date",
        "stratum", "truth_source", *fields, "evidence_page", "quote",
    ])


def _true(cell: Any) -> bool:
    return str(cell).strip().lower() == "true"


def truth_labels(truth_df: pd.DataFrame) -> list[LabelRow]:
    """Gold ``LabelRow`` records for every known (hand or template) Group C cell."""
    out: list[LabelRow] = []
    known = truth_df[truth_df["truth_source"] != "unread"]
    for r in known.to_dict("records"):
        for f in group_c_fields():
            cell = str(r.get(f, "") or "").strip()
            if not cell:
                continue
            if cell in GOLD_NULLS:
                out.append(LabelRow(doc_id=r["doc_id"], field=f, null_reason=cell))
                continue
            try:
                value: Any = label.coerce_value(f, cell)
            except ValueError:
                value = cell
            out.append(LabelRow(doc_id=r["doc_id"], field=f, value=value))
    return out


# ----------------------------------------------------------------------------- predictions


def baseline_predictions(baseline: pd.DataFrame) -> list[FieldValue]:
    """S0 rows of ``results/baseline.csv`` as FieldValues; a review section not located abstains."""
    out: list[FieldValue] = []
    for r in baseline.to_dict("records"):
        located = _as_bool(r.get("located"))
        for f in BINARY_FIELDS:
            v = r.get(f)
            missing = v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() == ""
            if not located or missing:
                out.append(FieldValue(doc_id=r["doc_id"], field=f, system=BASELINE_SYSTEM, status="abstain"))
            else:
                out.append(FieldValue(doc_id=r["doc_id"], field=f, system=BASELINE_SYSTEM, value=_as_bool(v)))
    return out


def llm_predictions(path: Path) -> list[FieldValue]:
    """Group C rows of every ``llm:`` system and of S5 (``s5``, which equals the S3 corpus
    tier for Group C) in a predictions file (``evaluate.read_predictions``)."""
    fields = set(group_c_fields())
    preds, _grounding = read_predictions(path)
    return [p for p in preds if p.field in fields and (p.system.startswith(LLM_PREFIX) or p.system == S5_SYSTEM)]


def negated_hits_by_regulator(baseline: pd.DataFrame, doc_ids: Iterable[str]) -> dict[str, int]:
    """Sum of S0 ``n_negated_hits`` per regulator over the given documents."""
    keep = baseline[baseline["doc_id"].isin(set(doc_ids))]
    sums = keep.groupby("regulator")["n_negated_hits"].sum()
    return {r: int(sums.get(r, 0)) for r in REGULATORS}


# ----------------------------------------------------------------------------- score


def _row(
    metric: str, system: str, field: str, stratum: str, k: int, n: int, note: str = "", ci: bool = True
) -> dict[str, Any]:
    lo, hi = clopper_pearson_ci(k, n) if ci and n > 0 else (math.nan, math.nan)
    return {
        "metric": metric,
        "system": system,
        "field": field,
        "stratum": stratum,
        "k": int(k),
        "n": int(n),
        "estimate": k / n if (ci and n > 0) else math.nan,
        "ci_low": lo,
        "ci_high": hi,
        "note": note,
    }


def _elements(cell: Any) -> list[str]:
    s = str(cell or "").strip()
    if not s:
        return []
    if s in GOLD_NULLS:
        return [s]
    return sorted({x.strip() for x in s.split(label.SET_SEPARATOR) if x.strip()})


def census_counts(truth_df: pd.DataFrame) -> list[dict[str, Any]]:
    """Census-level rows (system ``census``): sizes, prevalence, miss bound, attribute counts."""
    rows: list[dict[str, Any]] = []
    known = truth_df[truth_df["truth_source"] != "unread"]
    n_all = len(truth_df)
    for src in ("hand", "template", "unread"):
        rows.append(_row("documents", "census", "", f"truth_source={src}", int((truth_df["truth_source"] == src).sum()), n_all, ci=False))
    for stratum in (*STRATA, "unflagged"):
        part = truth_df[truth_df["stratum"] == stratum]
        rows.append(_row("documents_read", "census", "", stratum, int((part["truth_source"] == "hand").sum()), len(part), ci=False))

    cells = [("all", known)]
    cells += [(r, known[known["regulator"] == r]) for r in REGULATORS]
    cells += [
        (f"{r}/{m}", known[(known["regulator"] == r) & (known["method"] == m)])
        for r in REGULATORS
        for m in ("small", "isb")
    ]
    for f in BINARY_FIELDS:
        for name, part in cells:
            k = int(part[f].map(_true).astype(bool).sum())
            rows.append(_row("prevalence", "census", f, name, k, len(part), "exact CI over the labeled census"))

    misses = int((truth_df["stratum"] == "locator_miss").sum())
    rows.append(_row("locator_miss_rate", "census", "", "all", misses, n_all,
                     "review sections the locator did not find, over the census population"))

    rnd = known[(known["stratum"] == "random_unflagged") & (known["truth_source"] == "hand")]
    k = int(rnd["finding_present"].map(_true).astype(bool).sum())
    rows.append(_row("miss_bound", "census", "finding_present", "random_unflagged", k, len(rnd), "positives among the read random unflagged sections; ci_high is the exact upper bound"))

    positives = known[known["finding_present"].map(_true).astype(bool)]
    for f in COUNT_FIELDS:
        counter: Counter[str] = Counter()
        for cell in positives[f]:
            counter.update(_elements(cell))
        for element in sorted(counter):
            rows.append(_row("attribute_count", "census", f, element, counter[element], len(positives), "among census positives; set fields count elements"))
    negatives = known[~known["finding_present"].map(_true).astype(bool)]
    for flag in ("true", "false"):
        k = int((negatives["distractor_laws_named"].astype(str).str.lower() == flag).sum())
        rows.append(_row("negatives_by_distractor", "census", "distractor_laws_named", flag, k, len(negatives)))
    return rows


def system_metrics(
    truth_df: pd.DataFrame,
    predictions: Sequence[FieldValue],
    negated_hits: Mapping[str, int] | None = None,
    fp_rate_systems: Sequence[str] = (BASELINE_SYSTEM,),
) -> list[dict[str, Any]]:
    """Per-system rows against census truth.

    Each system is scored on the census documents it has a Group C row for (S4 runs on a
    subset; ``documents`` states n). Cells come from ``evaluate.score_predictions`` (the
    scoring matrix); precision and recall follow ``evaluate.metrics_table`` (abstentions
    excluded, a fabricated absence is a false negative) with exact intervals. The false
    positive rate (FP over answered census negatives) is reported per regulator for the
    systems in ``fp_rate_systems`` (S0, plan), next to ``n_negated_hits``: FP over every
    census negative of the system's documents (plan: FP / census negatives), so a negative on
    which the system abstained (an S0 locator miss) is in the denominator and is no FP.
    """
    rows: list[dict[str, Any]] = []
    known = truth_df[truth_df["truth_source"] != "unread"]
    reg_of = dict(zip(known["doc_id"], known["regulator"], strict=True))
    distractor_of = dict(zip(known["doc_id"], known["distractor_laws_named"].astype(str).str.lower(), strict=True))
    positive_docs = set(known.loc[known["finding_present"].map(_true).astype(bool), "doc_id"])
    labels = truth_labels(known)
    for system in sorted({p.system for p in predictions}):
        preds = [p for p in predictions if p.system == system and p.doc_id in reg_of]
        docs = {p.doc_id for p in preds}
        rows.append(_row("documents", system, "", "all", len(docs), len(known), "census documents with a Group C row of this system", ci=False))
        if not docs:
            continue
        sys_labels = [lab for lab in labels if lab.doc_id in docs]
        scored = score_predictions(sys_labels, preds, [system])
        if scored.empty:
            continue
        predicted_fields = {p.field for p in preds}
        for f in BINARY_FIELDS:
            if f not in predicted_fields:
                continue
            g = scored[scored["field"] == f]
            gp = g["gold_pos"].astype(bool)
            pp = g["pred_pos"].astype(bool)
            ans = g["pred_status"] == "value"
            not_abs = g["pred_status"] != "abstain"
            tp = int((gp & pp & ans).sum())
            fp = int((~gp & pp & ans).sum())
            fn = int((gp & ~pp & not_abs).sum())
            rows.append(_row("precision", system, f, "all", tp, tp + fp))
            rows.append(_row("recall", system, f, "all", tp, tp + fn, "conditional on answering"))
            rows.append(_row("abstention", system, f, "all", int((~not_abs).sum()), len(g)))
            neg = g[~gp & ans]
            neg_all = g[~gp]
            if f == "finding_present":
                for flag in ("true", "false"):
                    part = neg[neg["doc_id"].map(distractor_of) == flag]
                    rows.append(_row("fp_by_distractor", system, f, f"distractor_laws_named={flag}", int(part["pred_pos"].astype(bool).sum()), len(part)))
            if system in fp_rate_systems:
                for reg in ("all", *REGULATORS):
                    part = neg_all if reg == "all" else neg_all[neg_all["doc_id"].map(reg_of) == reg]
                    fp_k = int((part["pred_pos"].astype(bool) & (part["pred_status"] == "value")).sum())
                    note = "FP / all census negatives (abstentions in the denominator)"
                    if negated_hits is not None and reg != "all":
                        note += f"; n_negated_hits={negated_hits.get(reg, 0)}"
                    rows.append(_row("fp_rate", system, f, reg, fp_k, len(part), note))
            if system in fp_rate_systems and negated_hits is not None and f == "finding_present":
                for reg in REGULATORS:
                    n_docs = sum(1 for d in docs if reg_of.get(d) == reg)
                    rows.append(_row("n_negated_hits", system, "", reg, int(negated_hits.get(reg, 0)), n_docs, "negated S0 hits summed over the census documents", ci=False))
        pos = scored[scored["doc_id"].isin(positive_docs)]
        for f in ACCURACY_FIELDS:
            if f not in predicted_fields:
                continue
            g = pos[(pos["field"] == f) & (pos["gold_status"] == "value")]
            k = int((g["cell"] == "correct").sum())
            n = int(g["cell"].isin(["correct", "wrong", "fabricated_absence"]).sum())
            rows.append(_row("accuracy_among_positives", system, f, "census_positives", k, n))
        if "laws_cited" in predicted_fields:
            g = pos[(pos["field"] == "laws_cited") & (pos["pred_status"] != "abstain")]
            tp = int(g["tp"].fillna(0).sum())
            fp = int(g["fp"].fillna(0).sum())
            fn = int(g["fn"].fillna(0).sum())
            note = "element-level; elements of one document are not independent"
            rows.append(_row("element_precision", system, "laws_cited", "census_positives", tp, tp + fp, note))
            rows.append(_row("element_recall", system, "laws_cited", "census_positives", tp, tp + fn, note))
    return rows


def score(
    truth_df: pd.DataFrame,
    predictions: Sequence[FieldValue],
    negated_hits: Mapping[str, int] | None = None,
) -> pd.DataFrame:
    """The census summary table (``results/census_summary.csv``): census rows, then systems."""
    rows = census_counts(truth_df) + system_metrics(truth_df, predictions, negated_hits)
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


def positives_registry(truth_df: pd.DataFrame) -> pd.DataFrame:
    """Census positives with the attributes of the findings registry (design 7), by doc_id."""
    pos = truth_df[(truth_df["truth_source"] == "hand") & truth_df["finding_present"].map(_true).astype(bool)]
    return pos.reindex(columns=POSITIVES_COLUMNS).sort_values("doc_id").reset_index(drop=True)


# ----------------------------------------------------------------------------- reliability


def allocate(weights: Mapping[str, int], n: int, capacity: Mapping[str, int]) -> dict[str, int]:
    """Largest-remainder allocation of ``n`` in proportion to ``weights``, capped by ``capacity``.

    Ties in the remainder go to the key that sorts first. What a cap removes goes, one at a
    time, to the keys with spare capacity in order of weight (largest first); a total
    capacity below ``n`` leaves a shortfall.
    """
    total = sum(weights.values())
    if total <= 0:
        raise CensusError("no positives: the allocation in proportion to positives is undefined")
    keys = sorted(k for k, w in weights.items() if w > 0)
    quotas = {k: n * weights[k] / total for k in keys}
    alloc = {k: math.floor(q) for k, q in quotas.items()}
    left = n - sum(alloc.values())
    for k in sorted(keys, key=lambda k: (-(quotas[k] - alloc[k]), k))[:left]:
        alloc[k] += 1
    overflow = 0
    for k in keys:
        cap = int(capacity.get(k, 0))
        if alloc[k] > cap:
            overflow += alloc[k] - cap
            alloc[k] = cap
    order = sorted(keys, key=lambda k: (-weights[k], k))
    while overflow > 0:
        spare = [k for k in order if alloc[k] < int(capacity.get(k, 0))]
        if not spare:
            break
        for k in spare:
            if overflow == 0:
                break
            alloc[k] += 1
            overflow -= 1
    return alloc


def draw_double_read(truth_df: pd.DataFrame, seed: int, drawn_at: str, n_negatives: int = 20) -> dict[str, Any]:
    """Every census positive plus ``n_negatives`` template-negative sections for the reliability check.

    Negatives are drawn without replacement, within regulator in proportion to the positives
    (:func:`allocate`), from the random_unflagged sections that were read and coded negative,
    so both codings exist for every item. One ``numpy.random.default_rng(seed)`` draws the
    regulators in sorted order from their doc_id-sorted pools.
    """
    date.fromisoformat(drawn_at)
    hand = truth_df[truth_df["truth_source"] == "hand"]
    positives = hand[hand["finding_present"].map(_true).astype(bool)]
    pool = hand[(hand["stratum"] == "random_unflagged") & ~hand["finding_present"].map(_true).astype(bool)]
    weights = Counter(positives["regulator"])
    capacity = Counter(pool["regulator"])
    alloc = allocate(weights, n_negatives, capacity)
    rng = np.random.default_rng(seed)
    negatives: list[str] = []
    for reg in sorted(alloc):
        ids = sorted(pool.loc[pool["regulator"] == reg, "doc_id"])
        if alloc[reg]:
            picks = rng.choice(len(ids), size=alloc[reg], replace=False)
            negatives.extend(ids[int(i)] for i in picks)
    from cra_extract.strata import SAMPLE_DOC_IDS

    pos_ids = sorted(positives["doc_id"])
    doc_ids = sorted(pos_ids + negatives)
    return {
        "seed": int(seed),
        "drawn_at": drawn_at,
        "n_positives": len(pos_ids),
        "n_negatives": len(negatives),
        "n_negatives_wanted": n_negatives,
        "negative_pool_rule": "random_unflagged sections read and coded finding_present false",
        "positives_by_regulator": {r: int(weights.get(r, 0)) for r in REGULATORS},
        "negative_pool_by_regulator": {r: int(capacity.get(r, 0)) for r in REGULATORS},
        "allocation": {r: int(alloc.get(r, 0)) for r in REGULATORS},
        "fixture_sample_positives": [d for d in pos_ids if d in SAMPLE_DOC_IDS],
        "positives": pos_ids,
        "negatives": sorted(negatives),
        "doc_ids": doc_ids,
        "doc_id_hash": ids_hash(doc_ids),
    }


def build_double_sheet(record: Mapping[str, Any], first: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    """Second sheet for the drawn sections: the reading aids of the first sheet, empty labels.

    Blind to the first coding: no stratum, no template columns, no label cells, rows by
    doc_id so positives and negatives are interleaved.
    """
    by_id = {r["doc_id"]: r for r in first}
    missing = [d for d in record["doc_ids"] if d not in by_id]
    if missing:
        raise CensusError(f"drawn doc_ids not on the first sheet: {missing}")
    columns = double_sheet_columns()
    rows = []
    for doc_id in sorted(record["doc_ids"]):
        src = by_id[doc_id]
        row = dict.fromkeys(columns, "")
        row.update({c: src.get(c, "") for c in DOUBLE_INFO_COLUMNS})
        rows.append(row)
    return rows


def _minutes(rows: Iterable[Mapping[str, str]]) -> float:
    vals = []
    for r in rows:
        try:
            vals.append(float(r.get("minutes", "")))
        except ValueError:
            continue
    return float(np.mean(vals)) if vals else math.nan


def reliability(first: Sequence[Mapping[str, str]], second: Sequence[Mapping[str, str]]) -> pd.DataFrame:
    """Agreement per Group C field between two codings of the same sections (plan "Reliability").

    Items are the doc_ids read in both sheets, with cells completed by :func:`complete_row`;
    a null code is a category of its own. Cohen's kappa is reported where both codings use at
    least two categories, percent agreement with the majority-class prevalence otherwise
    (``statistic`` says which). Set fields are compared element-wise: one binary item per
    (section, allowed element), a null counting as the empty set. The last row gives the mean
    minutes per section of each coding.
    """
    a = {r["doc_id"]: complete_row(r) for r in first if is_read(r)}
    b = {r["doc_id"]: complete_row(r) for r in second if is_read(r)}
    items = sorted(set(a) & set(b))
    specs = load_fields()["fields"]
    out = []
    for f in group_c_fields():
        spec = specs[f]
        if spec["type"] == "set":
            xa, xb = [], []
            for d in items:
                sa = set(_elements(a[d].get(f))) - set(GOLD_NULLS)
                sb = set(_elements(b[d].get(f))) - set(GOLD_NULLS)
                for e in spec["allowed"]:
                    xa.append(e in sa)
                    xb.append(e in sb)
        else:
            xa = [a[d].get(f, "").lower() if spec["type"] == "bool" else a[d].get(f, "") for d in items]
            xb = [b[d].get(f, "").lower() if spec["type"] == "bool" else b[d].get(f, "") for d in items]
        n = len(xa)
        agree = sum(x == y for x, y in zip(xa, xb, strict=True))
        both = len(set(xa)) >= 2 and len(set(xb)) >= 2
        pooled = Counter(xa + xb)
        majority, count = pooled.most_common(1)[0] if pooled else ("", 0)
        out.append(
            {
                "field": f,
                "n_sections": len(items),
                "n_items": n,
                "n_agree": agree,
                "percent_agreement": agree / n if n else math.nan,
                "kappa": cohen_kappa(xa, xb) if both else math.nan,
                "statistic": "kappa" if both else "percent_agreement",
                "majority_class": str(majority),
                "majority_prevalence": count / (2 * n) if n else math.nan,
                "minutes_first_mean": math.nan,
                "minutes_second_mean": math.nan,
            }
        )
    out.append(
        {
            "field": "minutes_per_section",
            "n_sections": len(items),
            "minutes_first_mean": _minutes(a[d] for d in items),
            "minutes_second_mean": _minutes(b[d] for d in items),
        }
    )
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------- CLI


def _iso_date(s: str) -> str:
    return date.fromisoformat(s).isoformat()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rel(path: Path) -> str:
    from cra_extract.config import ROOT

    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.census", description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    p.add_argument("--census", type=Path, default=CENSUS_PATH, help="template-residual census CSV")
    p.add_argument("--sample-file", type=Path, default=SAMPLE_PATH, help="census_sample.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("draw", help="draw the random unflagged sample and record it")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--date", type=_iso_date, required=True, help="drawn-at date (ISO), recorded as given")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--force", action="store_true", help="replace an existing record")
    s.add_argument("--before-test-draw", action="store_true",
                   help="allow the draw before draw-test (later test documents can then be in the sample)")

    s = sub.add_parser("sheet", help="write the blind census sheet")
    s.add_argument("--out", type=Path, default=SHEET_PATH)
    s.add_argument("--text-dir", type=Path, default=TEXT_DIR)
    s.add_argument("--force", action="store_true", help="overwrite an existing sheet (destroys labels)")
    s.add_argument("--fallback-cap", action="store_true",
                   help=f"reading cap {READ_CAP_FALLBACK} (checkpoint fallback) instead of {READ_CAP}")

    s = sub.add_parser("validate", help="check the census sheet; exit 1 on problems")
    s.add_argument("path", type=Path, nargs="?", default=SHEET_PATH)

    s = sub.add_parser("score", help="census truth, summary and positives registry")
    s.add_argument("--sheet", type=Path, default=SHEET_PATH)
    s.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    s.add_argument("--predictions", type=Path, default=PREDICTIONS_PATH, help="used when it exists")
    s.add_argument("--out", type=Path, default=SUMMARY_PATH)
    s.add_argument("--positives-out", type=Path, default=POSITIVES_PATH)
    s.add_argument("--allow-problems", action="store_true", help="score a sheet that does not validate (progress view)")

    s = sub.add_parser("double", help="draw and write the reliability sheet, or compare a filled one")
    s.add_argument("--sheet", type=Path, default=SHEET_PATH, help="first (census) sheet")
    s.add_argument("--seed", type=int)
    s.add_argument("--date", type=_iso_date)
    s.add_argument("--n-negatives", type=int, default=20)
    s.add_argument("--out", type=Path, default=DOUBLE_SHEET_PATH)
    s.add_argument("--force", action="store_true")
    s.add_argument("--compare", type=Path, help="filled second sheet; writes the reliability table")
    s.add_argument("--reliability-out", type=Path, default=RELIABILITY_PATH)

    a = p.parse_args(argv)
    manifest = label.read_manifest(a.manifest)
    try:
        census = read_census(a.census)
        stale = missing_from_census(census, manifest)
        if stale:
            print(
                f"warning: {len(stale)} evaluable document(s) have no census row (rerun make templates): "
                + ", ".join(stale[:5]) + (" ..." if len(stale) > 5 else ""),
                file=sys.stderr,
            )
        if a.cmd == "draw":
            test_drawn = any(d.split == "test" for d in manifest)
            if not test_drawn and not a.before_test_draw:
                print("error: the test split is not drawn, so later test documents could be in the sample; "
                      "draw after draw-test, or pass --before-test-draw (the plan must say so)", file=sys.stderr)
                return 1
            record = draw_random_unflagged(census, manifest, a.seed, a.date, n=a.n)
            record = {"census_file": _rel(a.census), "census_sha256": _file_sha256(a.census), **record}
            misses = draw_locator_misses(census, manifest, a.seed, a.date)
            save_record("random_unflagged", record, a.sample_file, force=a.force)
            if misses is not None:
                save_record("locator_miss_sample", misses, a.sample_file, force=a.force)
                print(f"locator_miss_sample: {misses['n']} of {misses['misses']} misses, hash {misses['doc_id_hash']}")
            print(f"random_unflagged: {record['n']} of {record['pool']} sections, seed {a.seed}, hash {record['doc_id_hash']}")
            if not record["test_split_drawn"]:
                print("warning: the test split is not drawn; later test documents can be in the sample", file=sys.stderr)
            print(f"wrote {a.sample_file}")
            return 0
        if a.cmd == "sheet":
            if a.out.exists() and not a.force:
                print(f"error: {a.out} exists; pass --force to overwrite it", file=sys.stderr)
                return 1
            ids = sample_ids(a.sample_file)
            if not ids:
                print(f"error: no random_unflagged record in {a.sample_file}; run draw first", file=sys.stderr)
                return 1
            cap = READ_CAP_FALLBACK if a.fallback_cap else READ_CAP
            rows = build_sheet(census, manifest, ids, lambda d, r: load_review_context(d, r, a.text_dir),
                               miss_sample=miss_sample_ids(a.sample_file), cap=cap)
            write_csv(rows, sheet_columns(), a.out)
            counts = Counter(r["stratum"] for r in rows)
            print(f"wrote {len(rows)} rows to {a.out}: " + ", ".join(f"{s}={counts.get(s, 0)}" for s in STRATA))
            return 0
        if a.cmd == "validate":
            ids = sample_ids(a.sample_file) or None
            problems = validate(a.path, census, manifest, ids, miss_sample_ids(a.sample_file))
            for pr in problems:
                print(pr)
            print(f"{len(problems)} problem(s) in {a.path}")
            return 1 if problems else 0
        if a.cmd == "score":
            ids = sample_ids(a.sample_file) or None
            problems = validate(a.sheet, census, manifest, ids, miss_sample_ids(a.sample_file))
            if problems and not a.allow_problems:
                print(f"error: {len(problems)} problem(s) in {a.sheet}; run validate", file=sys.stderr)
                return 1
            t = truth(read_sheet(a.sheet), census, manifest)
            baseline = pd.read_csv(a.baseline)
            preds = baseline_predictions(baseline)
            if a.predictions.exists():
                preds += llm_predictions(a.predictions)
            summary = score(t, preds, negated_hits_by_regulator(baseline, t["doc_id"]))
            a.out.parent.mkdir(parents=True, exist_ok=True)
            summary.to_csv(a.out, index=False)
            positives_registry(t).to_csv(a.positives_out, index=False)
            n_pos = int(t["finding_present"].map(_true).astype(bool).sum())
            print(f"census: {len(t)} documents, {n_pos} positives, {int((t['truth_source'] == 'unread').sum())} unread")
            print(f"wrote {a.out} and {a.positives_out}")
            return 0
        if a.cmd == "double":
            first = read_sheet(a.sheet)
            if a.compare:
                table = reliability(first, read_sheet(a.compare))
                a.reliability_out.parent.mkdir(parents=True, exist_ok=True)
                table.to_csv(a.reliability_out, index=False)
                print(table.to_string(index=False))
                print(f"wrote {a.reliability_out}")
                return 0
            if a.seed is None or a.date is None:
                print("error: double needs --seed and --date (or --compare)", file=sys.stderr)
                return 2
            if a.out.exists() and not a.force:
                print(f"error: {a.out} exists; pass --force to overwrite it", file=sys.stderr)
                return 1
            t = truth(first, census, manifest)
            record = draw_double_read(t, a.seed, a.date, a.n_negatives)
            save_record("double_read", record, a.sample_file, force=a.force)
            write_csv(build_double_sheet(record, first), double_sheet_columns(), a.out)
            print(f"double_read: {record['n_positives']} positives + {record['n_negatives']} negatives; wrote {a.out}")
            return 0
    except CensusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
