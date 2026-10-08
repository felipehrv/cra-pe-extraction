"""Tests for the run stage (``cra_extract.run``).

No network. A workspace is built from the eight sample PDFs under a temporary root with the
layout ``config/corpus.csv``, ``data/pdfs``, ``data/text``, ``data/traces`` and ``results``;
the PDFs are parsed once per session with pymupdf and copied per test. The LLM path uses a
``FakeBackend`` whose canned response quotes the SouthStar rating sentence
(FDIC_12299_SouthStar_2022_ISB_NI_fairlending.txt line 56).

Assessment-area counts: SouthStar designates five assessment areas (line 165) and prints one
"X AA - Full/Limited-Scope Review" block each (lines 606, 935, 1246, 1403, 1561); FieldPoint
has four (line 221), one MSA or MD block each (lines 647, 1219, 1741, 2210).
"""

from __future__ import annotations

import csv
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from conftest import SAMPLE_META, SAMPLES

from cra_extract import llm, parse, run
from cra_extract.config import load_fields
from cra_extract.models import make_doc_id
from cra_extract.rules import RULE_FIELDS

MAKEFILE = Path(__file__).parent.parent / "Makefile"
SOUTHSTAR = "FDIC_12299_20220912"
FIELDPOINT = "FRB_3664588_20240116"
LIBERTY = "FDIC_26816_20220809"
N_LLM_FIELDS = sum(len(g["fields"]) for g in load_fields()["llm_field_groups"].values())


# ----------------------------------------------------------------------------- fixtures


def sample_rows() -> list[dict[str, str]]:
    rows = []
    for stem, (reg, bid, exam, method, rating) in sorted(SAMPLE_META.items()):
        rows.append({
            "doc_id": make_doc_id(reg, bid, date.fromisoformat(exam)),
            "regulator": reg,
            "bank_id": bid,
            "bank_name": stem.split("_")[2],
            "exam_date": exam,
            "release_date": "",
            "method": method,
            "overall_rating": rating,
            "url": f"file://{SAMPLES / (stem + '.pdf')}",
            "split": "dev",
            "stem": stem,
        })
    return rows


def write_manifest(root: Path, rows: list[dict[str, str]]) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    with open(root / "config" / "corpus.csv", "w", encoding="utf-8", newline="") as fh:
        cols = [c for c in rows[0] if c != "stem"]
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> Path:
    """Root with the manifest, the PDFs under data/pdfs and the parsed text under data/text."""
    root = tmp_path_factory.mktemp("ws")
    rows = sample_rows()
    write_manifest(root, rows)
    (root / "data" / "pdfs").mkdir(parents=True)
    for r in rows:
        pdf = root / "data" / "pdfs" / f"{r['doc_id']}.pdf"
        shutil.copy(SAMPLES / f"{r['stem']}.pdf", pdf)
        parse.parse_pdf(pdf, r["doc_id"], root / "data" / "text")
    return root


@pytest.fixture
def root(workspace: Path, tmp_path: Path) -> Path:
    """A fresh copy of the workspace for one test."""
    dest = tmp_path / "root"
    shutil.copytree(workspace, dest)
    return dest


class FakeBackend(llm.Backend):
    """Canned structured response; the rating quote is on SouthStar page 3."""

    name = "fake"

    def __init__(self, model: str = "fake-1") -> None:
        self.model = model
        self.calls = 0

    def _call(self, system_prompt, user_prompt, json_schema, model):
        self.calls += 1
        data: dict[str, Any] = {}
        if "overall_rating" in json_schema["properties"]:
            data["overall_rating"] = {
                "status": "value", "value": "NI",
                "quote": "This institution is rated Needs to Improve.", "page": 3,
            }
        text = json.dumps(data)
        return llm.BackendResult(text=text, parsed_json=data, model_reported=model, input_tokens=100, output_tokens=20)


def fake_factory(spec: run.SystemSpec, split: str, allow_api: bool, paths: run.Paths) -> llm.Backend:
    """Backend factory for tests: replay over the traces with a FakeBackend as the live backend."""
    live = FakeBackend(spec.model) if allow_api else None
    return llm.ReplayBackend(spec.backend, split, spec.model, live=live, allow_api=allow_api, trace_dir=paths.trace_dir)


def read_predictions(root: Path, split: str = "dev") -> pd.DataFrame:
    return pd.read_csv(root / "results" / f"predictions_{split}.csv", dtype=str, keep_default_na=False)


# ----------------------------------------------------------------------------- unit


