"""Tests for cra_extract.locator_check (design 6.10; plan, Metrics, "Locator recall").

The draw runs on a synthetic manifest: OCC and FDIC corpus documents, two FRB corpus
documents per Reserve Bank district (one state of the district each), test, audit, dev and
excluded documents, corpus documents of a test bank and of an audit bank, a non-evaluable
corpus document, tuning documents, a second exam of one bank and an FRB document with no
state. The sheet runs on fake locator output (and once on a fixture sample's text); the
score runs on hand-filled sheets whose recall and precision are computed by hand below.
Nothing here reads a document of split test.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from cra_extract import cli, locate
from cra_extract import locator_check as lc
from cra_extract.config import CORPUS_PATH, INDEX_DIR, TUNING_DOCS_PATH
from cra_extract.models import DocRecord, Section, make_doc_id
from cra_extract.stats import wilson_ci
from cra_extract.strata import bank_key, frb_states, load_tuning_documents, read_rows, to_records

SEED = 20261009
DRAWN_AT = "2026-10-09"
MAKEFILE = Path(__file__).parent.parent / "Makefile"
SAMPLES = Path(__file__).parent / "fixtures" / "samples"

# One state per district for the synthetic FRB documents.
DISTRICT_STATE = {
    "Boston": "MA", "New York": "NY", "Philadelphia": "PA", "Cleveland": "OH", "Richmond": "VA",
    "Atlanta": "GA", "Chicago": "IL", "St. Louis": "AR", "Minneapolis": "MN", "Kansas City": "KS",
    "Dallas": "TX", "San Francisco": "CA",
}
STATES_50_AND_DC = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


def _doc(regulator: str, bank_id: str, i: int, split: str = "corpus", **kw) -> DocRecord:
    exam = date(2024, 1, 1) + timedelta(days=i)
    kw.setdefault("sha256", f"{i:064x}")
    kw.setdefault("text_extractable", True)
    return DocRecord(
        doc_id=make_doc_id(regulator, bank_id, exam), regulator=regulator, bank_id=bank_id,
        bank_name=f"{regulator} bank {bank_id}", exam_date=exam, method="small" if i % 2 else "isb",
        url=f"https://example.invalid/{bank_id}.pdf", split=split, **kw,
    )


class World:
    """The synthetic manifest, FRB states and tuning list, with the documents of each role."""

    def __init__(self) -> None:
        self.docs: list[DocRecord] = []
        self.states: dict[tuple[str, str], str] = {}
        i = 0
        for reg in ("OCC", "FDIC"):
            for k in range(60):
                i += 1
                self.docs.append(_doc(reg, f"{reg[0]}{k}", i))
        for district, st in DISTRICT_STATE.items():
            for _k in range(2):
                i += 1
                d = _doc("FRB", f"9{len(self.states):03d}", i)
                self.docs.append(d)
                self.states[(d.bank_id, d.exam_date.isoformat())] = st
                assert lc.STATE_DISTRICT[st] == district
        i += 1
        self.frb_no_state = _doc("FRB", "8000", i)
        i += 1
        self.test_doc = _doc("OCC", "T1", i, split="test")
        i += 1
        self.test_bank_corpus = _doc("OCC", "T1", i)  # earlier exam of the test bank
        i += 1
        self.test_frb = _doc("FRB", "8001", i, split="test")
        i += 1
        self.audit_doc = _doc("FDIC", "A1", i, split="audit")
        i += 1
        self.audit_bank_corpus = _doc("FDIC", "A1", i)
        i += 1
        self.dev_doc = _doc("FDIC", "D1", i, split="dev")
        i += 1
        self.excluded_doc = _doc("OCC", "X1", i, split="excluded", text_extractable=False)
        i += 1
        self.not_evaluable = _doc("OCC", "N1", i, text_extractable=False)
        i += 1
        self.no_sha = _doc("FDIC", "N2", i, sha256=None)
        i += 1
        self.second_exam = _doc("OCC", "O0", i)  # same bank as OCC document O0
        self.docs += [self.frb_no_state, self.test_doc, self.test_bank_corpus, self.test_frb, self.audit_doc,
                      self.audit_bank_corpus, self.dev_doc, self.excluded_doc, self.not_evaluable, self.no_sha,
                      self.second_exam]
        self.states[(self.test_frb.bank_id, self.test_frb.exam_date.isoformat())] = "NY"
        self.tuning = [self.docs[0].doc_id, self.docs[60].doc_id]  # one OCC, one FDIC corpus document

    @property
    def never(self) -> set[str]:
        """doc_ids that must never be drawn."""
        return {d.doc_id for d in (self.test_doc, self.test_bank_corpus, self.test_frb, self.audit_doc,
                                   self.audit_bank_corpus, self.dev_doc, self.excluded_doc, self.not_evaluable,
                                   self.no_sha)} | set(self.tuning)


@pytest.fixture
def world() -> World:
    return World()


# ----------------------------------------------------------------------------- district map


def test_district_map_has_twelve_districts_and_every_state():
    assert len(lc.DISTRICTS) == 12 and len(set(lc.DISTRICTS)) == 12
    assert set(lc.STATE_DISTRICT.values()) == set(lc.DISTRICTS)
    assert STATES_50_AND_DC <= set(lc.STATE_DISTRICT)
    # Split states go to the district holding their larger part.
    assert (lc.STATE_DISTRICT["CT"], lc.STATE_DISTRICT["NJ"], lc.STATE_DISTRICT["PA"]) == ("Boston", "New York", "Philadelphia")
    assert (lc.STATE_DISTRICT["MI"], lc.STATE_DISTRICT["WI"], lc.STATE_DISTRICT["MO"]) == ("Chicago", "Chicago", "St. Louis")
    source = Path(lc.__file__).read_text(encoding="utf-8")
    assert "Twelve Federal Reserve Districts" in source and "Approximation:" in source
    assert "\u2014" not in source


# ----------------------------------------------------------------------------- draw


def test_draw_takes_one_frb_document_per_district_then_random(world: World):
    rec = lc.draw(world.docs, SEED, DRAWN_AT, world.states, world.tuning)
    assert rec["n"] == 100 and len(rec["doc_ids"]) == 100 and rec["doc_ids"] == sorted(set(rec["doc_ids"]))
    by_id = {d.doc_id: d for d in world.docs}
    for district in lc.DISTRICTS:
        stratum = [d for d in rec["docs"] if d["stratum"] == f"district:{district}"]
        assert len(stratum) == 1 and stratum[0]["district"] == district and stratum[0]["regulator"] == "FRB"
        assert lc.district_of(by_id[stratum[0]["doc_id"]], world.states) == district
        assert rec["by_district"][district] >= 1
    names = [s["name"] for s in rec["strata"]]
    assert names == [f"district:{d}" for d in lc.DISTRICTS] + ["simple_random"]
    fill = rec["strata"][-1]
    assert fill["drawn"] == 88 and fill["selection_probability"] == round(88 / fill["pool"], 6)
    assert rec["strata"][0]["population"] == 2 and rec["strata"][0]["selection_probability"] == 0.5
    assert sum(rec["by_regulator"].values()) == 100
    assert rec["doc_id_hash"] == lc.ids_hash(rec["doc_ids"])
    assert rec["district_map_sha256"] == lc.district_map_hash()
    assert rec["frb_pool_without_district"] == 1  # the FRB document with no state stays in the random pool


def test_draw_excludes_test_and_audit_banks_tuning_and_non_evaluable(world: World):
    pool, excl = lc.check_pool(world.docs, world.tuning)
    pool_ids = {d.doc_id for d in pool}
    assert not pool_ids & world.never
    assert world.second_exam.doc_id in pool_ids and world.frb_no_state.doc_id in pool_ids
    assert all(d.split == "corpus" for d in pool)
    assert excl["test_documents_excluded"] == 2 and excl["audit_documents_excluded"] == 1
    assert excl["held_out_banks"] == 3 and excl["corpus_documents_of_held_out_banks_removed"] == 2
    assert excl["corpus_documents_not_evaluable"] == 2
    assert (excl["tuning_documents"], excl["tuning_documents_removed"]) == (2, 2)
    assert excl["tuning_documents_hash"] == lc.ids_hash(world.tuning)
    assert excl["test_split_drawn"] is True and "test" in excl["held_out_splits"]
    for seed in range(5):
        rec = lc.draw(world.docs, seed, DRAWN_AT, world.states, world.tuning)
        assert not set(rec["doc_ids"]) & world.never
        assert rec["exclusions"] == excl
        assert rec["pool"] == len(pool) and rec["pool_doc_id_hash"] == lc.ids_hash(pool_ids)
        # A bank never appears twice in the sample.
        banks = [bank_key(d) for d in world.docs if d.doc_id in set(rec["doc_ids"])]
        assert len(banks) == len(set(banks))


def test_draw_is_deterministic_and_seed_sensitive(world: World, tmp_path: Path):
    a = lc.draw(world.docs, SEED, DRAWN_AT, world.states, world.tuning)
    b = lc.draw(list(reversed(world.docs)), SEED, DRAWN_AT, world.states, list(reversed(world.tuning)))
    assert a == b
    lc.save_record(a, tmp_path / "a.yaml")
    lc.save_record(b, tmp_path / "b.yaml")
    assert (tmp_path / "a.yaml").read_bytes() == (tmp_path / "b.yaml").read_bytes()
    assert lc.load_record(tmp_path / "a.yaml") == a
    c = lc.draw(world.docs, SEED + 1, DRAWN_AT, world.states, world.tuning)
    assert c["doc_ids"] != a["doc_ids"]
    with pytest.raises(lc.LocatorCheckError, match="already holds a draw"):
        lc.save_record(c, tmp_path / "a.yaml")
    lc.save_record(c, tmp_path / "a.yaml", force=True)
    assert lc.load_record(tmp_path / "a.yaml")["seed"] == SEED + 1


def test_draw_refuses_a_district_without_documents_and_a_small_pool(world: World):
    states = {k: v for k, v in world.states.items() if v != "TX"}  # Dallas documents lose their state
    with pytest.raises(lc.LocatorCheckError, match="district Dallas"):
        lc.draw(world.docs, SEED, DRAWN_AT, states, world.tuning)
    with pytest.raises(lc.LocatorCheckError, match="in the pool"):
        lc.draw(world.docs, SEED, DRAWN_AT, world.states, world.tuning, n=1000)
    with pytest.raises(lc.LocatorCheckError, match="district documents"):
        lc.draw(world.docs, SEED, DRAWN_AT, world.states, world.tuning, n=5)
    with pytest.raises(ValueError):
        lc.draw(world.docs, SEED, "10/09/2026", world.states, world.tuning)


# ----------------------------------------------------------------------------- sheet


def _section(name: str, doc_id: str, page: int, text: str) -> Section:
    return Section(name=name, doc_id=doc_id, page_start=page, page_end=page, char_start=0, char_end=len(text), text=text)


class FakeLocator:
    """Every section found on page 2 except ``ltd_ratio``; no text for one document."""

    def __init__(self, no_text: str | None = None) -> None:
        self.calls: list[str] = []
        self.no_text = no_text

    def __call__(self, doc_id: str, regulator: str) -> dict[str, Section] | None:
        self.calls.append(doc_id)
        if doc_id == self.no_text:
            return None
        long = "HEADING\n\n  " + "word " * 60
        return {name: _section(name, doc_id, 2, long) for _t, name in lc.CHECK_SECTIONS if name != "ltd_ratio"}


def test_sheet_rows_found_flags_and_snippets(world: World):
    rec = lc.draw(world.docs, SEED, DRAWN_AT, world.states, world.tuning, n=14)
    loader = FakeLocator(no_text=rec["doc_ids"][0])
    rows = lc.build_sheet(rec, world.docs, loader)
    assert len(rows) == 14 * len(lc.CHECK_SECTIONS) and loader.calls == rec["doc_ids"]
    assert list(rows[0]) == lc.SHEET_COLUMNS
    assert [r["section"] for r in rows[:6]] == [s for _t, s in lc.CHECK_SECTIONS]
    first = [r for r in rows if r["doc_id"] == rec["doc_ids"][0]]
    assert all(r["found"] == "false" and r["page"] == "" and "no page text" in r["locator_note"] for r in first)
    second = {r["section"]: r for r in rows if r["doc_id"] == rec["doc_ids"][1]}
    assert second["cover"]["found"] == "true" and second["cover"]["page"] == "2"
    assert second["ltd_ratio"]["found"] == "false" and second["ltd_ratio"]["locator_note"] == ""
    text = second["cover"]["located_text"]
    assert len(text) == 120 and text.startswith("HEADING word word") and "  " not in text
    assert all(r[c] == "" for r in rows for c in lc.READER_COLUMNS)
    frb = [r for r in rows if r["stratum"].startswith("district:")]
    assert len(frb) == 12 * 6 and all(r["district"] and r["regulator"] == "FRB" for r in frb)
    assert rows[0]["pdf_path"] == f"data/pdfs/{rows[0]['doc_id']}.pdf"


def test_sheet_never_reads_a_test_document(world: World):
    rec = lc.draw(world.docs, SEED, DRAWN_AT, world.states, world.tuning, n=14)
    target = rec["doc_ids"][3]
    moved = [d.model_copy(update={"split": "test"}) if d.doc_id == target else d for d in world.docs]
    loader = FakeLocator()
    with pytest.raises(lc.LocatorCheckError, match="test or audit bank; no text was read"):
        lc.build_sheet(rec, moved, loader)
    assert loader.calls == []
    # A later test document of a sampled bank blocks the sheet too.
    by_id = {d.doc_id: d for d in world.docs}
    later = _doc(by_id[target].regulator, by_id[target].bank_id, 999, split="test")
    with pytest.raises(lc.LocatorCheckError):
        lc.build_sheet(rec, [*world.docs, later], loader)
    assert loader.calls == []
    with pytest.raises(lc.LocatorCheckError, match="not in the manifest"):
        lc.build_sheet(rec, [d for d in world.docs if d.doc_id != target], loader)


def test_load_sections_runs_the_locator_on_page_text(tmp_path: Path):
    txt = SAMPLES / "FDIC_6078_OneAmericanBank_2024_ISB_NI.txt"
    pages = locate.pages_from_text(txt.read_text(encoding="utf-8"))
    (tmp_path / "FDIC_6078_20240805.pages.json").write_text(
        json.dumps([{"number": p.number, "text": p.text} for p in pages]), encoding="utf-8")
    found = lc.load_sections("FDIC_6078_20240805", "FDIC", tmp_path)
    assert found is not None and {"cover", "rating", "description_of_institution"} <= set(found)
    assert found["cover"].page_start == 1
    assert lc.load_sections("FDIC_1_20240101", "FDIC", tmp_path) is None


def test_write_sheet_refuses_to_overwrite_reader_entries(world: World, tmp_path: Path):
    rec = lc.draw(world.docs, SEED, DRAWN_AT, world.states, world.tuning, n=12)
    rows = lc.build_sheet(rec, world.docs, FakeLocator())
    path = tmp_path / "sheet.csv"
    lc.write_sheet(rows, path)
    first = path.read_bytes()
    lc.write_sheet(rows, path)  # no reader entries yet: rewriting is allowed and identical
    assert path.read_bytes() == first
    filled = lc.read_sheet(path)
    filled[0]["present_in_pdf"] = "yes"
    lc.write_sheet(filled, path)
    with pytest.raises(lc.LocatorCheckError, match="reader entries"):
        lc.write_sheet(rows, path)
    lc.write_sheet(rows, path, force=True)
    assert path.read_bytes() == first


# ----------------------------------------------------------------------------- score


def _row(doc_id: str, section: str, found: bool, present: str, correct: str = "", page: str = "",
         correct_page: str = "", minutes: str = "") -> dict[str, str]:
    regulator = doc_id.split("_")[0]
    return {
        "doc_id": doc_id, "regulator": regulator, "method": "small", "bank_name": "b", "exam_date": "2024-01-01",
        "district": "Kansas City" if regulator == "FRB" else "", "stratum": "simple_random",
        "pdf_path": f"data/pdfs/{doc_id}.pdf", "section_type": lc.SECTION_TYPE_OF[section], "section": section,
        "found": "true" if found else "false", "page": (page or "2") if found else "", "located_text": "x",
        "locator_note": "", "present_in_pdf": present, "locator_correct": correct, "correct_page": correct_page,
        "minutes": minutes, "notes": "",
    }


DOCS4 = ("FDIC_4_20240101", "FRB_3_20240101", "OCC_1_20240101", "OCC_2_20240101")


def filled_sheet() -> list[dict[str, str]]:
    """Hand-filled sheet. Hand counts per section (present, found, correct):
    cover 4/4/4; rating 4/3/2 (OCC_2 found at the wrong place, FRB_3 not found);
    description 3/3/3 (FDIC_4 neither printed nor found); review 3/4/3 (OCC_1 found but not
    printed); aa_concentration 4/4/4; ltd_ratio 4/3/3 (FRB_3 not found)."""
    rows = []
    for d in DOCS4:
        rows.append(_row(d, "cover", True, "yes", "yes", minutes="0.5"))
        if d == "OCC_2_20240101":
            rows.append(_row(d, "rating", True, "yes", "no", page="2", correct_page="4"))
        elif d == "FRB_3_20240101":
            rows.append(_row(d, "rating", False, "yes", "", correct_page="3"))
        else:
            rows.append(_row(d, "rating", True, "yes", "yes", correct_page="2"))
        if d == "FDIC_4_20240101":
            rows.append(_row(d, "description_of_institution", False, "no"))
        else:
            rows.append(_row(d, "description_of_institution", True, "yes", "yes"))
        if d == "OCC_1_20240101":
            rows.append(_row(d, "illegal_credit_practices_review", True, "no", "no"))
        else:
            rows.append(_row(d, "illegal_credit_practices_review", True, "yes", "yes"))
        rows.append(_row(d, "aa_concentration", True, "yes", "yes"))
        if d == "FRB_3_20240101":
            rows.append(_row(d, "ltd_ratio", False, "yes", "no", correct_page="9"))
        else:
            rows.append(_row(d, "ltd_ratio", True, "yes", "yes"))
    return rows


def _get(rows: list[dict], level: str, regulator: str, key: str) -> dict:
    field = "section" if level == "section" else "section_type"
    hits = [r for r in rows if r["level"] == level and r["regulator"] == regulator and r[field] == key]
    assert len(hits) == 1, (level, regulator, key)
    return hits[0]


def test_score_arithmetic_against_hand_counts():
    sheet = filled_sheet()
    assert lc.validate(sheet, {"doc_ids": list(DOCS4)}) == []
    recall, misses = lc.score(sheet)
    expect = {  # section: (present, found, correct)
        "cover": (4, 4, 4), "rating": (4, 3, 2), "description_of_institution": (3, 3, 3),
        "illegal_credit_practices_review": (3, 4, 3), "aa_concentration": (4, 4, 4), "ltd_ratio": (4, 3, 3),
    }
    for section, (p, f, c) in expect.items():
        r = _get(recall, "section", "all", section)
        assert (r["n_present"], r["n_found"], r["n_correct"], r["n_docs"]) == (p, f, c, 4)
        assert (r["n_missed"], r["n_false_found"]) == (p - c, f - c)
        assert r["recall"] == pytest.approx(c / p) and r["precision"] == pytest.approx(c / f)
        assert (r["recall_lo"], r["recall_hi"]) == pytest.approx(wilson_ci(c, p))
    # Wilson 95 percent interval for 3 of 4, by hand: centre 0.6275, half-width 0.3269.
    ltd = _get(recall, "section", "all", "ltd_ratio")
    assert ltd["recall_lo"] == pytest.approx(0.3006, abs=1e-4) and ltd["recall_hi"] == pytest.approx(0.9544, abs=1e-4)
    rating = _get(recall, "section", "all", "rating")
    assert rating["recall"] == 0.5 and rating["precision"] == pytest.approx(2 / 3)
    # Section types pool their two sections: cover and rating 8 printed, 7 found, 6 correct.
    cr = _get(recall, "section_type", "all", "cover_and_rating")
    assert (cr["n_present"], cr["n_found"], cr["n_correct"]) == (8, 7, 6) and cr["recall"] == 0.75
    assert cr["precision"] == pytest.approx(6 / 7) and "pooled" in cr["note"]
    aa = _get(recall, "section_type", "all", "aa_concentration_and_ltd")
    assert (aa["n_present"], aa["n_found"], aa["n_correct"]) == (8, 7, 7) and aa["precision"] == 1.0
    assert _get(recall, "section_type", "all", "review_section")["note"] == ""
    # By regulator: OCC rating 2 printed, 2 found, 1 correct; FRB rating 1 printed, 0 found.
    occ = _get(recall, "section", "OCC", "rating")
    assert (occ["n_present"], occ["n_found"], occ["n_correct"], occ["n_docs"]) == (2, 2, 1, 2)
    frb = _get(recall, "section", "FRB", "rating")
    assert (frb["n_present"], frb["n_found"], frb["n_correct"]) == (1, 0, 0) and frb["precision"] != frb["precision"]
    assert [r["regulator"] for r in recall if r["level"] == "section"][::6] == ["all", "OCC", "FDIC", "FRB"]
    assert len(recall) == 4 * 6 + 4 * 4
    assert [(m["doc_id"], m["section"], m["miss_type"]) for m in misses] == [
        ("FRB_3_20240101", "rating", "not_found"),
        ("FRB_3_20240101", "ltd_ratio", "not_found"),
        ("OCC_1_20240101", "illegal_credit_practices_review", "found_absent"),
        ("OCC_2_20240101", "rating", "wrong_location"),
    ]
    assert misses[3]["correct_page"] == "4" and misses[0]["district"] == "Kansas City"


@pytest.mark.parametrize(("change", "message"), [
    ({"present_in_pdf": ""}, "present_in_pdf must be yes or no"),
    ({"present_in_pdf": "maybe"}, "present_in_pdf must be yes or no"),
    ({"locator_correct": ""}, "locator_correct is required on a found section"),
    ({"locator_correct": "y"}, "locator_correct must be yes, no or blank"),
    ({"present_in_pdf": "no"}, "cannot be correct"),
    ({"locator_correct": "no"}, "correct_page is required"),
    ({"correct_page": "5"}, "differs from the located page"),
    ({"correct_page": "p5"}, "must be a page number"),
    ({"minutes": "-1"}, "non-negative number"),
    ({"found": "yes"}, "found must be true or false"),
    ({"section": "scope_of_evaluation"}, "unknown section"),
    ({"section_type": "review_section"}, "section_type should be cover_and_rating"),
])
def test_validate_reports_each_problem(change: dict[str, str], message: str):
    sheet = filled_sheet()
    sheet[0].update(change)
    problems = lc.validate(sheet)
    assert any(message in p for p in problems), problems
    assert problems[0].startswith("line 2 (FDIC_4_20240101")


def test_validate_rules_for_sections_not_found_and_the_sample():
    sheet = filled_sheet()
    frb_rating = next(r for r in sheet if r["doc_id"] == "FRB_3_20240101" and r["section"] == "rating")
    frb_rating["locator_correct"] = "yes"
    assert any("did not find cannot be correct" in p for p in lc.validate(sheet))
    frb_rating.update(locator_correct="", correct_page="")  # a printed section not found needs its page
    assert any("correct_page is required" in p for p in lc.validate(sheet))
    sheet = filled_sheet()
    fdic_desc = next(r for r in sheet if r["doc_id"] == "FDIC_4_20240101" and r["section"] == "description_of_institution")
    fdic_desc["locator_correct"] = "no"
    assert any("correct rejection" in p for p in lc.validate(sheet))
    fdic_desc["locator_correct"] = "yes"
    fdic_desc["correct_page"] = "3"
    assert any("does not print" in p for p in lc.validate(sheet))
    sheet = filled_sheet()
    assert any("duplicate row" in p for p in lc.validate([*sheet, dict(sheet[0])]))
    problems = lc.validate(sheet[6:], {"doc_ids": [*DOCS4, "OCC_9_20240101"]})
    assert any("rows missing" in p for p in problems)
    assert any("outside the recorded sample" in p for p in lc.validate(sheet, {"doc_ids": list(DOCS4[1:])}))
    with pytest.raises(lc.LocatorCheckError, match="not ready to score"):
        lc.write_score(sheet[1:] + [dict(sheet[1], present_in_pdf="")], Path("unused.csv"), Path("unused.csv"))


def test_write_score_is_deterministic(tmp_path: Path):
    a = lc.write_score(filled_sheet(), tmp_path / "a.csv", tmp_path / "am.csv", {"doc_ids": list(DOCS4)})
    lc.write_score(list(reversed(filled_sheet())), tmp_path / "b.csv", tmp_path / "bm.csv")
    assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()
    assert (tmp_path / "am.csv").read_bytes() == (tmp_path / "bm.csv").read_bytes()
    with open(tmp_path / "a.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == lc.RECALL_COLUMNS and len(rows) == len(a[0])
    rating = next(r for r in rows if r["level"] == "section" and r["regulator"] == "all" and r["section"] == "rating")
    assert (rating["recall"], rating["precision"], rating["recall_lo"]) == ("0.5000", "0.6667", "0.1500")
    assert "\r" not in (tmp_path / "a.csv").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- CLI and wiring


def _write_manifest(path: Path, docs: list[DocRecord]) -> None:
    cols = list(DocRecord.model_fields)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for d in docs:
            row = d.model_dump()
            w.writerow({c: ("" if row[c] is None else row[c]) for c in cols})


def test_cli_draw_sheet_score(world: World, tmp_path: Path, capsys):
    m, frb, tuning = tmp_path / "corpus.csv", tmp_path / "frb.csv", tmp_path / "tuning.yaml"
    sample, sheet, out, misses = tmp_path / "sample.yaml", tmp_path / "sheet.csv", tmp_path / "recall.csv", tmp_path / "miss.csv"
    _write_manifest(m, world.docs)
    with open(frb, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["bank_id", "exam_date", "state"])
        for (bank, exam), st in world.states.items():
            w.writerow([bank, exam, st])
    tuning.write_text(yaml.safe_dump({"version": 1, "documents": [{"doc_id": d} for d in world.tuning]}), encoding="utf-8")
    base = ["--manifest", str(m), "--sample", str(sample)]
    draw = ["draw", "--seed", str(SEED), "--date", DRAWN_AT, "--frb-index", str(frb), "--tuning", str(tuning), *base]
    assert lc.main(draw) == 0
    rec = lc.load_record(sample)
    assert rec == lc.draw(world.docs, SEED, DRAWN_AT, frb_states(frb), sorted(load_tuning_documents(tuning)))
    assert sample.read_text(encoding="utf-8").startswith("# Written by python -m cra_extract.locator_check draw")
    assert lc.main(draw) == 1 and "already holds a draw" in capsys.readouterr().err
    assert lc.main(["sheet", "--out", str(sheet), "--text-dir", str(tmp_path / "no_text"), *base]) == 0
    rows = lc.read_sheet(sheet)
    assert len(rows) == 600 and all(r["found"] == "false" for r in rows)
    assert lc.main(["score", "--sheet", str(sheet), "--out", str(out), "--misses", str(misses), *base]) == 1
    for r in rows:  # the reader finds every section printed: every row is a miss
        r.update(present_in_pdf="yes", correct_page="1")
    lc.write_sheet(rows, sheet, force=True)
    assert lc.main(["score", "--sheet", str(sheet), "--out", str(out), "--misses", str(misses), *base]) == 0
    with open(out, newline="", encoding="utf-8") as fh:
        res = list(csv.DictReader(fh))
    assert all(r["recall"] == "0.0000" and r["precision"] == "" for r in res)
    with open(misses, newline="", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 600
    assert lc.main(["sheet", "--out", str(sheet), "--text-dir", str(tmp_path), *base]) == 1
    assert lc.main(["score", "--manifest", str(m), "--sample", str(tmp_path / "none.yaml")]) == 1


def test_cli_and_makefile_wiring():
    assert cli.STAGES["locator-check"][0] == "cra_extract.locator_check"
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^locator-check-draw:\n(\t.*\n)*\t\$\(PY\) -m cra_extract\.locator_check draw --seed "
                     r"\$\(LOCATOR_SEED\) --date \$\(LOCATOR_DATE\)", text, re.MULTILINE)
    assert re.search(r"^locator-check-sheet:\n\t\$\(PY\) -m cra_extract\.locator_check sheet", text, re.MULTILINE)
    assert re.search(r"^locator-check-score:\n\t\$\(PY\) -m cra_extract\.locator_check score", text, re.MULTILINE)
    phony = next(line for line in text.splitlines() if line.startswith(".PHONY:"))
    assert {"locator-check-draw", "locator-check-sheet", "locator-check-score"} <= set(phony.split())


@pytest.mark.skipif(not (CORPUS_PATH.exists() and (INDEX_DIR / "frb.csv").exists() and TUNING_DOCS_PATH.exists()),
                    reason="live manifest, FRB index or tuning list not present")
def test_live_manifest_covers_every_district_without_test_documents():
    """Reads only the manifest, the FRB index and the tuning list (no document text)."""
    _cols, rows = read_rows(CORPUS_PATH)
    manifest = to_records(rows)
    tuning = sorted(load_tuning_documents(TUNING_DOCS_PATH))
    rec = lc.draw(manifest, 1, DRAWN_AT, frb_states(INDEX_DIR / "frb.csv"), tuning)
    assert all(rec["by_district"][d] >= 1 for d in lc.DISTRICTS)
    split = {d.doc_id: d.split for d in manifest}
    assert all(split[d] == "corpus" for d in rec["doc_ids"])
    assert not set(rec["doc_ids"]) & set(tuning)
    assert rec["exclusions"]["test_documents_excluded"] == sum(1 for d in manifest if d.split == "test")
