"""S5 routing: which system the composite S5 takes for each field (design section 4).

Implements ``docs/analysis_plan.md``, "Systems and versions" (last bullet) and "Decisions
that depend on dev results", item 1:

- :func:`compute` measures, for each Group A field and for the two component ratings in the
  FRB/isb cell (the only cell with an index value), S1 coverage (share of documents with an
  index value where S1 returns status ``value``) and S1 agreement (share of those S1 values
  that match the index under the field's tolerance, ``evaluate.values_match`` through
  ``group_a.compare_with_index``) over the documents of split ``corpus``: dev, test, audit
  and excluded rows are left out, and so is any row that is not evaluable (no sha256 or
  ``text_extractable`` not true). The field is routed to S1 when coverage is at least
  ``COVERAGE_MIN`` and agreement at least ``AGREEMENT_MIN``, otherwise to the S3 corpus
  tier. Index truth is the raw index of ``group_a.index_truth``: FDIC exam_date has none
  at routing time (the plan's file-id gate needs the test gold, which does not exist before
  the freeze), the hand override included, so exam_date is routed on OCC and FRB documents.
  ``compute`` refuses before the test draw (the plan computes routing after it) unless
  ``--provisional`` is given, and a provisional table never goes to ``config/``.
- :func:`write_outputs` writes ``results/s5_routing.csv``, ``config/s5_routing.yaml`` (date,
  RULES_VERSION, systems, thresholds, population counts and one entry per field) and
  ``results/s5_routing.md``, the table of the plan ready to paste.
- :func:`apply` builds the S5 rows from the S1 and S3 corpus-tier rows of a predictions
  frame: a routed field takes the routed system's row (component ratings only in FRB/isb;
  elsewhere, and for Group B and C, S5 is S3). Each S5 row carries ``source_system`` (where
  the value came from) and ``reference_system`` (the system its disagreement flag compares
  with: S1 for a value taken from S3, S3 for a value taken from S1), and its notes start
  with ``s5_from=<system>`` so the provenance survives a merge into a predictions file.
  An S1-sourced value carries ``grounding_failure`` ``na`` (``evaluate.GROUNDING_NOT_GRADED``):
  the plan grades groundedness only for the S3-sourced values of S5. ``--append-to`` adds
  the S5 rows to a run-stage predictions file (replacing earlier S5 rows), which is how S5
  enters the single test scoring run.

S5 takes the routed system's row as it is: an abstention of the routed system is an S5
abstention, with no fallback to the other system (the plan names no fallback).

CLI::

    python -m cra_extract.routing compute --predictions results/rules.csv
        [--s3 llm:gemini:gemini-3.8-flash] [--date 2026-10-13] [--provisional]
    python -m cra_extract.routing apply --predictions results/predictions_corpus.csv
        --s3 llm:gemini:gemini-3.8-flash [--out results/predictions_corpus_s5.csv]
    python -m cra_extract.routing apply --predictions results/predictions_test.csv
        --append-to results/predictions_test.csv
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from cra_extract.config import CONFIG_DIR, CORPUS_PATH, RESULTS_DIR, field_names, load_fields
from cra_extract.evaluate import GROUNDING_NOT_GRADED
from cra_extract.group_a import (
    COMPONENT_FIELDS,
    COMPONENT_INDEX_CELL,
    compare_with_index,
    index_truth,
)
from cra_extract.models import FieldValue
from cra_extract.stats import wilson_ci

COVERAGE_MIN = 0.95
AGREEMENT_MIN = 0.98
ROUTING_SPLIT = "corpus"
S1_DEFAULT = "rules"
S3_DEFAULT = "llm:gemini:gemini-3.8-flash"
S5_NAME = "s5"
ROUTES = ("s1", "s3")
PREDICTION_COLUMNS = [*FieldValue.model_fields, "grounding_failure", "locator_miss"]
S5_EXTRA_COLUMNS = ["source_system", "reference_system"]
ROUTING_CSV = RESULTS_DIR / "s5_routing.csv"
ROUTING_YAML = CONFIG_DIR / "s5_routing.yaml"
ROUTING_MD = RESULTS_DIR / "s5_routing.md"
PROVISIONAL_DIR = RESULTS_DIR / "provisional"
TABLE_COLUMNS = [
    "field", "cell", "n_population", "n_index", "n_no_truth", "n_s1_value", "n_agree",
    "coverage", "coverage_lo", "coverage_hi", "agreement", "agreement_lo", "agreement_hi", "route",
]


@dataclass(frozen=True)
class Systems:
    """System names as they appear in the ``system`` column of a predictions frame."""

    s1: str = S1_DEFAULT
    s3: str = S3_DEFAULT
    s5: str = S5_NAME


DEFAULT_SYSTEMS = Systems()


# ----------------------------------------------------------------------------- inputs


def routed_fields() -> list[str]:
    """Group A fields of ``fields.yaml`` followed by the two component ratings."""
    return [*field_names("A"), *COMPONENT_FIELDS]


def evaluable(manifest: pd.DataFrame) -> pd.Series:
    """Boolean mask of evaluable rows: a fetched PDF (sha256), ``text_extractable`` true and
    split not ``excluded`` (``docs/analysis_plan.md``, "Exclusions")."""
    m = manifest.fillna("").astype(str)
    ok = m["split"] != "excluded"
    if "sha256" in m.columns:
        ok &= m["sha256"].str.strip() != ""
    if "text_extractable" in m.columns:
        ok &= m["text_extractable"].str.strip().str.casefold() == "true"
    return ok


def routing_population(manifest: pd.DataFrame) -> pd.DataFrame:
    """Evaluable manifest rows of split ``corpus`` (dev, test, audit and excluded left out)."""
    m = manifest.fillna("").astype(str)
    return m[evaluable(m) & (m["split"] == ROUTING_SPLIT)].reset_index(drop=True)


def normalize_predictions(frame: pd.DataFrame) -> pd.DataFrame:
    """A predictions frame (run stage or ``rules.csv``) as strings with every
    ``PREDICTION_COLUMNS`` column present (missing ones empty); extra columns are kept."""
    out = frame.fillna("").astype(str).copy()
    for col in PREDICTION_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    return out


def read_predictions(paths: Iterable[Path]) -> pd.DataFrame:
    """Concatenate predictions files; a (doc_id, field, system) row seen in an earlier file wins."""
    frames = [normalize_predictions(pd.read_csv(p, dtype=str, keep_default_na=False)) for p in paths]
    if not frames:
        return normalize_predictions(pd.DataFrame(columns=PREDICTION_COLUMNS))
    out = pd.concat(frames, ignore_index=True).fillna("")
    return out.drop_duplicates(["doc_id", "field", "system"], keep="first").reset_index(drop=True)


def read_manifest(path: Path = CORPUS_PATH) -> pd.DataFrame:
    """The manifest as strings."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


