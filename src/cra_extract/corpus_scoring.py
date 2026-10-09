"""Group A at corpus scale and the corpus risk file (design sections 6.3 and 6.5).

Implements two parts of ``docs/analysis_plan.md``:

"Group A at corpus scale" (:func:`score`). S1, the S3 corpus tier and S5 are compared with
the agency index on every evaluable document (``routing.evaluable``: every split except
``excluded``, a fetched PDF and a text layer). The comparison is
``group_a.compare_with_index``: the index truth of ``group_a.index_truth`` (FDIC exam_date
has none; prior fields only where a previous index row exists; component ratings against
the FRB CSV in FRB/isb and a gold-null code in the applicability ABS cells), scored with
``evaluate.score_cell`` and the field's tolerance. Per (truth, scope, system, field, cell):

- accuracy = correct / (correct + wrong + fabricated_absence), i.e. among non-abstained
  documents with an index value, with a Wilson 95 percent interval;
- abstention rate = abstentions / documents with truth (index value or ABS cell);
- fabrication rate = values returned in an ABS cell / documents in ABS cells (component
  ratings), as ``evaluate.metrics_table`` counts the fabrication denominator.

Cells are regulator x method x release year (18) plus regulator x method pooled
(``release_year`` = ``pooled``). ``truth`` is ``raw`` (the index, primary) or ``triaged``
(secondary, ``group_a.apply_triage`` over ``results/group_a_triage.csv``: a source_error
row counts the printed value, a definitional row is dropped). The triage covers only the S1
(rules) disagreements on ``group_a.TRIAGE_FIELDS``, so the ``triaged`` block is written for
S1 alone and its ``note`` says so. ``scope`` is ``all`` and, once the test split exists,
``test`` (test documents alone). Written to ``results/group_a_corpus.csv``. Every row has
``in_sample`` true: rules and prompts are tuned against corpus-wide index agreement, so the
figures are in-sample for tuning (plan, Group A at corpus scale, bullet 1).
``consistency_check`` is true for regulator and exam_method on S3, and on S5 only when S5
takes the field from S3 (the S3 prompt states both, so those values check consistency rather
than measure extraction; an S1-sourced S5 value is extracted). FDIC exam_date has index truth
only when ``fdic_exam_date_truth`` is set, which the CLI takes from the plan's gate
(``group_a.fdic_exam_date_gate``, written to ``results/fdic_exam_date_gate.csv``).

"Audit design" and "Risk score" (:func:`write_risk`). Flags per value of the chosen system
(S5 by default) come from ``evaluate.risk_table`` on a frame built from predictions only;
no gold label is read or accepted:

- ``locator_miss``: none of the field group's sections (``llm_field_groups`` of
  ``fields.yaml``) is found in the sections file; for a document missing from that file,
  or without one, the predictions' ``locator_miss`` column;
- ``disagreement``: the reference system answered the item with a value that does not
  match under the field's tolerance (the as-of quarter is not checked). The reference is
  S1 for S3, S3 for S1, and for S5 the system the value did not come from
  (``reference_system`` column, else the ``s5_from=`` note), which is S1 for every Group B
  value;
- ``groundedness_fail``: a non-empty ``grounding_failure`` cell, when the column exists;
- ``consistency_fail``: ``evaluate.consistency_fail`` on the system's own values.

``doc_risk`` sums ``n_flags`` over the document's Group B values (``evaluate.RISK_GROUPS``;
abstained values add 0). ``results/risk_corpus.csv`` holds ``doc_id`` and ``doc_risk`` (the
two columns ``strata.draw_audit`` reads) followed by detail columns; the per-value flags go
to ``results/risk_values_corpus.csv``. With a manifest, the Group A flags are validated
against the index corpus-wide: error rate by flag count over answered values with truth
(error = cell not correct), raw and triaged, in ``results/risk_validation_group_a.csv``.

CLI::

    python -m cra_extract.corpus_scoring score --predictions results/predictions_corpus.csv
        [results/predictions_dev.csv ...] --s3 llm:gemini:gemini-3.8-flash
    python -m cra_extract.corpus_scoring risk --predictions results/predictions_corpus.csv
        --s3 llm:gemini:gemini-3.8-flash --sections results/sections_corpus.csv
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from cra_extract.config import CORPUS_PATH, GOLD_DIR, RESULTS_DIR, load_fields
from cra_extract.evaluate import GROUNDING_NOT_GRADED, RISK_GROUPS, risk_table
from cra_extract.group_a import (
    INDEX_TRUTH_COLUMNS,
    REGULATORS,
    TRIAGE_FIELDS,
    apply_triage,
    compare_with_index,
    fdic_exam_date_gate,
    index_truth,
)
from cra_extract.models import FieldValue
from cra_extract.routing import (
    DEFAULT_SYSTEMS,
    ROUTING_YAML,
    S1_DEFAULT,
    S3_DEFAULT,
    S5_NAME,
    Systems,
    ensure_s5,
    evaluable,
    normalize_predictions,
    read_manifest,
    read_predictions,
)
from cra_extract.stats import wilson_ci

METHODS = ("small", "isb")
POOLED = "pooled"
TRIAGE_PATH = RESULTS_DIR / "group_a_triage.csv"
GROUP_A_CORPUS = RESULTS_DIR / "group_a_corpus.csv"
RISK_CORPUS = RESULTS_DIR / "risk_corpus.csv"
RISK_VALUES = RESULTS_DIR / "risk_values_corpus.csv"
RISK_VALIDATION = RESULTS_DIR / "risk_validation_group_a.csv"
FDIC_GATE = RESULTS_DIR / "fdic_exam_date_gate.csv"
IN_SAMPLE_NOTE = "in-sample for tuning (rules and prompts tuned on corpus-wide index agreement)"
TRIAGED_NOTE = (
    "S1-triaged truth: the triage covers S1 disagreements on " + ", ".join(TRIAGE_FIELDS)
    + "; other fields equal the raw index"
)
FLAGS = ("locator_miss", "disagreement", "groundedness_fail", "consistency_fail")
PROMPT_GIVEN_FIELDS = ("regulator", "exam_method")
REFERENCE = "__reference__"
_S5_FROM = re.compile(r"s5_from=([^;]+)")

SCORE_COLUMNS = [
    "truth", "scope", "system", "field", "regulator", "method", "release_year", "consistency_check",
    "in_sample", "note",
    "n_docs", "n_truth_value", "n_truth_null", "n_no_truth",
    "n_value", "n_absent", "n_abstain",
    "n_correct", "n_wrong", "n_fabricated_absence", "n_fabrication", "n_correct_null",
    "accuracy_n", "accuracy", "accuracy_lo", "accuracy_hi",
    "abstention_n", "abstention_rate", "abstention_lo", "abstention_hi",
    "fabrication_denominator", "fabrication_rate", "fabrication_lo", "fabrication_hi",
]
RISK_DOC_COLUMNS = [
    "doc_id", "doc_risk", "system", "n_values", "n_abstain",
    "n_locator_miss", "n_disagreement", "n_groundedness_fail", "n_consistency_fail",
]
VALIDATION_COLUMNS = ["truth", "system", "by", "level", "n", "n_error", "error_rate", "error_lo", "error_hi"]


def _rate(k: int, n: int) -> tuple[float, float, float]:
    if not n:
        return (math.nan, math.nan, math.nan)
    lo, hi = wilson_ci(int(k), int(n))
    return (k / n, lo, hi)


def read_triage(path: Path = TRIAGE_PATH) -> pd.DataFrame | None:
    """``results/group_a_triage.csv`` as strings, or None when the file does not exist."""
    if not Path(path).exists():
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=False)


# ----------------------------------------------------------------------------- Group A


def _cell_metrics(g: pd.DataFrame) -> dict[str, Any]:
    cells = g["cell"].value_counts().to_dict()
    n = {c: int(cells.get(c, 0)) for c in (
        "correct", "wrong", "fabricated_absence", "fabrication", "correct_null", "abstention", "no_truth")}
    truth_value = int((g["truth"] == "value").sum())
    truth_null = int(g["truth"].isin(("absent_from_document", "not_applicable")).sum())
    with_truth = g[g["truth"] != "none"]
    acc_n = n["correct"] + n["wrong"] + n["fabricated_absence"]
    abst_n = truth_value + truth_null
    acc, abst, fab = _rate(n["correct"], acc_n), _rate(n["abstention"], abst_n), _rate(n["fabrication"], truth_null)
    return {
        "n_docs": g["doc_id"].nunique(), "n_truth_value": truth_value, "n_truth_null": truth_null,
        "n_no_truth": n["no_truth"],
        "n_value": int((with_truth["status"] == "value").sum()),
        "n_absent": int((with_truth["status"] == "absent").sum()),
        "n_abstain": n["abstention"],
        "n_correct": n["correct"], "n_wrong": n["wrong"], "n_fabricated_absence": n["fabricated_absence"],
        "n_fabrication": n["fabrication"], "n_correct_null": n["correct_null"],
        "accuracy_n": acc_n, "accuracy": acc[0], "accuracy_lo": acc[1], "accuracy_hi": acc[2],
        "abstention_n": abst_n, "abstention_rate": abst[0], "abstention_lo": abst[1], "abstention_hi": abst[2],
        "fabrication_denominator": truth_null,
        "fabrication_rate": fab[0], "fabrication_lo": fab[1], "fabrication_hi": fab[2],
    }


def aggregate(
    compared: pd.DataFrame,
    truth_name: str,
    scope: str,
    s1: str,
    consistency: dict[tuple[str, str], bool] | None = None,
) -> pd.DataFrame:
    """Per (system, field, regulator, method, release year) and per (system, field,
    regulator, method) pooled, with every regulator x method x year cell present (empty
    cells give zero counts and NaN rates). ``consistency`` maps (system, field) to the
    ``consistency_check`` value; without an entry it is true for the prompt-given fields
    on every system other than ``s1``."""
    consistency = consistency or {}
    note = IN_SAMPLE_NOTE if truth_name == "raw" else f"{IN_SAMPLE_NOTE}; {TRIAGED_NOTE}"
    rows = []
    if compared.empty:
        return pd.DataFrame(columns=SCORE_COLUMNS)
    years = sorted(y for y in compared["release_year"].unique() if y)
    order = list(INDEX_TRUTH_COLUMNS)
    fields = sorted(compared["field"].unique(), key=order.index)
    groups = {k: g for k, g in compared.groupby(["system", "field", "regulator", "method", "release_year"])}
    pooled = {k: g for k, g in compared.groupby(["system", "field", "regulator", "method"])}
    empty = compared.iloc[0:0]
    for system in sorted(compared["system"].unique()):
        for field in fields:
            for reg in REGULATORS:
                for method in METHODS:
                    base = {
                        "truth": truth_name, "scope": scope, "system": system, "field": field,
                        "regulator": reg, "method": method,
                        "consistency_check": consistency.get(
                            (system, field), field in PROMPT_GIVEN_FIELDS and system != s1),
                        "in_sample": True, "note": note,
                    }
                    for year in years:
                        g = groups.get((system, field, reg, method, year), empty)
                        rows.append({**base, "release_year": year, **_cell_metrics(g)})
                    g = pooled.get((system, field, reg, method), empty)
                    rows.append({**base, "release_year": POOLED, **_cell_metrics(g)})
    return pd.DataFrame(rows, columns=SCORE_COLUMNS)


def score(
    manifest: pd.DataFrame,
    predictions: pd.DataFrame,
    index: pd.DataFrame | None = None,
    systems: Systems = DEFAULT_SYSTEMS,
    triage: pd.DataFrame | None = None,
    fdic_exam_date_truth: bool = False,
) -> pd.DataFrame:
    """Group A corpus table (``SCORE_COLUMNS``) for the systems of ``systems`` that have rows
    in ``predictions``.

    ``index`` is the long truth table of ``group_a.index_truth`` (built from the manifest
    when omitted, with ``fdic_exam_date_truth``); ``triage`` is the hand and automatic
    triage of ``group_a`` and adds the ``triaged`` block for S1, the only system whose
    disagreements it covers. Documents outside :func:`routing.evaluable` are dropped.
    """
    m = manifest.fillna("").astype(str)
    docs = m[evaluable(m)]
    truth = index if index is not None else index_truth(docs, fdic_exam_date_truth=fdic_exam_date_truth)
    truth = truth[truth["doc_id"].isin(set(docs["doc_id"]))].reset_index(drop=True)
    p = normalize_predictions(predictions)
    present = [s for s in (systems.s1, systems.s3, systems.s5) if s in set(p["system"])]
    truths = [("raw", truth, present)]
    if triage is not None and systems.s1 in present:
        truths.append(("triaged", apply_triage(truth, triage), [systems.s1]))
    consistency = s5_consistency(p, systems)
    test_ids = set(docs.loc[docs["split"] == "test", "doc_id"])
    frames = []
    for name, t, which in truths:
        compared = compare_with_index(p, t, which)
        frames.append(aggregate(compared, name, "all", systems.s1, consistency))
        if test_ids:
            frames.append(aggregate(compared[compared["doc_id"].isin(test_ids)], name, "test",
                                    systems.s1, consistency))
    if not frames:
        return pd.DataFrame(columns=SCORE_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def s5_consistency(predictions: pd.DataFrame, systems: Systems = DEFAULT_SYSTEMS) -> dict[tuple[str, str], bool]:
    """(S5, field) -> consistency_check for the prompt-given fields: true only when some S5
    value of the field comes from S3 (``source_system`` column, else the ``s5_from=`` note)."""
    p = predictions[(predictions["system"] == systems.s5) & predictions["field"].isin(PROMPT_GIVEN_FIELDS)]
    out: dict[tuple[str, str], bool] = {}
    for field, g in p.groupby("field"):
        if "source_system" in g.columns:
            sources = {s for s in g["source_system"] if s}
        else:
            sources = set()
        if not sources:
            sources = {m.group(1).strip() for m in map(_S5_FROM.search, g["notes"].astype(str)) if m}
        out[(systems.s5, str(field))] = (not sources) or systems.s3 in sources
    return out


# ----------------------------------------------------------------------------- risk


def section_misses(sections: pd.DataFrame) -> dict[tuple[str, str], bool]:
    """(doc_id, field) -> True when none of the field group's sections was found, for every
    document in a ``sections_{split}.csv`` frame and every field of ``llm_field_groups``."""
    found = sections.fillna("").astype(str)
    found = found[found["found"].str.casefold().isin(("true", "1"))]
    by_doc: dict[str, set[str]] = {}
    for d, s in zip(found["doc_id"], found["section"], strict=True):
        by_doc.setdefault(d, set()).add(s)
    out: dict[tuple[str, str], bool] = {}
    groups = load_fields()["llm_field_groups"].values()
    for doc in set(sections["doc_id"].astype(str)):
        have = by_doc.get(doc, set())
        for group in groups:
            miss = not any(s in have for s in group["sections"])
            for field in group["fields"]:
                out[(doc, field)] = miss
    return out


def _reference(row: dict[str, Any], chosen: str, systems: Systems) -> str:
    """System the chosen system's value is compared with for the disagreement flag."""
    if row.get("reference_system"):
        return str(row["reference_system"])
    if chosen == systems.s1:
        return systems.s3
    if chosen == systems.s5:
        m = _S5_FROM.search(str(row.get("notes") or ""))
        source = m.group(1).strip() if m else systems.s3
        return systems.s3 if source == systems.s1 else systems.s1
    return systems.s1


