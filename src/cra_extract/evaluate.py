"""Evaluator: scoring matrix, tolerances, metrics table, errors, risk flags, test guard.

Implements design section 6 (6.1, 6.2, 6.4, 6.5, 6.6). The flow is:

1. ``score_cell`` applies the scoring matrix of section 6.2 to one (gold label,
   system value) pair and returns one of ``CELLS``.
2. ``values_match`` applies the field's tolerance rule from ``fields.yaml``.
3. ``score_predictions`` joins labels and predictions into one scored row per
   (doc_id, field, system), with the grounding outcome from ``grounding.check``
   when the document text is available (or from a ``grounding_failure`` column
   written by the run stage).
4. ``metrics_table`` aggregates per (field, system): accuracy or precision and
   recall with Wilson CIs, abstention and fabrication rates with denominators,
   the three groundedness failure rates, the 2x2 correctness x groundedness
   cells and the derived hallucination rate.
5. ``error_table`` lists every non-correct cell with an empty ``cause`` column
   for hand classification (section 6.6).
6. ``risk_table`` computes the four flags per value (section 6.5) and the
   document risk as the sum over Group B values.
7. ``check_test_guard`` enforces the work rule of section 6.1 for ``--split test``.

Conventions: a gold label without a prediction for a system counts as an
abstention of that system. Hallucination rate denominator is every non-abstained
item; numerator is wrong, fabrication, fabricated absence or a misgrounded quote.
Precision and recall are conditional on the system answering; ``recall_overall``
counts abstentions on gold positives as misses.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from rapidfuzz import fuzz

from cra_extract import grounding
from cra_extract.config import DOCS_DIR, GOLD_DIR, RESULTS_DIR, ROOT, load_fields
from cra_extract.models import RATING_WORDS, FieldValue, LabelRow, Page, Section
from cra_extract.stats import expected_calibration_error, wilson_ci

CELLS = ("correct", "wrong", "fabrication", "fabricated_absence", "correct_null", "abstention")
GROUNDING_FAILURES = ("quote_not_found", "quote_out_of_scope", "quote_does_not_support")
RISK_GROUPS = ("B",)
PLAN_TAG = "plan-v1.0"
ENV_OVERRIDE = "CRA_ALLOW_TEST_EVAL"


class TestGuardError(RuntimeError):
    """Raised when an evaluation on the test split is not allowed."""


# -------------------------------------------------------------- tolerances


def _norm_str(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _norm_enum(value: Any) -> str:
    """Casefolded enum code; a rating given by its printed word maps to its code."""
    s = _norm_str(value)
    return RATING_WORDS.get(s, s).casefold()


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _quarter(d: date | None) -> tuple[int, int] | None:
    return None if d is None else (d.year, (d.month - 1) // 3 + 1)


def values_match(
    gold_value: Any,
    pred_value: Any,
    spec: Mapping[str, Any],
    gold_asof: Any = None,
    pred_asof: Any = None,
) -> bool:
    """Apply the field's tolerance rule. ``*_asof`` are only used by
    ``within_1pct_same_quarter``: when both are present they must fall in the same
    calendar quarter; when either is missing only the numbers are compared."""
    tol = spec.get("tolerance", "exact")
    ftype = spec.get("type", "str")
    if gold_value is None or pred_value is None:
        return False
    if ftype == "set":
        return grounding.parse_set(gold_value) == grounding.parse_set(pred_value)
    if ftype == "bool":
        g, p = grounding.parse_bool(gold_value), grounding.parse_bool(pred_value)
        return g is not None and g == p
    if ftype == "date":
        g, p = grounding.parse_date(gold_value), grounding.parse_date(pred_value)
        return g is not None and g == p
    if ftype in {"int", "float"}:
        g, p = _to_float(gold_value), _to_float(pred_value)
        if g is None or p is None:
            return False
        if tol == "within_0.1pp":
            return abs(g - p) <= 0.1 + 1e-9
        if tol == "within_1pct_same_quarter":
            qg, qp = _quarter(grounding.parse_date(gold_asof)), _quarter(grounding.parse_date(pred_asof))
            if qg is not None and qp is not None and qg != qp:
                return False
            return abs(g - p) <= 0.01 * abs(g) + 1e-9
        return abs(g - p) <= 1e-9
    if ftype == "enum":
        return _norm_enum(gold_value) == _norm_enum(pred_value)
    if tol == "exact_strip_zeros":
        return _norm_str(gold_value).lstrip("0") == _norm_str(pred_value).lstrip("0")
    if tol == "name_fuzzy_90":
        return fuzz.token_set_ratio(_norm_str(gold_value), _norm_str(pred_value)) >= 90
    return _norm_str(gold_value) == _norm_str(pred_value)


# ----------------------------------------------------------- scoring matrix


def score_cell(
    gold: LabelRow,
    pred: FieldValue | None,
    spec: Mapping[str, Any],
    gold_asof: Any = None,
    pred_asof: Any = None,
) -> str:
    """Scoring matrix of design section 6.2 (rows gold, columns system).

    ======================  =========================  ================  ==========
    gold \\ system           value                      absent            abstain
    ======================  =========================  ================  ==========
    value                   correct or wrong           fabricated_absence  abstention
    not_applicable          fabrication                correct_null      abstention
    absent_from_document    fabrication                correct_null      abstention
    ======================  =========================  ================  ==========
    """
    status = "abstain" if pred is None else pred.status
    if status == "abstain":
        return "abstention"
    if gold.is_null:
        return "fabrication" if status == "value" else "correct_null"
    if status == "absent":
        return "fabricated_absence"
    assert pred is not None
    return "correct" if values_match(gold.value, pred.value, spec, gold_asof, pred_asof) else "wrong"


# ------------------------------------------------------- document loading


def load_document(doc_id: str, text_dir: Path) -> tuple[list[Page], dict[str, Section]] | None:
    """Pages and sections for grounding. Reads ``{doc_id}.pages.json`` (a dict with
    ``pages`` and optional ``sections`` lists, as written by the locator) or, failing
    that, ``{doc_id}.txt`` with pages separated by form feed and no scope tags."""
    json_path = text_dir / f"{doc_id}.pages.json"
    if json_path.exists():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            payload = {"pages": payload}
        pages = [Page(**p) for p in payload.get("pages", [])]
        sections = {s["name"]: Section(**s) for s in payload.get("sections", [])}
        return pages, sections
    txt_path = text_dir / f"{doc_id}.txt"
    if txt_path.exists():
        texts = txt_path.read_text(encoding="utf-8").split("\f")
        return [Page(number=i + 1, text=t, chars=len(t)) for i, t in enumerate(texts)], {}
    return None


def _regulator_of(doc_id: str) -> str:
    return doc_id.split("_", 1)[0]


# ---------------------------------------------------------------- scoring


def score_predictions(
    labels: list[LabelRow],
    preds: list[FieldValue],
    systems: list[str] | None = None,
    doc_loader: Callable[[str], tuple[list[Page], dict[str, Section]] | None] | None = None,
    grounding_column: Mapping[tuple[str, str, str], str | None] | None = None,
) -> pd.DataFrame:
    """One scored row per (doc_id, field, system).

    ``grounding_column`` maps (doc_id, field, system) to a failure category or
    ``None`` for grounded, as read from the predictions file; when it is absent the
    documents are loaded through ``doc_loader`` and ``grounding.check`` runs. Rows
    whose grounding could not be determined have ``grounding_failure`` NaN and
    ``grounded`` NaN.
    """
    fields = {k: v for k, v in load_fields()["fields"].items() if v.get("metric_type") != "not_scored"}
    gold_by_key = {(lab.doc_id, lab.field): lab for lab in labels if lab.field in fields}
    pred_by_key = {(p.doc_id, p.field, p.system): p for p in preds if p.field in fields}
    all_systems = sorted({p.system for p in preds}) if systems is None else list(systems)
    doc_cache: dict[str, tuple[list[Page], dict[str, Section]] | None] = {}
    rows: list[dict[str, Any]] = []
    for (doc_id, field), gold in gold_by_key.items():
        spec = fields[field]
        gold_asof = gold_by_key.get((doc_id, "total_assets_asof"))
        for system in all_systems:
            pred = pred_by_key.get((doc_id, field, system))
            pred_asof = pred_by_key.get((doc_id, "total_assets_asof", system))
            cell = score_cell(
                gold, pred, spec,
                gold_asof.value if gold_asof else None,
                pred_asof.value if pred_asof else None,
            )
            failure: str | float | None = math.nan
            if pred is not None and pred.status == "value":
                failure = _grounding_for(pred, grounding_column, doc_loader, doc_cache, spec)
            row: dict[str, Any] = {
                "doc_id": doc_id,
                "field": field,
                "system": system,
                "group": spec.get("group", ""),
                "metric_type": spec.get("metric_type", "accuracy"),
                "gold_status": gold.null_reason or "value",
                "gold_value": _display(gold.value),
                "pred_status": "abstain" if pred is None else pred.status,
                "pred_value": _display(None if pred is None else pred.value),
                "has_quote": bool(pred is not None and pred.quote),
                "cell": cell,
                "grounding_failure": failure,
                "grounded": math.nan if (isinstance(failure, float) and math.isnan(failure)) else failure is None,
            }
            row.update(_pr_counts(gold, pred, spec))
            rows.append(row)
    return pd.DataFrame(rows)


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, set, tuple)):
        return "|".join(sorted(str(v) for v in value))
    return str(value)


def _grounding_for(
    pred: FieldValue,
    column: Mapping[tuple[str, str, str], str | None] | None,
    loader: Callable[[str], tuple[list[Page], dict[str, Section]] | None] | None,
    cache: dict[str, tuple[list[Page], dict[str, Section]] | None],
    spec: Mapping[str, Any],
) -> str | float | None:
    key = (pred.doc_id, pred.field, pred.system)
    if column is not None and key in column:
        return column[key]
    if loader is None:
        return math.nan
    if pred.doc_id not in cache:
        cache[pred.doc_id] = loader(pred.doc_id)
    doc = cache[pred.doc_id]
    if doc is None:
        return math.nan
    pages, sections = doc
    return grounding.check(pred, pages, sections, spec, _regulator_of(pred.doc_id)).failure


def _pr_counts(gold: LabelRow, pred: FieldValue | None, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Binary and set-level counts used by ``metrics_table``."""
    out: dict[str, Any] = {"gold_pos": math.nan, "pred_pos": math.nan, "tp": math.nan, "fp": math.nan, "fn": math.nan}
    answered = pred is not None and pred.status == "value"
    if spec.get("metric_type") == "binary_pr":
        pos = spec.get("positive", True)
        g = grounding.parse_bool(gold.value) if not gold.is_null else None
        out["gold_pos"] = g == pos if g is not None else False
        out["pred_pos"] = (grounding.parse_bool(pred.value) == pos) if answered else False
    elif spec.get("metric_type") == "set_pr" and answered and not gold.is_null:
        g, p = grounding.parse_set(gold.value), grounding.parse_set(pred.value)
        out.update(tp=len(g & p), fp=len(p - g), fn=len(g - p))
    return out


