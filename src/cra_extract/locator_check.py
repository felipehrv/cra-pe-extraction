"""Locator hand check: the 100-document check of design 6.10 (analysis plan, Metrics,
"Locator recall"), reported before any LLM metric.

Three steps, one CLI subcommand each:

1. :func:`draw` selects ``n`` (100) documents and :func:`save_record` writes the sample to
   ``config/locator_check_sample.yaml`` before any document is read: the seed, the draw date
   (passed on the command line, never computed), the pool rule, size and doc_id hash, the
   exclusions with their counts, the strata with their selection probabilities, the drawn
   doc_ids and their hash. The pool is every evaluable document of split ``corpus`` (sha256
   present, ``text_extractable`` true) minus

   - every document of a bank that has a document in split ``test`` or ``audit``
     (:data:`HELD_OUT_SPLITS`). The plan (Leakage) allows tuning on this check and never on
     test or audit documents, so no test document is in the pool or ever read by
     :func:`build_sheet`, and no bank is shared with those splits;
   - the tuning documents of ``config/tuning_documents.yaml``: the locator headings were
     tuned on them, so they would overstate recall.

   Stratification: one FRB document for each of the twelve Reserve Bank districts
   (:data:`DISTRICTS`; the district is approximated by the bank's state in
   ``data/index/frb.csv`` through :data:`STATE_DISTRICT`), drawn in district order, then a
   simple random sample of the remaining slots from the documents left in the pool (every
   regulator). Draws use :class:`cra_extract.strata.Sampler`: one
   ``numpy.random.default_rng(seed)``, one document at a time from candidates sorted by
   doc_id, and a drawn bank leaves the pool, so the same manifest, FRB index, tuning list and
   seed give the same sample. Recall is reported unweighted; the district stratum gives each
   FRB district one document whatever its size, which the record states.
2. :func:`build_sheet` writes ``data/gold/locator_check_sheet.csv``: one row per (doc_id,
   section) for the sections the plan names (:data:`CHECK_SECTIONS`: cover and rating,
   description of institution, the review section, the AA concentration and LTD tables) with
   the locator's found flag, start page and the first 120 characters of the located text
   (whitespace collapsed), and empty reader columns. The reader opens the PDF and fills, per
   row: ``present_in_pdf`` (yes or no: the section is printed in the PDF);
   ``locator_correct`` (yes or no: for a found section, yes when the located start is that
   section's heading at institution level on the right page; for a section not found, yes
   only when the PDF does not print it, and the cell may stay blank, :func:`outcome` derives
   it); ``correct_page`` (the page where the section starts, required when the section is
   printed and the locator is not correct); ``minutes``; ``notes``. The sheet refuses a
   document of a test or audit bank before reading its text, and refuses to overwrite a
   sheet that holds reader entries.
3. :func:`score` validates the filled sheet (:func:`validate`) and writes
   ``results/locator_recall.csv``: per section and per section type, overall and by
   regulator, recall = correct among sections printed in the PDF and precision = correct
   among found sections, each with a Wilson 95 percent interval (``stats.wilson_ci``); and the
   miss list ``results/locator_recall_misses.csv`` (``not_found``, ``wrong_location``,
   ``found_absent``). A found section at the wrong place is both a miss and a false find.

CLI::

    python -m cra_extract.locator_check draw --seed SEED --date YYYY-MM-DD [--n 100] [--force]
    python -m cra_extract.locator_check sheet [--out data/gold/locator_check_sheet.csv] [--force]
    python -m cra_extract.locator_check score [--sheet data/gold/locator_check_sheet.csv]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import sys
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from cra_extract.config import (
    CONFIG_DIR,
    CORPUS_PATH,
    GOLD_DIR,
    RESULTS_DIR,
    TEXT_DIR,
    TUNING_DOCS_PATH,
)
from cra_extract.label import pdf_path_for
from cra_extract.models import DocRecord, Section
from cra_extract.report import write_csv
from cra_extract.stats import wilson_ci
from cra_extract.strata import (
    FRB_INDEX_PATH,
    Sampler,
    bank_key,
    frb_states,
    load_tuning_documents,
    read_rows,
    state_of,
    to_records,
)

SAMPLE_PATH = CONFIG_DIR / "locator_check_sample.yaml"
SHEET_PATH = GOLD_DIR / "locator_check_sheet.csv"
RECALL_PATH = RESULTS_DIR / "locator_recall.csv"
MISSES_PATH = RESULTS_DIR / "locator_recall_misses.csv"

N_DOCS = 100
# Splits whose banks leave the pool; documents of these splits are never read here.
HELD_OUT_SPLITS = ("test", "audit")
REGULATORS = ("OCC", "FDIC", "FRB")
SNIPPET_CHARS = 120

# (section type of the plan, locator section of config/headings.yaml), in sheet order.
CHECK_SECTIONS: tuple[tuple[str, str], ...] = (
    ("cover_and_rating", "cover"),
    ("cover_and_rating", "rating"),
    ("description_of_institution", "description_of_institution"),
    ("review_section", "illegal_credit_practices_review"),
    ("aa_concentration_and_ltd", "aa_concentration"),
    ("aa_concentration_and_ltd", "ltd_ratio"),
)
SECTION_TYPES: tuple[str, ...] = tuple(dict.fromkeys(t for t, _ in CHECK_SECTIONS))
SECTION_TYPE_OF: dict[str, str] = {s: t for t, s in CHECK_SECTIONS}

# The twelve districts in Federal Reserve numbering (1 Boston to 12 San Francisco).
DISTRICTS: tuple[str, ...] = (
    "Boston", "New York", "Philadelphia", "Cleveland", "Richmond", "Atlanta",
    "Chicago", "St. Louis", "Minneapolis", "Kansas City", "Dallas", "San Francisco",
)

STATE_DISTRICT: dict[str, str] = {
    # 1 Boston
    "CT": "Boston", "MA": "Boston", "ME": "Boston", "NH": "Boston", "RI": "Boston", "VT": "Boston",
    # 2 New York
    "NY": "New York", "NJ": "New York", "PR": "New York", "VI": "New York",
    # 3 Philadelphia
    "PA": "Philadelphia", "DE": "Philadelphia",
    # 4 Cleveland
    "OH": "Cleveland",
    # 5 Richmond
    "DC": "Richmond", "MD": "Richmond", "NC": "Richmond", "SC": "Richmond", "VA": "Richmond", "WV": "Richmond",
    # 6 Atlanta
    "AL": "Atlanta", "FL": "Atlanta", "GA": "Atlanta", "LA": "Atlanta", "MS": "Atlanta", "TN": "Atlanta",
    # 7 Chicago
    "IA": "Chicago", "IL": "Chicago", "IN": "Chicago", "MI": "Chicago", "WI": "Chicago",
    # 8 St. Louis
    "AR": "St. Louis", "KY": "St. Louis", "MO": "St. Louis",
    # 9 Minneapolis
    "MN": "Minneapolis", "MT": "Minneapolis", "ND": "Minneapolis", "SD": "Minneapolis",
    # 10 Kansas City
    "CO": "Kansas City", "KS": "Kansas City", "NE": "Kansas City", "NM": "Kansas City", "OK": "Kansas City",
    "WY": "Kansas City",
    # 11 Dallas
    "TX": "Dallas",
    # 12 San Francisco
    "AK": "San Francisco", "AZ": "San Francisco", "CA": "San Francisco", "HI": "San Francisco",
    "ID": "San Francisco", "NV": "San Francisco", "OR": "San Francisco", "UT": "San Francisco",
    "WA": "San Francisco", "AS": "San Francisco", "GU": "San Francisco", "MP": "San Francisco",
}
"""State (USPS code) -> Federal Reserve district: an APPROXIMATION of the twelve districts.

