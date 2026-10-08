"""Tests for cra_extract.strata on a synthetic manifest and on the live manifest.

The synthetic manifest has six (regulator, method) cells of controlled size (one below
the 50-document threshold), a few NI/SN documents, duplicate banks, FRB states on both
sides of the New York and Kansas City line, and eight "fixture" documents that stand in
for the sample PEs. The live-manifest tests copy ``config/corpus.csv`` to a temporary
directory and run ``draw-dev`` with the seed the author will use.
"""

from __future__ import annotations

import csv
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from cra_extract import strata
from cra_extract.config import CORPUS_PATH, INDEX_DIR
from cra_extract.models import DocRecord, make_doc_id
from tests.conftest import SAMPLE_META

DRAWN_AT = "2026-10-07"
SEED = 20261007

CELL_SIZES = {
    ("FDIC", "small"): 60,
    ("FDIC", "isb"): 55,
    ("OCC", "small"): 52,
    ("OCC", "isb"): 20,  # below min_cell: no cell stratum
    ("FRB", "small"): 51,
    ("FRB", "isb"): 50,
}
NY_STATE, OTHER_STATE = "NY", "WV"


def _doc(regulator: str, method: str, i: int, **kw) -> DocRecord:
    bank_id = kw.pop("bank_id", f"{regulator[0]}{method[0]}{i:03d}")
    exam = date(2024, 1, 1) + timedelta(days=i)
    assets = kw.pop("total_assets_usd", None if regulator == "OCC" else 10_000_000 * (i + 1))
    return DocRecord(
        doc_id=make_doc_id(regulator, bank_id, exam),
        regulator=regulator,
        bank_id=bank_id,
        bank_name=f"{regulator} {method} bank {i}",
        exam_date=exam,
        release_date=exam,
        method=method,
        overall_rating=kw.pop("overall_rating", "S"),
        total_assets_usd=assets,
        url=f"https://example.invalid/{regulator}/{bank_id}.pdf",
        **kw,
    )


@pytest.fixture
def manifest() -> list[DocRecord]:
    docs: list[DocRecord] = []
    for (reg, method), n in CELL_SIZES.items():
        for i in range(n):
            rating = "NI" if i % 10 == 3 else ("SN" if i == 7 else "S")
            docs.append(_doc(reg, method, i, overall_rating=rating))
    # A second exam of two banks (bank-level exclusivity must hold across splits).
    docs.append(_doc("FDIC", "small", 500, bank_id="Fs000"))
    docs.append(_doc("FRB", "isb", 501, bank_id="Fi001"))
    return docs


@pytest.fixture
def fixed(manifest: list[DocRecord]) -> list[str]:
    """Eight fixed documents covering five of the six cells (FRB/isb is left uncovered)."""
    want = [("FDIC", "small"), ("FDIC", "small"), ("FDIC", "isb"), ("OCC", "small"),
            ("OCC", "small"), ("OCC", "isb"), ("FRB", "small"), ("FRB", "small")]
    out: list[str] = []
    for cell in want:
        doc = next(d for d in manifest if strata.cell_of(d) == cell and d.doc_id not in out)
        out.append(doc.doc_id)
    return sorted(out)


@pytest.fixture
def states(manifest: list[DocRecord]) -> dict[tuple[str, str], str]:
    """FRB states: New York everywhere except FRB/small indices 45 and up, so neither the
    fixed FRB documents (indices 0 and 1) nor the FRB/isb coverage draw can satisfy the
    other-district rule and the dedicated draw must happen."""
    out = {}
    for d in manifest:
        if d.regulator == "FRB":
            i = int(d.doc_id.split("_")[1][2:])
            other = d.method == "small" and 45 <= i < 100
            out[(d.bank_id, d.exam_date.isoformat())] = OTHER_STATE if other else NY_STATE
    return out


def _by_id(manifest: list[DocRecord]) -> dict[str, DocRecord]:
    return {d.doc_id: d for d in manifest}


def _with_split(manifest: list[DocRecord], split: str, doc_ids: list[str]) -> list[DocRecord]:
    ids = set(doc_ids)
    return [d.model_copy(update={"split": split}) if d.doc_id in ids else d for d in manifest]


# ----------------------------------------------------------------------------- fixtures