# ----------------------------------------------------------------------------- compute


def _share(k: int, n: int) -> tuple[float, float, float]:
    if not n:
        return (float("nan"), float("nan"), float("nan"))
    lo, hi = wilson_ci(k, n)
    return (k / n, lo, hi)


def route_for(n_index: int, n_value: int, n_agree: int) -> str:
    """``s1`` when coverage >= ``COVERAGE_MIN`` and agreement >= ``AGREEMENT_MIN`` (integer
    comparisons, so a share exactly at the threshold passes); ``s3`` otherwise, including
    when no document has an index value."""
    if n_index == 0 or n_value == 0:
        return "s3"
    coverage_ok = n_value * 100 >= round(COVERAGE_MIN * 100) * n_index
    agreement_ok = n_agree * 100 >= round(AGREEMENT_MIN * 100) * n_value
    return "s1" if coverage_ok and agreement_ok else "s3"


def compute(
    manifest: pd.DataFrame,
    predictions: pd.DataFrame,
    index: pd.DataFrame | None = None,
    s1: str = S1_DEFAULT,
) -> pd.DataFrame:
    """Routing table, one row per routed field (``TABLE_COLUMNS``).

    ``predictions`` holds S1 rows (``system == s1``; other systems are ignored), from
    ``results/rules.csv`` or a run-stage predictions file. ``index`` is the long truth table
    of ``group_a.index_truth``; it is built from ``manifest`` when omitted. Only documents of
    :func:`routing_population` count; component ratings count only in the FRB/isb cell.
    ``n_population`` is the number of population documents in the field's cell,
    ``n_index`` those with an index value, ``n_no_truth`` the rest.
    """
    fields = routed_fields()
    pop = routing_population(manifest)
    truth = index if index is not None else index_truth(pop, fields)
    truth = truth[truth["doc_id"].isin(set(pop["doc_id"])) & truth["field"].isin(fields)]
    comp = truth["field"].isin(COMPONENT_FIELDS)
    in_cell = (truth["regulator"] == COMPONENT_INDEX_CELL[0]) & (truth["method"] == COMPONENT_INDEX_CELL[1])
    truth = truth[~comp | in_cell]
    with_value = truth[truth["truth"] == "value"]
    compared = compare_with_index(predictions, with_value, [s1])
    rows = []
    for field in fields:
        t = truth[truth["field"] == field]
        c = compared[compared["field"] == field]
        n_index = len(c)
        n_value = int((c["status"] == "value").sum())
        n_agree = int((c["cell"] == "correct").sum())
        cov = _share(n_value, n_index)
        agr = _share(n_agree, n_value)
        rows.append({
            "field": field,
            "cell": "/".join(COMPONENT_INDEX_CELL) if field in COMPONENT_FIELDS else "all",
            "n_population": len(t), "n_index": n_index, "n_no_truth": len(t) - n_index,
            "n_s1_value": n_value, "n_agree": n_agree,
            "coverage": cov[0], "coverage_lo": cov[1], "coverage_hi": cov[2],
            "agreement": agr[0], "agreement_lo": agr[1], "agreement_hi": agr[2],
            "route": route_for(n_index, n_value, n_agree),
        })
    return pd.DataFrame(rows, columns=TABLE_COLUMNS)