def test_parse_systems():
    specs = run.parse_systems("rules,baseline,llm:gemini:gemini-3.8-flash")
    assert [s.name for s in specs] == ["rules", "baseline", "llm:gemini:gemini-3.8-flash"]
    assert specs[2] == run.SystemSpec("llm:gemini:gemini-3.8-flash", "llm", "gemini", "gemini-3.8-flash")
    assert run.parse_systems(["rules", " baseline "])[1].kind == "baseline"
    for bad in ("llm", "llm:gemini", "llm:nope:model", "s3", "", "rules,rules"):
        with pytest.raises(ValueError):
            run.parse_systems(bad)


def test_aa_unit_key_normalizes_fdic_and_frb_headings():
    assert run.aa_unit_key("AUSTIN MSA AA – Full-Scope Review") == "austin msa"
    assert run.aa_unit_key("DESCRIPTION OF INSTITUTION’S OPERATIONS IN AUSTIN MSA AA") == "austin msa"
    assert run.aa_unit_key("NON-MSA AA – Limited-Scope Review") == "non-msa"
    assert run.aa_unit_key("MD 35614 (NEW YORK-JERSEY CITY-WHITE PLAINS, NY-NJ) FULL SCOPE REVIEW") == "35614"
    assert run.aa_unit_key("DESCRIPTION OF INSTITUTION’S OPERATIONS IN MD 35614 (NEW YORK") == "35614"


def test_gemini_limiter_reads_free_tier_from_prices(tmp_path, monkeypatch):
    prices = {"gemini": {"free_tier": {"requests_per_minute": 5, "requests_per_day": 20}}}
    monkeypatch.setattr(run, "load_prices", lambda: prices)
    limiter = run.gemini_limiter(tmp_path)
    assert (limiter.backend, limiter.per_minute, limiter.per_day) == ("gemini", 5, 20)
    assert limiter.path == tmp_path / "ratelimit_gemini.json"
    monkeypatch.setattr(run, "load_prices", lambda: {"gemini": {"free_tier": {}}})
    assert run.gemini_limiter(tmp_path).per_minute is None