def test_sample_doc_ids_match_conftest():
    expected = sorted(
        make_doc_id(reg, bank, date.fromisoformat(exam))
        for reg, bank, exam, _m, _r in SAMPLE_META.values()
    )
    assert strata.SAMPLE_DOC_IDS == expected
    assert set(strata.FIXTURE_SAMPLES) == set(SAMPLE_META)


# ----------------------------------------------------------------------------- dev


def test_draw_dev_covers_cells_and_other_district(manifest, fixed, states):
    draw = strata.draw_dev(manifest, SEED, DRAWN_AT, states, fixed_doc_ids=fixed)
    by_id = _by_id(manifest)
    assert draw.n == 10 and len(draw.doc_ids) == 10
    assert set(fixed) <= set(draw.doc_ids)
    cells = {strata.cell_of(by_id[i]) for i in draw.doc_ids}
    assert cells == set(CELL_SIZES)
    frb_other = [
        i for i in draw.doc_ids
        if by_id[i].regulator == "FRB" and strata.state_of(by_id[i], states) == OTHER_STATE
    ]
    assert frb_other, "an FRB document outside NY and KC must be in dev"
    reasons = {d.stratum for d in draw.docs}
    assert "cell_coverage:FRB/isb" in reasons
    assert "frb_other_district" in reasons
    assert draw.drawn_at == DRAWN_AT and draw.seed == SEED
    assert "frb_district_approximation" in draw.rules
    assert len(draw.doc_id_hash) == 64


def test_draw_dev_is_deterministic_and_seed_sensitive(manifest, fixed, states):
    a = strata.draw_dev(manifest, SEED, DRAWN_AT, states, fixed_doc_ids=fixed)
    b = strata.draw_dev(manifest, SEED, DRAWN_AT, states, fixed_doc_ids=fixed)
    assert a.to_dict() == b.to_dict()
    c = strata.draw_dev(manifest, SEED + 1, DRAWN_AT, states, fixed_doc_ids=fixed)
    assert c.doc_ids != a.doc_ids


def test_draw_dev_rejects_missing_fixed(manifest, states):
    with pytest.raises(strata.DrawError, match="not in manifest"):
        strata.draw_dev(manifest, SEED, DRAWN_AT, states, fixed_doc_ids=["OCC_0_20200101"])


def test_draw_dev_bank_exclusivity_within_split(manifest, fixed, states):
    draw = strata.draw_dev(manifest, SEED, DRAWN_AT, states, fixed_doc_ids=fixed)
    by_id = _by_id(manifest)
    banks = [strata.bank_key(by_id[i]) for i in draw.doc_ids]
    assert len(banks) == len(set(banks))


# ----------------------------------------------------------------------------- test


@pytest.fixture
def after_dev(manifest, fixed, states) -> list[DocRecord]:
    draw = strata.draw_dev(manifest, SEED, DRAWN_AT, states, fixed_doc_ids=fixed)
    return _with_split(manifest, "dev", draw.doc_ids)


def test_cell_allocation_minimum_and_remainder(manifest):
    alloc = strata.cell_allocation(manifest, 21, 3, 50)
    assert ("OCC", "isb") not in alloc
    assert all(n >= 3 for n in alloc.values())
    assert sum(alloc.values()) == 21
    # The 6 extra slots go one each to the five eligible cells, then to the largest again.
    assert alloc[("FDIC", "small")] == 5
    with pytest.raises(strata.DrawError):
        strata.cell_allocation(manifest, 10, 3, 50)