# ----------------------------------------------------------------------------- outputs


def _pct(x: float) -> str:
    return "" if pd.isna(x) else f"{100 * x:.1f}"


def markdown_table(table: pd.DataFrame) -> str:
    """The routing table of ``docs/analysis_plan.md`` (Decisions, item 1), values in percent
    to one decimal with their counts."""
    lines = [
        "| field | n with index value | S1 coverage | S1 agreement | route |",
        "|---|---|---|---|---|",
    ]
    for r in table.itertuples(index=False):
        label = f"{r.field} ({r.cell})" if r.cell != "all" else r.field
        cov = f"{_pct(r.coverage)} ({r.n_s1_value:,} of {r.n_index:,})" if r.n_index else "n/a (0)"
        agr = f"{_pct(r.agreement)} ({r.n_agree:,} of {r.n_s1_value:,})" if r.n_s1_value else "n/a (0)"
        lines.append(f"| {label} | {r.n_index:,} | {cov} | {agr} | {r.route.upper()} |")
    return "\n".join(lines) + "\n"


def rules_version_of(predictions: pd.DataFrame, s1: str = S1_DEFAULT) -> tuple[str, str]:
    """(RULES_VERSION, source): the ``rules_version`` column of the S1 rows when present and
    unique, else the version in ``rules.py`` (source ``code``). Several versions in the rows
    raise ``ValueError``: routing must use one frozen version."""
    rows = predictions[predictions["system"] == s1] if "system" in predictions.columns else predictions
    if "rules_version" in rows.columns:
        versions = sorted({v for v in rows["rules_version"].astype(str) if v})
        if len(versions) > 1:
            raise ValueError(f"S1 rows carry several rules versions: {versions}")
        if versions:
            return versions[0], "predictions"
    from cra_extract.rules import RULES_VERSION

    return RULES_VERSION, "code"


