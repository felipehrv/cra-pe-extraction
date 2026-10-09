"""Report stage: every table and figure of the analysis plan, assembled from ``results/``.

The stage reads only files that earlier stages wrote (``evaluate``, ``run``, the census
and audit outputs, the manifest and the indexes) and writes deterministic outputs: rows in
a fixed order, floats at fixed precision, no timestamps, and figures with their metadata
stripped, so the same inputs regenerate byte-identical files (``--check``). Pieces:

1. :func:`table1`: Table 1 from ``results/table1_{split}.csv`` (``evaluate.metrics_table``).
   Rows field x system for Group A (labeled smoke test), the component ratings and Group B,
   with the columns of the plan's "Metrics" section; the ablation (``:whole``) and
   closed-book (``:closedbook``) systems are left out of every Table 1 view. The fabrication
   probes (component-rating ABS cells; rating_before, rating_after and laws_cited on
   documents whose gold finding_present is false) go to ``table_probes_{split}``; the
   overall rates go to the appendix table and the correctness x groundedness cells to the
   grounding table. :func:`ablation` (located vs whole-document accuracy on the hash-sample
   documents, with discordant counts) and :func:`closed_book` (closed-book accuracy next to
   the test majority-class rate, plan Leakage) report the two variant systems.
2. :func:`figure1`: the review curve (Group B from the audit, Group A from the corpus index
   comparison), the calibration inset (from the pair run of ``evaluate --systems`` written to
   ``ReportPaths.calibration_dir``, default ``results/pair``, plan Risk score flag 2) and the
   manual-review error line. Only the inputs that exist are drawn; the caption file lists
   the missing ones.
3. :func:`table2`: the data-quality table, sorted by root cause and then source.
4. :func:`table3`: the cost table (tokens, locator fallback rate, cost per document and per
   correct field, corpus extrapolation, manual baseline, three review operating points).
5. :func:`descriptive`: the exploratory tables of design section 7 (NI and SN
   decomposition, findings registry, FRB ISB matrix-floor check, rating transitions, exam
   cycle length, publication lag). Generated text carries no comparative language.
6. :func:`check`: regenerate every output into a temporary directory and compare bytes with
   the committed files (CI).

Input contracts are documented on the reading functions: :func:`load_curve_frame` for
``review_curve_group_b.csv`` (``audit``) and ``risk_validation_group_a.csv``
(``corpus_scoring``), :func:`manual_review_error` for ``reliability.csv``
(``cra_extract.reliability``) and :func:`read_census_positives` for ``census_positives.csv``
(``census``).

CLI::

    python -m cra_extract.report [all|table1|ablation|closed-book|figure1|table2|table3|descriptive]
        [--split test] [--corpus-system SYSTEM] [--calibration-dir results/pair] [--no-figures]
    python -m cra_extract.report --check results/table1_test.csv [--no-figures]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from cra_extract.config import (
    CONFIG_DIR,
    CORPUS_PATH,
    GOLD_DIR,
    INDEX_DIR,
    PRICES_PATH,
    RESULTS_DIR,
    TRACE_DIR,
    applicability,
    field_names,
    load_fields,
)
from cra_extract.models import Trace
from cra_extract.stats import clopper_pearson_ci, wilson_ci

RATINGS = ("O", "S", "NI", "SN")
RATING_RANK = {"SN": 0, "NI": 1, "S": 2, "O": 3}
REGULATORS = ("OCC", "FDIC", "FRB")
METHODS = ("small", "isb")
LOW_RATINGS = ("NI", "SN")
OPERATING_SHARES = (0.10, 0.25, 0.50)

TABLE1_SECTIONS = {"A": "Group A (smoke test)", "A_or_B": "Component ratings", "B": "Group B"}
# Systems the plan reports with "error rate, no grounding component" instead of a
# hallucination rate (analysis plan, Metrics). The code gives them quotes; see the report.
NO_GROUNDING_SYSTEMS = ("rules", "baseline")
# Run-mode variants of an LLM tier (run.MODE_SUFFIXES): reported in their own tables only.
WHOLE_SUFFIX = ":whole"
CLOSED_BOOK_SUFFIX = ":closedbook"
VARIANT_SUFFIXES = (WHOLE_SUFFIX, CLOSED_BOOK_SUFFIX)
# Group C fabrication probes (plan, Field groups): on documents whose gold finding_present is false.
GROUP_C_PROBE_FIELDS = ("rating_before", "rating_after", "laws_cited")
CLOSED_BOOK_FIELDS = ("overall_rating", "finding_present")


def is_variant(system: str) -> bool:
    """True for the whole-document ablation and closed-book systems."""
    return str(system).endswith(VARIANT_SUFFIXES)
FIGURE_RC = {
    "svg.hashsalt": "cra-pe-extraction",
    "svg.fonttype": "path",
    "font.family": "DejaVu Sans",
    "font.size": 9.0,
    "axes.edgecolor": "#52514e",
    "axes.labelcolor": "#0b0b0b",
    "xtick.color": "#52514e",
    "ytick.color": "#52514e",
    "axes.spines.top": False,
    "axes.spines.right": False,
}
# Categorical slots in fixed order (dataviz reference palette, light mode).
SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"
REFERENCE_GRAY = "#8a8984"

# Words a generated descriptive text must not contain (no comparative language, plan
# "Purpose and scope"); checked by the tests on every generated text file.
COMPARATIVE_WORDS = (
    "higher", "lower", "more", "less", "fewer", "better", "worse", "faster", "slower",
    "larger", "smaller", "compared", "versus", "than", "outperform", "underperform",
)

# FRB ISB overall-rating floor implied by the Lending Test and Community Development Test
# ratings, keyed by (lending, cd). See the docstring of ``isb_overall_floor``.
ISB_OVERALL_FLOOR: dict[tuple[str, str], str] = {
    ("O", "O"): "O",
    ("O", "S"): "S",
    ("O", "NI"): "NI",
    ("O", "SN"): "SN",
    ("S", "O"): "S",
    ("S", "S"): "S",
    ("S", "NI"): "NI",
    ("S", "SN"): "SN",
    ("NI", "O"): "NI",
    ("NI", "S"): "NI",
    ("NI", "NI"): "NI",
    ("NI", "SN"): "SN",
    ("SN", "O"): "SN",
    ("SN", "S"): "SN",
    ("SN", "NI"): "SN",
    ("SN", "SN"): "SN",
}


def isb_overall_floor(lending: str, cd: str) -> str | None:
    """Lowest overall rating an intermediate small bank can receive from its two test
    ratings alone (``ISB_OVERALL_FLOOR``); ``None`` when either rating is not a code.

    Source: the intermediate small bank rules of Appendix A to 12 CFR part 228
    (Regulation BB, Federal Reserve; the FDIC equivalent is Appendix A to 12 CFR part 345)
    and 12 CFR 228.28 (FDIC 12 CFR 345.28, "Assigned ratings"), whose paragraph (c) lets
    evidence of discriminatory or other illegal credit practices lower a rating. As recalled
    here, the appendix says that an ISB with Outstanding on one test and at least Satisfactory
    on the other may receive Outstanding overall, and that it may not receive Satisfactory
    overall unless both tests are at least Satisfactory. The matrix encodes the floor as the
    lower of the two test ratings: an overall rating below both test ratings is not explained
    by the two tests, so the design (section 7) reads it as a candidate downgrade under
    paragraph (c). THE AUTHOR MUST VERIFY this matrix against the eCFR text in force on each
    exam date before any result of the check is reported.
    """
    return ISB_OVERALL_FLOOR.get((str(lending).strip(), str(cd).strip()))


# ----------------------------------------------------------------------------- paths


@dataclass(frozen=True)
class ReportPaths:
    """Where the report reads and writes; ``under(root)`` rebases everything for tests.

    ``results_dir`` holds the inputs written by earlier stages and ``out_dir`` receives the
    report outputs (the same directory in normal use, a temporary one under ``--check``).
    """

    results_dir: Path = RESULTS_DIR
    out_dir: Path = RESULTS_DIR
    manifest: Path = CORPUS_PATH
    frb_index: Path = INDEX_DIR / "frb.csv"
    gold_dir: Path = GOLD_DIR
    trace_dir: Path = TRACE_DIR
    prices: Path = PRICES_PATH
    matrix_floor_triage: Path = CONFIG_DIR / "frb_matrix_floor_triage.yaml"
    calibration_dir: Path | None = None

    @classmethod
    def under(cls, root: Path, out_dir: Path | None = None) -> ReportPaths:
        root = Path(root)
        return cls(
            results_dir=root / "results",
            out_dir=Path(out_dir) if out_dir is not None else root / "results",
            manifest=root / "config" / "corpus.csv",
            frb_index=root / "data" / "index" / "frb.csv",
            gold_dir=root / "data" / "gold",
            trace_dir=root / "data" / "traces",
            prices=root / "config" / "prices.yaml",
            matrix_floor_triage=root / "config" / "frb_matrix_floor_triage.yaml",
        )

    def inp(self, name: str) -> Path:
        return self.results_dir / name

    def calibration(self, split: str) -> Path:
        """``calibration_{split}.csv`` of the S1 and S3 pair run (plan, Risk score flag 2:
        ``evaluate --systems rules,<S3 tier> --out-dir results/pair``); ``calibration_dir``
        defaults to ``results_dir / "pair"``."""
        base = self.calibration_dir if self.calibration_dir is not None else self.results_dir / "pair"
        return Path(base) / f"calibration_{split}.csv"

    def out(self, name: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir / name


# ----------------------------------------------------------------------------- formatting


def _isnan(v: Any) -> bool:
    try:
        return v is None or (isinstance(v, (float, np.floating)) and math.isnan(float(v)))
    except (TypeError, ValueError):
        return False


def cell(v: Any, nd: int = 4) -> str:
    """Deterministic CSV cell: empty for missing, ``true``/``false``, integers as such,
    floats with ``nd`` decimals (negative zero printed as zero), strings unchanged."""
    if v is None or _isnan(v):
        return ""
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return f"{float(v) + 0.0:.{nd}f}"
    return str(v)


def pct(v: Any, nd: int = 1) -> str:
    """A proportion as a percent with ``nd`` decimals; empty when missing."""
    if v is None or _isnan(v) or v == "":
        return ""
    return f"{100.0 * float(v) + 0.0:.{nd}f}"


def pct_ci(p: Any, lo: Any, hi: Any) -> str:
    """``93.3 [83.3, 99.4]``; the point alone when the interval is missing."""
    if p is None or _isnan(p) or p == "":
        return ""
    if lo is None or hi is None or _isnan(lo) or _isnan(hi) or lo == "" or hi == "":
        return pct(p)
    return f"{pct(p)} [{pct(lo)}, {pct(hi)}]"


def _int(v: Any) -> int | None:
    if v is None or _isnan(v) or v == "":
        return None
    return int(round(float(v)))


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, Any]], nd: int = 4) -> Path:
    """Write rows with ``cell`` formatting, ``\\n`` line ends and minimal quoting."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([cell(r.get(c), nd) for c in columns])
    path.write_text(buf.getvalue(), encoding="utf-8", newline="")
    return path


