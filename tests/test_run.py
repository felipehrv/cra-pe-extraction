"""Tests for the run stage (``cra_extract.run``).

No network. A workspace is built from the eight sample PDFs under a temporary root with the
layout ``config/corpus.csv``, ``data/pdfs``, ``data/text``, ``data/traces`` and ``results``;
the PDFs are parsed once per session with pymupdf and copied per test. The LLM path uses a
``FakeBackend`` whose canned response quotes the SouthStar rating sentence
(FDIC_12299_SouthStar_2022_ISB_NI_fairlending.txt line 56).

Assessment-area counts: SouthStar designates five assessment areas (line 165) and prints one
"X AA - Full/Limited-Scope Review" block each (lines 606, 935, 1246, 1403, 1561); FieldPoint
has four (line 221), one MSA or MD block each (lines 647, 1219, 1741, 2210).

Field-group modes: ``all`` (the default) sends one LLM call per document, ``split`` one per
``llm_field_groups`` entry of fields.yaml (``N_GROUPS``); ``CALLS`` maps each mode to its
calls per document.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import shutil
import socket
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from conftest import SAMPLE_META, SAMPLES

from cra_extract import llm, parse, prompts, run
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
    """Canned structured response; the rating quote is on SouthStar page 3.

    ``prompts`` (shared between instances when passed in) records every prompt sent. The
    closed-book schema gets a closed-book answer (S, no finding).
    """

    name = "fake"

    def __init__(
        self,
        model: str = "fake-1",
        prompts: list[dict[str, Any]] | None = None,
        effort: str | None = None,
        limiter: llm.RateLimiter | None = None,
    ) -> None:
        self.model = model
        self.calls = 0
        self.prompts = prompts if prompts is not None else []
        self.effort = effort
        self.limiter = limiter

    def _call(self, system_prompt, user_prompt, json_schema, model):
        self.calls += 1
        self.prompts.append({"system": system_prompt, "user": user_prompt, "schema": json_schema})
        data: dict[str, Any] = {}
        if json_schema.get("title") == "cra_pe_closedbook":
            data = {
                "overall_rating": {"status": "value", "value": "S"},
                "finding_present": {"status": "value", "value": False},
            }
        elif "overall_rating" in json_schema["properties"]:
            data["overall_rating"] = {
                "status": "value", "value": "NI",
                "quote": "This institution is rated Needs to Improve.", "page": 3,
            }
        text = json.dumps(data)
        return llm.BackendResult(text=text, parsed_json=data, model_reported=model, input_tokens=100, output_tokens=20)


def fake_factory(spec: run.SystemSpec, split: str, allow_api: bool, paths: run.Paths) -> llm.Backend:
    """Backend factory for tests: replay over the traces with a FakeBackend as the live backend."""
    live = FakeBackend(spec.model, effort=spec.effort) if allow_api else None
    return llm.ReplayBackend(
        spec.backend, split, spec.model, live=live, allow_api=allow_api, trace_dir=paths.trace_dir,
        effort=spec.effort,
    )


def recording_factory(prompts: list[dict[str, Any]], limiter: llm.RateLimiter | None = None) -> run.BackendFactory:
    """Like ``fake_factory`` but every live call is recorded in ``prompts`` (and rate limited)."""

    def factory(spec: run.SystemSpec, split: str, allow_api: bool, paths: run.Paths) -> llm.Backend:
        live = FakeBackend(spec.model, prompts, spec.effort, limiter) if allow_api else None
        return llm.ReplayBackend(
            spec.backend, split, spec.model, live=live, allow_api=allow_api, trace_dir=paths.trace_dir,
            effort=spec.effort,
        )

    return factory


FAKE = run.llm_spec("fake", "fake-1")
FAKE_WHOLE = run.llm_spec("fake", "fake-1", run.WHOLE)
FAKE_CLOSED = run.llm_spec("fake", "fake-1", run.CLOSED_BOOK)
N_GROUPS = len(load_fields()["llm_field_groups"])
CALLS = {"all": 1, "split": N_GROUPS}
MODES = pytest.mark.parametrize("field_groups", ["all", "split"])


def read_predictions(root: Path, split: str = "dev") -> pd.DataFrame:
    return pd.read_csv(root / "results" / f"predictions_{split}.csv", dtype=str, keep_default_na=False)


# ----------------------------------------------------------------------------- unit


def test_parse_systems():
    specs = run.parse_systems("rules,baseline,llm:gemini:gemini-3.8-flash")
    assert [s.name for s in specs] == ["rules", "baseline", "llm:gemini:gemini-3.8-flash"]
    # Hosted backends default to effort low; the effort is not part of the system name.
    assert specs[2] == run.SystemSpec(
        "llm:gemini:gemini-3.8-flash", "llm", "gemini", "gemini-3.8-flash", "located", "low"
    )
    assert run.parse_systems(["rules", " baseline "])[1].kind == "baseline"
    for bad in ("llm", "llm:gemini", "llm:nope:model", "s3", "", "rules,rules", "llm:gemini::whole"):
        with pytest.raises(ValueError):
            run.parse_systems(bad)


def test_parse_systems_modes_effort_and_colon_models():
    # Ollama tags carry a colon (analysis plan, S4 row): everything after the backend is the model.
    ollama = run.parse_systems("llm:ollama:llama3.1:8b-instruct-q4_K_M")[0]
    assert (ollama.backend, ollama.model, ollama.mode, ollama.effort) == ("ollama", "llama3.1:8b-instruct-q4_K_M", "located", None)
    whole = run.parse_systems("llm:gemini:gemini-3.8-flash:whole")[0]
    assert (whole.name, whole.model, whole.mode) == ("llm:gemini:gemini-3.8-flash:whole", "gemini-3.8-flash", "whole")
    closed = run.parse_systems("llm:ollama:qwen2.5:7b:closedbook")[0]
    assert (closed.model, closed.mode, closed.name) == ("qwen2.5:7b", "closedbook", "llm:ollama:qwen2.5:7b:closedbook")
    assert run.parse_systems("llm:anthropic:claude-haiku-4-5", effort="high")[0].effort == "high"
    assert run.parse_systems("llm:anthropic:claude-haiku-4-5", effort="none")[0].effort is None
    assert run.parse_systems("llm:ollama:llama3.1", effort="low")[0].effort == "low"
    with pytest.raises(ValueError):
        run.parse_systems("llm:gemini:m", effort="extreme")
    # --whole-document / --closed-book switch located LLM systems only, keeping the effort.
    specs = run.with_mode(run.parse_systems("rules,llm:gemini:m,llm:ollama:l:closedbook", effort="medium"), run.WHOLE)
    assert [s.name for s in specs] == ["rules", "llm:gemini:m:whole", "llm:ollama:l:closedbook"]
    assert specs[1].effort == "medium" and specs[1].mode == "whole"
    with pytest.raises(ValueError):
        run.with_mode(run.parse_systems("llm:gemini:m,llm:gemini:m:whole"), run.WHOLE)


def test_hash_sample_orders_by_sha256_of_doc_id():
    ids = ["FDIC_12299_20220912", "FRB_3664588_20240116", "OCC_11715_20240408", "FDIC_6078_20240805"]
    records = [llm.sample_doc("FDIC_12299_SouthStar_2022_ISB_NI_fairlending").model_copy(update={"doc_id": i}) for i in ids]
    expected = sorted(ids, key=lambda i: hashlib.sha256(i.encode("utf-8")).hexdigest())
    assert [d.doc_id for d in run.hash_sample(records, 3)] == expected[:3]
    assert run.hash_sample(records, 0) == []


def test_aa_unit_key_normalizes_fdic_and_frb_headings():
    assert run.aa_unit_key("AUSTIN MSA AA – Full-Scope Review") == "austin msa"
    assert run.aa_unit_key("DESCRIPTION OF INSTITUTION’S OPERATIONS IN AUSTIN MSA AA") == "austin msa"
    assert run.aa_unit_key("NON-MSA AA – Limited-Scope Review") == "non-msa"
    assert run.aa_unit_key("MD 35614 (NEW YORK-JERSEY CITY-WHITE PLAINS, NY-NJ) FULL SCOPE REVIEW") == "35614"
    assert run.aa_unit_key("DESCRIPTION OF INSTITUTION’S OPERATIONS IN MD 35614 (NEW YORK") == "35614"


def _pages(*texts: str) -> list:
    from cra_extract.models import Page

    return [Page(number=i, text=t) for i, t in enumerate(texts, start=1)]


def test_count_aa_sections_counts_bare_dallas_markers_per_occurrence():
    # FRB_709143_20230130 prints the bare heading once per AA (pp12, 29, 44): three units.
    body = "The bank operates branches in the area.\n"
    pages = _pages(
        "PUBLIC DISCLOSURE\n",
        "DESCRIPTION OF INSTITUTION'S OPERATIONS\n\n" + body,
        "DESCRIPTION OF INSTITUTION'S OPERATIONS\n\n" + body,
        "DESCRIPTION OF INSTITUTION'S OPERATIONS\n\n" + body,
    )
    assert run.aa_unit_key("DESCRIPTION OF THE INSTITUTION'S OPERATIONS") == ""
    assert run.count_aa_sections(pages, "FRB") == 3


def test_count_aa_sections_dedups_fdic_names_and_ignores_appendix_prose():
    # FDIC_12303_20220921: two AAs, each printed twice (pp9, 14); glossary prose on p21 is in
    # the appendix scope and opens nothing.
    body = "The bank operates branches in the area.\n"
    pages = _pages(
        "PUBLIC DISCLOSURE\n",
        "NON-MSA AA – Full-Scope Review\n\nDESCRIPTION OF INSTITUTION'S OPERATIONS IN NON-MSA AA\n\n" + body,
        "WACO MSA AA – Full-Scope Review\n\nDESCRIPTION OF INSTITUTION'S OPERATIONS IN WACO MSA AA\n\n" + body,
        "GLOSSARY\n\nbranches in two or more states within a multistate metropolitan area, the institution will\n"
        "rating for the multistate metropolitan area.\n",
    )
    assert run.count_aa_sections(pages, "FDIC") == 2


def test_gemini_limiter_reads_free_tier_from_prices(tmp_path, monkeypatch):
    prices = {"gemini": {"free_tier": {"requests_per_minute": 5, "requests_per_day": 20}}}
    monkeypatch.setattr(run, "load_prices", lambda: prices)
    limiter = run.gemini_limiter(tmp_path)
    assert (limiter.backend, limiter.per_minute, limiter.per_day) == ("gemini", 5, 20)
    assert limiter.path == tmp_path / "ratelimit_gemini.json"
    # The quota day ends at midnight Pacific time: 2023-11-15 05:00 UTC is still 11-14 there.
    assert limiter.reset_tz == ZoneInfo("America/Los_Angeles")
    limiter._clock = lambda: 1_700_024_400.0
    assert limiter._today() == "2023-11-14"
    prices["gemini"]["free_tier"]["reset_timezone"] = "UTC"
    assert run.gemini_limiter(tmp_path).reset_tz == ZoneInfo("UTC")
    monkeypatch.setattr(run, "load_prices", lambda: {"gemini": {"free_tier": {}}})
    assert run.gemini_limiter(tmp_path).per_minute is None


def test_build_llm_backend_wires_limiter_only_with_allow_api(tmp_path, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_make_backend(name, model, **kwargs):
        captured.update(name=name, model=model, **kwargs)
        return FakeBackend(model, effort=kwargs.get("effort"))

    monkeypatch.setattr(llm, "make_backend", fake_make_backend)
    paths = run.Paths.under(tmp_path)
    spec = run.parse_systems("llm:gemini:gemini-3.8-flash")[0]
    replay = run.build_llm_backend(spec, "dev", allow_api=False, paths=paths)
    assert isinstance(replay, llm.ReplayBackend) and replay.live is None and not captured
    assert replay.path == paths.trace_dir / "dev" / "gemini.jsonl" and replay.effort == "low"
    live = run.build_llm_backend(spec, "dev", allow_api=True, paths=paths)
    assert isinstance(live.live, FakeBackend)
    assert isinstance(captured["limiter"], llm.RateLimiter) and captured["limiter"].backend == "gemini"
    assert captured["effort"] == "low" and live.live.effort == "low"
    captured.clear()
    run.build_llm_backend(run.parse_systems("llm:ollama:llama3.1")[0], "dev", True, paths)
    assert "limiter" not in captured and captured["name"] == "ollama" and captured["effort"] is None


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


@MODES
def test_llm_fake_backend_writes_traces_and_predictions(root: Path, field_groups: str):
    paths = run.Paths.under(root)
    # parse_systems rejects unknown backend names, so the fake spec is built directly.
    systems = [run.SystemSpec("llm:fake:fake-1", "llm", "fake", "fake-1")]
    kw = {"field_groups": field_groups}

    # Replay without traces: every pair fails with a TraceMissError and nothing is written.
    summary = run.run_split("dev", systems, paths=paths, backend_factory=fake_factory, limit=2, **kw)
    assert summary.n_errors == 2 and summary.n_pairs_written == 0 and summary.exit_code == 1
    assert not paths.predictions("dev").exists()
    entries = run.read_run_log(paths.run_log("dev"))
    assert all(e["error"].startswith("TraceMissError") for e in entries)

    # Live (fake) calls record one trace per call (one per document under all, one per field
    # group under split) and one row per field either way.
    summary = run.run_split("dev", systems, paths=paths, backend_factory=fake_factory, allow_api=True, **kw)
    assert summary.n_errors == 0 and summary.n_pairs_written == 8
    traces = llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")
    assert len(traces) == 8 * CALLS[field_groups]
    expected_groups = {"all"} if field_groups == "all" else set(load_fields()["llm_field_groups"])
    assert {t.field_group for t in traces} == expected_groups and {t.mode for t in traces} == {"located"}
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
    assert len(log_ok) == 8 and all(e["tokens"] == CALLS[field_groups] * 120 and e["cost_usd"] == 0 for e in log_ok)
    assert {e["field_groups"] for e in log_ok} == {field_groups}

    # A forced rerun without --allow-api replays every call from the stored traces.
    summary = run.run_split("dev", systems, paths=paths, backend_factory=fake_factory, force=True, **kw)
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
    assert "SYSTEMS ?= rules,baseline,llm:gemini:gemini-3.5-flash-lite" in text
    assert "--systems rules,llm" not in text
    # Every run target with located or whole-document LLM calls passes the field-group mode.
    assert f"FIELD_GROUPS ?= {run.DEFAULT_FIELD_GROUPS}" in text
    for target in ("pilot:", "scale:", "project:", "dev-rerun:", "ablation:"):
        assert "--field-groups $(FIELD_GROUPS)" in text[text.index(target) :].split("\n")[1], target


# ----------------------------------------------------------------------------- LLM modes


def sample_text(doc_id: str) -> str:
    stem = next(r["stem"] for r in sample_rows() if r["doc_id"] == doc_id)
    return (SAMPLES / f"{stem}.txt").read_text(encoding="utf-8")


@MODES
def test_whole_document_mode_sends_whole_text_and_marks_locator_na(root: Path, field_groups: str):
    paths = run.Paths.under(root)
    sent: list[dict[str, Any]] = []
    calls = CALLS[field_groups]
    summary = run.run_split(
        "dev", [FAKE, FAKE_WHOLE], paths=paths, allow_api=True, backend_factory=recording_factory(sent),
        field_groups=field_groups,
    )
    assert summary.n_errors == 0 and summary.n_pairs_written == 16
    preds = read_predictions(root)
    whole = preds[preds["system"] == "llm:fake:fake-1:whole"]
    located = preds[preds["system"] == "llm:fake:fake-1"]
    assert len(whole) == len(located) == 8 * N_LLM_FIELDS
    assert set(whole["locator_miss"]) == {run.LOCATOR_MISS_NA} and set(located["locator_miss"]) == {"0"}
    # The whole document goes to every call: one "document" section of all pages.
    whole_prompts = [p["user"] for p in sent if "=== section: document | scope: institution" in p["user"]]
    assert len(whole_prompts) == 8 * calls
    pages = parse.load_pages(SOUTHSTAR, root / "data" / "text")
    text = parse.doc_text(pages)
    traces = llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")
    ss_whole = [t for t in traces if t.doc_id == SOUTHSTAR and t.section_offsets == [(0, len(text))]]
    assert len(ss_whole) == calls
    # Trace.mode attributes each call to its run system (cost accounting).
    assert {t.mode for t in ss_whole} == {"whole"}
    assert {t.mode for t in traces} == {"whole", "located"}
    # Every SouthStar whole-document prompt holds the full text, every page joined.
    ss_prompts = [p for p in whole_prompts if pages[0].text.strip()[:80] in p]
    assert len(ss_prompts) == calls and all(text.strip() in p for p in ss_prompts)
    ss = whole[whole["doc_id"] == SOUTHSTAR].set_index("field")
    assert ss.loc["overall_rating", "value"] == "NI" and ss.loc["overall_rating", "page"] == "3"
    assert ss.loc["overall_rating", "grounding_failure"] == ""
    # Tokens are recorded per (document, system) in the run log.
    entries = [e for e in run.read_run_log(paths.run_log("dev")) if e["system"] == "llm:fake:fake-1:whole"]
    assert len(entries) == 8 and all(e["input_tokens"] == calls * 100 and e["mode"] == "whole" for e in entries)
    assert {e["field_groups"] for e in entries} == {field_groups}
    assert all(e["n_locator_fallbacks"] == 0 for e in entries)
    # evaluate reads "na" as no locator flag.
    from cra_extract.evaluate import read_predictions as eval_read

    fvs, _ = eval_read(paths.predictions("dev"))
    assert not any("locator_miss" in (f.notes or "") for f in fvs if f.system.endswith(":whole"))


@MODES
def test_closed_book_prompts_contain_no_pe_text(root: Path, field_groups: str):
    paths = run.Paths.under(root)
    # No text and no PDFs: the closed-book path never loads the document. It is one call per
    # document under either field-group mode.
    shutil.rmtree(root / "data" / "text")
    shutil.rmtree(root / "data" / "pdfs")
    sent: list[dict[str, Any]] = []
    summary = run.run_split(
        "dev", [FAKE_CLOSED], paths=paths, allow_api=True, backend_factory=recording_factory(sent),
        field_groups=field_groups,
    )
    assert summary.n_errors == 0 and summary.n_pairs_written == 8 and len(sent) == 8
    rows = {r["doc_id"]: r for r in sample_rows()}
    for p in sent:
        prompt = p["system"] + "\n" + p["user"]
        doc_id = next(d for d, r in rows.items() if f"Bank: {r['bank_name']}" in p["user"])
        r = rows[doc_id]
        assert f"Evaluation date: {r['exam_date']}" in p["user"]
        assert doc_id not in prompt and r["regulator"] not in prompt
        assert "Federal Reserve" not in prompt and "Comptroller" not in prompt
        assert "Small Bank" not in prompt and "Regulator" not in prompt
        lines = {ln.strip() for ln in sample_text(doc_id).splitlines() if len(ln.strip()) >= 25}
        assert lines and not [ln for ln in lines if ln in prompt]
        assert set(p["schema"]["properties"]) == {"overall_rating", "finding_present"}
    preds = read_predictions(root)
    assert set(preds["system"]) == {"llm:fake:fake-1:closedbook"}
    assert set(preds["field"]) == {"overall_rating", "finding_present"} and len(preds) == 16
    assert set(preds["locator_miss"]) == {run.LOCATOR_MISS_NA}
    assert set(preds["grounding_failure"]) == {"quote_not_found"}
    assert set(preds.loc[preds["field"] == "finding_present", "value"]) == {"false"}
    assert not paths.sections("dev").exists()
    traces = llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")
    assert {t.field_group for t in traces} == {"closedbook"} and all(t.section_offsets == [] for t in traces)
    assert {t.mode for t in traces} == {"closedbook"}
    assert {e["field_groups"] for e in run.read_run_log(paths.run_log("dev"))} == {None}
    assert traces[0].prompt_hash == llm.prompt_hash(
        sent[0]["system"], sent[0]["user"], sent[0]["schema"], prompts.CLOSED_BOOK_PROMPT_VERSION
    )
    # A forced rerun replays every closed-book call.
    other = "split" if field_groups == "all" else "all"
    summary = run.run_split(
        "dev", [FAKE_CLOSED], paths=paths, force=True, backend_factory=recording_factory(sent), field_groups=other
    )
    assert summary.n_pairs_written == 8 and len(sent) == 8


def test_trace_dir_isolates_a_second_live_run_and_agreement(root: Path):
    first = run.Paths.under(root)
    sent: list[dict[str, Any]] = []
    run.run_split("dev", [FAKE], paths=first, allow_api=True, backend_factory=recording_factory(sent))
    assert len(sent) == 8 * CALLS[run.DEFAULT_FIELD_GROUPS]
    first_traces = (first.trace_dir / "dev" / "fake.jsonl").read_text(encoding="utf-8")

    second = dataclasses.replace(
        first, trace_dir=root / "data" / "traces" / "dev_rerun", results_dir=root / "results" / "dev_rerun"
    )
    # Without --allow-api the second directory has nothing to replay: the first run is not served.
    summary = run.run_split("dev", [FAKE], paths=second, backend_factory=recording_factory(sent))
    assert summary.n_errors == 8 and summary.n_pairs_written == 0
    summary = run.run_split("dev", [FAKE], paths=second, allow_api=True, backend_factory=recording_factory(sent))
    assert summary.n_pairs_written == 8 and len(sent) == 2 * 8 * CALLS[run.DEFAULT_FIELD_GROUPS]
    assert (first.trace_dir / "dev" / "fake.jsonl").read_text(encoding="utf-8") == first_traces
    assert len(llm.read_traces(second.trace_dir / "dev" / "fake.jsonl")) == 8 * CALLS[run.DEFAULT_FIELD_GROUPS]
    assert second.predictions("dev").exists() and len(read_predictions(root)) == 8 * N_LLM_FIELDS

    out = root / "results" / "run_agreement_dev.csv"
    table = run.agreement(first.predictions("dev"), second.predictions("dev"), out)
    assert list(table.columns) == run.AGREEMENT_COLUMNS and out.exists()
    assert set(table["system"]) == {"llm:fake:fake-1"} and len(table) == N_LLM_FIELDS
    assert (table["n"] == 8).all() and (table["agreement"] == 1.0).all()
    lo, hi = run.wilson_ci(8, 8)
    assert table["ci_low"].iloc[0] == pytest.approx(lo) and table["ci_high"].iloc[0] == pytest.approx(hi)
    # Traces pair by prompt hash and give the same table.
    by_traces = run.agreement(first.trace_dir / "dev", second.trace_dir / "dev")
    assert by_traces[["field", "n", "n_agree"]].equals(table[["field", "n", "n_agree"]])
    with pytest.raises(ValueError):
        run.agreement(first.predictions("dev"), second.trace_dir)

    # One changed value and one changed status in the second run; one item only in the first.
    df = pd.read_csv(second.predictions("dev"), dtype=str, keep_default_na=False)
    rating = (df["doc_id"] == SOUTHSTAR) & (df["field"] == "overall_rating")
    df.loc[rating, "value"] = "S"
    df.loc[(df["doc_id"] == LIBERTY) & (df["field"] == "overall_rating"), "status"] = "abstain"
    df = df[~((df["doc_id"] == FIELDPOINT) & (df["field"] == "overall_rating"))]
    df.to_csv(second.predictions("dev"), index=False)
    row = run.agreement(first.predictions("dev"), second.predictions("dev")).set_index("field").loc["overall_rating"]
    assert (row["n"], row["n_agree"], row["n_value_mismatch"], row["n_status_mismatch"]) == (7, 5, 1, 1)
    assert (row["n_only_a"], row["n_only_b"]) == (1, 0)
    assert row["agreement"] == pytest.approx(5 / 7)
    assert (row["ci_low"], row["ci_high"]) == pytest.approx(run.wilson_ci(5, 7))


def test_agreement_cli_and_trace_dir_refused_on_test(root: Path, capsys):
    paths = run.Paths.under(root)
    run.run_split("dev", [FAKE], paths=paths, allow_api=True, backend_factory=fake_factory)
    out = root / "agree.csv"
    pred = str(paths.predictions("dev"))
    assert run.main(["agreement", pred, pred, "--out", str(out)]) == 0
    assert pd.read_csv(out)["agreement"].eq(1.0).all()
    assert run.main(["agreement", pred, str(root / "missing.csv")]) == 2
    with pytest.raises(SystemExit) as exc:
        run.main(["--split", "test", "--systems", "llm:gemini:m", "--trace-dir", str(root / "x"), "--root", str(root)])
    assert exc.value.code == 2
    assert "refused on test" in capsys.readouterr().err


def test_main_passes_effort_trace_dir_and_results_dir(root: Path, monkeypatch, capsys):
    made: list[dict[str, Any]] = []

    def fake_make_backend(name, model, **kwargs):
        made.append({"name": name, "model": model, **kwargs})
        return FakeBackend(model, effort=kwargs.get("effort"), limiter=kwargs.get("limiter"))

    monkeypatch.setattr(llm, "make_backend", fake_make_backend)
    monkeypatch.setattr(run, "load_prices", lambda: {"gemini": {"free_tier": {"requests_per_minute": None, "requests_per_day": 100}}})
    traces, results = root / "data" / "traces" / "dev_rerun", root / "results" / "dev_rerun"
    args = [
        "--split", "dev", "--systems", "llm:gemini:gemini-3.8-flash", "--allow-api", "--doc-id", SOUTHSTAR,
        "--root", str(root), "--trace-dir", str(traces), "--results-dir", str(results),
    ]
    assert run.main(args) == 0
    assert made[0]["effort"] == "low" and made[0]["limiter"].reset_tz == ZoneInfo("America/Los_Angeles")
    written = llm.read_traces(traces / "dev" / "gemini.jsonl")
    # The default field-group mode is all: one call for the document.
    assert len(written) == 1 and {t.effort for t in written} == {"low"} and written[0].field_group == "all"
    assert not (root / "data" / "traces" / "dev").exists() and not (root / "results" / "predictions_dev.csv").exists()
    assert (results / "predictions_dev.csv").exists() and (results / "run_log_dev.jsonl").exists()
    # The daily count is persisted under the root's cache directory.
    assert json.loads((root / "data" / "cache" / "ratelimit_gemini.json").read_text())["count"] == 1
    # Another effort is another replay key: a replay-only run at effort high misses.
    assert run.main([*args[:4], "--doc-id", SOUTHSTAR, "--root", str(root), "--trace-dir", str(traces),
                     "--results-dir", str(root / "results" / "high"), "--effort", "high"]) == 1
    entry = run.read_run_log(root / "results" / "high" / "run_log_dev.jsonl")[0]
    assert entry["error"].startswith("TraceMissError") and entry["effort"] == "high"
    capsys.readouterr()
    # --whole-document renames the system; replay of a whole run without traces misses.
    assert run.main([*args[:4], "--whole-document", "--doc-id", SOUTHSTAR, "--root", str(root),
                     "--results-dir", str(root / "results" / "w")]) == 1
    entry = run.read_run_log(root / "results" / "w" / "run_log_dev.jsonl")[0]
    assert entry["system"] == "llm:gemini:gemini-3.8-flash:whole" and entry["mode"] == "whole"
    # --field-groups split sends the four group calls; the all trace is not replayed for them.
    assert run.main([*args, "--field-groups", "split", "--results-dir", str(root / "results" / "s")]) == 0
    written = llm.read_traces(traces / "dev" / "gemini.jsonl")
    assert len(written) == 1 + N_GROUPS and {t.field_group for t in written[1:]} == set(load_fields()["llm_field_groups"])
    assert run.read_run_log(root / "results" / "s" / "run_log_dev.jsonl")[0]["field_groups"] == "split"
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        run.main([*args, "--field-groups", "groups"])
    assert exc.value.code == 2


def test_daily_limit_halt_resumes_without_repeating_calls(root: Path, tmp_path: Path):
    paths = run.Paths.under(root)
    clock = {"t": 1_700_000_000.0}
    limiter = llm.RateLimiter(
        "fake", per_day=14, cache_dir=tmp_path, clock=lambda: clock["t"], reset_tz=ZoneInfo("America/Los_Angeles")
    )
    sent: list[dict[str, Any]] = []
    factory = recording_factory(sent, limiter)

    # Split mode (four calls per document), so a halt can fall inside a document.
    def run_split(**kw: Any) -> run.RunSummary:
        return run.run_split("dev", [FAKE], paths=paths, allow_api=True, backend_factory=factory, field_groups="split", **kw)

    # Day 1: 14 calls = 3 documents (12 calls) and half of the fourth, then the halt.
    s1 = run_split()
    assert s1.halted and s1.n_pairs_written == 3 and s1.exit_code == 1 and len(sent) == 14
    assert read_predictions(root)["doc_id"].nunique() == 3
    assert run.read_run_log(paths.run_log("dev"))[-1]["error"].startswith("DailyLimitExceeded")
    # Same day: nothing new can be sent; the stored traces of the half document replay.
    s2 = run_split()
    assert s2.halted and s2.n_pairs_written == 0 and len(sent) == 14
    # Next quota day: the fourth document finishes from 2 stored + 2 new calls.
    clock["t"] += 86_400
    s3 = run_split()
    assert s3.halted and s3.n_pairs_written == 4 and s3.n_skipped == 3 and len(sent) == 28
    clock["t"] += 86_400
    s4 = run_split()
    assert not s4.halted and s4.n_pairs_written == 1 and s4.exit_code == 0
    # Every prompt was sent exactly once and every trace written once.
    assert len(sent) == 8 * N_GROUPS
    traces = llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")
    assert len(traces) == 8 * N_GROUPS == len({t.prompt_hash for t in traces})
    assert len(read_predictions(root)) == 8 * N_LLM_FIELDS


def test_daily_limit_in_all_mode_counts_one_request_per_document(root: Path, tmp_path: Path):
    paths = run.Paths.under(root)
    clock = {"t": 1_700_000_000.0}
    limiter = llm.RateLimiter(
        "fake", per_day=3, cache_dir=tmp_path, clock=lambda: clock["t"], reset_tz=ZoneInfo("America/Los_Angeles")
    )
    sent: list[dict[str, Any]] = []
    factory = recording_factory(sent, limiter)
    days = []
    while True:
        s = run.run_split("dev", [FAKE], paths=paths, allow_api=True, backend_factory=factory)
        days.append(s.n_pairs_written)
        if not s.halted:
            break
        clock["t"] += 86_400
    # 3 requests a day give 3 documents a day: 8 documents in 3 quota days, each sent once.
    assert days == [3, 3, 2] and len(sent) == 8
    assert len(read_predictions(root)) == 8 * N_LLM_FIELDS


# ----------------------------------------------------------------------------- field-group modes


def _located(paths: run.Paths, doc_id: str) -> tuple[Any, run.Located]:
    doc = run.select_documents(paths, "dev", doc_id)[0]
    return doc, run.locate_document(doc_id, parse.load_pages(doc_id, paths.text_dir), doc.regulator)


def test_all_mode_prompt_lists_every_section_and_field_once(root: Path):
    paths = run.Paths.under(root)
    combined = prompts.llm_field_group("all")
    for r in sample_rows():
        doc, located = _located(paths, r["doc_id"])
        calls = run.llm_inputs(doc, located, field_groups="all")
        assert len(calls) == 1
        group, secs, fallback = calls[0]
        assert group == "all" and fallback is False
        # Every located section of the union is sent, once, in document order.
        assert set(secs) == {n for n in combined["sections"] if n in located.sections}
        starts = [sec.char_start for sec in secs.values()]
        assert starts == sorted(starts)
        user = prompts.build_prompt(doc, secs, group)
        for name, sec in secs.items():
            assert user.count(f"=== section: {name} | scope: {sec.scope} |") == 1, (r["doc_id"], name)
        labels = [user.index(f"=== section: {name} |") for name in secs]
        assert labels == sorted(labels)
        # Every LLM field once, the same set and order as the schema.
        for name in combined["fields"]:
            assert user.count(f"\n- {name} (") == 1, (r["doc_id"], name)
        schema = prompts.build_schema(group)
        assert list(schema["properties"]) == combined["fields"] == schema["required"]
        # The four split calls send the same sections between them, each in one call.
        split = run.llm_inputs(doc, located, field_groups="split")
        assert [g for g, _, _ in split] == prompts.split_group_names()
        assert sorted(n for _, ss, _ in split for n in ss) == sorted(secs)


def test_field_group_modes_never_replay_each_other_and_resume_warns(root: Path, caplog):
    paths = run.Paths.under(root)
    sent: list[dict[str, Any]] = []
    factory = recording_factory(sent)
    assert run.run_split("dev", [FAKE], paths=paths, allow_api=True, backend_factory=factory, field_groups="split").n_pairs_written == 8
    split_traces = llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")
    assert len(split_traces) == 8 * N_GROUPS

    # Same documents, same sections: the all prompts hash differently from every split prompt.
    doc, located = _located(paths, SOUTHSTAR)
    hashes = {
        mode: {llm.prompt_hash(sp, up, schema, version) for _, sp, up, schema, version, _ in run.call_prompts(doc, located, run.LOCATED, mode)}
        for mode in ("all", "split")
    }
    assert len(hashes["all"]) == 1 and len(hashes["split"]) == N_GROUPS and not hashes["all"] & hashes["split"]
    assert hashes["split"] == {t.prompt_hash for t in split_traces if t.doc_id == SOUTHSTAR}

    # The split pairs are in the predictions file: an all run keeps and skips them, with a warning.
    with caplog.at_level("WARNING"):
        summary = run.run_split("dev", [FAKE], paths=paths, backend_factory=factory)
    assert summary.n_skipped == 8 and summary.n_pairs_written == 0 and summary.n_pairs_other_field_groups == 8
    assert "written with --field-groups split" in caplog.text
    # Recomputed in the all mode without --allow-api: no split trace is served for an all call.
    summary = run.run_split("dev", [FAKE], paths=paths, backend_factory=factory, force=True)
    assert summary.n_errors == 8 and summary.n_pairs_written == 0
    assert all(e["error"].startswith("TraceMissError") for e in run.read_run_log(paths.run_log("dev"))[-8:])
    summary = run.run_split("dev", [FAKE], paths=paths, allow_api=True, backend_factory=factory, force=True)
    assert summary.n_pairs_written == 8 and len(sent) == 8 * N_GROUPS + 8
    traces = llm.read_traces(paths.trace_dir / "dev" / "fake.jsonl")
    all_traces = [t for t in traces if t.field_group == "all"]
    assert len(all_traces) == 8 and {t.mode for t in all_traces} == {"located"}
    assert not {t.prompt_hash for t in all_traces} & {t.prompt_hash for t in split_traces}
    assert run.recorded_field_groups(paths.run_log("dev")) == {(d, FAKE.name): "all" for d in {t.doc_id for t in all_traces}}
    # The per-field rows have the same shape as in the split mode.
    preds = read_predictions(root)
    assert preds.groupby("doc_id").size().to_dict() == {t.doc_id: N_LLM_FIELDS for t in all_traces}
    # Both modes replay their own traces: a forced split rerun sends nothing.
    assert run.run_split("dev", [FAKE], paths=paths, backend_factory=factory, force=True, field_groups="split").n_pairs_written == 8
    assert len(sent) == 8 * N_GROUPS + 8
    # Run-log entries without the key (before --field-groups) count as split.
    legacy = paths.results_dir / "legacy.jsonl"
    run.append_run_log(legacy, {"doc_id": SOUTHSTAR, "system": FAKE.name, "mode": "located", "error": None})
    run.append_run_log(legacy, {"doc_id": SOUTHSTAR, "system": FAKE_CLOSED.name, "mode": "closedbook", "error": None})
    run.append_run_log(legacy, {"doc_id": SOUTHSTAR, "system": "rules", "mode": None, "error": None})
    assert run.recorded_field_groups(legacy) == {(SOUTHSTAR, FAKE.name): "split"}


def test_all_mode_locator_fallback_keeps_per_field_flags(root: Path):
    paths = run.Paths.under(root)
    doc, located = _located(paths, SOUTHSTAR)
    review_fields = set(prompts.llm_field_group("review")["fields"])
    missing = run.Located(
        located.pages,
        {n: sec for n, sec in located.sections.items() if n != "illegal_credit_practices_review"},
        [*located.missing, "illegal_credit_practices_review"],
        located.n_aa_sections,
    )
    # all: one call, and the whole document replaces the located sections because one group
    # (review) has none; split: only the review call falls back.
    (group, secs, fallback), = run.llm_inputs(doc, missing, field_groups="all")
    assert (group, list(secs), fallback) == ("all", ["document"], True)
    split = {g: (list(ss), fb) for g, ss, fb in run.llm_inputs(doc, missing, field_groups="split")}
    assert split["review"] == (["document"], True) and not any(fb for g, (_, fb) in split.items() if g != "review")
    rows = {}
    for mode in ("all", "split"):
        out = run.run_llm(doc, missing, FakeBackend(), "dev", paths.trace_dir, field_groups=mode)
        assert out.n_locator_fallbacks == 1
        rows[mode] = {r["field"]: r["locator_miss"] for r in out.rows}
    # locator_miss is the field's own group miss in both modes.
    assert rows["all"] == rows["split"] == {f: int(f in review_fields) for f in rows["split"]}
    assert run.field_locator_misses(missing) == {f: f in review_fields for f in rows["all"]}
    # The whole-document mode sends the whole document once under all and marks na.
    whole = run.run_llm(doc, missing, FakeBackend(), "dev", paths.trace_dir, mode=run.WHOLE, field_groups="all")
    assert len(whole.traces) == 1 and {r["locator_miss"] for r in whole.rows} == {run.LOCATOR_MISS_NA}
    assert whole.n_locator_fallbacks == 0 and whole.traces[0].mode == "whole"


# ----------------------------------------------------------------------------- projection


def test_projection_needs_no_network_and_counts_calls(root: Path, monkeypatch, capsys):
    def no_network(*args, **kwargs):
        raise AssertionError("the projection must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(llm, "make_backend", no_network)
    monkeypatch.setattr(llm.GeminiBackend, "__init__", no_network)
    monkeypatch.setattr(run, "load_prices", lambda: {"gemini": {"free_tier": {"requests_per_day": 10}}})
    args = ["--split", "dev", "--systems", "rules,llm:gemini:gemini-3.8-flash", "--project", "--root", str(root)]
    # Default field-group mode all: one call per document, 8 calls, 1 day at 10 requests a day.
    assert run.main(args) == 0
    out = capsys.readouterr().out
    assert "(field groups all): 8 documents" in out and "8 calls (0 in traces)" in out
    table = pd.read_csv(root / "results" / "cost_projection_dev.csv")
    assert list(table.columns) == run.PROJECTION_COLUMNS and len(table) == 1
    row = table.iloc[0]
    assert (row["n_docs"], row["n_calls"], row["n_calls_cached"], row["n_calls_new"]) == (8, 8, 0, 8)
    assert row["field_groups"] == "all" and row["days_at_free_tier"] == 1
    assert row["effort"] == "low" and not row["priced"]
    # chars / 4 of the prompts, which hold the located sections.
    assert row["input_tokens_all"] == row["input_tokens_new"] >= row["prompt_chars"] / 4 > row["section_chars"] / 4
    all_row = row
    # Split mode: four calls per document, 32 calls, 4 days.
    assert run.main([*args, "--field-groups", "split"]) == 0
    assert "(field groups split): 8 documents" in capsys.readouterr().out
    row = pd.read_csv(root / "results" / "cost_projection_dev.csv").iloc[0]
    assert (row["n_docs"], row["n_calls"], row["n_calls_new"]) == (8, 32, 32)
    assert row["field_groups"] == "split" and row["days_at_free_tier"] == 4
    # The same sections are sent once per document either way; split repeats the system
    # prompt, header and section notes per group.
    assert row["section_chars"] == all_row["section_chars"] and row["prompt_chars"] > all_row["prompt_chars"]
    assert not (root / "results" / "predictions_dev.csv").exists() and not (root / "data" / "traces" / "dev").exists()


@MODES
def test_projection_independent_count_cache_prices_and_modes(root: Path, monkeypatch, field_groups: str):
    paths = run.Paths.under(root)
    kw = {"field_groups": field_groups}
    n = 8 * CALLS[field_groups]
    doc = run.select_documents(paths, "dev", SOUTHSTAR)[0]
    located = run.locate_document(SOUTHSTAR, parse.load_pages(SOUTHSTAR, paths.text_dir), "FDIC")
    expected = 0
    for group, secs, _ in run.llm_inputs(doc, located, **kw):
        chars = len(prompts.SYSTEM_PROMPT) + len(prompts.build_prompt(doc, secs, group)) + len(json.dumps(prompts.build_schema(group)))
        expected += -(-chars // 4)
    one = run.project("dev", [FAKE], paths=paths, doc_id=SOUTHSTAR, write=False, **kw).iloc[0]
    assert one["input_tokens_all"] == expected and one["n_calls"] == CALLS[field_groups]
    assert one["output_tokens_source"] == "none" and one["cost_usd_new"] == 0

    # After a live run every call is cached and output tokens come from the traces.
    run.run_split("dev", [FAKE], paths=paths, allow_api=True, backend_factory=fake_factory, **kw)
    monkeypatch.setattr(llm, "load_prices", lambda: {"x": {"models": {"fake-1": {"input": 1.0, "output": 2.0}}}})
    after = run.project("dev", [FAKE, FAKE_WHOLE, FAKE_CLOSED], paths=paths, **kw).set_index("system")
    loc, whole, closed = after.loc[FAKE.name], after.loc[FAKE_WHOLE.name], after.loc[FAKE_CLOSED.name]
    assert (loc["n_calls_cached"], loc["n_calls_new"], loc["input_tokens_new"], loc["cost_usd_new"]) == (n, 0, 0, 0)
    assert loc["output_tokens_per_call"] == 20 and loc["output_tokens_source"] == "traces" and loc["priced"]
    assert whole["n_calls_new"] == n and whole["input_tokens_all"] > loc["input_tokens_all"]
    assert whole["cost_usd_new"] == pytest.approx(round((whole["input_tokens_new"] + 2 * 20 * n) / 1e6, 4))
    assert closed["n_calls"] == 8 and closed["output_tokens_source"] == "none"
    assert closed["input_tokens_all"] < loc["input_tokens_all"] / 10
    assert (loc["field_groups"], whole["field_groups"], closed["field_groups"]) == (field_groups, field_groups, "")
    assert paths.cost_projection("dev").exists()
    # The other mode's prompts are not in the traces, and its output tokens are not borrowed.
    other = "split" if field_groups == "all" else "all"
    swapped = run.project("dev", [FAKE], paths=paths, write=False, field_groups=other).iloc[0]
    assert swapped["n_calls_cached"] == 0 and swapped["n_calls"] == 8 * CALLS[other]
    assert swapped["output_tokens_source"] == "none"
    with pytest.raises(ValueError):
        run.project("dev", run.parse_systems("rules"), paths=paths)
    with pytest.raises(ValueError):
        run.project("dev", [FAKE], paths=paths, field_groups="groups")