def routing_record(
    table: pd.DataFrame,
    manifest: pd.DataFrame,
    computed_on: date,
    rules_version: str,
    rules_version_source: str,
    systems: Systems,
) -> dict[str, Any]:
    """The ``config/s5_routing.yaml`` payload."""
    m = manifest.fillna("").astype(str)
    pop = routing_population(m)
    by_split = m["split"].value_counts().to_dict()
    exam = index_truth(pop, ["exam_date"])
    exam = exam[exam["truth"] == "value"]
    exam_by_reg = {reg: int((exam["regulator"] == reg).sum()) for reg in ("OCC", "FDIC", "FRB")}
    fields = {}
    for r in table.to_dict("records"):
        fields[r["field"]] = {
            "cell": r["cell"], "route": r["route"],
            "n_population": int(r["n_population"]), "n_index": int(r["n_index"]),
            "n_no_truth": int(r["n_no_truth"]), "n_s1_value": int(r["n_s1_value"]),
            "n_agree": int(r["n_agree"]),
            "coverage": None if pd.isna(r["coverage"]) else round(float(r["coverage"]), 4),
            "agreement": None if pd.isna(r["agreement"]) else round(float(r["agreement"]), 4),
        }
    return {
        "computed_on": computed_on.isoformat(),
        "rules_version": rules_version,
        "rules_version_source": rules_version_source,
        "s1_system": systems.s1,
        "s3_system": systems.s3,
        "s5_system": systems.s5,
        "rule": (
            f"route S1 when S1 coverage >= {COVERAGE_MIN} and S1 agreement >= {AGREEMENT_MIN} over "
            f"evaluable documents of split {ROUTING_SPLIT} with an index value; S3 corpus tier otherwise"
        ),
        "thresholds": {"coverage": COVERAGE_MIN, "agreement": AGREEMENT_MIN},
        "population": {
            "split": ROUTING_SPLIT,
            "n_documents": len(pop),
            "n_manifest": len(m),
            "n_by_split": {str(k): int(v) for k, v in sorted(by_split.items())},
            "n_not_evaluable": int((~evaluable(m)).sum()),
            "test_drawn": bool((m["split"] == "test").any()),
        },
        "exam_date_index_by_regulator": exam_by_reg,
        "notes": [
            "FDIC exam_date has no index truth at routing time (the file-id gate of the plan, Group A "
            "bullet 3, needs the test gold), the hand override included, so the exam_date route rests on "
            + " and ".join(f"{k} ({v:,})" for k, v in exam_by_reg.items() if v)
            + " documents and applies to FDIC documents too.",
            "Component ratings are routed only in FRB/isb; in every other cell S5 takes S3.",
            "Agreement uses evaluate.values_match with the field's tolerance against the raw index.",
        ],
        "fields": fields,
    }