# ------------------------------------------------------------ metrics table


def _rate(k: float, n: float) -> tuple[float, float, float]:
    if not n:
        return (math.nan, math.nan, math.nan)
    lo, hi = wilson_ci(int(k), int(n))
    return (k / n, lo, hi)


def metrics_table(scored: pd.DataFrame) -> pd.DataFrame:
    """Per (field, system) metrics (design 6.2 and 6.4). One row per pair, ordered
    as in ``fields.yaml`` and then by system name."""
    order = {f: i for i, f in enumerate(load_fields()["fields"])}
    rows: list[dict[str, Any]] = []
    if scored.empty:
        return pd.DataFrame(columns=["field", "system"])
    for (field, system), g in scored.groupby(["field", "system"], sort=False):
        mt = g["metric_type"].iloc[0]
        n = len(g)
        counts = {c: int((g["cell"] == c).sum()) for c in CELLS}
        n_gold_value = int((g["gold_status"] == "value").sum())
        n_gold_null = n - n_gold_value
        answered = g[g["pred_status"] == "value"]
        not_abstained = g[g["pred_status"] != "abstain"]
        row: dict[str, Any] = {
            "field": field,
            "system": system,
            "group": g["group"].iloc[0],
            "metric_type": mt,
            "n_gold": n,
            "n_gold_value": n_gold_value,
            "n_gold_null": n_gold_null,
            "n_answered": len(answered),
            "n_absent": int((g["pred_status"] == "absent").sum()),
            "n_abstain": counts["abstention"],
        }
        row.update({f"n_{c}": counts[c] for c in CELLS if c != "abstention"})
        row["abstention_rate"] = counts["abstention"] / n if n else math.nan
        row["fabrication_denominator"] = n_gold_null
        row["fabrication_rate"], row["fabrication_lo"], row["fabrication_hi"] = _rate(counts["fabrication"], n_gold_null)
        row["fabricated_absence_rate"] = counts["fabricated_absence"] / n_gold_value if n_gold_value else math.nan

        nan3 = (math.nan, math.nan, math.nan)
        acc = prec = rec = nan3
        n_positive = tp = fp = fn = math.nan
        recall_overall = math.nan
        if mt == "accuracy" or mt == "set_pr":
            acc = _rate(counts["correct"], counts["correct"] + counts["wrong"])
        if mt == "binary_pr":
            gp = g["gold_pos"].astype(bool)
            pp = g["pred_pos"].astype(bool)
            ans = g["pred_status"] == "value"
            tp = int((gp & pp & ans).sum())
            fp = int((~gp & pp & ans).sum())
            fn = int((gp & ~pp & ans).sum())
            n_positive = int(gp.sum())
            prec = _rate(tp, tp + fp)
            rec = _rate(tp, tp + fn)
            recall_overall = tp / n_positive if n_positive else math.nan
        if mt == "set_pr":
            tp = int(g["tp"].fillna(0).sum())
            fp = int(g["fp"].fillna(0).sum())
            fn = int(g["fn"].fillna(0).sum())
            prec = _rate(tp, tp + fp)
            rec = _rate(tp, tp + fn)
        if mt == "counts":
            acc = nan3
        row.update(
            accuracy=acc[0], accuracy_lo=acc[1], accuracy_hi=acc[2],
            precision=prec[0], precision_lo=prec[1], precision_hi=prec[2],
            recall=rec[0], recall_lo=rec[1], recall_hi=rec[2],
            recall_overall=recall_overall, n_positive=n_positive, tp=tp, fp=fp, fn=fn,
        )

        # Groundedness over answered items whose grounding could be determined.
        known = answered[answered["grounding_failure"].notna() | (answered["grounded"] == True)]  # noqa: E712
        n_known = len(known)
        has_any_quote = bool(answered["has_quote"].any())
        if n_known and has_any_quote:
            fails = known["grounding_failure"]
            row["n_grounding_checked"] = n_known
            for cat in GROUNDING_FAILURES:
                row[f"{cat}_rate"] = float((fails == cat).sum()) / n_known
            row["grounded_rate"] = float(known["grounded"].astype(bool).sum()) / n_known
            kv = known[known["gold_status"] == "value"]
            gr = kv["grounded"].astype(bool)
            row["correct_grounded"] = int(((kv["cell"] == "correct") & gr).sum())
            row["correct_misgrounded"] = int(((kv["cell"] == "correct") & ~gr).sum())
            row["wrong_grounded"] = int(((kv["cell"] == "wrong") & gr).sum())
            row["wrong_misgrounded"] = int(((kv["cell"] == "wrong") & ~gr).sum())
            misgrounded_correct = row["correct_misgrounded"] + int(((known["cell"] == "correct_null") & ~known["grounded"].astype(bool)).sum())
        else:
            row["n_grounding_checked"] = 0
            for cat in GROUNDING_FAILURES:
                row[f"{cat}_rate"] = math.nan
            row["grounded_rate"] = math.nan
            for c in ("correct_grounded", "correct_misgrounded", "wrong_grounded", "wrong_misgrounded"):
                row[c] = math.nan
            misgrounded_correct = 0
        halluc = counts["wrong"] + counts["fabrication"] + counts["fabricated_absence"] + misgrounded_correct
        row["hallucination_rate"], row["hallucination_lo"], row["hallucination_hi"] = _rate(halluc, len(not_abstained))
        if mt == "counts":
            for k in list(row):
                if k.endswith(("_rate", "_lo", "_hi")) or k in {"accuracy", "precision", "recall", "recall_overall"}:
                    row[k] = math.nan
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["_o"] = df["field"].map(order)
    return df.sort_values(["_o", "system"]).drop(columns="_o").reset_index(drop=True)