def _md_escape(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """GitHub pipe table with escaped cells."""
    lines = ["| " + " | ".join(_md_escape(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        lines.append("| " + " | ".join(_md_escape(c) for c in r) + " |")
    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str) -> Path:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="")
    return path


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([math.nan] * len(df), index=df.index, dtype=float)
    return pd.to_numeric(df[col].replace("", np.nan), errors="coerce")


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes", "1.0"}


def _rate(k: int, n: int) -> tuple[float, float, float]:
    if not n:
        return (math.nan, math.nan, math.nan)
    lo, hi = wilson_ci(int(k), int(n))
    return (k / n, lo, hi)


def _exact(k: int, n: int) -> tuple[float, float, float]:
    if not n:
        return (math.nan, math.nan, math.nan)
    lo, hi = clopper_pearson_ci(int(k), int(n))
    return (k / n, lo, hi)


def _field_order() -> dict[str, int]:
    return {f: i for i, f in enumerate(load_fields()["fields"])}


# ----------------------------------------------------------------------------- manifest


def read_manifest(path: Path) -> pd.DataFrame:
    """The manifest as strings (empty cells stay empty)."""
    return _read_csv(path)


def evaluable(manifest: pd.DataFrame) -> pd.DataFrame:
    """Rows evaluated by any system: a fetched PDF (sha256 present), ``text_extractable``
    true and split not ``excluded`` (analysis plan, Exclusions)."""
    ok = manifest["text_extractable"].map(_truthy)
    if "sha256" in manifest.columns:
        ok &= manifest["sha256"].str.strip() != ""
    if "split" in manifest.columns:
        ok &= manifest["split"] != "excluded"
    return manifest[ok]


# ----------------------------------------------------------------------------- (1) Table 1


TABLE1_COLUMNS = [
    "section", "field", "system", "metric_type", "n_gold", "n",
    "accuracy", "accuracy_lo", "accuracy_hi",
    "precision", "precision_lo", "precision_hi", "recall", "recall_lo", "recall_hi",
    "abstention_rate",
    "fabrication_rate", "fabrication_lo", "fabrication_hi", "fabrication_denominator",
    "grounded_rate",
    "hallucination_rate", "hallucination_lo", "hallucination_hi",
    "error_rate_no_grounding", "error_rate_no_grounding_lo", "error_rate_no_grounding_hi",
    "note",
]
PROBE_COLUMNS = [
    "section", "field", "system", "metric_type", "probe_basis", "n_gold", "n_probe",
    "n_fabrication", "fabrication_rate", "fabrication_lo", "fabrication_hi", "n_probe_abstain",
    "accuracy", "accuracy_lo", "accuracy_hi", "precision", "precision_lo", "precision_hi",
    "recall", "recall_lo", "recall_hi", "recall_overall",
]
APPENDIX_COLUMNS = [
    "section", "field", "system", "n_gold_value", "n_correct", "overall_accuracy",
    "fabricated_absence_rate", "recall_overall", "recall_overall_basis",
]
GROUNDING_COLUMNS = [
    "field", "system", "group", "n_grounding_checked", "grounding_checked",
    "quote_not_found_rate", "quote_out_of_scope_rate", "quote_does_not_support_rate",
    "grounded_rate", "correct_grounded", "correct_misgrounded", "wrong_grounded", "wrong_misgrounded",
]


def read_table1(path: Path) -> pd.DataFrame:
    """``evaluate.metrics_table`` output, ordered by ``fields.yaml`` and then system."""
    df = pd.read_csv(path, keep_default_na=True)
    if df.empty:
        return df
    order = _field_order()
    df["_o"] = df["field"].map(order).fillna(len(order))
    return df.sort_values(["_o", "system"], kind="mergesort").drop(columns="_o").reset_index(drop=True)


def _g(row: Mapping[str, Any], key: str) -> Any:
    v = row.get(key, math.nan)
    return math.nan if v is None else v


def _table1_row(r: Mapping[str, Any]) -> dict[str, Any]:
    group = str(r.get("group", ""))
    system = str(r["system"])
    mt = str(r.get("metric_type", "accuracy"))
    n_correct = _int(_g(r, "n_correct")) or 0
    n_wrong = _int(_g(r, "n_wrong")) or 0
    n_fab = _int(_g(r, "n_fabrication")) or 0
    n_fa = _int(_g(r, "n_fabricated_absence")) or 0
    n_gold = _int(_g(r, "n_gold")) or 0
    n_abstain = _int(_g(r, "n_abstain")) or 0
    out: dict[str, Any] = {
        "section": TABLE1_SECTIONS.get(group, group),
        "field": r["field"],
        "system": system,
        "metric_type": mt,
        "n_gold": n_gold,
        "n": n_correct + n_wrong + n_fa if mt in ("accuracy", "set_pr") else None,
        "accuracy": _g(r, "accuracy"), "accuracy_lo": _g(r, "accuracy_lo"), "accuracy_hi": _g(r, "accuracy_hi"),
        "abstention_rate": _g(r, "abstention_rate"),
    }
    notes = []
    if mt == "set_pr":
        for k in ("precision", "precision_lo", "precision_hi", "recall", "recall_lo", "recall_hi"):
            out[k] = _g(r, k)
        notes.append("precision and recall element-level")
    if group == "A_or_B":
        notes.append("probe cells in table_probes")
    else:
        for k in ("fabrication_rate", "fabrication_lo", "fabrication_hi"):
            out[k] = _g(r, k)
        out["fabrication_denominator"] = _int(_g(r, "fabrication_denominator"))
    if system in NO_GROUNDING_SYSTEMS:
        not_abstained = n_gold - n_abstain
        rate = _rate(n_wrong + n_fab + n_fa, not_abstained)
        out["error_rate_no_grounding"], out["error_rate_no_grounding_lo"], out["error_rate_no_grounding_hi"] = rate
    else:
        out["grounded_rate"] = _g(r, "grounded_rate")
        if _truthy(r.get("grounding_checked", False)):
            out["hallucination_rate"] = _g(r, "hallucination_rate")
            out["hallucination_lo"] = _g(r, "hallucination_lo")
            out["hallucination_hi"] = _g(r, "hallucination_hi")
        else:
            # e.g. the S1-sourced values of S5 (grounding_failure "na"): no grounding component
            not_abstained = n_gold - n_abstain
            rate = _rate(n_wrong + n_fab + n_fa, not_abstained)
            out["error_rate_no_grounding"], out["error_rate_no_grounding_lo"], out["error_rate_no_grounding_hi"] = rate
            notes.append("grounding not checked on every answered item; error rate without grounding shown")
    out["note"] = "; ".join(notes)
    return out


def _probe_cells_from_scored(
    scored: pd.DataFrame, manifest: pd.DataFrame | None, field: str, system: str
) -> tuple[int, int, int] | None:
    """(n_probe, n_fabrication, n_abstain) on the applicability ABS cells of one component
    rating, or ``None`` when the cells cannot be identified."""
    if manifest is None or scored.empty:
        return None
    meta = manifest.set_index("doc_id")[["regulator", "method"]]
    sub = scored[(scored["field"] == field) & (scored["system"] == system)]
    if sub.empty:
        return (0, 0, 0)
    keep = []
    for doc_id in sub["doc_id"]:
        if doc_id not in meta.index:
            return None
        reg, method = meta.loc[doc_id, "regulator"], meta.loc[doc_id, "method"]
        keep.append(applicability(field, str(reg), str(method)) != "value")
    probe = sub[np.asarray(keep, dtype=bool)]
    return (len(probe), int((probe["cell"] == "fabrication").sum()), int((probe["cell"] == "abstention").sum()))


def _group_c_probe_cells(scored: pd.DataFrame, field: str, system: str) -> tuple[int, int, int, int]:
    """(n_documents, n_probe, n_fabrication, n_abstain) of a Group C probe field on the
    documents whose gold finding_present is false (plan, Field groups)."""
    fp = scored[(scored["field"] == "finding_present") & (scored["gold_status"] == "value")]
    negative = set(fp.loc[fp["gold_value"].str.strip().str.lower() == "false", "doc_id"])
    sub = scored[(scored["field"] == field) & (scored["system"] == system) & scored["doc_id"].isin(negative)]
    return (len(negative), len(sub), int((sub["cell"] == "fabrication").sum()),
            int((sub["cell"] == "abstention").sum()))


def _set_recall_overall(scored: pd.DataFrame, field: str, system: str) -> float:
    """TP / (TP + FN + gold elements on abstained items) for a set field (plan, Metrics)."""
    sub = scored[(scored["field"] == field) & (scored["system"] == system)]
    if sub.empty:
        return math.nan
    tp = _num(sub, "tp").fillna(0).sum()
    fn = _num(sub, "fn").fillna(0).sum()
    abst = sub[(sub["cell"] == "abstention") & (sub["gold_status"] == "value")]
    extra = sum(len([e for e in str(v).split("|") if e.strip()]) for v in abst["gold_value"])
    den = tp + fn + extra
    return float(tp / den) if den else math.nan


def table1(split: str = "test", paths: ReportPaths | None = None) -> dict[str, Path]:
    """Table 1, the fabrication-probe table, the appendix table and the grounding table
    of one split from ``results/table1_{split}.csv``.

    Writes ``table1_{split}_report.csv`` and ``table1_{split}.md`` (Group A as smoke test,
    component ratings, Group B), ``table_probes_{split}.csv`` and ``.md`` (Group C rows and
    the component-rating probe cells), ``table1_{split}_appendix.csv`` (overall accuracy
    and overall recall) and ``table_grounding_{split}.csv`` (failure categories and the
    correctness x groundedness cells for the quoting systems). ``scored_{split}.csv`` and
    the manifest, when present, give the exact probe cells and the set-field overall recall.
    """
    paths = paths or ReportPaths()
    src = paths.inp(f"table1_{split}.csv")
    if not src.exists():
        raise FileNotFoundError(f"{src} not found; run evaluate --split {split} first")
    t1 = read_table1(src)
    scored_path = paths.inp(f"scored_{split}.csv")
    scored = _read_csv(scored_path) if scored_path.exists() else pd.DataFrame()
    manifest = read_manifest(paths.manifest) if paths.manifest.exists() else None
    records = [r for r in t1.to_dict("records") if not is_variant(str(r["system"]))]

    main_rows = [_table1_row(r) for r in records if str(r.get("group", "")) in TABLE1_SECTIONS]

    probe_rows: list[dict[str, Any]] = []
    for r in records:
        group, field, system = str(r.get("group", "")), str(r["field"]), str(r["system"])
        if group == "A_or_B":
            exact = _probe_cells_from_scored(scored, manifest, field, system) if not scored.empty else None
            if exact is not None:
                n_probe, n_fab, n_abs = exact
                basis = "applicability ABS cells"
            else:
                n_probe = _int(_g(r, "fabrication_denominator")) or 0
                n_fab = _int(_g(r, "n_fabrication")) or 0
                n_abs = None
                basis = "all gold-null items"
            fr = _rate(n_fab, n_probe)
            probe_rows.append({
                "section": "Component rating probe", "field": field, "system": system,
                "metric_type": r.get("metric_type"), "probe_basis": basis,
                "n_gold": _int(_g(r, "n_gold")), "n_probe": n_probe, "n_fabrication": n_fab,
                "fabrication_rate": fr[0], "fabrication_lo": fr[1], "fabrication_hi": fr[2],
                "n_probe_abstain": n_abs,
            })
        elif group == "C" and field in GROUP_C_PROBE_FIELDS:
            # Plan, Field groups: rating_before, rating_after and laws_cited on every document
            # with finding_present false. The other Group C rows are not reported here: Group C
            # is scored against the census.
            if not scored.empty:
                _n_docs, n_probe, n_fab, n_abs = _group_c_probe_cells(scored, field, system)
                basis = "documents with gold finding_present false"
            else:
                n_probe = _int(_g(r, "fabrication_denominator")) or 0
                n_fab = _int(_g(r, "n_fabrication")) or 0
                n_abs = None
                basis = f"all gold-null items (scored_{split}.csv not available)"
            fr = _rate(n_fab, n_probe)
            probe_rows.append({
                "section": "Group C (fabrication probe)", "field": field, "system": system,
                "metric_type": r.get("metric_type"), "probe_basis": basis,
                "n_gold": _int(_g(r, "n_gold")), "n_probe": n_probe, "n_fabrication": n_fab,
                "fabrication_rate": fr[0], "fabrication_lo": fr[1], "fabrication_hi": fr[2],
                "n_probe_abstain": n_abs,
            })

    appendix_rows: list[dict[str, Any]] = []
    for r in records:
        group, field, system = str(r.get("group", "")), str(r["field"]), str(r["system"])
        if group not in TABLE1_SECTIONS:
            continue
        n_gv = _int(_g(r, "n_gold_value")) or 0
        n_c = _int(_g(r, "n_correct")) or 0
        rec, basis = math.nan, ""
        if r.get("metric_type") == "set_pr":
            if not scored.empty:
                rec, basis = _set_recall_overall(scored, field, system), "scored file, elements"
            else:
                basis = f"scored_{split}.csv not available"
        elif r.get("metric_type") == "binary_pr":
            rec, basis = _g(r, "recall_overall"), "table1"
        appendix_rows.append({
            "section": TABLE1_SECTIONS[group], "field": field, "system": system,
            "n_gold_value": n_gv, "n_correct": n_c, "overall_accuracy": n_c / n_gv if n_gv else math.nan,
            "fabricated_absence_rate": _g(r, "fabricated_absence_rate"),
            "recall_overall": rec, "recall_overall_basis": basis,
        })

    grounding_rows = [
        {k: (_int(_g(r, k)) if k in ("n_grounding_checked", "correct_grounded", "correct_misgrounded",
                                       "wrong_grounded", "wrong_misgrounded")
             else (_truthy(r.get(k)) if k == "grounding_checked" else r.get(k)))
         for k in GROUNDING_COLUMNS}
        for r in records
        if str(r["system"]) not in NO_GROUNDING_SYSTEMS and (_int(_g(r, "n_grounding_checked")) or 0) > 0
    ]

    out = {
        "table1_csv": write_csv(paths.out(f"table1_{split}_report.csv"), TABLE1_COLUMNS, main_rows),
        "table1_md": write_text(paths.out(f"table1_{split}.md"), _table1_md(split, main_rows)),
        "probes_csv": write_csv(paths.out(f"table_probes_{split}.csv"), PROBE_COLUMNS, probe_rows),
        "probes_md": write_text(paths.out(f"table_probes_{split}.md"), _probes_md(split, probe_rows)),
        "appendix_csv": write_csv(paths.out(f"table1_{split}_appendix.csv"), APPENDIX_COLUMNS, appendix_rows),
        "grounding_csv": write_csv(paths.out(f"table_grounding_{split}.csv"), GROUNDING_COLUMNS, grounding_rows),
    }
    return out


def _table1_md(split: str, rows: list[dict[str, Any]]) -> str:
    headers = [
        "Section", "Field", "System", "n", "Accuracy [95% CI]", "Precision [95% CI]", "Recall [95% CI]",
        "Abstention", "Fabrication [95% CI] (denominator)", "Grounded", "Hallucination [95% CI]",
        "Error rate, no grounding component [95% CI]",
    ]
    body = []
    for r in rows:
        fab = pct_ci(r.get("fabrication_rate"), r.get("fabrication_lo"), r.get("fabrication_hi"))
        if r.get("fabrication_denominator") is not None and r.get("section") != TABLE1_SECTIONS["A_or_B"]:
            fab = f"{fab} ({r['fabrication_denominator']})".strip()
        if r.get("section") == TABLE1_SECTIONS["A_or_B"]:
            fab = "see probe table"
        body.append([
            r["section"], r["field"], r["system"], cell(r.get("n")),
            pct_ci(r.get("accuracy"), r.get("accuracy_lo"), r.get("accuracy_hi")),
            pct_ci(r.get("precision"), r.get("precision_lo"), r.get("precision_hi")),
            pct_ci(r.get("recall"), r.get("recall_lo"), r.get("recall_hi")),
            pct(r.get("abstention_rate")), fab, pct(r.get("grounded_rate")),
            pct_ci(r.get("hallucination_rate"), r.get("hallucination_lo"), r.get("hallucination_hi")),
            pct_ci(r.get("error_rate_no_grounding"), r.get("error_rate_no_grounding_lo"), r.get("error_rate_no_grounding_hi")),
        ])
    notes = [
        f"# Table 1 ({split} split)",
        "",
        "Percent; intervals are Wilson 95 percent. n is the accuracy denominator (gold-value items, "
        "abstentions excluded). Group A rows are a smoke test; Group A is evaluated against the agency "
        "indexes on the whole corpus. Precision and recall of the set field are element-level. "
        "Hallucination rate is shown only where grounding was checked on every answered item; systems "
        f"{', '.join(NO_GROUNDING_SYSTEMS)} show the error rate with no grounding component. Group C and the "
        "component-rating fabrication probes are in the probe table.",
        "",
    ]
    return "\n".join(notes) + "\n" + md_table(headers, body)


def _probes_md(split: str, rows: list[dict[str, Any]]) -> str:
    headers = ["Section", "Field", "System", "Probe items", "Fabrication [95% CI]", "Abstained probes", "Basis"]
    body = [
        [r["section"], r["field"], r["system"], cell(r.get("n_probe")),
         pct_ci(r.get("fabrication_rate"), r.get("fabrication_lo"), r.get("fabrication_hi")),
         cell(r.get("n_probe_abstain")), r.get("probe_basis", "")]
        for r in rows
    ]
    head = [
        f"# Fabrication probes ({split} split)",
        "",
        "Percent; Wilson 95 percent intervals. Probe items: the applicability ABS cells of the component "
        "ratings (cd_test_rating on Small documents, lending_test_rating on FDIC and FRB Small documents), "
        "and rating_before, rating_after and laws_cited on every document whose gold finding_present is "
        "false. The other Group C fields are scored against the census, not on test.",
        "",
    ]
    return "\n".join(head) + "\n" + md_table(headers, body)


# ----------------------------------------------------------------------------- ablation, closed book


ABLATION_COLUMNS = [
    "field", "group", "located_system", "whole_system", "n_docs", "n_gold_value",
    "located_n", "located_correct", "located_accuracy", "located_lo", "located_hi", "located_abstain",
    "whole_n", "whole_correct", "whole_accuracy", "whole_lo", "whole_hi", "whole_abstain",
    "n_both_answered", "both_correct", "located_only_correct", "whole_only_correct", "both_not_correct",
]
CLOSED_BOOK_COLUMNS = [
    "field", "system", "n_gold_value", "n_answered", "n_correct", "n_abstain",
    "accuracy_answered", "accuracy_answered_lo", "accuracy_answered_hi",
    "accuracy_overall", "accuracy_overall_lo", "accuracy_overall_hi",
    "majority_class", "majority_class_rate", "note",
]
_ACCURACY_CELLS = ("correct", "wrong", "fabricated_absence")


def ablation(split: str = "test", paths: ReportPaths | None = None) -> dict[str, Path]:
    """Whole-document ablation (plan, Decisions item 3): per field, located vs whole-document
    accuracy of the same tier on the documents the ``:whole`` system ran on (the 20
    hash-sample documents), with the discordant counts.

    Accuracy is over gold-value items, abstentions excluded (as in Table 1). The 2x2 is over
    gold-value items both systems answered: ``both_correct``, ``located_only_correct``,
    ``whole_only_correct`` and ``both_not_correct`` (a fabricated absence is not correct).
    Reads ``scored_{split}.csv``; writes ``table_ablation_{split}.csv`` and ``.md``.
    """
    paths = paths or ReportPaths()
    src = paths.inp(f"scored_{split}.csv")
    if not src.exists():
        raise FileNotFoundError(f"{src} not found; run evaluate --split {split} first")
    scored = _read_csv(src)
    pred_path = paths.inp(f"predictions_{split}.csv")
    predictions = _read_csv(pred_path) if pred_path.exists() else pd.DataFrame(columns=["doc_id", "system"])
    order = _field_order()
    rows: list[dict[str, Any]] = []
    for whole_sys in sorted(s for s in scored["system"].unique() if str(s).endswith(WHOLE_SUFFIX)):
        located_sys = whole_sys[: -len(WHOLE_SUFFIX)]
        # The documents the ablation ran on: every document with a row of the :whole system in
        # the predictions file (abstentions included); scored rows exist for every gold item.
        docs = set(predictions.loc[predictions["system"] == whole_sys, "doc_id"])
        if not docs:
            docs = set(scored.loc[(scored["system"] == whole_sys) & (scored["pred_status"] != "abstain"), "doc_id"])
        part = scored[scored["doc_id"].isin(docs) & (scored["gold_status"] == "value")]
        fields = sorted(set(part["field"]), key=lambda f: (order.get(f, len(order)), f))
        for field in fields:
            f = part[part["field"] == field]
            loc = f[f["system"] == located_sys].set_index("doc_id")["cell"]
            who = f[f["system"] == whole_sys].set_index("doc_id")["cell"]
            row: dict[str, Any] = {
                "field": field, "group": str(f["group"].iloc[0]) if "group" in f.columns and len(f) else "",
                "located_system": located_sys, "whole_system": whole_sys, "n_docs": len(docs),
                "n_gold_value": int(f["doc_id"].nunique()),
            }
            for name, cells in (("located", loc), ("whole", who)):
                k = int((cells == "correct").sum())
                n = int(cells.isin(_ACCURACY_CELLS).sum())
                acc = _rate(k, n)
                row.update({f"{name}_n": n, f"{name}_correct": k, f"{name}_accuracy": acc[0],
                            f"{name}_lo": acc[1], f"{name}_hi": acc[2],
                            f"{name}_abstain": int((cells == "abstention").sum())})
            both = sorted(set(loc.index[loc.isin(_ACCURACY_CELLS)]) & set(who.index[who.isin(_ACCURACY_CELLS)]))
            lc = np.array([loc[d] == "correct" for d in both], dtype=bool)
            wc = np.array([who[d] == "correct" for d in both], dtype=bool)
            row.update(n_both_answered=len(both), both_correct=int((lc & wc).sum()),
                       located_only_correct=int((lc & ~wc).sum()), whole_only_correct=int((~lc & wc).sum()),
                       both_not_correct=int((~lc & ~wc).sum()))
            rows.append(row)
    body = [[r["field"], r["located_system"], cell(r["n_docs"]),
             f"{pct_ci(r['located_accuracy'], r['located_lo'], r['located_hi'])} ({r['located_correct']}/{r['located_n']})",
             f"{pct_ci(r['whole_accuracy'], r['whole_lo'], r['whole_hi'])} ({r['whole_correct']}/{r['whole_n']})",
             cell(r["n_both_answered"]), cell(r["located_only_correct"]), cell(r["whole_only_correct"])]
            for r in rows]
    md = "\n".join([
        f"# Whole-document ablation ({split} split)",
        "",
        "Per field: accuracy of the located system and of its whole-document run on the same documents "
        "(the first 20 test doc_ids by sha256 of the doc_id), over gold-value items with abstentions "
        "excluded, Wilson 95 percent intervals. Discordant items: gold-value items both runs answered where "
        "exactly one is correct.",
        "",
    ]) + "\n" + md_table(
        ["Field", "Located system", "Documents", "Located accuracy", "Whole-document accuracy",
         "Both answered", "Located only correct", "Whole only correct"], body)
    return {
        "csv": write_csv(paths.out(f"table_ablation_{split}.csv"), ABLATION_COLUMNS, rows),
        "md": write_text(paths.out(f"table_ablation_{split}.md"), md),
    }


def closed_book(split: str = "test", paths: ReportPaths | None = None) -> dict[str, Path]:
    """Closed-book contamination baseline (plan, Leakage): accuracy of each ``:closedbook``
    system on overall_rating and finding_present next to the split's majority-class rate,
    reported in Limitations as a contamination bound.

    Two accuracies: among answered items (abstentions excluded, as in Table 1) and overall
    (abstentions as misses, comparable with the majority-class rate, which always answers);
    the plan names accuracy among answered items as the contamination bound and reports the
    overall figure beside it (plan, Leakage and contamination controls). The majority class is
    the most frequent gold value of the field over the split's gold-value items. Reads
    ``scored_{split}.csv``; writes ``table_closed_book_{split}.csv`` and ``.md``.
    """
    paths = paths or ReportPaths()
    src = paths.inp(f"scored_{split}.csv")
    if not src.exists():
        raise FileNotFoundError(f"{src} not found; run evaluate --split {split} first")
    scored = _read_csv(src)
    rows: list[dict[str, Any]] = []
    systems = sorted(s for s in scored["system"].unique() if str(s).endswith(CLOSED_BOOK_SUFFIX))
    for system in systems:
        for field in CLOSED_BOOK_FIELDS:
            g = scored[(scored["system"] == system) & (scored["field"] == field) & (scored["gold_status"] == "value")]
            gold = scored[(scored["field"] == field) & (scored["gold_status"] == "value")].drop_duplicates("doc_id")
            values = gold["gold_value"].str.strip().str.casefold()
            counts = values.value_counts()
            majority = str(counts.index[0]) if len(counts) else ""
            maj_rate = float(counts.iloc[0]) / len(values) if len(values) else math.nan
            k = int((g["cell"] == "correct").sum())
            n_ans = int(g["cell"].isin(_ACCURACY_CELLS).sum())
            ans = _rate(k, n_ans)
            overall = _rate(k, len(g))
            rows.append({
                "field": field, "system": system, "n_gold_value": len(g), "n_answered": n_ans, "n_correct": k,
                "n_abstain": int((g["cell"] == "abstention").sum()),
                "accuracy_answered": ans[0], "accuracy_answered_lo": ans[1], "accuracy_answered_hi": ans[2],
                "accuracy_overall": overall[0], "accuracy_overall_lo": overall[1], "accuracy_overall_hi": overall[2],
                "majority_class": majority, "majority_class_rate": maj_rate,
                "note": "contamination bound (Limitations); the model saw only the bank name and exam date",
            })
    body = [[r["field"], r["system"], cell(r["n_gold_value"]),
             pct_ci(r["accuracy_answered"], r["accuracy_answered_lo"], r["accuracy_answered_hi"]),
             pct_ci(r["accuracy_overall"], r["accuracy_overall_lo"], r["accuracy_overall_hi"]),
             cell(r["n_abstain"]), f"{r['majority_class']} ({pct(r['majority_class_rate'])})"] for r in rows]
    md = "\n".join([
        f"# Closed-book baseline ({split} split)",
        "",
        "The S3 corpus tier received only the bank name and exam date of each document. Accuracy among "
        "answered items and overall (abstentions as misses), Wilson 95 percent intervals, next to the "
        "majority-class rate of the split. Reported in Limitations as a contamination bound.",
        "",
    ]) + "\n" + md_table(["Field", "System", "Gold items", "Accuracy, answered", "Accuracy, overall",
                           "Abstained", "Majority class (rate)"], body)
    return {
        "csv": write_csv(paths.out(f"table_closed_book_{split}.csv"), CLOSED_BOOK_COLUMNS, rows),
        "md": write_text(paths.out(f"table_closed_book_{split}.md"), md),
    }


# ----------------------------------------------------------------------------- review curve


def review_curve(n_flags: Sequence[float], errors: Sequence[float], weights: Sequence[float] | None = None) -> pd.DataFrame:
    """Residual error against the share of values reviewed (plan, "Risk score, calibration
    and review curve").

    Values are reviewed by ``n_flags``, highest first; inside a block of equal flag counts the
    expected residual error under random order falls linearly, so the curve is piecewise
    linear with a knot at each block boundary. ``share_reviewed`` is the cumulative weight
    share and ``residual_error`` the weighted errors among unreviewed values over all values
    (review assumed to correct every reviewed value). ``n_flags`` on a knot is the block just
    completed (empty on the first knot). With design weights the curve is design-weighted.
    """
    flags = np.asarray(n_flags, dtype=float)
    err = np.asarray(errors, dtype=float)
    w = np.ones_like(flags) if weights is None else np.asarray(weights, dtype=float)
    cols = ["share_reviewed", "residual_error", "n_flags"]
    if flags.size == 0 or float(w.sum()) <= 0:
        return pd.DataFrame(columns=cols)
    total_w = float(w.sum())
    y = float((w * err).sum()) / total_w
    x = 0.0
    rows: list[dict[str, Any]] = [{"share_reviewed": 0.0, "residual_error": y, "n_flags": None}]
    for k in sorted(set(flags.tolist()), reverse=True):
        mask = flags == k
        x += float(w[mask].sum()) / total_w
        y -= float((w[mask] * err[mask]).sum()) / total_w
        rows.append({"share_reviewed": min(x, 1.0), "residual_error": max(y, 0.0), "n_flags": int(k)})
    return pd.DataFrame(rows, columns=cols)


def residual_at(curve: pd.DataFrame, share: float) -> float:
    """Residual error at ``share`` reviewed, by linear interpolation between knots."""
    if curve.empty:
        return math.nan
    x = curve["share_reviewed"].astype(float).to_numpy()
    y = curve["residual_error"].astype(float).to_numpy()
    return float(np.interp(share, x, y))


@dataclass(frozen=True)
class Curve:
    """One review curve: its group label (``A`` or ``B``), system and knots."""

    group: str
    system: str
    knots: pd.DataFrame


def load_curve_frame(df: pd.DataFrame, group: str) -> list[Curve]:
    """Review curves from one input file, one per ``system`` value (``""`` without the column).

    Accepted schemas:

    - a precomputed curve: ``share_reviewed``, ``residual_error`` (``audit.review_curve``
      writes this with a ``point`` column; only its ``knot`` rows are read, and the
      operating points are interpolated here the same way);
    - value-level rows: ``n_flags``, ``error`` (0 or 1), optional ``weight`` (design weight);
    - counts per flag level: ``n_flags``, ``n``, ``n_error``; ``corpus_scoring``'s
      ``risk_validation_group_a.csv`` is read this way from its rows with ``by`` =
      ``n_flags`` (``level`` is the flag count) and ``truth`` = ``raw`` (the raw index is the
      primary truth, plan "Group A at corpus scale").
    """
    df = df.copy()
    if "point" in df.columns:
        df = df[df["point"] == "knot"]
    if "n_flags_block" in df.columns and "n_flags" not in df.columns:
        df = df.rename(columns={"n_flags_block": "n_flags"})
    if "by" in df.columns:
        df = df[df["by"] == "n_flags"]
        if "truth" in df.columns:
            df = df[df["truth"] == "raw"]
        if "level" in df.columns and "n_flags" not in df.columns:
            df = df.rename(columns={"level": "n_flags"})
    cols = set(df.columns)
    systems = sorted(df["system"].astype(str).unique()) if "system" in cols else [""]
    curves: list[Curve] = []
    for system in systems:
        part = df[df["system"].astype(str) == system] if "system" in cols else df
        if {"share_reviewed", "residual_error"} <= cols:
            knots = pd.DataFrame({
                "share_reviewed": _num(part, "share_reviewed"),
                "residual_error": _num(part, "residual_error"),
                "n_flags": _num(part, "n_flags") if "n_flags" in cols else math.nan,
            }).sort_values("share_reviewed", kind="mergesort").reset_index(drop=True)
        elif {"n_flags", "error"} <= cols:
            weights = _num(part, "weight").fillna(1.0) if "weight" in cols else None
            knots = review_curve(_num(part, "n_flags"), _num(part, "error"), weights)
        elif {"n_flags", "n", "n_error"} <= cols:
            n = _num(part, "n").fillna(0).to_numpy(dtype=float)
            ne = _num(part, "n_error").fillna(0).to_numpy(dtype=float)
            knots = review_curve(_num(part, "n_flags").to_numpy(), np.divide(ne, n, out=np.zeros_like(n), where=n > 0), n)
        else:
            raise ValueError(
                f"group {group} review-curve input has columns {sorted(cols)}; expected "
                "(share_reviewed, residual_error), (n_flags, error[, weight]) or (n_flags, n, n_error)"
            )
        curves.append(Curve(group, system, knots))
    return curves


def load_curves(paths: ReportPaths) -> tuple[list[Curve], list[str]]:
    """Group B and Group A curves from the files that exist, plus the missing file names."""
    curves: list[Curve] = []
    missing: list[str] = []
    for group, name in (("B", "review_curve_group_b.csv"), ("A", "risk_validation_group_a.csv")):
        p = paths.inp(name)
        if p.exists():
            curves += load_curve_frame(_read_csv(p), group)
        else:
            missing.append(name)
    return curves, missing


def manual_review_error(paths: ReportPaths) -> float | None:
    """One minus pooled agreement on the Group B fields from ``results/reliability.csv``
    (columns ``field``, ``n`` and ``n_agree``, or ``field``, ``n`` and ``agreement``);
    ``None`` when the file is missing or holds no Group B row."""
    p = paths.inp("reliability.csv")
    if not p.exists():
        return None
    df = _read_csv(p)
    df = df[df["field"].isin(field_names("B"))]
    n = _num(df, "n").fillna(0)
    agree = _num(df, "n_agree") if "n_agree" in df.columns else _num(df, "agreement") * n
    total = float(n.sum())
    return None if total <= 0 else 1.0 - float(agree.fillna(0).sum()) / total


# ----------------------------------------------------------------------------- (2) Figure 1


def figure1(split: str = "test", paths: ReportPaths | None = None) -> dict[str, Path]:
    """Figure 1: review curves, operating points, the manual-review error line and the
    calibration inset (``calibration_{split}.csv``). Writes ``figure1.png``, ``figure1.svg``
    (metadata stripped), ``figure1_data.csv`` (the plotted points) and
    ``figure1_caption.txt`` (what was drawn and which inputs are missing)."""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    paths = paths or ReportPaths()
    curves, missing = load_curves(paths)
    manual = manual_review_error(paths)
    if manual is None:
        missing.append("reliability.csv")
    calib_path = paths.calibration(split)
    calib = _read_csv(calib_path) if calib_path.exists() else None
    if calib is None:
        missing.append(f"{calib_path.parent.name}/{calib_path.name}")
    pair = sorted(calib["system"].astype(str).unique()) if calib is not None and "system" in calib.columns else []

    data_rows: list[dict[str, Any]] = []
    drawn: list[str] = []
    with matplotlib.rc_context(FIGURE_RC):
        fig = Figure(figsize=(7.0, 4.8), facecolor=SURFACE)
        FigureCanvasAgg(fig)
        ax = fig.add_axes((0.09, 0.22, 0.88, 0.70), facecolor=SURFACE)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        ax.set_xlim(0, 100)
        ax.set_xlabel("Share of values reviewed (percent)")
        ax.set_ylabel("Residual error (percent of all values)")
        ax.set_title("Figure 1. Residual error by share of values reviewed", loc="left", color=INK, fontsize=10)
        handles = []
        for i, c in enumerate(sorted(curves, key=lambda c: (c.group != "B", c.group, c.system))):
            color = SERIES_COLORS[i % len(SERIES_COLORS)]
            label = ("Group B, audit (design-weighted)" if c.group == "B" else "Group A, corpus index (exact)")
            if c.system:
                label += f", {c.system}"
            x = c.knots["share_reviewed"].astype(float) * 100
            y = c.knots["residual_error"].astype(float) * 100
            (h,) = ax.plot(x, y, color=color, linewidth=2.0, label=label, solid_capstyle="round")
            handles.append(h)
            ops = [(s, residual_at(c.knots, s)) for s in OPERATING_SHARES]
            ax.plot([s * 100 for s, _ in ops], [v * 100 for _, v in ops], linestyle="none", marker="o",
                    markersize=5, color=color, markeredgecolor=SURFACE, markeredgewidth=1.5)
            drawn.append(label)
            for k in c.knots.itertuples(index=False):
                data_rows.append({"series": f"review_curve_group_{c.group}", "system": c.system,
                                  "x": float(k.share_reviewed), "y": float(k.residual_error), "kind": "knot"})
            for s, v in ops:
                data_rows.append({"series": f"review_curve_group_{c.group}", "system": c.system,
                                  "x": s, "y": v, "kind": "operating_point"})
        if manual is not None:
            h = ax.axhline(manual * 100, color=REFERENCE_GRAY, linewidth=1.2, linestyle=(0, (4, 3)),
                           label="Manual-review error (reliability check)")
            handles.append(h)
            drawn.append("manual-review error line")
            data_rows.append({"series": "manual_review_error", "system": "", "x": math.nan, "y": manual, "kind": "reference"})
        if not curves and manual is None:
            ax.set_ylim(0, 10)
            ax.text(50, 5, "No review-curve input available", ha="center", va="center", color=MUTED)
        else:
            ax.set_ylim(bottom=0)

        if calib is not None and not calib.empty:
            inset = ax.inset_axes((0.62, 0.50, 0.35, 0.42), facecolor=SURFACE)
            inset.plot([0, 1], [0, 1], color=REFERENCE_GRAY, linewidth=1.0, linestyle=(0, (4, 3)))
            for j, system in enumerate(sorted(calib["system"].astype(str).unique())):
                part = calib[calib["system"].astype(str) == system]
                px = _num(part, "n_flags") / 4.0
                py = _num(part, "error_rate")
                inset.plot(px, py, linestyle="none", marker="o", markersize=4,
                           color=SERIES_COLORS[(len(curves) + j) % len(SERIES_COLORS)], label=system)
                for a, b, n in zip(px, py, _num(part, "n"), strict=True):
                    data_rows.append({"series": f"calibration_{split}", "system": system, "x": float(a), "y": float(b),
                                      "kind": f"n={_int(n)}"})
            inset.set_xlim(-0.05, 1.05)
            inset.set_ylim(-0.05, 1.05)
            inset.set_title("Calibration (indicative)", fontsize=7.5, color=INK)
            inset.set_xlabel("Flags / 4", fontsize=7)
            inset.set_ylabel("Observed error", fontsize=7)
            inset.tick_params(labelsize=6.5)
            inset.legend(fontsize=6, frameon=False, loc="upper left")
            drawn.append(f"calibration inset ({split} split)")
        if handles:
            fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.08, 0.0), ncol=2,
                       frameon=False, fontsize=7.5)

        png = paths.out("figure1.png")
        svg = paths.out("figure1.svg")
        fig.savefig(png, dpi=150, facecolor=SURFACE, metadata={"Software": None})
        fig.savefig(svg, facecolor=SURFACE, metadata={"Date": None, "Creator": None})

    data_path = write_csv(paths.out("figure1_data.csv"), ["series", "system", "x", "y", "kind"], data_rows, nd=6)
    caption = [
        "Figure 1. Review curve: residual error (errors among unreviewed values divided by all values) "
        "against the share of values reviewed, values ordered by risk-flag count, highest first, with the "
        "expected residual error under random order inside blocks of equal flag count. Dots mark the "
        "operating points at 10, 25 and 50 percent of values reviewed. Group B curve from the audit with "
        "design weights; Group A curve exact against the agency indexes corpus-wide. Dashed line: "
        "manual-review error rate from the reliability check (one minus agreement on Group B fields). "
        f"Inset: error rate by flag count on the {split} split, probability = flags / 4, indicative only; "
        "flags from the pair run of evaluate (disagreement restricted to the pair"
        + (f": {', '.join(pair)}" if pair else "") + ").",
        "Drawn: " + ("; ".join(drawn) if drawn else "nothing (no input available)") + ".",
        "Missing inputs: " + (", ".join(sorted(missing)) if missing else "none") + ".",
    ]
    cap_path = write_text(paths.out("figure1_caption.txt"), "\n".join(caption))
    return {"png": png, "svg": svg, "data": data_path, "caption": cap_path}