def write_outputs(
    table: pd.DataFrame,
    record: Mapping[str, Any],
    csv_path: Path = ROUTING_CSV,
    yaml_path: Path = ROUTING_YAML,
    md_path: Path = ROUTING_MD,
) -> dict[str, Path]:
    """Write the routing CSV, YAML and markdown table; return their paths."""
    for p in (csv_path, yaml_path, md_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(csv_path, index=False, float_format="%.5f")
    with open(yaml_path, "w", encoding="utf-8") as fh:
        fh.write("# S5 routing (docs/analysis_plan.md, Decisions that depend on dev results, item 1).\n")
        fh.write("# Written by python -m cra_extract.routing compute; do not edit by hand.\n")
        yaml.safe_dump(dict(record), fh, sort_keys=False, allow_unicode=False)
    md_path.write_text(markdown_table(table), encoding="utf-8")
    return {"csv": csv_path, "yaml": yaml_path, "md": md_path}


def load_routing(path: Path = ROUTING_YAML) -> tuple[dict[str, str], dict[str, Any]]:
    """(field -> route, full record) from a routing YAML written by :func:`write_outputs`."""
    with open(path, encoding="utf-8") as fh:
        record = yaml.safe_load(fh) or {}
    routes = {f: v["route"] for f, v in (record.get("fields") or {}).items()}
    return routes, record


# ----------------------------------------------------------------------------- apply


def extracted_fields() -> list[str]:
    """Every field an LLM field group extracts, in ``fields.yaml`` order."""
    cfg = load_fields()
    wanted = {f for g in cfg["llm_field_groups"].values() for f in g["fields"]}
    return [f for f in cfg["fields"] if f in wanted]


def source_for(field: str, regulator: str, method: str, routes: Mapping[str, str], systems: Systems) -> str:
    """System name S5 takes ``field`` from in a (regulator, method) cell."""
    if field not in routes:
        return systems.s3
    if field in COMPONENT_FIELDS and (regulator, method) != COMPONENT_INDEX_CELL:
        return systems.s3
    return systems.s1 if routes[field] == "s1" else systems.s3


def apply(
    predictions: pd.DataFrame,
    routes: Mapping[str, str],
    manifest: pd.DataFrame,
    systems: Systems = DEFAULT_SYSTEMS,
    doc_ids: Iterable[str] | None = None,
) -> pd.DataFrame:
    """S5 rows (system ``systems.s5``) in the predictions-file contract plus
    ``S5_EXTRA_COLUMNS``.

    One row per (document, extracted field) for every document with at least one S3 row
    (or for ``doc_ids`` when given). The row is a copy of the source system's row with its
    grounding and locator columns; a missing source row gives an abstention noted
    ``s5: no <system> row``. Every routed field must have a route in ``routes``.
    """
    missing = [f for f in routed_fields() if f not in routes]
    if missing:
        raise ValueError(f"routing has no entry for {missing}")
    bad = {f: r for f, r in routes.items() if r not in ROUTES}
    if bad:
        raise ValueError(f"unknown routes {bad}; expected one of {ROUTES}")
    p = normalize_predictions(predictions)
    p = p[p["system"].isin({systems.s1, systems.s3})]
    by_key = {(r["doc_id"], r["field"], r["system"]): r for r in p.to_dict("records")}
    if doc_ids is None:
        doc_ids = sorted(set(p.loc[p["system"] == systems.s3, "doc_id"]))
    m = manifest.fillna("").astype(str).set_index("doc_id")
    docs = list(doc_ids)
    unknown = [d for d in docs if d not in m.index]
    if unknown:
        raise ValueError(f"{len(unknown)} documents are not in the manifest, e.g. {unknown[:3]}")
    rows = []
    for doc in docs:
        reg, method = m.at[doc, "regulator"], m.at[doc, "method"]
        for field in extracted_fields():
            src = source_for(field, reg, method, routes, systems)
            ref = systems.s3 if src == systems.s1 else systems.s1
            row = by_key.get((doc, field, src))
            if row is None:
                out = {c: "" for c in PREDICTION_COLUMNS}
                out.update(doc_id=doc, field=field, status="abstain", notes=f"s5_from={src}; s5: no {src} row")
            else:
                out = {c: row.get(c, "") for c in PREDICTION_COLUMNS}
                out["notes"] = f"s5_from={src}" + (f"; {row['notes']}" if row.get("notes") else "")
                if src == systems.s1 and out["status"] == "value":
                    out["grounding_failure"] = GROUNDING_NOT_GRADED
            out["system"] = systems.s5
            out["source_system"] = src
            out["reference_system"] = ref
            rows.append(out)
    return pd.DataFrame(rows, columns=[*PREDICTION_COLUMNS, *S5_EXTRA_COLUMNS])


def ensure_s5(
    predictions: pd.DataFrame,
    manifest: pd.DataFrame,
    systems: Systems,
    routing_path: Path | None,
) -> pd.DataFrame:
    """``predictions`` with S5 rows added, from ``routing_path``, for every document that
    has S3 rows and no S5 rows yet. Without a routing file the frame is returned as is."""
    p = normalize_predictions(predictions)
    if routing_path is None or not Path(routing_path).exists():
        return p
    have = set(p.loc[p["system"] == systems.s5, "doc_id"])
    todo = sorted(set(p.loc[p["system"] == systems.s3, "doc_id"]) - have)
    if not todo:
        return p
    routes, _ = load_routing(Path(routing_path))
    s5 = apply(p, routes, manifest, systems, doc_ids=todo)
    return pd.concat([p, s5], ignore_index=True).fillna("")


def append_s5(path: Path, s5: pd.DataFrame, s5_name: str = S5_NAME) -> int:
    """Rewrite the predictions file ``path`` with its rows of system ``s5_name`` replaced by
    ``s5`` (only the file's own columns are written, so the run-stage contract holds; the
    provenance stays in the ``s5_from=`` note). Returns the number of S5 rows written."""
    current = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = list(current.columns)
    missing = [c for c in PREDICTION_COLUMNS if c not in columns]
    if missing:
        raise ValueError(f"{path}: not a run-stage predictions file (missing {missing})")
    kept = current[current["system"] != s5_name]
    out = pd.concat([kept, s5.reindex(columns=columns).fillna("")], ignore_index=True)
    out.to_csv(path, index=False)
    return len(s5)


# ----------------------------------------------------------------------------- CLI


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; ``compute`` exits 1 when the S1 rows carry several rules versions
    or the version in the rows differs from ``rules.py`` (unless ``--allow-version-mismatch``)."""
    ap = argparse.ArgumentParser(description="S5 routing: compute the table or apply it (design 4).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compute", help="S1 coverage and agreement per field over split corpus")
    c.add_argument("--predictions", type=Path, nargs="+", default=[RESULTS_DIR / "rules.csv"])
    c.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    c.add_argument("--s1", default=S1_DEFAULT)
    c.add_argument("--s3", default=S3_DEFAULT, help="S3 corpus tier recorded in the YAML for apply")
    c.add_argument("--date", type=_parse_date, default=None, help="computation date (default today)")
    c.add_argument("--out-csv", type=Path, default=ROUTING_CSV)
    c.add_argument("--out-yaml", type=Path, default=ROUTING_YAML)
    c.add_argument("--out-md", type=Path, default=ROUTING_MD)
    c.add_argument("--allow-version-mismatch", action="store_true")
    c.add_argument("--provisional", action="store_true",
                   help="allow a run before the test draw; outputs go to results/provisional/, never config/")
    a = sub.add_parser("apply", help="build the S5 rows from S1 and S3 rows")
    a.add_argument("--predictions", type=Path, nargs="+", default=[RESULTS_DIR / "predictions_corpus.csv"])
    a.add_argument("--manifest", type=Path, default=CORPUS_PATH)
    a.add_argument("--routing", type=Path, default=ROUTING_YAML)
    a.add_argument("--s1", default=None, help="default: s1_system of the routing YAML")
    a.add_argument("--s3", default=None, help="default: s3_system of the routing YAML")
    a.add_argument("--out", type=Path, default=RESULTS_DIR / "predictions_corpus_s5.csv")
    a.add_argument("--append-to", type=Path, default=None,
                   help="add the S5 rows to this run-stage predictions file instead of writing --out")
    args = ap.parse_args(argv)

    manifest = read_manifest(args.manifest)
    preds = read_predictions(args.predictions)
    if args.cmd == "compute":
        systems = Systems(s1=args.s1, s3=args.s3)
        try:
            version, source = rules_version_of(preds, systems.s1)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        from cra_extract.rules import RULES_VERSION

        if version != RULES_VERSION and not args.allow_version_mismatch:
            print(f"error: S1 rows are rules {version}, rules.py is {RULES_VERSION}; rerun make rules "
                  "or pass --allow-version-mismatch", file=sys.stderr)
            return 1
        test_drawn = bool((manifest["split"] == "test").any())
        out_csv, out_yaml, out_md = args.out_csv, args.out_yaml, args.out_md
        if not test_drawn:
            if not args.provisional:
                print("error: the test split is not drawn; the plan computes routing after the test draw "
                      "(pass --provisional for a table under results/provisional/)", file=sys.stderr)
                return 1
            if out_csv == ROUTING_CSV:
                out_csv = PROVISIONAL_DIR / ROUTING_CSV.name
            if out_yaml == ROUTING_YAML:
                out_yaml = PROVISIONAL_DIR / ROUTING_YAML.name
            if out_md == ROUTING_MD:
                out_md = PROVISIONAL_DIR / ROUTING_MD.name
            if Path(out_yaml).resolve().parent == CONFIG_DIR.resolve():
                print("error: a provisional routing table is never written to config/", file=sys.stderr)
                return 1
            print("warning: provisional routing before the test draw; not for S5", file=sys.stderr)
        table = compute(manifest, preds, s1=systems.s1)
        record = routing_record(table, manifest, args.date or date.today(), version, source, systems)
        record["provisional"] = not test_drawn
        paths = write_outputs(table, record, out_csv, out_yaml, out_md)
        print(markdown_table(table))
        for name, p in paths.items():
            print(f"{name}: {p}")
        return 0

    routes, record = load_routing(args.routing)
    systems = Systems(s1=args.s1 or record.get("s1_system") or S1_DEFAULT,
                      s3=args.s3 or record.get("s3_system") or S3_DEFAULT)
    if record.get("provisional"):
        print(f"error: {args.routing} is a provisional routing table (computed before the test draw)",
              file=sys.stderr)
        return 1
    try:
        s5 = apply(preds, routes, manifest, systems)
        if args.append_to is not None:
            n = append_s5(args.append_to, s5, systems.s5)
            print(f"s5: {n} rows for {s5['doc_id'].nunique()} documents appended to {args.append_to}")
            return 0 if n else 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    s5.to_csv(args.out, index=False)
    n_docs = s5["doc_id"].nunique()
    print(f"s5: {len(s5)} rows for {n_docs} documents from {systems.s1} and {systems.s3}; written to {args.out}")
    return 0 if n_docs else 1


if __name__ == "__main__":
    sys.exit(main())