Source: the list of the twelve Federal Reserve districts (Federal Reserve Act, section 2; the
Board of Governors' list "The Twelve Federal Reserve Districts", federalreserve.gov), with
each district's territory as its Reserve Bank describes it, read on 2026-10-08 through the
English Wikipedia articles on the twelve Reserve Banks, which quote those descriptions:
1 Boston: Maine, Massachusetts, New Hampshire, Rhode Island, Vermont, Connecticut except
Fairfield County. 2 New York: New York, the 12 northern counties of New Jersey, Fairfield
County (CT), Puerto Rico, the US Virgin Islands. 3 Philadelphia: eastern and central
Pennsylvania, the 9 southern counties of New Jersey, Delaware. 4 Cleveland: Ohio, western
Pennsylvania, eastern Kentucky, the northern panhandle of West Virginia. 5 Richmond: the
District of Columbia, Maryland, North Carolina, South Carolina, Virginia, West Virginia except
the northern panhandle. 6 Atlanta: Alabama, Florida, Georgia, the eastern two-thirds of
Tennessee, southern Louisiana, southern Mississippi. 7 Chicago: Iowa, northern Illinois,
northern Indiana, southern Wisconsin, the Lower Peninsula of Michigan. 8 St. Louis: Arkansas,
eastern Missouri, western Tennessee and parts of Illinois, Indiana, Kentucky and Mississippi.
9 Minneapolis: Minnesota, Montana, North Dakota, South Dakota, northwestern Wisconsin, the
Upper Peninsula of Michigan. 10 Kansas City: Colorado, Kansas, Nebraska, Oklahoma, Wyoming,
western Missouri, northern New Mexico. 11 Dallas: Texas, northern Louisiana, southern New
Mexico. 12 San Francisco: Alaska, Arizona, California, Hawaii, Idaho, Nevada, Oregon, Utah,
Washington, American Samoa, Guam, the Northern Mariana Islands.

Approximation: the manifest carries no district and the FRB index gives only the bank's
state, so each of the 14 states split between two districts (CT, NJ, PA, KY, WV, TN, MS, LA,
IL, IN, MI, WI, MO, NM) is assigned whole to one district, the one holding the larger part of
the state by the descriptions above; for Kentucky (St. Louis) and Mississippi (Atlanta) the
two parts are close in size and the assignment is a judgment call. A bank in the minority
part of a split state (Fairfield County, the Upper Peninsula, western Pennsylvania, ...)
counts for the wrong district. This grouping differs from ``strata.NY_KC_STATES``, the dev
draw's coarser "outside New York and Kansas City" test, which counts CT and NJ with New York.
"""

SHEET_INFO_COLUMNS = [
    "doc_id", "regulator", "method", "bank_name", "exam_date", "district", "stratum", "pdf_path",
    "section_type", "section", "found", "page", "located_text", "locator_note",
]
READER_COLUMNS = ["present_in_pdf", "locator_correct", "correct_page", "minutes", "notes"]
SHEET_COLUMNS = SHEET_INFO_COLUMNS + READER_COLUMNS

RECALL_COLUMNS = [
    "level", "regulator", "section_type", "section", "n_docs", "n_present", "n_found", "n_correct",
    "n_missed", "n_false_found", "recall", "recall_lo", "recall_hi",
    "precision", "precision_lo", "precision_hi", "note",
]
MISS_COLUMNS = [
    "doc_id", "regulator", "district", "section_type", "section", "miss_type", "found", "page",
    "correct_page", "notes",
]
MISS_TYPES = ("not_found", "wrong_location", "found_absent")
YES_NO = ("yes", "no")
PROBLEM_LIMIT = 50


class LocatorCheckError(ValueError):
    """The draw, the sheet or the score cannot proceed under the rules above."""


# ----------------------------------------------------------------------------- helpers


def ids_hash(doc_ids: Iterable[str]) -> str:
    """sha256 of the sorted doc_id list, one id per line (``label.split_hash`` convention)."""
    ids = sorted(doc_ids)
    return hashlib.sha256(("\n".join(ids) + "\n").encode("utf-8")).hexdigest()


def district_map_hash() -> str:
    """sha256 of :data:`STATE_DISTRICT` as sorted ``STATE=District`` lines (recorded with a
    draw, so a changed map shows up as a changed record)."""
    lines = [f"{s}={d}" for s, d in sorted(STATE_DISTRICT.items())]
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def is_evaluable(doc: DocRecord) -> bool:
    """A fetched (sha256 present), text-extractable document (plan, Exclusions)."""
    return bool((doc.sha256 or "").strip()) and doc.text_extractable is True


def district_of(doc: DocRecord, states: dict[tuple[str, str], str]) -> str | None:
    """Approximate Reserve Bank district of an FRB document (None for OCC, FDIC or an FRB
    document whose state is unknown or unmapped)."""
    if doc.regulator != "FRB":
        return None
    st = state_of(doc, states)
    return STATE_DISTRICT.get(st) if st else None


def held_out_banks(manifest: Sequence[DocRecord]) -> set[tuple[str, str]]:
    """Bank keys with a document in a held-out split (test, audit)."""
    return {bank_key(d) for d in manifest if d.split in HELD_OUT_SPLITS}


def check_pool(
    manifest: Sequence[DocRecord], tuning: Collection[str] = ()
) -> tuple[list[DocRecord], dict[str, Any]]:
    """(pool sorted by doc_id, exclusion counts) under the pool rule of the module docstring."""
    held = held_out_banks(manifest)
    tuning_ids = set(tuning)
    corpus = [d for d in manifest if d.split == "corpus"]
    evaluable = [d for d in corpus if is_evaluable(d)]
    of_held = [d for d in evaluable if bank_key(d) in held]
    tuned = [d for d in evaluable if bank_key(d) not in held and d.doc_id in tuning_ids]
    pool = sorted(
        (d for d in evaluable if bank_key(d) not in held and d.doc_id not in tuning_ids),
        key=lambda d: d.doc_id,
    )
    split_counts = Counter(d.split for d in manifest)
    exclusions = {
        "held_out_splits": list(HELD_OUT_SPLITS),
        "rule": (
            "no document of split test or audit is in the pool or read by the sheet; corpus "
            "documents of a bank with a test or audit document leave the pool; tuning documents "
            "leave the pool"
        ),
        "test_split_drawn": split_counts.get("test", 0) > 0,
        "split_counts": {s: int(split_counts[s]) for s in sorted(split_counts)},
        "test_documents_excluded": int(split_counts.get("test", 0)),
        "audit_documents_excluded": int(split_counts.get("audit", 0)),
        "held_out_banks": len(held),
        "corpus_documents_of_held_out_banks_removed": len(of_held),
        "corpus_documents_not_evaluable": len(corpus) - len(evaluable),
        "tuning_documents": len(tuning_ids),
        "tuning_documents_hash": ids_hash(tuning_ids),
        "tuning_documents_removed": len(tuned),
    }
    return pool, exclusions


# ----------------------------------------------------------------------------- draw


def draw(
    manifest: Sequence[DocRecord],
    seed: int,
    drawn_at: str,
    states: dict[tuple[str, str], str],
    tuning: Collection[str] = (),
    n: int = N_DOCS,
) -> dict[str, Any]:
    """The locator-check sample as a record for ``config/locator_check_sample.yaml``.

    One FRB document per district (:data:`DISTRICTS` order), then ``n - 12`` documents by
    simple random sampling from what is left of the pool; see the module docstring for the
    pool. Raises :class:`LocatorCheckError` when a district has no FRB document in the pool or
    the pool is too small (``strata.DrawError`` from the sampler is a ``ValueError`` too).
    """
    date.fromisoformat(drawn_at)
    if n < len(DISTRICTS):
        raise LocatorCheckError(f"n={n} is below the {len(DISTRICTS)} district documents")
    pool, exclusions = check_pool(manifest, tuning)
    if len(pool) < n:
        raise LocatorCheckError(f"{n} documents wanted, {len(pool)} in the pool")
    districts = {d.doc_id: district_of(d, states) for d in pool}
    sampler = Sampler(pool, seed)
    strata_rec: list[dict[str, Any]] = []
    for district in DISTRICTS:
        members = [d for d in pool if districts[d.doc_id] == district]
        cands = [d for d in sampler.pool if districts[d.doc_id] == district]
        if not cands:
            raise LocatorCheckError(f"district {district}: no FRB document left in the pool")
        sampler.draw(cands, 1, f"district:{district}", "frb_district")
        strata_rec.append({
            "name": f"district:{district}",
            "population": len(members),
            "pool": len(cands),
            "drawn": 1,
            "selection_probability": round(1 / len(cands), 6),
            "states": sorted(s for s, dist in STATE_DISTRICT.items() if dist == district),
        })
    n_fill = n - len(sampler.drawn)
    cands = list(sampler.pool)
    sampler.draw(cands, n_fill, "simple_random", "simple_random")
    strata_rec.append({
        "name": "simple_random",
        "population": len(pool),
        "pool": len(cands),
        "drawn": n_fill,
        "selection_probability": round(n_fill / len(cands), 6),
    })

    by_id = {d.doc_id: d for d in pool}
    docs = []
    for dd in sorted(sampler.drawn, key=lambda x: x.doc_id):
        doc = by_id[dd.doc_id]
        docs.append({
            "doc_id": dd.doc_id,
            "regulator": doc.regulator,
            "method": doc.method,
            "state": state_of(doc, states) if doc.regulator == "FRB" else None,
            "district": districts[dd.doc_id],
            "stratum": dd.stratum,
            "selection_probability": dd.selection_probability,
        })
    doc_ids = [d["doc_id"] for d in docs]
    if len(set(doc_ids)) != len(doc_ids):
        raise LocatorCheckError("a document was drawn twice")
    by_reg = Counter(d["regulator"] for d in docs)
    by_district = Counter(d["district"] for d in docs if d["district"])
    frb_pool = [d for d in pool if d.regulator == "FRB"]
    return {
        "seed": int(seed),
        "drawn_at": drawn_at,
        "n": n,
        "manifest": "config/corpus.csv",
        "pool_rule": (
            "documents of split corpus with sha256 present and text_extractable true, minus every "
            "document of a bank with a document in split test or audit, minus the tuning documents "
            "of config/tuning_documents.yaml; sorted by doc_id"
        ),
        "stratification": (
            "one FRB document per Reserve Bank district in district order (1 Boston to 12 San "
            "Francisco), then simple random sampling of the remaining slots from the documents left "
            "in the pool; strata.Sampler with numpy default_rng(seed), one document at a time, a "
            "drawn bank leaves the pool"
        ),
        "district_rule": (
            "approximation: the district is not in the manifest; an FRB document's district is the "
            "district of its state in data/index/frb.csv under locator_check.STATE_DISTRICT, which "
            "assigns each state split between two districts to the district holding its larger part"
        ),
        "district_map_sha256": district_map_hash(),
        "exclusions": exclusions,
        "pool": len(pool),
        "pool_doc_id_hash": ids_hash(d.doc_id for d in pool),
        "frb_pool": len(frb_pool),
        "frb_pool_without_district": sum(1 for d in frb_pool if districts[d.doc_id] is None),
        "strata": strata_rec,
        "by_regulator": {r: int(by_reg.get(r, 0)) for r in REGULATORS},
        "by_district": {dist: int(by_district.get(dist, 0)) for dist in DISTRICTS},
        "docs": docs,
        "doc_ids": doc_ids,
        "doc_id_hash": ids_hash(doc_ids),
        "notes": [
            "recall and precision are reported unweighted; the district stratum gives each FRB "
            "district one document whatever its size",
            "these documents are read while tuning (plan, Leakage): list them in "
            "config/tuning_documents.yaml before draw-audit so that their banks leave the audit pool",
            f"documents removed from the pool by bank exclusivity during the draw: {sampler.banks_removed}",
        ],
    }


def load_record(path: Path = SAMPLE_PATH) -> dict[str, Any] | None:
    """The ``draw`` record of the sample file, or None when no draw is recorded."""
    if not Path(path).exists():
        return None
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("draw")


def save_record(record: Mapping[str, Any], path: Path = SAMPLE_PATH, force: bool = False) -> None:
    """Write the draw record; an existing record is replaced only with ``force`` (the sample
    is fixed before any document is read)."""
    path = Path(path)
    if load_record(path) is not None and not force:
        raise LocatorCheckError(f"{path} already holds a draw; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Written by python -m cra_extract.locator_check draw; do not edit by hand.\n")
        yaml.safe_dump({"version": 1, "draw": dict(record)}, fh, sort_keys=False, allow_unicode=True, width=100)


# ----------------------------------------------------------------------------- sheet


SectionLoader = Callable[[str, str], "dict[str, Section] | None"]


def load_sections(doc_id: str, regulator: str, text_dir: Path = TEXT_DIR) -> dict[str, Section] | None:
    """Sections ``locate.locate_all`` finds in ``{doc_id}.pages.json`` (None without page text)."""
    from cra_extract import locate

    path = Path(text_dir) / f"{doc_id}.pages.json"
    if not path.exists():
        return None
    found, _missing = locate.locate_all(doc_id, locate.load_pages_json(path), regulator)
    return found


def snippet(text: str, n: int = SNIPPET_CHARS) -> str:
    """The first ``n`` characters of ``text`` with whitespace runs collapsed to one space."""
    return " ".join(text.split())[:n]


def build_sheet(
    record: Mapping[str, Any],
    manifest: Sequence[DocRecord],
    loader: SectionLoader | None = None,
) -> list[dict[str, str]]:
    """Rows of the hand-check sheet, by doc_id and then :data:`CHECK_SECTIONS` order.

    Every sampled document is checked against the current manifest before any text is read:
    a document missing from the manifest, of split test or audit, or of a bank with a test or
    audit document raises :class:`LocatorCheckError` and nothing is loaded.
    """
    loader = loader or load_sections
    by_id = {d.doc_id: d for d in manifest}
    doc_ids = sorted(record["doc_ids"])
    unknown = [d for d in doc_ids if d not in by_id]
    if unknown:
        raise LocatorCheckError(f"sampled doc_ids not in the manifest: {unknown}")
    held = held_out_banks(manifest)
    blocked = [d for d in doc_ids if by_id[d].split in HELD_OUT_SPLITS or bank_key(by_id[d]) in held]
    if blocked:
        raise LocatorCheckError(
            "sampled documents belong to a test or audit bank; no text was read (redraw the "
            f"check): {blocked}"
        )
    meta = {doc["doc_id"]: doc for doc in record.get("docs", [])}
    rows: list[dict[str, str]] = []
    for doc_id in doc_ids:
        doc = by_id[doc_id]
        found = loader(doc_id, doc.regulator)
        for section_type, section in CHECK_SECTIONS:
            sec = found.get(section) if found is not None else None
            row = dict.fromkeys(SHEET_COLUMNS, "")
            row.update(
                doc_id=doc_id,
                regulator=doc.regulator,
                method=doc.method,
                bank_name=doc.bank_name,
                exam_date=doc.exam_date.isoformat() if doc.exam_date else "",
                district=meta.get(doc_id, {}).get("district") or "",
                stratum=meta.get(doc_id, {}).get("stratum") or "",
                pdf_path=pdf_path_for(doc_id),
                section_type=section_type,
                section=section,
                found="true" if sec is not None else "false",
                page=str(sec.page_start) if sec is not None else "",
                located_text=snippet(sec.text) if sec is not None else "",
                locator_note="no page text in data/text; the locator did not run" if found is None else "",
            )
            rows.append(row)
    return rows


def read_sheet(path: Path) -> list[dict[str, str]]:
    """The sheet as string dicts (missing cells empty)."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in SHEET_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise LocatorCheckError(f"{path}: missing columns {missing}")
        return [{k: (v or "") for k, v in r.items() if k is not None} for r in reader]


def has_reader_entries(path: Path) -> bool:
    """True when an existing sheet has any reader cell filled."""
    if not Path(path).exists():
        return False
    with open(path, newline="", encoding="utf-8") as fh:
        return any((r.get(c) or "").strip() for r in csv.DictReader(fh) for c in READER_COLUMNS)


def write_sheet(rows: Sequence[Mapping[str, str]], path: Path = SHEET_PATH, force: bool = False) -> Path:
    """Write the sheet; refuses to overwrite reader entries unless ``force``."""
    path = Path(path)
    if has_reader_entries(path) and not force:
        raise LocatorCheckError(f"{path} already holds reader entries; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in SHEET_COLUMNS})
    return path


# ----------------------------------------------------------------------------- score


def _yes_no(v: str) -> str:
    return v.strip().lower()


def _int_cell(v: str) -> int | None:
    try:
        return int(v.strip())
    except (TypeError, ValueError):
        return None


def outcome(row: Mapping[str, str]) -> tuple[bool, bool, bool]:
    """(present, found, correct) of a valid filled row. A blank ``locator_correct`` on a
    section not found means correct exactly when the section is not printed."""
    present = _yes_no(row["present_in_pdf"]) == "yes"
    found = row["found"].strip().lower() == "true"
    lc = _yes_no(row["locator_correct"])
    correct = (lc == "yes") if lc else (not found and not present)
    return present, found, correct


def validate(rows: Sequence[Mapping[str, str]], record: Mapping[str, Any] | None = None) -> list[str]:
    """Problems of a filled sheet (empty when it can be scored). Line numbers count the header
    as line 1."""
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for i, r in enumerate(rows):
        where = f"line {i + 2} ({r.get('doc_id', '')}, {r.get('section', '')})"
        key = (r.get("doc_id", ""), r.get("section", ""))
        if key in seen:
            problems.append(f"{where}: duplicate row")
        seen.add(key)
        section = r.get("section", "")
        if section not in SECTION_TYPE_OF:
            problems.append(f"{where}: unknown section")
        elif r.get("section_type", "") != SECTION_TYPE_OF[section]:
            problems.append(f"{where}: section_type should be {SECTION_TYPE_OF[section]}")
        found = r.get("found", "").strip().lower()
        if found not in ("true", "false"):
            problems.append(f"{where}: found must be true or false")
            continue
        present = _yes_no(r.get("present_in_pdf", ""))
        if present not in YES_NO:
            problems.append(f"{where}: present_in_pdf must be yes or no")
            continue
        lc = _yes_no(r.get("locator_correct", ""))
        if lc not in (*YES_NO, ""):
            problems.append(f"{where}: locator_correct must be yes, no or blank")
            continue
        if lc == "" and found == "true":
            problems.append(f"{where}: locator_correct is required on a found section")
            continue
        if found == "true" and present == "no" and lc == "yes":
            problems.append(f"{where}: a found section the PDF does not print cannot be correct")
        if found == "false" and present == "yes" and lc == "yes":
            problems.append(f"{where}: a printed section the locator did not find cannot be correct")
        if found == "false" and present == "no" and lc == "no":
            problems.append(f"{where}: a section neither printed nor found is a correct rejection")
        correct = (lc == "yes") if lc else (found == "false" and present == "no")
        cp = r.get("correct_page", "").strip()
        if cp:
            page = _int_cell(cp)
            if page is None or page < 1:
                problems.append(f"{where}: correct_page must be a page number")
            elif present == "no":
                problems.append(f"{where}: correct_page given for a section the PDF does not print")
            elif lc == "yes" and found == "true" and page != _int_cell(r.get("page", "")):
                problems.append(f"{where}: correct_page differs from the located page of a correct row")
        elif present == "yes" and not correct:
            problems.append(f"{where}: correct_page is required when a printed section is missed or misplaced")
        minutes = r.get("minutes", "").strip()
        if minutes:
            try:
                ok = float(minutes) >= 0 and math.isfinite(float(minutes))
            except ValueError:
                ok = False
            if not ok:
                problems.append(f"{where}: minutes must be a non-negative number")
    if record is not None:
        want = {(d, s) for d in record["doc_ids"] for _t, s in CHECK_SECTIONS}
        missing = sorted(want - seen)
        extra = sorted(seen - want)
        if missing:
            problems.append(f"{len(missing)} sampled (doc_id, section) rows missing, e.g. {missing[:3]}")
        if extra:
            problems.append(f"{len(extra)} rows outside the recorded sample, e.g. {extra[:3]}")
    return problems


def _recall_row(level: str, regulator: str, section_type: str, section: str,
                rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    n_present = n_found = n_correct = 0
    for r in rows:
        present, found, correct = outcome(r)
        n_present += present
        n_found += found
        n_correct += present and found and correct
    rec_lo, rec_hi = wilson_ci(n_correct, n_present)
    prec_lo, prec_hi = wilson_ci(n_correct, n_found)
    note = ""
    if level == "section_type" and sum(1 for t, _ in CHECK_SECTIONS if t == section_type) > 1:
        note = "two sections per document pooled; the interval treats sections as independent"
    return {
        "level": level, "regulator": regulator, "section_type": section_type, "section": section,
        "n_docs": len({r["doc_id"] for r in rows}), "n_present": n_present, "n_found": n_found,
        "n_correct": n_correct, "n_missed": n_present - n_correct, "n_false_found": n_found - n_correct,
        "recall": n_correct / n_present if n_present else math.nan, "recall_lo": rec_lo, "recall_hi": rec_hi,
        "precision": n_correct / n_found if n_found else math.nan, "precision_lo": prec_lo, "precision_hi": prec_hi,
        "note": note,
    }


def score(rows: Sequence[Mapping[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(recall rows, miss rows) of a valid filled sheet (run :func:`validate` first).

    Recall rows: level ``section`` (one per locator section) and ``section_type`` (the plan's
    four types), each for regulator ``all`` and then OCC, FDIC and FRB (regulators without
    rows are left out), in :data:`CHECK_SECTIONS` order.
    """
    regs = ["all", *[r for r in REGULATORS if any(x["regulator"] == r for x in rows)]]
    out: list[dict[str, Any]] = []
    for reg in regs:
        sub = [r for r in rows if reg == "all" or r["regulator"] == reg]
        for section_type, section in CHECK_SECTIONS:
            part = [r for r in sub if r["section"] == section]
            out.append(_recall_row("section", reg, section_type, section, part))
    for reg in regs:
        sub = [r for r in rows if reg == "all" or r["regulator"] == reg]
        for section_type in SECTION_TYPES:
            part = [r for r in sub if r["section_type"] == section_type]
            out.append(_recall_row("section_type", reg, section_type, "all", part))
    order = {s: i for i, (_t, s) in enumerate(CHECK_SECTIONS)}
    misses: list[dict[str, Any]] = []
    for r in sorted(rows, key=lambda x: (x["doc_id"], order.get(x["section"], len(order)))):
        present, found, correct = outcome(r)
        if present and not found:
            kind = "not_found"
        elif present and found and not correct:
            kind = "wrong_location"
        elif found and not present:
            kind = "found_absent"
        else:
            continue
        misses.append({
            "doc_id": r["doc_id"], "regulator": r["regulator"], "district": r.get("district", ""),
            "section_type": r["section_type"], "section": r["section"], "miss_type": kind,
            "found": r["found"].strip().lower(), "page": r.get("page", ""),
            "correct_page": r.get("correct_page", "").strip(), "notes": r.get("notes", ""),
        })
    return out, misses


def write_score(
    rows: Sequence[Mapping[str, str]], recall_path: Path = RECALL_PATH, misses_path: Path = MISSES_PATH,
    record: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate, score and write both outputs (deterministic: fixed row order, four decimals)."""
    problems = validate(rows, record)
    if problems:
        shown = problems[:PROBLEM_LIMIT]
        more = f"\n... and {len(problems) - len(shown)} further problems" if len(problems) > len(shown) else ""
        raise LocatorCheckError("sheet not ready to score:\n" + "\n".join(shown) + more)
    recall, misses = score(rows)
    for p in (recall_path, misses_path):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    write_csv(Path(recall_path), RECALL_COLUMNS, recall)
    write_csv(Path(misses_path), MISS_COLUMNS, misses)
    return recall, misses


# ----------------------------------------------------------------------------- CLI


def _iso_date(s: str) -> str:
    return date.fromisoformat(s).isoformat()


def _manifest(path: Path) -> list[DocRecord]:
    _columns, rows = read_rows(path)
    return to_records(rows)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code (1 on a refused step)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    common.add_argument("--sample", type=Path, default=SAMPLE_PATH, help="draw record (YAML)")
    p = argparse.ArgumentParser(prog="python -m cra_extract.locator_check", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("draw", parents=[common], help="draw the 100 documents and record the sample")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--date", type=_iso_date, required=True, help="drawn-at date (ISO), recorded as given")
    s.add_argument("--n", type=int, default=N_DOCS)
    s.add_argument("--frb-index", type=Path, default=FRB_INDEX_PATH, help="FRB index with the state column")
    s.add_argument("--tuning", type=Path, default=TUNING_DOCS_PATH, help="tuning documents left out of the pool")
    s.add_argument("--force", action="store_true", help="replace an existing draw record")
    s = sub.add_parser("sheet", parents=[common], help="write the hand-check sheet from the recorded sample")
    s.add_argument("--out", type=Path, default=SHEET_PATH)
    s.add_argument("--text-dir", type=Path, default=TEXT_DIR, help="folder with {doc_id}.pages.json")
    s.add_argument("--force", action="store_true", help="overwrite a sheet that holds reader entries")
    s = sub.add_parser("score", parents=[common], help="recall and precision from the filled sheet")
    s.add_argument("--sheet", type=Path, default=SHEET_PATH)
    s.add_argument("--out", type=Path, default=RECALL_PATH)
    s.add_argument("--misses", type=Path, default=MISSES_PATH)
    a = p.parse_args(argv)

    try:
        if a.cmd == "draw":
            record = draw(
                _manifest(a.manifest), a.seed, a.date, frb_states(a.frb_index),
                tuning=sorted(load_tuning_documents(a.tuning)), n=a.n,
            )
            save_record(record, a.sample, force=a.force)
            print(f"locator check: {record['n']} documents, seed {record['seed']}, drawn {record['drawn_at']}, "
                  f"pool {record['pool']}, hash {record['doc_id_hash']}")
            print("  by regulator: " + ", ".join(f"{k}={v}" for k, v in record["by_regulator"].items()))
            print("  exclusions: " + ", ".join(
                f"{k}={record['exclusions'][k]}" for k in (
                    "test_documents_excluded", "audit_documents_excluded",
                    "corpus_documents_of_held_out_banks_removed", "tuning_documents_removed")))
            print(f"wrote {a.sample}")
            return 0
        record = load_record(a.sample)
        if record is None:
            print(f"error: no draw recorded in {a.sample}; run draw first", file=sys.stderr)
            return 1
        if a.cmd == "sheet":
            text_dir = a.text_dir
            rows = build_sheet(record, _manifest(a.manifest), lambda d, r: load_sections(d, r, text_dir))
            write_sheet(rows, a.out, force=a.force)
            n_found = sum(r["found"] == "true" for r in rows)
            print(f"wrote {a.out}: {len(rows)} rows, {n_found} sections found by the locator")
            return 0
        rows = read_sheet(a.sheet)
        recall, misses = write_score(rows, a.out, a.misses, record)
        for r in recall:
            if r["regulator"] == "all":
                print(f"  {r['level']} {r['section'] if r['level'] == 'section' else r['section_type']}: "
                      f"recall {r['n_correct']}/{r['n_present']}, precision {r['n_correct']}/{r['n_found']}")
        print(f"wrote {a.out} and {a.misses} ({len(misses)} misses)")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