def _truthy(value: Any) -> bool:
    return str(value).strip().casefold() in ("1", "true", "yes")


def risk_values(
    predictions: pd.DataFrame,
    systems: Systems = DEFAULT_SYSTEMS,
    chosen: str = S5_NAME,
    sections: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per-value flags of the ``chosen`` system from ``evaluate.risk_table``, computed from
    predictions only (``labels`` is never passed). Columns of ``risk_table`` plus
    ``reference_system`` and ``value``; ``cell`` is empty because nothing is scored here."""
    fields = load_fields()["fields"]
    p = normalize_predictions(predictions)
    p = p[p["field"].isin(fields)]
    rows = p[p["system"] == chosen].drop_duplicates(["doc_id", "field"], keep="first")
    if rows.empty:
        raise ValueError(f"no rows of system {chosen!r} in the predictions")
    by_key = {(r["doc_id"], r["field"], r["system"]): r for r in p.to_dict("records")}
    misses = section_misses(sections) if sections is not None else {}
    preds: list[FieldValue] = []
    scored: list[dict[str, Any]] = []
    refs: list[str] = []
    fv_fields = set(FieldValue.model_fields)
    for r in rows.to_dict("records"):
        key = (r["doc_id"], r["field"])
        miss = misses[key] if key in misses else _truthy(r.get("locator_miss", ""))
        notes = str(r.get("notes") or "")
        if miss:
            notes = (notes + "; " if notes else "") + "locator_miss"
        elif "locator_miss" in notes:
            # risk_table reads the flag from the notes; the sections file says found
            notes = notes.replace("locator_miss", "locator_found")
        fv = FieldValue(**{k: (v if v != "" else None) for k, v in r.items() if k in fv_fields})
        fv.notes = notes or None
        preds.append(fv)
        ref = _reference(r, chosen, systems)
        refs.append(ref)
        o = by_key.get((r["doc_id"], r["field"], ref))
        if o is not None and o["status"] == "value":
            preds.append(FieldValue(doc_id=o["doc_id"], field=o["field"], system=REFERENCE,
                                    status="value", value=o["value"] or None))
        scored.append({
            "doc_id": r["doc_id"], "field": r["field"], "system": chosen,
            "group": fields[r["field"]].get("group", ""),
            # an S1-sourced S5 value is not graded for groundedness (GROUNDING_NOT_GRADED): no flag
            "grounding_failure": "" if r.get("grounding_failure") == GROUNDING_NOT_GRADED
            else str(r.get("grounding_failure") or ""), "cell": "",
        })
    risk = risk_table(pd.DataFrame(scored), preds)
    keys = list(zip(rows["doc_id"], rows["field"], strict=True))
    ref_by = dict(zip(keys, refs, strict=True))
    value_by = dict(zip(keys, rows["value"], strict=True))
    pairs = list(zip(risk["doc_id"], risk["field"], strict=True))
    risk["reference_system"] = [ref_by[k] for k in pairs]
    risk["value"] = [value_by[k] for k in pairs]
    return risk


def risk_documents(risk: pd.DataFrame) -> pd.DataFrame:
    """``RISK_DOC_COLUMNS``: doc_risk and flag counts over each document's Group B values."""
    if risk.empty:
        return pd.DataFrame(columns=RISK_DOC_COLUMNS)
    rows = []
    for (doc, system), g in risk.groupby(["doc_id", "system"], sort=True):
        b = g[g["group"].isin(RISK_GROUPS)]
        rows.append({
            "doc_id": doc, "doc_risk": int(b["n_flags"].sum()), "system": system,
            "n_values": int(b["answered"].sum()), "n_abstain": int((~b["answered"].astype(bool)).sum()),
            **{f"n_{f}": int(b[f].astype(bool).sum()) for f in FLAGS},
        })
    return pd.DataFrame(rows, columns=RISK_DOC_COLUMNS)


def validate_group_a(
    risk: pd.DataFrame,
    manifest: pd.DataFrame,
    chosen: str,
    triage: pd.DataFrame | None = None,
    s1: str = S1_DEFAULT,
    fdic_exam_date_truth: bool = False,
) -> pd.DataFrame:
    """Error rate by flag count (and by each flag) of the chosen system's answered Group A
    values against the index, over every evaluable document with truth; Wilson CIs.
    Error = cell not ``correct`` (wrong, or a fabrication in an ABS cell). The triaged block
    is written only when the chosen system is S1 (``s1``), whose disagreements the triage
    covers."""
    m = manifest.fillna("").astype(str)
    docs = m[evaluable(m)]
    truth = index_truth(docs, fdic_exam_date_truth=fdic_exam_date_truth)
    truths = [("raw", truth)]
    if triage is not None and chosen == s1:
        truths.append(("triaged", apply_triage(truth, triage)))
    answered = risk[risk["answered"].astype(bool) & risk["field"].isin(INDEX_TRUTH_COLUMNS)]
    values = answered[["doc_id", "field", "value"]].assign(system=chosen, status="value")
    rows = []
    for name, t in truths:
        compared = compare_with_index(values, t, [chosen])
        compared = compared[(compared["status"] == "value") & (compared["truth"] != "none")]
        j = compared.merge(answered[["doc_id", "field", "n_flags", *FLAGS]], on=["doc_id", "field"], how="inner")
        j["error"] = j["cell"] != "correct"
        for k, g in j.groupby("n_flags"):
            rows.append(_validation_row(name, chosen, "n_flags", int(k), g))
        for flag in FLAGS:
            for level in (0, 1):
                g = j[j[flag].astype(bool) == bool(level)]
                if len(g):
                    rows.append(_validation_row(name, chosen, f"flag:{flag}", level, g))
    return pd.DataFrame(rows, columns=VALIDATION_COLUMNS)


def _validation_row(truth: str, system: str, by: str, level: int, g: pd.DataFrame) -> dict[str, Any]:
    n, k = len(g), int(g["error"].sum())
    rate, lo, hi = _rate(k, n)
    return {"truth": truth, "system": system, "by": by, "level": level, "n": n, "n_error": k,
            "error_rate": rate, "error_lo": lo, "error_hi": hi}


def write_risk(
    predictions: pd.DataFrame,
    systems: Systems = DEFAULT_SYSTEMS,
    chosen: str = S5_NAME,
    sections: pd.DataFrame | None = None,
    manifest: pd.DataFrame | None = None,
    triage: pd.DataFrame | None = None,
    out_path: Path = RISK_CORPUS,
    values_path: Path | None = RISK_VALUES,
    validation_path: Path | None = RISK_VALIDATION,
    fdic_exam_date_truth: bool = False,
) -> dict[str, Path]:
    """Write ``risk_corpus.csv`` (doc_id, doc_risk, detail columns), the per-value flags and,
    with a manifest, the Group A validation. Returns the paths written."""
    risk = risk_values(predictions, systems, chosen, sections)
    docs = risk_documents(risk)
    paths = {"risk": out_path}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    docs.to_csv(out_path, index=False)
    if values_path is not None:
        values_path.parent.mkdir(parents=True, exist_ok=True)
        risk.drop(columns=["cell"]).to_csv(values_path, index=False)
        paths["values"] = values_path
    if manifest is not None and validation_path is not None:
        validation = validate_group_a(risk, manifest, chosen, triage, systems.s1, fdic_exam_date_truth)
        validation_path.parent.mkdir(parents=True, exist_ok=True)
        validation.to_csv(validation_path, index=False, float_format="%.5f")
        paths["validation"] = validation_path
    return paths


# ----------------------------------------------------------------------------- CLI


def _load(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, Systems]:
    manifest = read_manifest(args.manifest)
    systems = Systems(s1=args.s1, s3=args.s3, s5=args.s5)
    preds = read_predictions(args.predictions)
    preds = ensure_s5(preds, manifest, systems, args.routing)
    return manifest, preds, systems


def _existing(paths: Iterable[Path]) -> list[Path]:
    return [p for p in paths if Path(p).exists()]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit 1 when a requested system has no rows (``score``) or the
    chosen system has none (``risk``)."""
    ap = argparse.ArgumentParser(description="Group A corpus scoring and the corpus risk file.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("score", "Group A against the index on every evaluable document"),
                            ("risk", "risk flags per value and doc_risk per document")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--predictions", type=Path, nargs="+", default=[RESULTS_DIR / "predictions_corpus.csv"])
        s.add_argument("--manifest", type=Path, default=CORPUS_PATH)
        s.add_argument("--routing", type=Path, default=ROUTING_YAML,
                       help="routing YAML used to add S5 rows for documents that have none")
        s.add_argument("--triage", type=Path, default=TRIAGE_PATH)
        s.add_argument("--s1", default=S1_DEFAULT)
        s.add_argument("--s3", default=S3_DEFAULT)
        s.add_argument("--s5", default=S5_NAME)
        s.add_argument("--gold-dir", type=Path, default=GOLD_DIR,
                       help="labels_{dev,test}.csv for the FDIC exam_date gate (exam_date rows only)")
        s.add_argument("--gate-out", type=Path, default=FDIC_GATE)
    sub.choices["score"].add_argument("--out", type=Path, default=GROUP_A_CORPUS)
    r = sub.choices["risk"]
    r.add_argument("--system", default=None, help="system whose values are flagged (default: --s5)")
    r.add_argument("--sections", type=Path, default=None, help="sections_{split}.csv for locator_miss")
    r.add_argument("--out", type=Path, default=RISK_CORPUS)
    r.add_argument("--values-out", type=Path, default=RISK_VALUES)
    r.add_argument("--validation-out", type=Path, default=RISK_VALIDATION)
    args = ap.parse_args(argv)

    args.predictions = _existing(args.predictions)
    if not args.predictions:
        print("error: none of the predictions files exists", file=sys.stderr)
        return 1
    manifest, preds, systems = _load(args)
    triage = read_triage(args.triage)
    gate, gate_table = fdic_exam_date_gate(manifest, args.gold_dir)
    args.gate_out.parent.mkdir(parents=True, exist_ok=True)
    gate_table.to_csv(args.gate_out, index=False)
    print(f"FDIC exam_date gate: {gate} ({len(gate_table)} FDIC gold documents; {args.gate_out}); "
          f"FDIC exam_date index truth {'used' if gate == 'pass' else 'not used'}")
    fdic_truth = gate == "pass"
    if args.cmd == "score":
        missing = [s for s in (systems.s1, systems.s3, systems.s5) if s not in set(preds["system"])]
        table = score(manifest, preds, systems=systems, triage=triage, fdic_exam_date_truth=fdic_truth)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(args.out, index=False, float_format="%.5f")
        pooled = table[(table["release_year"] == POOLED) & (table["truth"] == "raw") & (table["scope"] == "all")]
        with pd.option_context("display.width", 200, "display.max_rows", 500):
            print(pooled[["system", "field", "regulator", "method", "accuracy_n", "accuracy",
                          "abstention_rate", "fabrication_rate"]].to_string(index=False))
        print(f"written to {args.out}")
        if missing:
            print(f"systems without rows (not scored): {missing}", file=sys.stderr)
            return 1
        return 0
    chosen = args.system or systems.s5
    sections = pd.read_csv(args.sections, dtype=str, keep_default_na=False) if args.sections else None
    try:
        paths = write_risk(preds, systems, chosen, sections, manifest, triage,
                           args.out, args.values_out, args.validation_out, fdic_truth)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for name, p in paths.items():
        print(f"{name}: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