# ----------------------------------------------------------------------------- (3) Table 2


TABLE2_COLUMNS = ["source", "field", "n_affected", "example_doc", "root_cause", "recommendation", "found_on"]


def table2(paths: ReportPaths | None = None) -> dict[str, Path]:
    """Data-quality table from ``results/data_quality_findings.csv``, sorted by root cause,
    then source (ties by field and example document). Writes ``table2.md`` and ``table2.csv``."""
    paths = paths or ReportPaths()
    src = paths.inp("data_quality_findings.csv")
    if not src.exists():
        raise FileNotFoundError(f"{src} not found")
    df = _read_csv(src)
    cols = [c for c in TABLE2_COLUMNS if c in df.columns] + [c for c in df.columns if c not in TABLE2_COLUMNS]
    df = df.sort_values(["root_cause", "source", "field", "example_doc"], kind="mergesort").reset_index(drop=True)
    rows = df.to_dict("records")
    head = [
        "# Table 2. Data-quality findings",
        "",
        "Rows sorted alphabetically by root cause, then by source. Exploratory "
        "(analysis plan, Purpose and scope); a row reaches the README only after verification against "
        "at least three PEs.",
        "",
    ]
    md = "\n".join(head) + "\n" + md_table(cols, ([r[c] for c in cols] for r in rows))
    return {
        "csv": write_csv(paths.out("table2.csv"), cols, rows),
        "md": write_text(paths.out("table2.md"), md),
    }