# ------------------------------------------------------------- error table


def error_table(scored: pd.DataFrame) -> pd.DataFrame:
    """One row per non-correct cell, with an empty ``cause`` column (design 6.6)."""
    cols = ["doc_id", "field", "system", "gold_status", "gold_value", "pred_status", "pred_value", "cell", "grounding_failure", "cause"]
    if scored.empty:
        return pd.DataFrame(columns=cols)
    bad = scored[~scored["cell"].isin(["correct", "correct_null"])].copy()
    bad = bad[["doc_id", "field", "system", "gold_status", "gold_value", "pred_status", "pred_value", "cell", "grounding_failure"]]
    bad["grounding_failure"] = bad["grounding_failure"].fillna("")
    bad["cause"] = ""
    return bad.sort_values(["doc_id", "field", "system"]).reset_index(drop=True)


# -------------------------------------------------------------- risk flags


def _is_truthy(value: Any) -> bool:
    return grounding.parse_bool(value) is True


def consistency_fail(
    field: str,
    value: Any,
    doc_id: str,
    lookup: Callable[[str], Any],
    spec: Mapping[str, Any],
) -> bool:
    """Apply the field's ``consistency`` key from ``fields.yaml``. ``lookup(field)``
    returns the same system's value for another field of the same document, falling
    back to gold. Keys that need information outside the predictions (the count of
    assessment-area sections) never flag."""
    key = spec.get("consistency")
    if key is None or value is None:
        return False
    if key == "equals_overall_for_OCC_small":
        method = lookup("exam_method")
        overall = lookup("overall_rating")
        if _regulator_of(doc_id) != "OCC" or _norm_str(method) != "small" or overall is None:
            return False
        return _norm_enum(value) != _norm_enum(overall)
    if key == "lte_exam_date":
        end, exam = grounding.parse_date(value), grounding.parse_date(lookup("exam_date"))
        return end is not None and exam is not None and end > exam
    if key == "lte_n_assessment_areas":
        v, n = _to_float(value), _to_float(lookup("n_assessment_areas"))
        return v is not None and n is not None and v > n
    if key == "equals_overall_when_level_overall":
        level, overall = lookup("rating_lowered_level"), lookup("overall_rating")
        if _norm_str(level) != "overall" or overall is None:
            return False
        return _norm_enum(value) != _norm_enum(overall)
    return False