def test_draw_test_strata_without_aa_counts(after_dev):
    draw = strata.draw_test(after_dev, SEED, DRAWN_AT)
    by_id = _by_id(after_dev)
    assert draw.n == 30
    dev_banks = {strata.bank_key(d) for d in after_dev if d.split == "dev"}
    assert all(strata.bank_key(by_id[i]) not in dev_banks for i in draw.doc_ids)
    assert all(by_id[i].split != "dev" for i in draw.doc_ids)
    per_cell = {}
    for i in draw.doc_ids:
        per_cell[strata.cell_of(by_id[i])] = per_cell.get(strata.cell_of(by_id[i]), 0) + 1
    for cell, n in CELL_SIZES.items():
        if n >= 50:
            assert per_cell.get(cell, 0) >= 3, cell
    cell_strata = [s for s in draw.strata if s.name.startswith("cell:")]
    assert {s.name for s in cell_strata} == {f"cell:{strata.cell_name(c)}" for c, n in CELL_SIZES.items() if n >= 50}
    low = [d for d in draw.docs if d.stratum == "low_rating"]
    assert len(low) == 5 and all(by_id[d.doc_id].overall_rating in ("NI", "SN") for d in low)
    multi = [d for d in draw.docs if d.stratum == "multi_aa"]
    assert len(multi) == 4 and all(d.reason == "assets_top_decile_proxy" for d in multi)
    thresholds = strata.assets_thresholds(after_dev, 0.9)
    for d in multi:
        doc = by_id[d.doc_id]
        assert doc.regulator != "OCC" and doc.total_assets_usd >= thresholds[strata.cell_of(doc)]
    assert draw.rules["multi_aa_rule"] == "assets_top_decile_proxy"
    multi_stratum = next(s for s in draw.strata if s.name == "multi_aa")
    assert "OCC/isb" in multi_stratum.detail["cells_without_assets"]
    for s in draw.strata:
        assert 0 < s.selection_probability <= 1
        assert s.drawn <= s.pool <= s.population
    assert all(0 < d.selection_probability <= 1 for d in draw.docs)


def test_draw_test_with_aa_counts(after_dev):
    multi_ids = [d.doc_id for d in after_dev if d.regulator == "OCC"][:12]
    counts = {i: 3 for i in multi_ids}
    counts[after_dev[0].doc_id] = 2  # below aa_min, never drawn as multi
    draw = strata.draw_test(after_dev, SEED, DRAWN_AT, aa_counts=counts)
    multi = [d for d in draw.docs if d.stratum == "multi_aa"]
    assert len(multi) == 4
    assert all(d.reason == "aa_counts" and d.doc_id in multi_ids for d in multi)
    assert draw.rules["multi_aa_rule"] == "aa_counts"


def test_draw_test_deterministic(after_dev):
    a = strata.draw_test(after_dev, 7, DRAWN_AT)
    b = strata.draw_test(after_dev, 7, DRAWN_AT)
    assert a.to_dict() == b.to_dict()
    assert strata.draw_test(after_dev, 8, DRAWN_AT).doc_ids != a.doc_ids


def test_draw_test_fails_when_stratum_too_small(after_dev):
    with pytest.raises(strata.DrawError, match="low_rating"):
        strata.draw_test(after_dev, SEED, DRAWN_AT, n_test=60, n_rating=35)


# ----------------------------------------------------------------------------- audit


@pytest.fixture
def after_test(after_dev) -> list[DocRecord]:
    draw = strata.draw_test(after_dev, SEED, DRAWN_AT)
    return _with_split(after_dev, "test", draw.doc_ids)


def test_draw_audit_tertiles(after_test):
    risk = {d.doc_id: float(i % 4) for i, d in enumerate(after_test)}
    missing = strata.eligible_pool(after_test, "audit")[0].doc_id
    del risk[missing]
    draw = strata.draw_audit(after_test, risk, SEED, DRAWN_AT)
    by_id = _by_id(after_test)
    assert draw.n == 30
    assert missing not in draw.doc_ids
    gold_banks = {strata.bank_key(d) for d in after_test if d.split in ("dev", "test")}
    assert all(strata.bank_key(by_id[i]) not in gold_banks for i in draw.doc_ids)
    names = [s.name for s in draw.strata]
    assert names == ["tertile:low", "tertile:mid", "tertile:high"]
    assert all(s.drawn == 10 for s in draw.strata)
    assert all(abs(s.weight - s.population / 10) < 1e-6 for s in draw.strata)
    pool = strata.eligible_pool(after_test, "audit")
    assert sum(s.population for s in draw.strata) == len(pool) - 1
    lo, mid, hi = draw.strata
    assert lo.detail["doc_risk_max"] <= mid.detail["doc_risk_min"] <= mid.detail["doc_risk_max"] <= hi.detail["doc_risk_min"]
    for d in draw.docs:
        s = next(s for s in draw.strata if s.name == d.stratum)
        assert s.detail["doc_risk_min"] <= risk[d.doc_id] <= s.detail["doc_risk_max"]
    assert "excluded from the pool: 1" in draw.notes[0]
    again = strata.draw_audit(after_test, risk, SEED, DRAWN_AT)
    assert again.to_dict() == draw.to_dict()


