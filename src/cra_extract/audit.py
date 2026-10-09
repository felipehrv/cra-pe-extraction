"""Stratified audit of the Group B values released by the corpus pass (design 5.3).

The audit split is drawn by ``strata.draw_audit``: ten documents per document-risk
tertile, with the tertile sizes ``N_h`` and draw counts ``n_h`` recorded in
``config/strata.yaml``. This module covers what happens after the draw, as fixed in
``docs/analysis_plan.md`` ("Audit design" and "Risk score, calibration and review
curve"):

- ``build_sheet`` writes ``data/gold/audit_sheet.csv``, one row per (audit document,
  Group B field). The blinding mode of a document follows its tertile: ``low`` is
  ``blind`` (no model output on the sheet), ``high`` is ``shown`` (the released value,
  its status and its quote are pre-filled in ``model_status``, ``model_value`` and
  ``model_quote``), and ``mid`` alternates by position in the sorted list of its drawn
  doc_ids (positions counted from 1: odd ``blind``, even ``shown``). The gold columns
  (``value``, ``null_reason``, ``evidence_page``, ``quote``, ``scope``) are always
  written empty: the sheet carries no gold and no value from any other label file.
- ``validate`` reuses ``label.validate`` on the Group B rows (exactly one of value and
  null_reason, evidence and quote for a value, ranges, the Group B consistency rules,
  minutes on every row) and adds the audit rules: every audit document has its 11
  rows, nothing else is on the sheet, the ``mode`` column follows the tertile rule and
  a blind row carries no model output.
- ``score_audit`` scores every audited value with ``evaluate.score_cell`` (through
  ``evaluate.score_predictions``, which also applies the as-of quarter of
  ``total_assets_usd``) and attaches tertile, weight ``w_h = N_h / n_h``, regulator,
  mode and minutes. ``flags_from_risk_values`` attaches ``n_flags`` from
  ``results/risk_values_corpus.csv``, the per-value flags of S5 that produced the
  ``doc_risk`` the tertiles were drawn on (plan: doc_risk is for S5 with the disagreement
  flag against S1), so the strata, the error by flag count and the review curve share one
  flag input. ``add_flags`` recomputes them with ``evaluate.risk_table`` (pair restricted to
  the released and comparison systems) when no risk-values file is given; either way the
  estimate refuses to run when a document's ``n_flags`` sum differs from its ``doc_risk``.
- ``estimate`` computes the design-weighted error ``sum(w y) / sum(w)`` over answered
  values, where ``y`` is 1 for ``wrong``, ``fabrication`` and ``fabricated_absence`` and
  0 for ``correct`` and ``correct_null``; abstentions are excluded and their
  design-weighted rate is reported. The 95 percent interval is
  ``stats.stratified_weighted_mean`` (1,000 document resamples within tertile, seed 0)
  over every audited document, an abstained value entering with weight 0, so a document
  whose values all abstained is still resampled and each tertile keeps ``n_h`` documents.
  With no error in the sample the bootstrap is degenerate and the per-tertile Wilson
  upper bounds are reported instead. Also: per field (same bootstrap, labeled wide),
  per tertile (raw error pooled over fields, Wilson), by regulator (point estimates),
  middle-tertile error by mode (descriptive), error by flag count (the verification of
  the risk flags) and reviewer minutes per value by mode (the cost table input).
- ``review_curve`` gives residual error against the design-weighted share of values
  reviewed, values ordered by ``n_flags`` highest first and linear inside each block of
  equal flag counts, with the operating points 10, 25 and 50 percent.

Outputs of ``python -m cra_extract.audit estimate``: ``results/audit_summary.csv``,
``results/audit_by_field.csv``, ``results/audit_by_tertile.csv`` and
``results/review_curve_group_b.csv``.

CLI::

    python -m cra_extract.audit sheet --system s5 --predictions results/predictions_corpus.csv
        results/predictions_corpus_s5.csv
    python -m cra_extract.audit validate [data/gold/audit_sheet.csv]
    python -m cra_extract.audit estimate --system s5 --predictions results/predictions_corpus.csv
        results/predictions_corpus_s5.csv [--risk-values results/risk_values_corpus.csv]
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from cra_extract import evaluate, label, strata
from cra_extract.config import GOLD_DIR, RESULTS_DIR, STRATA_PATH, field_names, load_fields
from cra_extract.models import FieldValue, LabelRow
from cra_extract.stats import stratified_weighted_mean, wilson_ci

TERTILES = ("low", "mid", "high")
MODES = ("blind", "shown")
ERROR_CELLS = ("wrong", "fabrication", "fabricated_absence")
OK_CELLS = ("correct", "correct_null")
OPERATING_POINTS = (0.10, 0.25, 0.50)
N_BOOT = 1000
BOOT_SEED = 0
DEFAULT_COMPARE_SYSTEM = "rules"

SHEET_PATH = GOLD_DIR / "audit_sheet.csv"
PREDICTIONS_PATH = RESULTS_DIR / "predictions_corpus.csv"
S5_PREDICTIONS_PATH = RESULTS_DIR / "predictions_corpus_s5.csv"
RISK_PATH = RESULTS_DIR / "risk_corpus.csv"
RISK_VALUES_PATH = RESULTS_DIR / "risk_values_corpus.csv"
DEFAULT_SYSTEM = "s5"

# Columns the labeler fills; ``build_sheet`` always leaves them empty.
GOLD_COLUMNS = ("value", "null_reason", "evidence_page", "quote", "scope")
MODEL_COLUMNS = ("model_status", "model_value", "model_quote")
SHEET_COLUMNS = [
    "doc_id",
    "field",
    "field_group",
    "pdf_path",
    "mode",
    *MODEL_COLUMNS,
    *GOLD_COLUMNS,
    "minutes",
    "labeler",
    "labeled_at",
    "notes",
]
SUMMARY_COLUMNS = [
    "quantity", "stratum", "n_docs", "n_values", "n_answered", "n_errors",
    "estimate", "ci_lo", "ci_hi", "interval", "note",
]
BY_FIELD_COLUMNS = [
    "field", "n_docs", "n_values", "n_answered", "n_errors", "n_abstentions",
    "error_weighted", "ci_lo", "ci_hi", "abstention_rate_weighted", "interval", "note",
]
BY_TERTILE_COLUMNS = [
    "tertile", "N_h", "n_h", "weight", "n_docs_blind", "n_docs_shown", "n_values",
    "n_answered", "n_errors", "n_abstentions", "error_raw", "ci_lo", "ci_hi",
    "abstention_rate", "interval",
]
CURVE_COLUMNS = ["point", "share_reviewed", "residual_error", "n_flags_block", "note"]



def boot_label(n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> str:
    """Interval label naming the bootstrap actually run."""
    return f"stratified_bootstrap_{n_boot}_seed{seed}_percentile"


BOOT_LABEL = boot_label()


class AuditError(ValueError):
    """The audit inputs are missing or inconsistent with the draw record."""


# ----------------------------------------------------------------------------- design


@dataclass(frozen=True)
class AuditDesign:
    """The audit draw as recorded in ``config/strata.yaml``.

    ``tertile_of`` maps each drawn doc_id to ``low``, ``mid`` or ``high``; ``population``
    and ``drawn`` hold ``N_h`` and ``n_h`` per tertile.
    """

    tertile_of: dict[str, str]
    population: dict[str, int]
    drawn: dict[str, int]

    @property
    def doc_ids(self) -> list[str]:
        return sorted(self.tertile_of)

    def weight(self, tertile: str) -> float:
        """Design weight ``w_h = N_h / n_h`` of a tertile (document level)."""
        return self.population[tertile] / self.drawn[tertile]

    def docs_in(self, tertile: str) -> list[str]:
        return sorted(d for d, t in self.tertile_of.items() if t == tertile)


def _tertile_name(stratum_name: str) -> str:
    name = stratum_name.split(":", 1)[-1]
    if name not in TERTILES:
        raise AuditError(f"unknown audit stratum {stratum_name!r}")
    return name


def design_from_record(record: dict[str, Any]) -> AuditDesign:
    """Build the design from the ``splits.audit`` record of ``config/strata.yaml``.

    Checks that every tertile is present, that the documents listed per stratum match
    its ``drawn`` count, and that the recorded weight equals ``population / drawn``
    (the record rounds it to six decimals; the estimator uses the exact ratio).
    """
    population: dict[str, int] = {}
    drawn: dict[str, int] = {}
    for s in record.get("strata", []):
        t = _tertile_name(s["name"])
        population[t] = int(s["population"])
        drawn[t] = int(s["drawn"])
        if drawn[t] <= 0:
            raise AuditError(f"tertile {t} has no drawn documents")
        w = s.get("weight")
        if w is not None and abs(float(w) - population[t] / drawn[t]) > 1e-5:
            raise AuditError(f"tertile {t}: recorded weight {w} differs from N_h / n_h")
    missing = set(TERTILES) - set(population)
    if missing:
        raise AuditError(f"audit record lacks tertiles {sorted(missing)}")
    tertile_of = {d["doc_id"]: _tertile_name(d["stratum"]) for d in record.get("docs", [])}
    for t in TERTILES:
        n = sum(1 for v in tertile_of.values() if v == t)
        if n != drawn[t]:
            raise AuditError(f"tertile {t}: {n} documents listed, {drawn[t]} recorded as drawn")
    return AuditDesign(tertile_of=tertile_of, population=population, drawn=drawn)


def load_design(path: Path = STRATA_PATH) -> AuditDesign:
    """The audit design from ``config/strata.yaml``; ``AuditError`` before the draw."""
    record = strata.load_strata(path).get("splits", {}).get("audit")
    if not record:
        raise AuditError(f"no audit draw in {path}; run make draw-audit first")
    return design_from_record(record)


def blinding_modes(design: AuditDesign) -> dict[str, str]:
    """doc_id -> ``blind`` or ``shown`` by the plan's tertile rule.

    Low tertile blind, high tertile shown; the middle tertile by 1-based position in the
    sorted list of its drawn doc_ids, odd positions blind and even positions shown.
    """
    modes: dict[str, str] = {}
    for doc_id in design.docs_in("low"):
        modes[doc_id] = "blind"
    for doc_id in design.docs_in("high"):
        modes[doc_id] = "shown"
    for pos, doc_id in enumerate(design.docs_in("mid"), start=1):
        modes[doc_id] = "blind" if pos % 2 == 1 else "shown"
    return modes


def group_b_fields() -> list[str]:
    """The audited fields: Group B of ``fields.yaml`` in file order."""
    return field_names("B")


def regulator_of(doc_id: str) -> str:
    return doc_id.split("_", 1)[0]


# ----------------------------------------------------------------------------- sheet


def build_sheet(
    design: AuditDesign, predictions: Iterable[FieldValue], system: str
) -> list[dict[str, str]]:
    """Audit sheet rows: one per (audit document, Group B field), sorted by doc_id and
    then in ``fields.yaml`` order.

    Shown rows carry the released system's status, value and quote; a value the system
    did not output is shown as status ``abstain``. Blind rows and the gold columns are
    empty. Raises ``AuditError`` when ``predictions`` hold no row of ``system`` for any
    audit document (a wrong system name or predictions file).
    """
    fields = group_b_fields()
    modes = blinding_modes(design)
    audit_docs = set(design.tertile_of)
    pred = {
        (p.doc_id, p.field): p
        for p in predictions
        if p.system == system and p.doc_id in audit_docs and p.field in fields
    }
    if not pred:
        raise AuditError(f"no prediction of system {system!r} for any audit document")
    group = load_fields()["fields"]
    rows: list[dict[str, str]] = []
    for doc_id in design.doc_ids:
        for field in fields:
            row = dict.fromkeys(SHEET_COLUMNS, "")
            row.update(
                doc_id=doc_id,
                field=field,
                field_group=str(group[field].get("group", "")),
                pdf_path=label.pdf_path_for(doc_id),
                mode=modes[doc_id],
            )
            if modes[doc_id] == "shown":
                p = pred.get((doc_id, field))
                row["model_status"] = "abstain" if p is None else p.status
                if p is not None and p.status == "value":
                    row["model_value"] = _cell(p.value)
                    row["model_quote"] = p.quote or ""
            rows.append(row)
    return rows


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return label.SET_SEPARATOR.join(str(v) for v in value)
    return str(value)


def write_sheet(rows: list[dict[str, str]], path: Path) -> None:
    """Write audit sheet rows with ``SHEET_COLUMNS``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in SHEET_COLUMNS})