def test_build_llm_backend_wires_limiter_only_with_allow_api(tmp_path, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_make_backend(name, model, **kwargs):
        captured.update(name=name, model=model, **kwargs)
        return FakeBackend(model)

    monkeypatch.setattr(llm, "make_backend", fake_make_backend)
    paths = run.Paths.under(tmp_path)
    spec = run.parse_systems("llm:gemini:gemini-3.8-flash")[0]
    replay = run.build_llm_backend(spec, "dev", allow_api=False, paths=paths)
    assert isinstance(replay, llm.ReplayBackend) and replay.live is None and not captured
    assert replay.path == paths.trace_dir / "dev" / "gemini.jsonl"
    live = run.build_llm_backend(spec, "dev", allow_api=True, paths=paths)
    assert isinstance(live.live, FakeBackend)
    assert isinstance(captured["limiter"], llm.RateLimiter) and captured["limiter"].backend == "gemini"
    captured.clear()
    run.build_llm_backend(run.parse_systems("llm:ollama:llama3.1")[0], "dev", True, paths)
    assert "limiter" not in captured and captured["name"] == "ollama"


def test_csv_table_append_drop_and_header_guard(tmp_path):
    t = run.CsvTable(tmp_path / "t.csv", ["a", "b"])
    t.append([{"a": "1", "b": "x"}, {"a": "2", "b": "y"}])
    t.append([{"a": "3", "b": "z", "extra": "ignored"}])
    assert t.keys(("a",)) == {("1",), ("2",), ("3",)}
    assert t.drop(("a",), {("2",)}) == 1
    assert [r["a"] for r in run.CsvTable(tmp_path / "t.csv", ["b", "a"]).rows()] == ["1", "3"]
    with pytest.raises(RuntimeError):
        run.CsvTable(tmp_path / "t.csv", ["a", "c"])


# ----------------------------------------------------------------------------- end to end


def test_rules_and_baseline_on_samples(root: Path, capsys):
    rc = run.main(["--split", "dev", "--systems", "rules,baseline", "--root", str(root)])
    assert rc == 0
    assert "16 (doc, system) pairs written, 0 errors" in capsys.readouterr().out

    preds = read_predictions(root)
    assert list(preds.columns) == run.PREDICTION_COLUMNS
    assert preds.groupby(["doc_id", "field", "system"]).size().max() == 1
    assert preds.groupby("system").size().to_dict() == {"rules": 8 * len(RULE_FIELDS), "baseline": 16}
    assert set(preds.loc[preds["system"] == "rules", "field"]) == set(RULE_FIELDS)
    assert set(preds["status"]) <= {"value", "absent", "abstain"}
    assert set(preds["locator_miss"]) == {"0"}
    # Grounding failures are only ever recorded on answered rows and use the three categories.
    bad = preds[preds["grounding_failure"] != ""]
    assert set(bad["status"]) <= {"value"}
    assert set(bad["grounding_failure"]) <= {"quote_not_found", "quote_out_of_scope", "quote_does_not_support"}
    southstar = preds[(preds["doc_id"] == SOUTHSTAR) & (preds["system"] == "rules")].set_index("field")
    assert southstar.loc["overall_rating", "value"] == "NI"
    assert southstar.loc["exam_date", "value"] == "2022-09-12"
    finding = preds[(preds["doc_id"] == SOUTHSTAR) & (preds["system"] == "baseline")].set_index("field")
    assert finding.loc["finding_present", "value"] == "true"
    assert finding.loc["rating_lowered", "value"] == "true"

    sections = pd.read_csv(root / "results" / "sections_dev.csv")
    assert list(sections.columns) == run.SECTION_COLUMNS
    assert sections.groupby("doc_id").size().to_dict() == {d: 8 for d in preds["doc_id"].unique()}
    by_doc = sections.groupby("doc_id")["n_aa_sections"].first()
    assert by_doc[SOUTHSTAR] == 5 and by_doc[FIELDPOINT] == 4 and by_doc[LIBERTY] == 0
    review = sections[sections["section"] == "illegal_credit_practices_review"]
    assert review["found"].all() and (review["chars"] > 0).all()
    # A section found at its declared scope has confidence 1.0; the locator reports a lower
    # confidence (and the scope it did find) otherwise, which the coverage report keeps.
    assert (review.loc[review["confidence"] == 1.0, "scope"] == "institution").all()
    assert (review["confidence"] == 1.0).sum() >= 7

    entries = run.read_run_log(root / "results" / "run_log_dev.jsonl")
    assert len(entries) == 16 and all(e["error"] is None for e in entries)
    assert {e["system"] for e in entries} == {"rules", "baseline"}
    assert all(set(e) >= {"doc_id", "system", "seconds", "tokens", "cost_usd", "error"} for e in entries)


def test_resumable_limit_and_force(root: Path):
    args = ["--split", "dev", "--systems", "rules", "--root", str(root)]
    assert run.main([*args, "--limit", "3"]) == 0
    assert read_predictions(root)["doc_id"].nunique() == 3
    assert run.main([*args, "--limit", "3"]) == 0
    assert read_predictions(root)["doc_id"].nunique() == 6
    assert run.main(args) == 0
    first = read_predictions(root)
    assert first["doc_id"].nunique() == 8 and len(first) == 8 * len(RULE_FIELDS)

    # A third run has nothing to do: no rows, no log entries, sections untouched.
    assert run.main(args) == 0
    assert len(read_predictions(root)) == len(first)
    assert len(run.read_run_log(root / "results" / "run_log_dev.jsonl")) == 8
    assert len(pd.read_csv(root / "results" / "sections_dev.csv")) == 64

    # A new system appends only its rows; --force on one document recomputes it in place.
    assert run.main([*args[:3], "rules,baseline", *args[4:]]) == 0
    assert len(read_predictions(root)) == len(first) + 16
    assert run.main([*args, "--doc-id", LIBERTY, "--force"]) == 0
    after = read_predictions(root)
    assert len(after) == len(first) + 16
    assert len(run.read_run_log(root / "results" / "run_log_dev.jsonl")) == 17
    assert run.main([*args, "--doc-id", "FDIC_0_20200101"]) == 1


def test_llm_fake_backend_writes_traces_and_predictions(root: Path):
    paths = run.Paths.under(root)
    # parse_systems rejects unknown backend names, so the fake spec is built directly.
    systems = [run.SystemSpec("llm:fake:fake-1", "llm", "fake", "fake-1")]

    # Replay without traces: every pair fails with a TraceMissError and nothing is written.
    summary = run.run_split("dev", systems, paths=paths, backend_factory=fake_factory,limit=2)
    assert summary.n_errors == 2 and summary.n_pairs_written == 0 and summary.exit_code == 1
    assert not paths.predictions("dev").exists()
    entries = run.read_run_log(paths.run_log("dev"))
    assert all(e["error"].startswith("TraceMissError") for e in entries)

    # Live (fake) calls record one trace per field group and one row per field.
    summary = run.run_split("dev", systems, paths=paths, backend_factory=fake_factory,allow_api=True)
    assert summary.n_errors == 0 and summary.n_pairs_written == 8
    traces = llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")
    assert len(traces) == 8 * len(load_fields()["llm_field_groups"])
    assert {t.doc_id for t in traces} == {r["doc_id"] for r in sample_rows()}
    assert all(t.backend == "fake" and t.model == "fake-1" and t.error is None for t in traces)
    preds = read_predictions(root)
    assert list(preds.columns) == run.PREDICTION_COLUMNS
    assert set(preds["system"]) == {"llm:fake:fake-1"}
    assert preds.groupby("doc_id").size().to_dict() == {t: N_LLM_FIELDS for t in preds["doc_id"].unique()}
    ss = preds[preds["doc_id"] == SOUTHSTAR].set_index("field")
    assert ss.loc["overall_rating", "value"] == "NI" and ss.loc["overall_rating", "page"] == "3"
    assert ss.loc["overall_rating", "grounding_failure"] == ""
    assert ss.loc["overall_rating", "scope"] == "institution"
    assert ss.loc["cd_test_rating", "status"] == "abstain"
    # OCC samples lack description_of_assessment_area but the group still has located sections.
    assert set(preds["locator_miss"]) == {"0"}
    log_ok = [e for e in run.read_run_log(paths.run_log("dev")) if e["error"] is None]
    assert len(log_ok) == 8 and all(e["tokens"] == 4 * 120 and e["cost_usd"] == 0 for e in log_ok)

    # A forced rerun without --allow-api replays every call from the stored traces.
    summary = run.run_split("dev", systems, paths=paths, backend_factory=fake_factory,force=True)
    assert summary.n_errors == 0 and summary.n_pairs_written == 8
    assert len(llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")) == len(traces)
    assert len(read_predictions(root)) == len(preds)


def test_document_error_is_logged_and_skipped(root: Path, monkeypatch, capsys):
    original = run.locate_document

    def failing(doc_id, pages, regulator):
        if doc_id == FIELDPOINT:
            raise RuntimeError("boom")
        return original(doc_id, pages, regulator)

    monkeypatch.setattr(run, "locate_document", failing)
    rc = run.main(["--split", "dev", "--systems", "rules,baseline", "--root", str(root)])
    assert rc == 1
    assert "2 errors" in capsys.readouterr().out
    preds = read_predictions(root)
    assert FIELDPOINT not in set(preds["doc_id"]) and preds["doc_id"].nunique() == 7
    entries = run.read_run_log(root / "results" / "run_log_dev.jsonl")
    failed = [e for e in entries if e["error"]]
    assert {(e["doc_id"], e["system"]) for e in failed} == {(FIELDPOINT, "rules"), (FIELDPOINT, "baseline")}
    assert all(e["error"] == "RuntimeError: boom" for e in failed)
    assert FIELDPOINT not in set(pd.read_csv(root / "results" / "sections_dev.csv")["doc_id"])

    # The failed document is retried on the next run once the fault is gone.
    monkeypatch.setattr(run, "locate_document", original)
    assert run.main(["--split", "dev", "--systems", "rules,baseline", "--root", str(root)]) == 0
    assert read_predictions(root)["doc_id"].nunique() == 8


def test_parse_missing_parses_from_pdfs(tmp_path: Path):
    rows = [r for r in sample_rows() if r["doc_id"] == LIBERTY]
    write_manifest(tmp_path, rows)
    (tmp_path / "data" / "pdfs").mkdir(parents=True)
    shutil.copy(SAMPLES / f"{rows[0]['stem']}.pdf", tmp_path / "data" / "pdfs" / f"{LIBERTY}.pdf")
    args = ["--split", "dev", "--systems", "rules", "--root", str(tmp_path)]
    assert run.main(args) == 1
    entry = run.read_run_log(tmp_path / "results" / "run_log_dev.jsonl")[0]
    assert entry["error"].startswith("FileNotFoundError") and "--parse-missing" in entry["error"]
    assert run.main([*args, "--parse-missing"]) == 0
    assert (tmp_path / "data" / "text" / f"{LIBERTY}.pages.json").exists()
    assert (tmp_path / "data" / "text" / f"{LIBERTY}.txt").exists()
    preds = read_predictions(tmp_path)
    assert len(preds) == len(RULE_FIELDS) and preds.loc[preds["field"] == "bank_id", "value"].item() == "26816"


def test_makefile_targets_use_real_flags():
    text = MAKEFILE.read_text(encoding="utf-8")
    pilot = text[text.index("pilot:") :].split("\n")[1]
    scale = text[text.index("scale:") :].split("\n")[1]
    assert "cra_extract.run --split dev --systems $(SYSTEMS)" in pilot
    assert "cra_extract.run --split corpus --systems $(SYSTEMS)" in scale
    assert "SYSTEMS ?= rules,baseline,llm:gemini:gemini-3.8-flash" in text
    assert "--systems rules,llm" not in text