def test_draw_audit_needs_risk(after_test):
    with pytest.raises(strata.DrawError):
        strata.draw_audit(after_test, {}, SEED, DRAWN_AT)


# ----------------------------------------------------------------------------- exclusivity and pool


def test_exclusivity_problems_and_pool(manifest):
    a, b = manifest[0], manifest[-2]  # b is the second exam of a's bank (Fs000)
    assert strata.bank_key(a) == strata.bank_key(b)
    m = _with_split(_with_split(manifest, "dev", [a.doc_id]), "test", [b.doc_id])
    problems = strata.exclusivity_problems(m)
    assert problems == ["FDIC Fs000 is in dev and test"]
    pool = strata.eligible_pool(m, "test")
    assert b.doc_id not in {d.doc_id for d in pool}, "dev bank excluded from the test pool"
    pool_dev = strata.eligible_pool(m, "dev")
    assert b.doc_id in {d.doc_id for d in pool_dev}, "later splits do not shrink the dev pool"


def test_assign_resets_previous_members():
    rows = [{"doc_id": "a", "split": "dev"}, {"doc_id": "b", "split": "corpus"}, {"doc_id": "c", "split": "test"}]
    strata.assign(rows, "dev", ["b"])
    assert [r["split"] for r in rows] == ["corpus", "dev", "test"]
    with pytest.raises(strata.DrawError):
        strata.assign(rows, "dev", ["zzz"])


# ----------------------------------------------------------------------------- CLI on a synthetic manifest


def _write_manifest(path: Path, manifest: list[DocRecord]) -> None:
    cols = list(DocRecord.model_fields)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for d in manifest:
            row = d.model_dump()
            w.writerow({c: ("" if row[c] is None else row[c]) for c in cols})