def risk_table(scored: pd.DataFrame, preds: list[FieldValue], labels: list[LabelRow]) -> pd.DataFrame:
    """Risk flags per value (design 6.5) and document risk summed over Group B values.

    Flags: ``locator_miss`` (a ``locator_miss`` note or attribute on the prediction),
    ``disagreement`` (another system answered the same item with a non-matching value),
    ``groundedness_fail`` (any grounding failure), ``consistency_fail`` (the field's
    consistency key). ``doc_risk`` is the sum of ``n_flags`` over the document's Group
    B values for the same system.
    """
    fields = load_fields()["fields"]
    answered = {(p.doc_id, p.field, p.system): p for p in preds if p.status == "value" and p.field in fields}
    gold = {(lab.doc_id, lab.field): lab.value for lab in labels if not lab.is_null}
    by_system: dict[str, set[str]] = {}
    for p in preds:
        by_system.setdefault(p.system, set())
    rows: list[dict[str, Any]] = []
    for r in scored.itertuples(index=False):
        pred = answered.get((r.doc_id, r.field, r.system))
        spec = fields[r.field]
        locator_miss = bool(pred is not None and "locator_miss" in (pred.notes or ""))
        disagreement = False
        if pred is not None:
            for other in by_system:
                if other == r.system:
                    continue
                o = answered.get((r.doc_id, r.field, other))
                if o is not None and not values_match(pred.value, o.value, spec):
                    disagreement = True
                    break
        ground_fail = bool(isinstance(r.grounding_failure, str) and r.grounding_failure)

        def lookup(f: str, _doc: str = r.doc_id, _sys: str = r.system) -> Any:
            p = answered.get((_doc, f, _sys))
            return p.value if p is not None else gold.get((_doc, f))

        cons = bool(pred is not None and consistency_fail(r.field, pred.value, r.doc_id, lookup, spec))
        rows.append({
            "doc_id": r.doc_id, "field": r.field, "system": r.system, "group": r.group,
            "answered": pred is not None,
            "locator_miss": locator_miss, "disagreement": disagreement,
            "groundedness_fail": ground_fail, "consistency_fail": cons,
            "n_flags": int(locator_miss) + int(disagreement) + int(ground_fail) + int(cons),
            "cell": r.cell,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    in_b = df["group"].isin(RISK_GROUPS)
    doc_risk = df[in_b].groupby(["doc_id", "system"])["n_flags"].sum()
    df["doc_risk"] = [int(doc_risk.get((d, s), 0)) for d, s in zip(df["doc_id"], df["system"], strict=True)]
    return df.sort_values(["doc_id", "field", "system"]).reset_index(drop=True)


def calibration_table(risk: pd.DataFrame, n_boot: int = 1000) -> pd.DataFrame:
    """Error rate by flag count on answered Group B values plus ECE with a
    bootstrapped SE (design 6.5; indicative only). Probability = n_flags / 4."""
    if risk.empty:
        return pd.DataFrame(columns=["n_flags", "n", "error_rate", "ece", "ece_se"])
    sub = risk[risk["answered"] & risk["group"].isin(RISK_GROUPS)]
    if sub.empty:
        return pd.DataFrame(columns=["n_flags", "n", "error_rate", "ece", "ece_se"])
    err = (sub["cell"] != "correct").astype(int)
    rows = []
    for k, g in sub.assign(err=err).groupby("n_flags"):
        rows.append({"n_flags": int(k), "n": len(g), "error_rate": float(g["err"].mean())})
    ece, se = expected_calibration_error((sub["n_flags"] / 4.0).tolist(), err.tolist(), n_bins=5, n_boot=n_boot)
    df = pd.DataFrame(rows)
    df["ece"] = ece
    df["ece_se"] = se
    return df


# --------------------------------------------------------------- test guard


def git_tag_exists(root: Path, tag: str = PLAN_TAG) -> bool:
    try:
        out = subprocess.run(["git", "tag", "-l", tag], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return False
    return tag in out.stdout.split()


_DATE_LINE = re.compile(r"^\s*(?:#+|[-*]|\d+\.)\s.*?(\d{4}-\d{2}-\d{2})")


def deviation_dates(path: Path) -> list[date]:
    """Dates of entries in ``docs/deviations.md`` (headings or list items that carry an
    ISO date)."""
    if not path.exists():
        return []
    dates = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _DATE_LINE.match(line)
        if m:
            try:
                dates.append(date.fromisoformat(m.group(1)))
            except ValueError:
                continue
    return dates


def check_test_guard(
    split: str,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    replay_only: bool = False,
    now: datetime | None = None,
    note: str = "",
) -> None:
    """Enforce the work rule (design 6.1) for ``split == "test"``:

    - the git tag ``plan-v1.0`` must exist (or ``CRA_ALLOW_TEST_EVAL=1`` for CI on traces);
    - every invocation is appended to ``results/test_invocations.log``;
    - a second scoring run needs an entry in ``docs/deviations.md`` dated on or after
      the first invocation. ``--replay-only`` regenerations are logged but neither
      count as a run nor are blocked by this rule.
    """
    if split != "test":
        return
    env = os.environ if env is None else env
    if not (git_tag_exists(root) or env.get(ENV_OVERRIDE) == "1"):
        raise TestGuardError(
            f"split=test requires git tag {PLAN_TAG} (make freeze) or {ENV_OVERRIDE}=1 for replay in CI"
        )
    log = root / "results" / "test_invocations.log"
    runs = []
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == "run":
                runs.append(datetime.fromisoformat(parts[0]))
    if runs and not replay_only:
        first = runs[0].date()
        newer = [d for d in deviation_dates(root / "docs" / "deviations.md") if d >= first]
        if not newer:
            raise TestGuardError(
                f"test split already evaluated on {runs[0].isoformat(timespec='seconds')}; "
                "add a dated entry to docs/deviations.md before running again"
            )
    now = now or datetime.now().astimezone()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{now.isoformat(timespec='seconds')}\t{'replay' if replay_only else 'run'}\t{note}\n")


# ------------------------------------------------------------------- I/O


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (None if v == "" else v) for k, v in d.items() if k is not None}


def read_labels(path: Path) -> list[LabelRow]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return [LabelRow(**_clean(r)) for r in df.to_dict("records")]


def read_predictions(path: Path) -> tuple[list[FieldValue], dict[tuple[str, str, str], str | None] | None]:
    """Predictions as ``FieldValue`` rows plus the optional ``grounding_failure`` column
    (empty cell = grounded) keyed by (doc_id, field, system)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    fv_fields = set(FieldValue.model_fields)
    preds, column = [], None
    if "grounding_failure" in df.columns:
        column = {}
    for raw in df.to_dict("records"):
        r = _clean(raw)
        fv = FieldValue(**{k: v for k, v in r.items() if k in fv_fields})
        if "locator_miss" in r and _is_truthy(r["locator_miss"]):
            fv.notes = (fv.notes + "; " if fv.notes else "") + "locator_miss"
        preds.append(fv)
        if column is not None:
            g = r.get("grounding_failure")
            column[(fv.doc_id, fv.field, fv.system)] = g if g in GROUNDING_FAILURES else None
    return preds, column


def run(
    split: str,
    systems: list[str] | None = None,
    replay_only: bool = False,
    labels_path: Path | None = None,
    predictions_path: Path | None = None,
    out_dir: Path | None = None,
    text_dir: Path | None = None,
    root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    note: str = "",
) -> dict[str, Path]:
    """Evaluate one split end to end and write the result files (table1, errors,
    risk, calibration and the row-level scored table)."""
    check_test_guard(split, root=root, env=env, replay_only=replay_only, note=note)
    labels_path = labels_path or root / "data" / "gold" / f"labels_{split}.csv"
    predictions_path = predictions_path or root / "results" / f"predictions_{split}.csv"
    out_dir = out_dir or root / "results"
    text_dir = text_dir or root / "data" / "gold" / "text"
    out_dir.mkdir(parents=True, exist_ok=True)

    labels = read_labels(labels_path)
    preds, column = read_predictions(predictions_path)
    if systems:
        preds = [p for p in preds if p.system in systems]
    scored = score_predictions(labels, preds, systems, doc_loader=lambda d: load_document(d, text_dir), grounding_column=column)
    table1 = metrics_table(scored)
    errors = error_table(scored)
    risk = risk_table(scored, preds, labels)
    calib = calibration_table(risk)

    paths = {
        "table1": out_dir / f"table1_{split}.csv",
        "errors": out_dir / f"errors_{split}.csv",
        "risk": out_dir / f"risk_{split}.csv",
        "calibration": out_dir / f"calibration_{split}.csv",
        "scored": out_dir / f"scored_{split}.csv",
    }
    table1.to_csv(paths["table1"], index=False, float_format="%.5f")
    errors.to_csv(paths["errors"], index=False)
    risk.to_csv(paths["risk"], index=False)
    calib.to_csv(paths["calibration"], index=False, float_format="%.5f")
    scored.to_csv(paths["scored"], index=False)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score predictions against gold labels.")
    parser.add_argument("--split", required=True, choices=["dev", "test"])
    parser.add_argument("--systems", nargs="*", default=None, help="restrict to these system names")
    parser.add_argument("--replay-only", action="store_true", help="regeneration from traces; logged, not counted as a test run")
    parser.add_argument("--labels", type=Path, default=None, help=f"default {GOLD_DIR}/labels_{{split}}.csv")
    parser.add_argument("--predictions", type=Path, default=None, help=f"default {RESULTS_DIR}/predictions_{{split}}.csv")
    parser.add_argument("--out-dir", type=Path, default=None, help=f"default {RESULTS_DIR}")
    parser.add_argument("--text-dir", type=Path, default=None, help=f"default {GOLD_DIR}/text")
    parser.add_argument("--root", type=Path, default=ROOT, help=f"repo root for the test guard (default {ROOT}; docs in {DOCS_DIR})")
    args = parser.parse_args(argv)
    systems = None
    if args.systems:
        systems = [s for chunk in args.systems for s in chunk.split(",") if s]
    try:
        paths = run(
            args.split, systems, args.replay_only, args.labels, args.predictions,
            args.out_dir, args.text_dir, root=args.root, note=" ".join(argv if argv is not None else sys.argv[1:]),
        )
    except TestGuardError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for name, p in paths.items():
        print(f"{name}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