# ----------------------------------------------------------------------------- validate


def validate(path: Path, design: AuditDesign) -> list[label.Problem]:
    """Check a filled audit sheet. Returns an empty list when it is clean.

    ``label.validate`` runs on the Group B rows (row numbers mapped back to this
    sheet); it already requires minutes on every row. Added here: required audit
    columns, Group B fields only, the 11 rows of every audit document and no other
    document, ``mode`` equal to the tertile rule, and no model output on a blind row.
    """
    rows = label.read_sheet(path)
    if not rows:
        return [label.Problem(0, "", "", "columns", "empty sheet")]
    missing_cols = set(SHEET_COLUMNS) - set(rows[0])
    if missing_cols:
        return [label.Problem(0, "", "", "columns", f"missing columns {sorted(missing_cols)}")]

    fields = group_b_fields()
    modes = blinding_modes(design)
    problems: list[label.Problem] = []
    keep: list[int] = []
    seen: set[tuple[str, str]] = set()
    for i, r in enumerate(rows, start=1):
        doc_id, field = r["doc_id"], r["field"]
        if field not in fields:
            problems.append(label.Problem(i, doc_id, field, "not_group_b", "the audit labels Group B fields only"))
            continue
        keep.append(i)
        seen.add((doc_id, field))
        if doc_id not in modes:
            problems.append(label.Problem(i, doc_id, field, "not_audit_doc", "document is not in the audit draw"))
            continue
        if r["mode"] != modes[doc_id]:
            problems.append(
                label.Problem(i, doc_id, field, "mode", f"mode {r['mode']!r}; the tertile rule gives {modes[doc_id]!r}")
            )
        if modes[doc_id] == "blind" and any(r.get(c) for c in MODEL_COLUMNS):
            problems.append(label.Problem(i, doc_id, field, "blind_leak", "a blind row shows model output"))
    for doc_id in design.doc_ids:
        for field in fields:
            if (doc_id, field) not in seen:
                problems.append(label.Problem(0, doc_id, field, "missing_row", "audit document lacks this field"))

    if keep:
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "group_b.csv"
            with open(sub, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
                w.writeheader()
                for i in keep:
                    w.writerow({c: rows[i - 1].get(c, "") for c in SHEET_COLUMNS})
            for p in label.validate(sub):
                row = keep[p.row - 1] if p.row >= 1 else 0
                problems.append(label.Problem(row, p.doc_id, p.field, p.rule, p.message))
    return sorted(problems, key=lambda p: (p.row, p.doc_id, p.field, p.rule))


# ----------------------------------------------------------------------------- scoring


def label_rows(rows: Iterable[dict[str, str]]) -> list[LabelRow]:
    """Audit sheet rows as ``LabelRow`` records (values coerced as in ``label``)."""
    out = []
    for r in rows:
        value: Any = label.coerce_value(r["field"], r["value"]) if r.get("value") else None
        out.append(
            LabelRow(
                doc_id=r["doc_id"],
                field=r["field"],
                value=value,
                null_reason=r.get("null_reason") or None,
                minutes=float(r["minutes"]) if r.get("minutes") else None,
            )
        )
    return out


def score_audit(
    rows: list[dict[str, str]],
    predictions: Iterable[FieldValue],
    design: AuditDesign,
    system: str,
) -> pd.DataFrame:
    """One scored row per audited value of ``system``.

    Columns: doc_id, field, tertile, weight, regulator, mode, minutes, cell (from
    ``evaluate.score_cell``), answered (cell is not an abstention) and y (1 error, 0
    correct, NaN for an abstention). A value the system did not output is an abstention.
    """
    fields = set(group_b_fields())
    rows = [r for r in rows if r["field"] in fields and r["doc_id"] in design.tertile_of]
    labels = label_rows(rows)
    preds = [p for p in predictions if p.system == system and p.doc_id in design.tertile_of]
    scored = evaluate.score_predictions(labels, preds, systems=[system])
    cols = ["doc_id", "field", "tertile", "weight", "regulator", "mode", "minutes", "cell", "answered", "y"]
    if scored.empty:
        return pd.DataFrame(columns=cols)
    sheet = {(r["doc_id"], r["field"]): r for r in rows}
    df = scored[["doc_id", "field", "cell"]].copy()
    df["tertile"] = df["doc_id"].map(design.tertile_of)
    df["weight"] = df["tertile"].map(design.weight)
    df["regulator"] = df["doc_id"].map(regulator_of)
    df["mode"] = [sheet[(d, f)].get("mode", "") for d, f in zip(df["doc_id"], df["field"], strict=True)]
    df["minutes"] = [
        float(sheet[(d, f)]["minutes"]) if sheet[(d, f)].get("minutes") else math.nan
        for d, f in zip(df["doc_id"], df["field"], strict=True)
    ]
    df["answered"] = df["cell"] != "abstention"
    df["y"] = df["cell"].map(lambda c: 1.0 if c in ERROR_CELLS else (0.0 if c in OK_CELLS else math.nan))
    order = {f: i for i, f in enumerate(group_b_fields())}
    df["_o"] = df["field"].map(order)
    df = df.sort_values(["doc_id", "_o"]).drop(columns="_o").reset_index(drop=True)
    return df[cols]


def add_flags(
    values: pd.DataFrame,
    predictions: Iterable[FieldValue],
    system: str,
    grounding_column: dict[tuple[str, str, str], str | None] | None = None,
    compare_system: str = DEFAULT_COMPARE_SYSTEM,
) -> pd.DataFrame:
    """Attach ``n_flags`` (0 to 4) to every audited value with ``evaluate.risk_table``.

    The prediction input is restricted to ``system`` and ``compare_system`` (the plan
    restricts the disagreement flag to that pair) and to the audit documents; every
    field of the two systems is kept so that the consistency rules can read the
    system's own exam_date. The grounding failure comes from the predictions file's
    ``grounding_failure`` column. An abstained value has ``n_flags`` 0, as in the code.
    """
    out = values.copy()
    if out.empty:
        out["n_flags"] = pd.Series(dtype=int)
        return out
    docs = set(out["doc_id"])
    pair = {system, compare_system}
    preds = [p for p in predictions if p.system in pair and p.doc_id in docs]
    column = grounding_column or {}
    scored_like = pd.DataFrame({
        "doc_id": out["doc_id"],
        "field": out["field"],
        "system": system,
        "group": "B",
        "grounding_failure": [column.get((d, f, system)) or "" for d, f in zip(out["doc_id"], out["field"], strict=True)],
        "cell": out["cell"],
    })
    risk = evaluate.risk_table(scored_like, preds)
    flags = {(r.doc_id, r.field): int(r.n_flags) for r in risk.itertuples(index=False)}
    out["n_flags"] = [flags.get((d, f), 0) for d, f in zip(out["doc_id"], out["field"], strict=True)]
    return out


def flags_from_risk_values(values: pd.DataFrame, risk_values_path: Path, system: str) -> pd.DataFrame:
    """Attach ``n_flags`` to every audited value from the per-value risk file
    (``corpus_scoring.write_risk``: ``results/risk_values_corpus.csv``) of ``system``.

    That file produced ``results/risk_corpus.csv``, the ``doc_risk`` the tertiles were drawn
    on, so reading it keeps one flag input for the strata, the error by flag count and the
    review curve. An audited value without a row of ``system`` raises ``AuditError``.
    """
    out = values.copy()
    risk = pd.read_csv(risk_values_path, dtype=str, keep_default_na=False)
    missing_cols = {"doc_id", "field", "system", "n_flags"} - set(risk.columns)
    if missing_cols:
        raise AuditError(f"{risk_values_path}: missing columns {sorted(missing_cols)}")
    risk = risk[risk["system"] == system]
    if risk.empty:
        raise AuditError(f"{risk_values_path}: no rows of system {system!r}")
    flags = {(d, f): int(float(n)) for d, f, n in zip(risk["doc_id"], risk["field"], risk["n_flags"], strict=True)}
    keys = list(zip(out["doc_id"], out["field"], strict=True))
    missing = [k for k in keys if k not in flags]
    if missing:
        raise AuditError(
            f"{risk_values_path}: {len(missing)} audited values have no {system} row, e.g. {missing[:3]}"
        )
    out["n_flags"] = [flags[k] for k in keys] if keys else pd.Series(dtype=int)
    return out


# ----------------------------------------------------------------------------- estimate


def _weighted_mean(df: pd.DataFrame, col: str) -> float:
    w = df["weight"].astype(float)
    return float((w * df[col].astype(float)).sum() / w.sum()) if len(df) and w.sum() > 0 else math.nan


def _row(quantity: str, stratum: str, sub: pd.DataFrame, **kw: Any) -> dict[str, Any]:
    answered = sub[sub["answered"]] if len(sub) else sub
    base = {
        "quantity": quantity,
        "stratum": stratum,
        "n_docs": int(sub["doc_id"].nunique()) if len(sub) else 0,
        "n_values": len(sub),
        "n_answered": len(answered),
        "n_errors": int(answered["y"].sum()) if len(answered) else 0,
        "estimate": math.nan,
        "ci_lo": math.nan,
        "ci_hi": math.nan,
        "interval": "",
        "note": "",
    }
    base.update(kw)
    return base


def _bootstrap(values: pd.DataFrame, n_boot: int, seed: int) -> tuple[float, float, float]:
    """Design-weighted error over answered values with the stratified bootstrap over every
    audited document. An abstained value enters with y 0 and weight 0, so it adds nothing
    to either sum while its document stays in the resampling (n_h kept, plan Estimator)."""
    v = values.reset_index(drop=True)
    answered = v["answered"].astype(bool)
    frame = pd.DataFrame({
        "doc_id": v["doc_id"],
        "tertile": v["tertile"],
        "y": v["y"].where(answered, 0.0).astype(float),
        "w": v["weight"].astype(float) * answered.astype(float),
    })
    if not answered.any():
        return (math.nan, math.nan, math.nan)
    return stratified_weighted_mean(
        frame, "y", doc_col="doc_id", stratum_col="tertile", weight_col="w", n_boot=n_boot, seed=seed,
    )


@dataclass
class AuditEstimates:
    """The three estimate tables written by ``estimate``."""

    summary: pd.DataFrame
    by_field: pd.DataFrame
    by_tertile: pd.DataFrame


def estimate(
    values: pd.DataFrame, design: AuditDesign, n_boot: int = N_BOOT, seed: int = BOOT_SEED
) -> AuditEstimates:
    """Design-weighted Group B error and its breakdowns (analysis plan, Audit design).

    ``values`` is ``score_audit`` output, optionally with ``n_flags`` from ``add_flags``
    (then the design-weighted error by flag count is added to the summary).
    """
    answered = values[values["answered"]]
    summary: list[dict[str, Any]] = []
    no_error = len(answered) > 0 and answered["y"].sum() == 0
    label_ = boot_label(n_boot, seed)

    point, lo, hi = _bootstrap(values, n_boot, seed) if len(answered) else (math.nan,) * 3
    if no_error:
        summary.append(_row(
            "design_weighted_error", "all", values, estimate=point,
            interval="none", note="no error in the sample: the bootstrap is degenerate; see wilson_upper_bound rows",
        ))
    else:
        summary.append(_row(
            "design_weighted_error", "all", values, estimate=point, ci_lo=lo, ci_hi=hi,
            interval=label_,
        ))
    abst = values.assign(_a=(~values["answered"]).astype(float))
    summary.append(_row(
        "design_weighted_abstention_rate", "all", values, estimate=_weighted_mean(abst, "_a"),
        note="abstentions are excluded from the error estimate",
    ))

    by_tertile_rows = []
    for t in TERTILES:
        sub = values[values["tertile"] == t]
        ans = sub[sub["answered"]]
        k, n = int(ans["y"].sum()), len(ans)
        t_lo, t_hi = wilson_ci(k, n)
        docs = sorted(set(sub["doc_id"]))
        modes = sub.drop_duplicates("doc_id")["mode"]
        by_tertile_rows.append({
            "tertile": t,
            "N_h": design.population[t],
            "n_h": design.drawn[t],
            "weight": design.weight(t),
            "n_docs_blind": int((modes == "blind").sum()),
            "n_docs_shown": int((modes == "shown").sum()),
            "n_values": len(sub),
            "n_answered": n,
            "n_errors": k,
            "n_abstentions": len(sub) - n,
            "error_raw": k / n if n else math.nan,
            "ci_lo": t_lo,
            "ci_hi": t_hi,
            "abstention_rate": (len(sub) - n) / len(sub) if len(sub) else math.nan,
            "interval": "wilson_95",
        })
        if no_error:
            summary.append(_row(
                "wilson_upper_bound", f"tertile:{t}", sub, estimate=0.0, ci_lo=0.0, ci_hi=t_hi,
                interval="wilson_95", note=f"{len(docs)} documents",
            ))

    for reg in sorted(values["regulator"].unique()):
        sub = values[values["regulator"] == reg]
        summary.append(_row(
            "error_by_regulator", reg, sub, estimate=_weighted_mean(sub[sub["answered"]], "y"),
            note="design-weighted point estimate; regulator is a post-stratification covariate",
        ))

    mid = values[values["tertile"] == "mid"]
    for mode in MODES:
        sub = mid[mid["mode"] == mode]
        ans = sub[sub["answered"]]
        k, n = int(ans["y"].sum()), len(ans)
        m_lo, m_hi = wilson_ci(k, n)
        summary.append(_row(
            "mid_tertile_error_by_mode", mode, sub, estimate=k / n if n else math.nan,
            ci_lo=m_lo, ci_hi=m_hi, interval="wilson_95", note="descriptive",
        ))

    if "n_flags" in values.columns:
        for k_flags in sorted(answered["n_flags"].unique()):
            sub = values[values["answered"] & (values["n_flags"] == k_flags)]
            summary.append(_row(
                "error_by_n_flags", f"n_flags={int(k_flags)}", sub, estimate=_weighted_mean(sub, "y"),
                note="design-weighted point estimate; verification of the risk flags",
            ))

    summary.extend(minutes_by_mode(values))

    by_field_rows = []
    for field in group_b_fields():
        sub = values[values["field"] == field]
        ans = sub[sub["answered"]]
        abst_f = sub.assign(_a=(~sub["answered"]).astype(float))
        row: dict[str, Any] = {
            "field": field,
            "n_docs": int(sub["doc_id"].nunique()),
            "n_values": len(sub),
            "n_answered": len(ans),
            "n_errors": int(ans["y"].sum()),
            "n_abstentions": len(sub) - len(ans),
            "error_weighted": math.nan,
            "ci_lo": math.nan,
            "ci_hi": math.nan,
            "abstention_rate_weighted": _weighted_mean(abst_f, "_a"),
            "interval": "",
            "note": "",
        }
        if len(ans):
            f_point, f_lo, f_hi = _bootstrap(sub, n_boot, seed)
            row["error_weighted"] = f_point
            if ans["y"].sum() == 0:
                row["note"] = "no error in this field: the bootstrap is degenerate"
            else:
                row.update(ci_lo=f_lo, ci_hi=f_hi, interval=label_, note="wide")
        by_field_rows.append(row)

    return AuditEstimates(
        summary=pd.DataFrame(summary, columns=SUMMARY_COLUMNS),
        by_field=pd.DataFrame(by_field_rows, columns=BY_FIELD_COLUMNS),
        by_tertile=pd.DataFrame(by_tertile_rows, columns=BY_TERTILE_COLUMNS),
    )


def minutes_by_mode(values: pd.DataFrame) -> list[dict[str, Any]]:
    """Mean labeling minutes per value by blinding mode and overall (cost table input).

    Unweighted means over every audited value, abstained values included, since each
    sheet row is one timed value.
    """
    out = []
    for mode in (*MODES, "all"):
        sub = values if mode == "all" else values[values["mode"] == mode]
        mins = sub["minutes"].astype(float).dropna()
        out.append(_row(
            "minutes_per_value", mode, sub, estimate=float(mins.mean()) if len(mins) else math.nan,
            note=f"unweighted mean over {len(mins)} timed values; total {mins.sum():.1f} minutes",
        ))
    return out


# ----------------------------------------------------------------------------- review curve


def review_curve(values: pd.DataFrame, points: Iterable[float] = OPERATING_POINTS) -> pd.DataFrame:
    """Residual Group B error against the share of values reviewed (design weights).

    Answered values are ordered by ``n_flags``, highest first. Reviewing a block of equal
    flag counts in random order removes its weighted errors linearly, so the curve is
    piecewise linear with knots at block ends: x = cumulative weight reviewed / total
    weight, y = weighted errors among unreviewed values / total weight, assuming review
    corrects every reviewed value. At x = 0 the curve equals the design-weighted error.
    Rows: the knots (``point`` = ``knot``) and the operating points interpolated on them
    (``point`` = ``operating_point``).
    """
    ans = values[values["answered"]]
    if ans.empty:
        return pd.DataFrame(columns=CURVE_COLUMNS)
    total = float(ans["weight"].sum())
    errors = float((ans["weight"] * ans["y"]).sum())
    xs, ys, blocks = [0.0], [errors / total], [math.nan]
    rows: list[dict[str, Any]] = [{
        "point": "knot", "share_reviewed": 0.0, "residual_error": errors / total,
        "n_flags_block": math.nan, "note": "nothing reviewed",
    }]
    x, remaining = 0.0, errors
    for k in sorted(ans["n_flags"].unique(), reverse=True):
        block = ans[ans["n_flags"] == k]
        x += float(block["weight"].sum()) / total
        remaining -= float((block["weight"] * block["y"]).sum())
        y = max(remaining, 0.0) / total
        xs.append(x)
        ys.append(y)
        blocks.append(int(k))
        rows.append({
            "point": "knot", "share_reviewed": x, "residual_error": y, "n_flags_block": int(k),
            "note": f"end of the n_flags={int(k)} block ({len(block)} values)",
        })
    xs[-1] = 1.0
    rows[-1]["share_reviewed"] = 1.0
    for p in points:
        y = float(np.interp(p, xs, ys))
        i = next((j for j in range(1, len(xs)) if p <= xs[j] + 1e-12), len(xs) - 1)
        rows.append({
            "point": "operating_point", "share_reviewed": float(p), "residual_error": y,
            "n_flags_block": blocks[i], "note": f"top {round(p * 100)} percent of values by n_flags",
        })
    return pd.DataFrame(rows, columns=CURVE_COLUMNS)


# ----------------------------------------------------------------------------- outputs


def write_outputs(est: AuditEstimates, curve: pd.DataFrame, out_dir: Path) -> dict[str, Path]:
    """Write the four result files and return their paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": out_dir / "audit_summary.csv",
        "by_field": out_dir / "audit_by_field.csv",
        "by_tertile": out_dir / "audit_by_tertile.csv",
        "review_curve": out_dir / "review_curve_group_b.csv",
    }
    est.summary.to_csv(paths["summary"], index=False, float_format="%.5f")
    est.by_field.to_csv(paths["by_field"], index=False, float_format="%.5f")
    est.by_tertile.to_csv(paths["by_tertile"], index=False, float_format="%.5f")
    curve.to_csv(paths["review_curve"], index=False, float_format="%.5f")
    return paths


def doc_risk_mismatches(values: pd.DataFrame, risk_path: Path) -> list[str]:
    """Audit documents whose ``n_flags`` sum differs from ``doc_risk`` in the risk file
    the draw used (a different flag input would mean the strata and the curve disagree)."""
    if not Path(risk_path).exists() or "n_flags" not in values.columns:
        return []
    recorded = strata.read_two_column_csv(Path(risk_path), "doc_risk", float)
    sums = values.groupby("doc_id")["n_flags"].sum()
    return [
        f"{d}: n_flags sum {int(s)}, doc_risk {recorded[d]:g}"
        for d, s in sums.items()
        if d in recorded and abs(recorded[d] - s) > 1e-9
    ]


def read_prediction_files(
    paths: Iterable[Path],
) -> tuple[list[FieldValue], dict[tuple[str, str, str], str | float | None]]:
    """``evaluate.read_predictions`` over several files that exist (the corpus predictions
    and the S5 rows of ``routing apply``); a later file never replaces an earlier key."""
    preds: list[FieldValue] = []
    column: dict[tuple[str, str, str], str | float | None] = {}
    seen: set[tuple[str, str, str]] = set()
    found = [Path(p) for p in paths if Path(p).exists()]
    if not found:
        raise AuditError("none of the predictions files exists: " + ", ".join(str(p) for p in paths))
    for path in found:
        rows, col = evaluate.read_predictions(path)
        for fv in rows:
            key = (fv.doc_id, fv.field, fv.system)
            if key in seen:
                continue
            seen.add(key)
            preds.append(fv)
            if col is not None and key in col:
                column[key] = col[key]
    return preds, column


# ----------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point; returns the process exit code (1 on problems)."""
    p = argparse.ArgumentParser(prog="python -m cra_extract.audit", description=__doc__.split("\n\n")[0])
    p.add_argument("--strata", type=Path, default=STRATA_PATH, help="strata.yaml with the audit draw")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sheet", help="write the blinded audit sheet")
    s.add_argument("--system", default=DEFAULT_SYSTEM, help="system name of the released Group B values (S5)")
    s.add_argument("--predictions", type=Path, nargs="+", default=[PREDICTIONS_PATH, S5_PREDICTIONS_PATH])
    s.add_argument("--out", type=Path, default=SHEET_PATH)
    s.add_argument("--force", action="store_true", help="overwrite an existing sheet")

    s = sub.add_parser("validate", help="check a filled audit sheet; exit 1 on problems")
    s.add_argument("path", type=Path, nargs="?", default=SHEET_PATH)

    s = sub.add_parser("estimate", help="design-weighted error, breakdowns and review curve")
    s.add_argument("--system", default=DEFAULT_SYSTEM, help="system name of the released Group B values (S5)")
    s.add_argument("--compare-system", default=DEFAULT_COMPARE_SYSTEM,
                   help="system for the disagreement flag when the flags are recomputed")
    s.add_argument("--sheet", type=Path, default=SHEET_PATH)
    s.add_argument("--predictions", type=Path, nargs="+", default=[PREDICTIONS_PATH, S5_PREDICTIONS_PATH])
    s.add_argument("--risk", type=Path, default=RISK_PATH, help="doc_risk file of the draw, cross-checked")
    s.add_argument("--risk-values", type=Path, default=RISK_VALUES_PATH,
                   help="per-value flags behind --risk; recomputed with risk_table when the file is absent")
    s.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    s.add_argument("--n-boot", type=int, default=N_BOOT)
    s.add_argument("--seed", type=int, default=BOOT_SEED)

    a = p.parse_args(argv)
    try:
        design = load_design(a.strata)
        if a.cmd == "sheet":
            if a.out.exists() and not a.force:
                print(f"error: {a.out} exists; pass --force to overwrite it", file=sys.stderr)
                return 1
            preds, _ = read_prediction_files(a.predictions)
            rows = build_sheet(design, preds, a.system)
            write_sheet(rows, a.out)
            n_shown = sum(1 for m in blinding_modes(design).values() if m == "shown")
            print(f"wrote {len(rows)} rows for {len(design.doc_ids)} audit documents ({n_shown} shown) to {a.out}")
            return 0
        if a.cmd == "validate":
            problems = validate(a.path, design)
            for pr in problems:
                print(pr)
            print(f"{len(problems)} problem(s) in {a.path}")
            return 1 if problems else 0
        problems = validate(a.sheet, design)
        if problems:
            for pr in problems:
                print(pr, file=sys.stderr)
            print(f"error: {len(problems)} problem(s) in {a.sheet}; estimate refused", file=sys.stderr)
            return 1
        preds, column = read_prediction_files(a.predictions)
        values = score_audit(label.read_sheet(a.sheet), preds, design, a.system)
        if Path(a.risk_values).exists():
            values = flags_from_risk_values(values, a.risk_values, a.system)
        else:
            print(f"warning: {a.risk_values} not found; n_flags recomputed from the predictions", file=sys.stderr)
            values = add_flags(values, preds, a.system, column, a.compare_system)
        mismatches = doc_risk_mismatches(values, a.risk)
        if mismatches:
            for msg in mismatches:
                print(msg, file=sys.stderr)
            print(f"error: the n_flags of {len(mismatches)} audit documents differ from the doc_risk the "
                  f"tertiles were drawn on ({a.risk}); estimate refused", file=sys.stderr)
            return 1
        est = estimate(values, design, n_boot=a.n_boot, seed=a.seed)
        paths = write_outputs(est, review_curve(values), a.out_dir)
    except AuditError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    head = est.summary.iloc[0]
    print(f"design-weighted Group B error {head['estimate']:.4f} [{head['ci_lo']:.4f}, {head['ci_hi']:.4f}]")
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