def _write_states(path: Path, states: dict[tuple[str, str], str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["bank_id", "exam_date", "state"])
        for (bank, exam), st in states.items():
            w.writerow([bank, exam, st])


def test_cli_round_trip(manifest, fixed, states, tmp_path, capsys, monkeypatch):
    mpath, spath, fpath = tmp_path / "m.csv", tmp_path / "strata.yaml", tmp_path / "frb.csv"
    _write_manifest(mpath, manifest)
    _write_states(fpath, states)
    monkeypatch.setattr(strata, "SAMPLE_DOC_IDS", fixed)
    base = ["--manifest", str(mpath), "--strata", str(spath)]

    assert strata.main(base + ["draw-dev", "--seed", str(SEED), "--date", DRAWN_AT, "--frb-index", str(fpath)]) == 0
    first_manifest, first_yaml = mpath.read_bytes(), spath.read_bytes()
    assert strata.main(base + ["draw-dev", "--seed", str(SEED), "--date", DRAWN_AT, "--frb-index", str(fpath)]) == 0
    assert mpath.read_bytes() == first_manifest and spath.read_bytes() == first_yaml, "idempotent rerun"

    risk_path = tmp_path / "risk.csv"
    with open(risk_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", "doc_risk"])
        for i, d in enumerate(manifest):
            w.writerow([d.doc_id, i % 3])
    assert strata.main(base + ["draw-test", "--seed", "1", "--date", "2026-10-13"]) == 0
    assert strata.main(base + ["draw-audit", "--seed", "2", "--date", "2026-10-16", "--risk", str(risk_path)]) == 0

    # dev redraw with the same seed still reproduces after later splits exist.
    assert strata.main(base + ["draw-dev", "--seed", str(SEED), "--date", DRAWN_AT, "--frb-index", str(fpath)]) == 0
    _cols, rows = strata.read_rows(mpath)
    counts = {}
    for r in rows:
        counts[r["split"]] = counts.get(r["split"], 0) + 1
    assert counts == {"dev": 10, "test": 30, "audit": 30, "corpus": len(manifest) - 70}
    records = strata.to_records(rows)
    assert strata.exclusivity_problems(records) == []

    data = yaml.safe_load(spath.read_text(encoding="utf-8"))
    assert list(data["splits"]) == ["dev", "test", "audit"]
    for split in ("dev", "test", "audit"):
        rec = data["splits"][split]
        in_manifest = sorted(r["doc_id"] for r in rows if r["split"] == split)
        assert rec["doc_ids"] == in_manifest
        assert rec["doc_id_hash"] == strata.split_hash(records, split)
    assert data["splits"]["audit"]["strata"][0]["weight"] is not None

    assert strata.main(base + ["show"]) == 0
    out = capsys.readouterr().out
    assert "bank exclusivity: ok" in out and "tertile:high" in out

    # A redraw of dev with another seed that collides with a test bank is refused.
    rc = strata.main(base + ["draw-dev", "--seed", "99", "--date", DRAWN_AT, "--frb-index", str(fpath)])
    err = capsys.readouterr().err
    assert rc in (0, 1)
    if rc == 1:
        assert "redraw the later split" in err
        assert mpath.read_bytes() != b"" and strata.exclusivity_problems(strata.to_records(strata.read_rows(mpath)[1])) == []


def test_cli_rejects_bad_aa_counts_columns(manifest, tmp_path, capsys):
    mpath, spath = tmp_path / "m.csv", tmp_path / "strata.yaml"
    _write_manifest(mpath, manifest)
    bad = tmp_path / "aa.csv"
    bad.write_text("doc_id,n_sections\nx,3\n", encoding="utf-8")
    rc = strata.main(["--manifest", str(mpath), "--strata", str(spath), "draw-test", "--seed", "1", "--date", DRAWN_AT, "--aa-counts", str(bad)])
    assert rc == 1 and "n_aa_sections" in capsys.readouterr().err


# ----------------------------------------------------------------------------- live manifest


@pytest.fixture
def live(tmp_path) -> tuple[Path, Path]:
    mpath = tmp_path / "corpus.csv"
    shutil.copy(CORPUS_PATH, mpath)
    return mpath, tmp_path / "strata.yaml"


@pytest.mark.skipif(not (INDEX_DIR / "frb.csv").exists(), reason="FRB index not built")
def test_live_manifest_draw_dev(live):
    mpath, spath = live
    original = mpath.read_bytes()
    base = ["--manifest", str(mpath), "--strata", str(spath)]
    args = base + ["draw-dev", "--seed", str(SEED), "--date", DRAWN_AT]
    assert strata.main(args) == 0
    first_manifest, first_yaml = mpath.read_bytes(), spath.read_bytes()
    assert strata.main(args) == 0
    assert mpath.read_bytes() == first_manifest and spath.read_bytes() == first_yaml

    _cols, rows = strata.read_rows(mpath)
    records = strata.to_records(rows)
    dev = [d for d in records if d.split == "dev"]
    dev_ids = {d.doc_id for d in dev}
    assert len(dev) == 10
    assert set(strata.SAMPLE_DOC_IDS) <= dev_ids
    assert {strata.cell_of(d) for d in dev} == {strata.cell_of(d) for d in records}
    states = strata.frb_states()
    assert any(
        d.regulator == "FRB" and strata.state_of(d, states) not in strata.NY_KC_STATES | {None}
        for d in dev
    )
    assert strata.exclusivity_problems(records) == []
    dev_banks = {strata.bank_key(d) for d in dev}
    assert all(strata.bank_key(d) not in dev_banks for d in records if d.split == "test")
    assert "FDIC_26816_20231023" not in dev_ids, "Liberty's second exam is not a second dev document"
    # Only the split column changed: every other cell is byte-identical to the original.
    with open(CORPUS_PATH, newline="", encoding="utf-8") as fh:
        before = list(csv.DictReader(fh))
    assert original == CORPUS_PATH.read_bytes()
    for b, a in zip(before, rows, strict=True):
        assert {k: v for k, v in b.items() if k != "split"} == {k: v for k, v in a.items() if k != "split"}
    data = yaml.safe_load(spath.read_text(encoding="utf-8"))
    rec = data["splits"]["dev"]
    assert rec["seed"] == SEED and rec["drawn_at"] == DRAWN_AT
    assert rec["doc_ids"] == sorted(dev_ids)
    assert rec["doc_id_hash"] == strata.split_hash(records, "dev")