# ----------------------------------------------------------------------------- (4) Table 3


TABLE3_COLUMNS = [
    "system", "n_docs", "n_calls", "n_calls_whole_document", "n_calls_mode_unknown", "locator_fallback_rate",
    "input_tokens_per_call_located", "input_tokens_per_call_whole_document",
    "input_tokens_per_doc", "output_tokens_per_doc",
    "price_status", "prices_retrieved", "pricing_mode",
    "cost_spent_usd", "cost_per_doc_usd", "n_correct", "cost_per_correct_field_usd",
    "corpus_cost_usd", "corpus_cost_basis", "wall_clock_s", "hardware", "minutes_per_doc",
]
REVIEW_COLUMNS = [
    "share_reviewed", "n_values", "n_values_reviewed", "residual_error", "reviewer_minutes_per_value",
    "review_cost_usd", "extraction_cost_usd", "total_cost_usd", "note",
]
MANUAL_COLUMNS = ["field", "source", "n_rows_timed", "mean_minutes"]
WAGE_NOT_SET = "wage not set"


def read_traces_dir(directory: Path) -> list[Trace]:
    """Every trace of a split directory (``*.jsonl`` and ``*.jsonl.gz``), files in name order."""
    if not directory.exists():
        return []
    out: list[Trace] = []
    for p in sorted(directory.iterdir()):
        if p.name.endswith(".jsonl"):
            text = p.read_text(encoding="utf-8")
        elif p.name.endswith(".jsonl.gz"):
            text = gzip.decompress(p.read_bytes()).decode("utf-8")
        else:
            continue
        out += [Trace.model_validate_json(line) for line in text.splitlines() if line.strip()]
    return out


def trace_mode(t: Trace) -> str:
    """Run mode of a trace: ``Trace.mode`` when recorded, ``closedbook`` for the closed-book
    field group, else ``located`` (a trace written before ``Trace.mode`` existed cannot be
    told apart from a located call)."""
    if t.mode:
        return str(t.mode)
    return "closedbook" if t.field_group == "closedbook" else "located"


def trace_system(t: Trace) -> str:
    """System name a trace belongs to (``run.parse_systems`` naming, with the ``:whole`` or
    ``:closedbook`` suffix of the run mode)."""
    base = f"llm:{t.backend}:{t.model}"
    mode = trace_mode(t)
    return base + (f":{mode}" if mode in ("whole", "closedbook") else "")


def latest_traces(traces: list[Trace]) -> list[Trace]:
    """The last trace per (doc_id, field_group, backend, model, mode, effort), in file order:
    the call whose output the current predictions reflect. Earlier calls still count as
    spent. The mode keeps an ablation call from replacing the located call of the same
    document and group."""
    last: dict[tuple[str, str, str, str, str, str], Trace] = {}
    for t in traces:
        last[(t.doc_id, t.field_group, t.backend, t.model, trace_mode(t), str(t.effort or ""))] = t
    return [last[k] for k in sorted(last)]


def read_run_log_latest(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Last run-log entry per (doc_id, system)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            e = json.loads(line)
            out[(str(e.get("doc_id")), str(e.get("system")))] = e
    return out


def price_lookup(prices: Mapping[str, Any], model: str) -> tuple[dict[str, Any] | None, str | None]:
    """Price entry and provider retrieval date for ``model`` (exact key or ``provider/model``)."""
    for provider in prices.values():
        if not isinstance(provider, dict):
            continue
        for key, entry in (provider.get("models") or {}).items():
            if key == model or str(key).endswith("/" + model):
                retrieved = provider.get("retrieved")
                return dict(entry or {}), (None if retrieved is None else str(retrieved))
    return None, None


def _price_status(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "not in prices.yaml"
    if entry.get("input") is None or entry.get("output") is None:
        return "price not set"
    if float(entry["input"]) == 0.0 and float(entry["output"]) == 0.0:
        return "local, priced 0"
    return "priced"


def _list_cost(entry: dict[str, Any] | None, t: Trace) -> float:
    if _price_status(entry) not in ("priced", "local, priced 0"):
        return math.nan
    assert entry is not None
    return (t.input_tokens or 0) * float(entry["input"]) / 1e6 + (t.output_tokens or 0) * float(entry["output"]) / 1e6


def _call_modes(predictions: pd.DataFrame) -> dict[tuple[str, str, str], bool]:
    """(doc_id, field_group, system) -> whole-document input, from ``locator_miss``."""
    if predictions.empty or "locator_miss" not in predictions.columns:
        return {}
    group_of = {f: g for g, spec in load_fields()["llm_field_groups"].items() for f in spec["fields"]}
    out: dict[tuple[str, str, str], bool] = {}
    for doc_id, field, system, miss in zip(
        predictions["doc_id"], predictions["field"], predictions["system"], predictions["locator_miss"], strict=True
    ):
        g = group_of.get(field)
        if g is None:
            continue
        key = (doc_id, g, system)
        out[key] = out.get(key, False) or _truthy(miss)
    return out


def manual_minutes(gold_dir: Path, splits: Sequence[str] = ("dev", "test")) -> tuple[pd.DataFrame, list[str]]:
    """Timed label rows of the gold sheets (``labels_{split}.csv``): doc_id, field, minutes."""
    frames, used = [], []
    for split in splits:
        p = gold_dir / f"labels_{split}.csv"
        if not p.exists():
            continue
        df = _read_csv(p)
        df = df.assign(minutes=_num(df, "minutes"))
        frames.append(df[["doc_id", "field", "minutes"]].assign(split=split))
        used.append(p.name)
    if not frames:
        return pd.DataFrame(columns=["doc_id", "field", "minutes", "split"]), used
    return pd.concat(frames, ignore_index=True), used


def audit_blind_minutes(gold_dir: Path) -> pd.DataFrame:
    """Timed rows of the blind-mode audit sheet (``data/gold/audit_sheet.csv``, ``mode``
    blind): doc_id, field, minutes, split ``audit_blind``. Blind rows are re-extracted from
    the PDF with no model output, so they are manual-baseline timers (plan, Cost accounting)."""
    p = gold_dir / "audit_sheet.csv"
    cols = ["doc_id", "field", "minutes", "split"]
    if not p.exists():
        return pd.DataFrame(columns=cols)
    df = _read_csv(p)
    if "mode" not in df.columns:
        return pd.DataFrame(columns=cols)
    df = df[df["mode"] == "blind"]
    return df.assign(minutes=_num(df, "minutes"), split="audit_blind")[cols]


def reviewer_minutes(paths: ReportPaths) -> tuple[float | None, str | None]:
    """Reviewer minutes per value from ``results/audit_summary.csv`` (``audit``): the
    ``minutes_per_value`` row of the ``shown`` mode, where the reviewer checks the released
    value as in a review, else the ``all`` row. ``(None, None)`` without the file."""
    p = paths.inp("audit_summary.csv")
    if not p.exists():
        return None, None
    df = _read_csv(p)
    df = df[df["quantity"] == "minutes_per_value"]
    for mode in ("shown", "all"):
        est = _num(df[df["stratum"] == mode], "estimate").dropna()
        if len(est):
            return float(est.iloc[0]), mode
    return None, None


def released_group_b_values(paths: ReportPaths, n_evaluable: int) -> tuple[int, str]:
    """Number of values the review operating points apply to, on the base of the review
    curve: answered Group B values of the released system in ``risk_values_corpus.csv``
    (the curve's x is a share of answered values). Without that file, evaluable documents
    x 11 Group B fields, which counts abstentions too (stated in the note)."""
    p = paths.inp("risk_values_corpus.csv")
    if p.exists():
        df = _read_csv(p)
        if {"group", "answered"} <= set(df.columns):
            b = df[df["group"] == "B"]
            return int(b["answered"].map(_truthy).sum()), "n_values: answered Group B values (risk_values_corpus.csv)"
    n_b = len(field_names("B"))
    return n_evaluable * n_b, f"n_values: evaluable documents x {n_b} Group B fields (abstentions included)"


def table3(split: str = "test", paths: ReportPaths | None = None, corpus_system: str | None = None) -> dict[str, Path]:
    """Cost table (plan, "Cost accounting"; design 6.9).

    Per system: tokens per call under located and whole-document input (call mode from the
    ``locator_miss`` column of ``predictions_{split}.csv``), tokens per document, the
    locator fallback rate (whole-document calls over calls), the cost actually recorded in
    the traces (every call, including superseded ones), the cost per document at the
    ``prices.yaml`` list price over the latest call per (document, field group), cost per
    correct field (``scored_{split}.csv``), the corpus cost (spent from
    ``data/traces/corpus`` when the system ran there, else cost per document x evaluable
    documents), wall clock from ``run_log_{split}.jsonl`` and the hardware of local models.
    A manual-baseline row comes from the gold label timers at the ``prices.yaml`` wage
    ("wage not set" while it is null). The review table gives the three operating points
    with the Group B review curve's residual error, reviewer minutes per value from the
    audit timers (:func:`reviewer_minutes`) and the extraction cost of ``corpus_system``.
    """
    paths = paths or ReportPaths()
    prices = yaml.safe_load(paths.prices.read_text(encoding="utf-8")) if paths.prices.exists() else {}
    prices = prices or {}
    wage = ((prices.get("manual_baseline") or {}).get("wage_usd_per_hour"))
    manifest = read_manifest(paths.manifest) if paths.manifest.exists() else None
    n_evaluable = len(evaluable(manifest)) if manifest is not None else 0

    traces = read_traces_dir(paths.trace_dir / split)
    corpus_traces = read_traces_dir(paths.trace_dir / "corpus") if split != "corpus" else traces
    run_log = read_run_log_latest(paths.inp(f"run_log_{split}.jsonl"))
    pred_path = paths.inp(f"predictions_{split}.csv")
    predictions = _read_csv(pred_path) if pred_path.exists() else pd.DataFrame()
    modes = _call_modes(predictions)
    scored_path = paths.inp(f"scored_{split}.csv")
    scored = _read_csv(scored_path) if scored_path.exists() else pd.DataFrame()

    systems = sorted({s for _, s in run_log} | {trace_system(t) for t in traces})
    latest = latest_traces(traces)
    rows: list[dict[str, Any]] = []
    for system in systems:
        sys_latest = [t for t in latest if trace_system(t) == system]
        sys_all = [t for t in traces if trace_system(t) == system]
        log_entries = {d: e for (d, s), e in run_log.items() if s == system}
        docs = sorted({t.doc_id for t in sys_latest} | {d for d, e in log_entries.items() if not e.get("error")})
        model = sys_latest[0].model if sys_latest else (system.split(":", 2)[2] if system.startswith("llm:") else None)
        entry, retrieved = price_lookup(prices, model) if model else (None, None)
        is_llm = system.startswith("llm:")
        row: dict[str, Any] = {"system": system, "n_docs": len(docs)}
        if is_llm:
            if system.endswith(WHOLE_SUFFIX):
                # Ablation calls send the whole document by design.
                whole, located = list(sys_latest), []
            else:
                whole = [t for t in sys_latest if modes.get((t.doc_id, t.field_group, system)) is True]
                located = [t for t in sys_latest if modes.get((t.doc_id, t.field_group, system)) is False]
            unknown = len(sys_latest) - len(whole) - len(located)
            per_doc_in: dict[str, int] = {}
            per_doc_out: dict[str, int] = {}
            per_doc_cost: dict[str, float] = {}
            for t in sys_latest:
                per_doc_in[t.doc_id] = per_doc_in.get(t.doc_id, 0) + (t.input_tokens or 0)
                per_doc_out[t.doc_id] = per_doc_out.get(t.doc_id, 0) + (t.output_tokens or 0)
                per_doc_cost[t.doc_id] = per_doc_cost.get(t.doc_id, 0.0) + _list_cost(entry, t)
            n_calls = len(sys_latest)
            cost_total = float(sum(per_doc_cost.values())) if per_doc_cost else math.nan
            cost_per_doc = cost_total / len(per_doc_cost) if per_doc_cost else math.nan
            row.update(
                n_calls=n_calls, n_calls_whole_document=len(whole), n_calls_mode_unknown=unknown,
                locator_fallback_rate=(len(whole) / (len(whole) + len(located))) if (whole or located) else math.nan,
                input_tokens_per_call_located=float(np.mean([t.input_tokens or 0 for t in located])) if located else math.nan,
                input_tokens_per_call_whole_document=float(np.mean([t.input_tokens or 0 for t in whole])) if whole else math.nan,
                input_tokens_per_doc=float(np.mean(list(per_doc_in.values()))) if per_doc_in else math.nan,
                output_tokens_per_doc=float(np.mean(list(per_doc_out.values()))) if per_doc_out else math.nan,
                price_status=_price_status(entry), prices_retrieved=retrieved, pricing_mode="standard",
                cost_spent_usd=float(sum(t.cost_usd or 0.0 for t in sys_all)),
                cost_per_doc_usd=cost_per_doc,
            )
            corpus_sys = [t for t in corpus_traces if trace_system(t) == system]
            if split != "corpus" and corpus_sys:
                row["corpus_cost_usd"] = float(sum(t.cost_usd or 0.0 for t in corpus_sys))
                row["corpus_cost_basis"] = "spent (corpus traces)"
            elif split == "corpus":
                row["corpus_cost_usd"] = row["cost_spent_usd"]
                row["corpus_cost_basis"] = "spent (corpus traces)"
            elif not math.isnan(cost_per_doc):
                row["corpus_cost_usd"] = cost_per_doc * n_evaluable
                row["corpus_cost_basis"] = f"extrapolated ({split} mean x {n_evaluable} evaluable)"
            else:
                row["corpus_cost_basis"] = row["price_status"]
        else:
            row.update(price_status="no model calls", cost_spent_usd=0.0, cost_per_doc_usd=0.0,
                       corpus_cost_usd=0.0, corpus_cost_basis="no model calls")
            cost_total = 0.0
        n_correct = int((scored[(scored["system"] == system)]["cell"] == "correct").sum()) if not scored.empty else None
        row["n_correct"] = n_correct
        if n_correct and not math.isnan(cost_total):
            row["cost_per_correct_field_usd"] = cost_total / n_correct
        secs = [float(e.get("seconds") or 0.0) for e in log_entries.values()]
        row["wall_clock_s"] = float(sum(secs)) if secs else math.nan
        row["hardware"] = (entry or {}).get("hardware")
        rows.append(row)

    minutes, sheets = manual_minutes(paths.gold_dir)
    timed = minutes[minutes["minutes"].notna()]
    audit_blind = audit_blind_minutes(paths.gold_dir)
    audit_timed = audit_blind[audit_blind["minutes"].notna()]
    if not audit_timed.empty:
        sheets = [*sheets, "audit_sheet.csv (blind mode, per field only)"]
    per_doc = timed.groupby("doc_id")["minutes"].sum() if not timed.empty else pd.Series(dtype=float)
    manual_row: dict[str, Any] = {"system": "manual (gold label timers)", "n_docs": len(per_doc)}
    if len(per_doc):
        mean_min = float(per_doc.mean())
        manual_row["minutes_per_doc"] = mean_min
        if wage is None:
            manual_row["price_status"] = WAGE_NOT_SET
            manual_row["cost_per_doc_usd"] = WAGE_NOT_SET
            manual_row["corpus_cost_usd"] = WAGE_NOT_SET
        else:
            manual_row["price_status"] = f"wage {float(wage):.2f} USD per hour"
            manual_row["cost_per_doc_usd"] = mean_min / 60.0 * float(wage)
            manual_row["corpus_cost_usd"] = mean_min / 60.0 * float(wage) * n_evaluable
            manual_row["corpus_cost_basis"] = f"extrapolated (mean minutes x {n_evaluable} evaluable)"
    else:
        manual_row["price_status"] = "no timed label rows" if wage is not None else WAGE_NOT_SET
    rows.append(manual_row)

    order = _field_order()
    manual_fields = []
    for source, frame in (("gold sheets (dev, test)", timed), ("audit sheet, blind mode", audit_timed)):
        if frame.empty:
            continue
        for field, g in sorted(frame.groupby("field"), key=lambda kv: (order.get(kv[0], len(order)), kv[0])):
            manual_fields.append({"field": field, "source": source, "n_rows_timed": len(g),
                                  "mean_minutes": float(g["minutes"].mean())})

    # Review operating points.
    curves, _missing = load_curves(paths)
    b_curves = [c for c in curves if c.group == "B"]
    curve = None
    if len(b_curves) == 1:
        curve = b_curves[0]
    elif corpus_system:
        curve = next((c for c in b_curves if c.system == corpus_system), None)
    per_value, per_value_mode = reviewer_minutes(paths)
    n_values, n_values_basis = released_group_b_values(paths, n_evaluable)
    corpus_cost: Any = None
    if corpus_system:
        match = next((r for r in rows if r["system"] == corpus_system), None)
        corpus_cost = None if match is None else match.get("corpus_cost_usd")
    review_rows = []
    for share in OPERATING_SHARES:
        notes = []
        reviewed = int(round(share * n_values))
        r: dict[str, Any] = {
            "share_reviewed": share, "n_values": n_values, "n_values_reviewed": reviewed,
            "residual_error": residual_at(curve.knots, share) if curve is not None else math.nan,
            "reviewer_minutes_per_value": per_value,
        }
        notes.append(n_values_basis)
        if curve is None:
            notes.append("Group B review curve not available")
        if per_value is None:
            notes.append("audit timers not available")
            r["review_cost_usd"] = None
        else:
            notes.append(f"reviewer minutes: audit mode {per_value_mode}")
            if wage is None:
                r["review_cost_usd"] = WAGE_NOT_SET
            else:
                r["review_cost_usd"] = reviewed * per_value / 60.0 * float(wage)
        if not corpus_system:
            notes.append("corpus tier not chosen")
        r["extraction_cost_usd"] = corpus_cost if isinstance(corpus_cost, float) else None
        if isinstance(r["review_cost_usd"], float) and isinstance(r["extraction_cost_usd"], float):
            r["total_cost_usd"] = r["review_cost_usd"] + r["extraction_cost_usd"]
        elif r["review_cost_usd"] == WAGE_NOT_SET:
            r["total_cost_usd"] = WAGE_NOT_SET
        r["note"] = "; ".join(notes)
        review_rows.append(r)

    md = _table3_md(split, rows, review_rows, manual_fields, sheets, corpus_system, n_evaluable, wage)
    return {
        "csv": write_csv(paths.out(f"table3_{split}.csv"), TABLE3_COLUMNS, rows),
        "review_csv": write_csv(paths.out(f"table3_{split}_review.csv"), REVIEW_COLUMNS, review_rows),
        "manual_csv": write_csv(paths.out(f"table3_{split}_manual.csv"), MANUAL_COLUMNS, manual_fields),
        "md": write_text(paths.out(f"table3_{split}.md"), md),
    }


def _usd(v: Any, nd: int = 4) -> str:
    if isinstance(v, str):
        return v
    return "" if v is None or _isnan(v) else f"{float(v) + 0.0:.{nd}f}"


def _num_str(v: Any, nd: int = 0) -> str:
    return "" if v is None or _isnan(v) else f"{float(v) + 0.0:.{nd}f}"


def _table3_md(
    split: str, rows: list[dict[str, Any]], review: list[dict[str, Any]], manual_fields: list[dict[str, Any]],
    sheets: list[str], corpus_system: str | None, n_evaluable: int, wage: Any,
) -> str:
    headers = [
        "System", "Documents", "Calls", "Locator fallback", "Input tokens per call, located",
        "Input tokens per call, whole document", "Tokens per document (in / out)", "Price", "Cost spent (USD)",
        "Cost per document (USD)", "Cost per correct field (USD)", "Corpus cost (USD)", "Wall clock (s)",
        "Minutes per document",
    ]
    body = []
    for r in rows:
        tok = ""
        if not _isnan(r.get("input_tokens_per_doc", math.nan)):
            tok = f"{_num_str(r.get('input_tokens_per_doc'))} / {_num_str(r.get('output_tokens_per_doc'))}"
        corpus = _usd(r.get("corpus_cost_usd"), 2)
        if r.get("corpus_cost_basis") and corpus:
            corpus = f"{corpus} ({r['corpus_cost_basis']})"
        body.append([
            r["system"], cell(r.get("n_docs")), cell(r.get("n_calls")), pct(r.get("locator_fallback_rate")),
            _num_str(r.get("input_tokens_per_call_located")), _num_str(r.get("input_tokens_per_call_whole_document")),
            tok, r.get("price_status", ""), _usd(r.get("cost_spent_usd")), _usd(r.get("cost_per_doc_usd")),
            _usd(r.get("cost_per_correct_field_usd")), corpus, _num_str(r.get("wall_clock_s"), 1),
            _num_str(r.get("minutes_per_doc"), 1),
        ])
    rev_headers = ["Share reviewed", "Values reviewed", "Residual error", "Reviewer minutes per value",
                   "Review cost (USD)", "Extraction cost (USD)", "Total (USD)", "Note"]
    rev_body = [[
        pct(r["share_reviewed"], 0), f"{r['n_values_reviewed']} of {r['n_values']}", pct(r.get("residual_error")),
        _num_str(r.get("reviewer_minutes_per_value"), 2), _usd(r.get("review_cost_usd"), 2),
        _usd(r.get("extraction_cost_usd"), 2), _usd(r.get("total_cost_usd"), 2), r.get("note", ""),
    ] for r in review]
    man_body = [[r["field"], r.get("source", ""), cell(r["n_rows_timed"]), _num_str(r["mean_minutes"], 2)]
                for r in manual_fields]
    wage_txt = WAGE_NOT_SET if wage is None else f"{float(wage):.2f} USD per hour"
    parts = [
        f"# Table 3. Cost ({split} split)",
        "",
        "Tokens are the usage fields of the latest call per document and field group; cost per document is "
        "those tokens at the prices in config/prices.yaml (empty while a price is not set); cost spent is the "
        "sum recorded in every trace, superseded calls included. Locator fallback = whole-document calls over "
        "calls with a known input mode. Pricing mode: standard (no Batch API path). Manual baseline: gold "
        f"label timers ({', '.join(sheets) if sheets else 'no sheet found'}); wage: {wage_txt}. Corpus: "
        f"{n_evaluable} evaluable documents.",
        "",
        md_table(headers, body),
        f"## Review operating points (corpus tier: {corpus_system or 'not chosen'})",
        "",
        "Values on the base of the review curve (answered Group B values of the released system when "
        "results/risk_values_corpus.csv exists, see the note); residual error from the Group B review curve; "
        "reviewer minutes per value from the audit timers (shown mode, else all modes).",
        "",
        md_table(rev_headers, rev_body),
        "## Manual minutes per field",
        "",
        md_table(["Field", "Source", "Timed rows", "Mean minutes"], man_body),
    ]
    return "\n".join(parts)


# ----------------------------------------------------------------------------- (5) descriptive


CENSUS_COLUMNS = [
    "doc_id", "finding_type", "rating_effect", "rating_lowered", "rating_lowered_level", "rating_before",
    "rating_after", "laws_cited", "practice_basis", "finding_source",
]
REGISTRY_COLUMNS = [
    "doc_id", "bank_name", "regulator", "bank_id", "method", "exam_date", "release_date", "overall_rating",
    *CENSUS_COLUMNS[1:],
]
DECOMP_COLUMNS = [
    "regulator", "method", "n_evaluable", "n_ni", "n_sn", "n_low", "n_compliance_driven",
    "n_performance_driven", "share_compliance_driven", "share_lo", "share_hi", "note",
]
FLOOR_COLUMNS = [
    "doc_id", "bank_name", "exam_date", "lending_test_rating", "cd_test_rating", "floor", "overall_rating",
    "below_floor", "census_lowered", "status", "root_cause", "triage_note",
]
TRANSITION_COLUMNS = ["regulator", "prior_rating", "overall_rating", "n", "row_share"]
CYCLE_COLUMNS = [
    "regulator", "prior_rating", "n", "n_nonpositive", "median_days", "p25_days", "p75_days", "mean_days",
    "median_years",
]
LAG_COLUMNS = [
    "regulator", "overall_rating", "n", "n_negative", "median_months", "p25_months", "p75_months",
    "median_days", "p25_days", "p75_days", "precision",
]


def read_census_positives(path: Path) -> pd.DataFrame | None:
    """Census positives (``results/census_positives.csv``), one row per positive document with
    ``doc_id`` and the Group C attributes (``CENSUS_COLUMNS``; missing attributes are left
    empty). ``rating_lowered`` is derived from ``rating_effect`` when the column is absent.
    ``None`` when the file does not exist."""
    if not path.exists():
        return None
    df = _read_csv(path)
    if "doc_id" not in df.columns:
        raise ValueError(f"{path}: census positives need a doc_id column")
    for c in CENSUS_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    derived = df["rating_effect"].str.strip() == "substantive_lowered"
    given = df["rating_lowered"].str.strip() != ""
    df["rating_lowered"] = np.where(given, df["rating_lowered"].map(_truthy), derived)
    return df.sort_values("doc_id", kind="mergesort").reset_index(drop=True)


def _dates(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.replace("", np.nan), errors="coerce", format="%Y-%m-%d")


def low_rating_decomposition(manifest: pd.DataFrame, census: pd.DataFrame | None) -> list[dict[str, Any]]:
    """NI and SN overall ratings of evaluable documents split into compliance-driven (census
    rating_effect substantive_lowered, at any level) and performance-driven (the rest), by
    regulator and method, with exact binomial CIs on the compliance-driven share."""
    ev = evaluable(manifest)
    lowered = set() if census is None else set(census.loc[census["rating_lowered"].astype(bool), "doc_id"])
    rows = []
    cells = [(r, m) for r in REGULATORS for m in METHODS] + [(r, "all") for r in REGULATORS] + [("all", "all")]
    for reg, method in cells:
        sub = ev
        if reg != "all":
            sub = sub[sub["regulator"] == reg]
        if method != "all":
            sub = sub[sub["method"] == method]
        low = sub[sub["overall_rating"].isin(LOW_RATINGS)]
        row: dict[str, Any] = {
            "regulator": reg, "method": method, "n_evaluable": len(sub),
            "n_ni": int((low["overall_rating"] == "NI").sum()), "n_sn": int((low["overall_rating"] == "SN").sum()),
            "n_low": len(low),
        }
        if census is None:
            row["note"] = "census_positives.csv not available"
        else:
            k = int(low["doc_id"].isin(lowered).sum())
            row["n_compliance_driven"] = k
            row["n_performance_driven"] = len(low) - k
            row["share_compliance_driven"], row["share_lo"], row["share_hi"] = _exact(k, len(low))
        rows.append(row)
    return rows


def findings_registry(manifest: pd.DataFrame, census: pd.DataFrame) -> list[dict[str, Any]]:
    """Census positives with bank name and dates from the manifest, ordered by doc_id."""
    meta = manifest.set_index("doc_id")
    rows = []
    for rec in census.to_dict("records"):
        d = rec["doc_id"]
        m = meta.loc[d] if d in meta.index else None
        row = {c: rec.get(c) for c in CENSUS_COLUMNS}
        for c in ("bank_name", "regulator", "bank_id", "method", "exam_date", "release_date", "overall_rating"):
            row[c] = None if m is None else m[c]
        rows.append(row)
    return rows


def _load_triage(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): (v or {}) for k, v in data.items()}


def matrix_floor_check(
    manifest: pd.DataFrame, frb: pd.DataFrame, census: pd.DataFrame | None, triage: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """FRB ISB matrix-floor check (design 7) on evaluable FRB ISB documents: the overall rating
    of the FRB CSV main row against ``isb_overall_floor`` of its Lending and CD ratings, set
    against the census ``rating_lowered``. Root causes of disagreements come from the optional
    triage file (``doc_id: {root_cause, note}``)."""
    ev = evaluable(manifest)
    ev = ev[(ev["regulator"] == "FRB") & (ev["method"] == "isb")]
    idx = frb.copy()
    idx["_key"] = idx["bank_id"].str.lstrip("0") + "_" + idx["exam_date"]
    idx = idx.sort_values(["_key"], kind="mergesort").drop_duplicates("_key", keep="first").set_index("_key")
    lowered = None if census is None else set(census.loc[census["rating_lowered"].astype(bool), "doc_id"])
    rows = []
    for rec in ev.sort_values("doc_id").to_dict("records"):
        key = f"{str(rec['bank_id']).lstrip('0')}_{rec['exam_date']}"
        if key not in idx.index:
            continue
        f = idx.loc[key]
        floor = isb_overall_floor(f["lending_test_rating"], f["cd_test_rating"])
        overall = str(f["overall_rating"]).strip()
        if floor is None or overall not in RATING_RANK:
            continue
        below = RATING_RANK[overall] < RATING_RANK[floor]
        if lowered is None:
            census_lowered, status = None, "census_not_available"
        else:
            census_lowered = rec["doc_id"] in lowered
            status = {
                (True, True): "below_floor_lowered",
                (True, False): "below_floor_not_lowered",
                (False, True): "lowered_not_below_floor",
                (False, False): "at_or_above_floor_not_lowered",
            }[(below, census_lowered)]
        t = triage.get(rec["doc_id"], {})
        rows.append({
            "doc_id": rec["doc_id"], "bank_name": rec["bank_name"], "exam_date": rec["exam_date"],
            "lending_test_rating": f["lending_test_rating"], "cd_test_rating": f["cd_test_rating"],
            "floor": floor, "overall_rating": overall, "below_floor": below, "census_lowered": census_lowered,
            "status": status, "root_cause": t.get("root_cause"), "triage_note": t.get("note"),
        })
    return rows


def rating_transitions(manifest: pd.DataFrame) -> list[dict[str, Any]]:
    """Counts of prior_rating_index to overall_rating (``none`` = no prior index row), pooled
    and per regulator, with the row share."""
    rows = []
    priors = (*RATINGS, "none")
    for reg in ("all", *REGULATORS):
        sub = manifest if reg == "all" else manifest[manifest["regulator"] == reg]
        prior = sub["prior_rating_index"].replace("", "none") if "prior_rating_index" in sub.columns else pd.Series("none", index=sub.index)
        for p in priors:
            row_n = int((prior == p).sum())
            for o in RATINGS:
                n = int(((prior == p) & (sub["overall_rating"] == o)).sum())
                rows.append({"regulator": reg, "prior_rating": p, "overall_rating": o, "n": n,
                             "row_share": n / row_n if row_n else math.nan})
    return rows


def _quantiles(values: np.ndarray) -> tuple[float, float, float, float]:
    if values.size == 0:
        return (math.nan, math.nan, math.nan, math.nan)
    p25, med, p75 = np.percentile(values, [25, 50, 75])
    return (float(med), float(p25), float(p75), float(values.mean()))


def exam_cycle_length(manifest: pd.DataFrame) -> list[dict[str, Any]]:
    """Days from prior_evaluation_date_index to exam_date by prior rating, pooled and per
    regulator. Non-positive gaps are counted and kept out of the quantiles."""
    exam = _dates(manifest["exam_date"])
    if "prior_evaluation_date_index" in manifest.columns:
        prior = _dates(manifest["prior_evaluation_date_index"])
    else:
        prior = pd.Series(pd.NaT, index=manifest.index, dtype="datetime64[ns]")
    gap = (exam - prior).dt.days
    prior_rating = manifest.get("prior_rating_index", pd.Series("", index=manifest.index))
    rows = []
    for reg in ("all", *REGULATORS):
        for pr in (*RATINGS, "all"):
            mask = gap.notna()
            if reg != "all":
                mask &= manifest["regulator"] == reg
            if pr != "all":
                mask &= prior_rating == pr
            g = gap[mask].to_numpy(dtype=float)
            pos = g[g > 0]
            med, p25, p75, mean = _quantiles(pos)
            rows.append({"regulator": reg, "prior_rating": pr, "n": int(pos.size), "n_nonpositive": int((g <= 0).sum()),
                         "median_days": med, "p25_days": p25, "p75_days": p75, "mean_days": mean,
                         "median_years": med / 365.25 if not math.isnan(med) else math.nan})
    return rows


def publication_lag(manifest: pd.DataFrame) -> list[dict[str, Any]]:
    """release_date minus exam_date by regulator and overall rating. Months at month precision
    for every regulator; days only for OCC and FRB, since FDIC release dates are release
    months stored as the first of the month (``data_quality_findings.csv``)."""
    exam = _dates(manifest["exam_date"])
    rel = _dates(manifest["release_date"])
    months = (rel.dt.year * 12 + rel.dt.month) - (exam.dt.year * 12 + exam.dt.month)
    days = (rel - exam).dt.days
    rows = []
    for reg in ("all", *REGULATORS):
        for rating in (*RATINGS, "all"):
            mask = months.notna()
            if reg != "all":
                mask &= manifest["regulator"] == reg
            if rating != "all":
                mask &= manifest["overall_rating"] == rating
            m = months[mask].to_numpy(dtype=float)
            row: dict[str, Any] = {"regulator": reg, "overall_rating": rating, "n": int(m.size),
                                   "n_negative": int((m < 0).sum())}
            row["median_months"], row["p25_months"], row["p75_months"], _ = _quantiles(m)
            if reg in ("OCC", "FRB"):
                d = days[mask].to_numpy(dtype=float)
                row["median_days"], row["p25_days"], row["p75_days"], _ = _quantiles(d)
                row["precision"] = "day"
            else:
                row["precision"] = "month (FDIC release month only)"
            rows.append(row)
    return rows


def descriptive(paths: ReportPaths | None = None) -> dict[str, Path]:
    """Exploratory tables of design section 7, from the manifest, ``census_positives.csv``,
    the FRB index CSV and the manifest's prior-exam columns. Writes
    ``desc_low_rating_decomposition.csv``, ``findings_registry.csv`` (when the census
    exists), ``frb_matrix_floor.csv``, ``desc_rating_transitions.csv``,
    ``desc_exam_cycle_length.csv``, ``desc_publication_lag.csv`` and ``descriptive.md``."""
    paths = paths or ReportPaths()
    manifest = read_manifest(paths.manifest)
    census = read_census_positives(paths.inp("census_positives.csv"))
    frb = _read_csv(paths.frb_index) if paths.frb_index.exists() else None
    out: dict[str, Path] = {}
    missing = []
    decomp = low_rating_decomposition(manifest, census)
    out["decomposition"] = write_csv(paths.out("desc_low_rating_decomposition.csv"), DECOMP_COLUMNS, decomp)
    registry: list[dict[str, Any]] = []
    if census is not None:
        registry = findings_registry(manifest, census)
        out["registry"] = write_csv(paths.out("findings_registry.csv"), REGISTRY_COLUMNS, registry)
    else:
        missing.append("census_positives.csv")
    floor: list[dict[str, Any]] = []
    if frb is not None:
        floor = matrix_floor_check(manifest, frb, census, _load_triage(paths.matrix_floor_triage))
        out["matrix_floor"] = write_csv(paths.out("frb_matrix_floor.csv"), FLOOR_COLUMNS, floor)
    else:
        missing.append(paths.frb_index.name)
    trans = rating_transitions(manifest)
    out["transitions"] = write_csv(paths.out("desc_rating_transitions.csv"), TRANSITION_COLUMNS, trans)
    cycle = exam_cycle_length(manifest)
    out["cycle"] = write_csv(paths.out("desc_exam_cycle_length.csv"), CYCLE_COLUMNS, cycle)
    lag = publication_lag(manifest)
    out["lag"] = write_csv(paths.out("desc_publication_lag.csv"), LAG_COLUMNS, lag)
    out["md"] = write_text(paths.out("descriptive.md"), _descriptive_md(decomp, registry, floor, trans, cycle, lag, missing, census is not None))
    return out


def _descriptive_md(
    decomp: list[dict[str, Any]], registry: list[dict[str, Any]], floor: list[dict[str, Any]],
    trans: list[dict[str, Any]], cycle: list[dict[str, Any]], lag: list[dict[str, Any]],
    missing: list[str], have_census: bool,
) -> str:
    parts = [
        "# Descriptive analyses (design section 7)",
        "",
        "Exploratory and non-causal (analysis plan, Purpose and scope). Counts describe what the documents "
        "and the agency indexes say; each regulator is reported on its own. Missing inputs: "
        + (", ".join(missing) if missing else "none") + ".",
        "",
        "## NI and SN ratings: performance-driven and compliance-driven",
        "",
        "Compliance-driven: the census codes rating_effect substantive_lowered (any level). Share with an exact "
        "binomial 95 percent interval. Evaluable documents only.",
        "",
        md_table(
            ["Regulator", "Method", "Evaluable", "NI", "SN", "NI or SN", "Compliance-driven", "Performance-driven",
             "Compliance-driven share [95% CI]"],
            [[r["regulator"], r["method"], cell(r["n_evaluable"]), cell(r["n_ni"]), cell(r["n_sn"]), cell(r["n_low"]),
              cell(r.get("n_compliance_driven")), cell(r.get("n_performance_driven")),
              pct_ci(r.get("share_compliance_driven"), r.get("share_lo"), r.get("share_hi"))] for r in decomp],
        ),
        "## Findings registry",
        "",
        (f"{len(registry)} census positives; full rows in results/findings_registry.csv." if have_census
         else "Census positives not available."),
        "",
    ]
    if registry:
        parts.append(md_table(
            ["doc_id", "Bank", "Regulator", "Exam date", "Finding type", "Rating effect", "Before", "After", "Laws", "Source"],
            [[r["doc_id"], r["bank_name"], r["regulator"], r["exam_date"], r["finding_type"], r["rating_effect"],
              r["rating_before"], r["rating_after"], r["laws_cited"], r["finding_source"]] for r in registry],
        ))
    counts: dict[str, int] = {}
    for r in floor:
        key = f"{r['status']} (below floor: {'yes' if r['below_floor'] else 'no'})"
        counts[key] = counts.get(key, 0) + 1
    parts += [
        "## FRB ISB matrix-floor check",
        "",
        "Floor = the lowest overall rating the Lending and Community Development Test ratings of the FRB CSV "
        "main row allow (report.ISB_OVERALL_FLOOR; to be verified against the regulation text). An overall "
        "rating below the floor is read as a candidate downgrade and set against the census.",
        "",
        md_table(["Status", "Documents"], [[k, str(counts[k])] for k in sorted(counts)]),
    ]
    flagged = [r for r in floor if r["below_floor"] or r["status"] == "lowered_not_below_floor"]
    if flagged:
        parts.append(md_table(
            ["doc_id", "Lending", "CD", "Floor", "Overall", "Census lowered", "Status", "Root cause"],
            [[r["doc_id"], r["lending_test_rating"], r["cd_test_rating"], r["floor"], r["overall_rating"],
              cell(r["census_lowered"]), r["status"], cell(r["root_cause"])] for r in flagged],
        ))
    all_trans = [r for r in trans if r["regulator"] == "all"]
    parts += [
        "## Rating transitions, all regulators pooled",
        "",
        "Rows: prior rating from the previous index row (none = no prior index row); columns: overall rating. "
        "Per-regulator counts in results/desc_rating_transitions.csv.",
        "",
        md_table(
            ["Prior \\ overall", *RATINGS],
            [[p, *[cell(next(r["n"] for r in all_trans if r["prior_rating"] == p and r["overall_rating"] == o))
                   for o in RATINGS]] for p in (*RATINGS, "none")],
        ),
        "## Exam cycle length by prior rating (days)",
        "",
        md_table(
            ["Regulator", "Prior rating", "n", "Median", "P25", "P75", "Mean", "Non-positive gaps"],
            [[r["regulator"], r["prior_rating"], cell(r["n"]), _num_str(r["median_days"]), _num_str(r["p25_days"]),
              _num_str(r["p75_days"]), _num_str(r["mean_days"], 1), cell(r["n_nonpositive"])] for r in cycle],
        ),
        "## Publication lag (release date minus exam date)",
        "",
        "Months at month precision for every regulator; days for OCC and FRB only (FDIC release dates are "
        "release months).",
        "",
        md_table(
            ["Regulator", "Overall rating", "n", "Median months", "P25", "P75", "Median days", "P25 days",
             "P75 days", "Negative lags"],
            [[r["regulator"], r["overall_rating"], cell(r["n"]), _num_str(r["median_months"], 1),
              _num_str(r["p25_months"], 1), _num_str(r["p75_months"], 1), _num_str(r.get("median_days")),
              _num_str(r.get("p25_days")), _num_str(r.get("p75_days")), cell(r["n_negative"])] for r in lag],
        ),
    ]
    return "\n".join(parts)


# ----------------------------------------------------------------------------- build and check


def build_all(
    split: str = "test", paths: ReportPaths | None = None, corpus_system: str | None = None, figures: bool = True
) -> tuple[list[Path], list[str]]:
    """Run every piece; returns the written files (sorted) and the pieces skipped for a
    missing required input."""
    paths = paths or ReportPaths()
    written: list[Path] = []
    skipped: list[str] = []
    try:
        written += table1(split, paths).values()
    except FileNotFoundError as exc:
        skipped.append(f"table1: {exc}")
    scored_path = paths.inp(f"scored_{split}.csv")
    if scored_path.exists():
        systems = set(_read_csv(scored_path)["system"])
        if any(str(x).endswith(WHOLE_SUFFIX) for x in systems):
            written += ablation(split, paths).values()
        if any(str(x).endswith(CLOSED_BOOK_SUFFIX) for x in systems):
            written += closed_book(split, paths).values()
    if figures:
        written += figure1(split, paths).values()
    try:
        written += table2(paths).values()
    except FileNotFoundError as exc:
        skipped.append(f"table2: {exc}")
    written += table3(split, paths, corpus_system).values()
    if paths.manifest.exists():
        written += descriptive(paths).values()
    else:
        skipped.append(f"descriptive: {paths.manifest} not found")
    return sorted(set(written)), skipped


_TABLE1_NAME = re.compile(r"^table1_(?P<split>[A-Za-z0-9]+)\.csv$")


def check(
    table1_path: Path, paths: ReportPaths | None = None, corpus_system: str | None = None, figures: bool = True
) -> list[str]:
    """Regenerate every output from the inputs next to ``table1_path`` into a temporary
    directory and compare bytes with the committed files there. Returns the problems
    (empty when every output is byte-identical)."""
    m = _TABLE1_NAME.match(Path(table1_path).name)
    if not m:
        raise ValueError(f"{table1_path}: expected a path ending in table1_{{split}}.csv")
    split = m.group("split")
    results_dir = Path(table1_path).parent
    base = paths or ReportPaths()
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        p = replace(base, results_dir=results_dir, out_dir=Path(tmp))
        written, skipped = build_all(split, p, corpus_system, figures)
        problems += [f"skipped {s}" for s in skipped]
        for path in written:
            committed = results_dir / path.relative_to(tmp)
            if not committed.exists():
                problems.append(f"{committed}: not committed")
            elif committed.read_bytes() != path.read_bytes():
                problems.append(f"{committed}: differs from the regenerated file")
    return problems


def main(argv: list[str] | None = None) -> int:
    """CLI. Exit 0 on success, 1 when ``--check`` finds a difference or a piece lacks its input."""
    parser = argparse.ArgumentParser(description="Assemble the report tables and figures from results/.")
    parser.add_argument("piece", nargs="?", default="all",
                        choices=["all", "table1", "ablation", "closed-book", "figure1", "table2", "table3",
                                 "descriptive"])
    parser.add_argument("--split", default="test", help="split of Table 1, Figure 1 inset and Table 3 (default test)")
    parser.add_argument("--corpus-system", default=None, help="system whose corpus cost enters the review operating points")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR, help=f"inputs (default {RESULTS_DIR})")
    parser.add_argument("--out-dir", type=Path, default=None, help="outputs (default: the results dir)")
    parser.add_argument("--no-figures", action="store_true", help="skip Figure 1 (all and --check)")
    parser.add_argument("--calibration-dir", type=Path, default=None,
                        help="directory of the pair run's calibration_{split}.csv (default: <results-dir>/pair)")
    parser.add_argument("--check", type=Path, default=None, metavar="TABLE1_CSV",
                        help="regenerate into a temp dir and compare bytes with the files next to this table1_{split}.csv")
    args = parser.parse_args(argv)
    paths = replace(ReportPaths(), results_dir=args.results_dir, out_dir=args.out_dir or args.results_dir,
                    calibration_dir=args.calibration_dir)
    if args.check is not None:
        try:
            problems = check(args.check, paths, args.corpus_system, figures=not args.no_figures)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for prob in problems:
            print(prob, file=sys.stderr)
        print("report check: " + ("ok" if not problems else f"{len(problems)} problem(s)"))
        return 1 if problems else 0
    try:
        if args.piece == "all":
            written, skipped = build_all(args.split, paths, args.corpus_system, figures=not args.no_figures)
            for s in skipped:
                print(f"skipped {s}", file=sys.stderr)
        elif args.piece == "table1":
            written = list(table1(args.split, paths).values())
        elif args.piece == "ablation":
            written = list(ablation(args.split, paths).values())
        elif args.piece == "closed-book":
            written = list(closed_book(args.split, paths).values())
        elif args.piece == "figure1":
            written = list(figure1(args.split, paths).values())
        elif args.piece == "table2":
            written = list(table2(paths).values())
        elif args.piece == "table3":
            written = list(table3(args.split, paths, args.corpus_system).values())
        else:
            written = list(descriptive(paths).values())
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
