"""Command-line dispatcher: ``cra <stage> [options]``.

Every pipeline stage is a module with its own ``main(argv) -> int`` and can be run as
``python -m cra_extract.<stage>``. This module only maps a subcommand name to that
function so the ``cra`` console script (``pyproject.toml``) covers the whole pipeline::

    cra index --window 2023-01-01 2025-12-31 --prior-from 2018-01-01
    cra parse --manifest config/corpus.csv
    cra evaluate --split dev

Modules are imported lazily so ``cra --help`` does not load pymupdf or the LLM SDKs.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence

# subcommand -> (module, one-line help). Order is the pipeline order.
STAGES: dict[str, tuple[str, str]] = {
    "index": ("cra_extract.index", "build the corpus manifest from the agency indexes"),
    "fetch": ("cra_extract.fetch", "download the PDFs listed in the manifest"),
    "parse": ("cra_extract.parse", "extract page text from the PDFs"),
    "overrides": ("cra_extract.overrides", "apply the documented manifest overrides and exclusions"),
    "locate": ("cra_extract.locate", "locate sections and report locator coverage"),
    "templates": ("cra_extract.templates", "template-residual census of the review section"),
    "rules": ("cra_extract.rules", "rule-based extraction (system S1)"),
    "baseline": ("cra_extract.baseline", "keyword baseline for Group C (system S0)"),
    "census": ("cra_extract.census", "Group C census: random draw, sheet, validation, scoring"),
    "group-a": ("cra_extract.group_a", "rules versus the agency index, corpus-wide (Group A)"),
    "strata": ("cra_extract.strata", "draw the dev, test and audit splits"),
    "label": ("cra_extract.label", "init, validate and hash gold label sheets"),
    "llm": ("cra_extract.llm", "LLM extraction smoke test"),
    "run": ("cra_extract.run", "run the extraction systems over a split"),
    "grounding": ("cra_extract.grounding", "check one quote against a text file"),
    "evaluate": ("cra_extract.evaluate", "score predictions against gold"),
    "routing": ("cra_extract.routing", "S5 routing table from S1 index agreement, and the S5 rows"),
    "corpus-scoring": ("cra_extract.corpus_scoring", "Group A at corpus scale and the corpus risk file"),
    "audit": ("cra_extract.audit", "audit sheet, design-weighted Group B error, review curve"),
    "reliability": ("cra_extract.reliability", "Group B reliability: second blind coding and agreement"),
    "freeze": ("cra_extract.freeze", "check the analysis plan and tag plan-v1.0"),
    "stats": ("cra_extract.stats", "binomial confidence intervals"),
    "report": ("cra_extract.report", "assemble the report tables and figures from results/"),
}


def build_parser() -> argparse.ArgumentParser:
    """Parser with one subcommand per stage; stage options are passed through untouched."""
    parser = argparse.ArgumentParser(
        prog="cra",
        description="CRA Performance Evaluation extraction pipeline.",
        epilog="Run 'cra <stage> --help' for the options of one stage.",
    )
    sub = parser.add_subparsers(dest="stage", metavar="stage", required=True)
    for name, (_module, help_text) in STAGES.items():
        stage = sub.add_parser(name, help=help_text, add_help=False)
        stage.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch ``argv`` to the chosen stage's ``main`` and return its exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in STAGES:
        # Let the stage parse its own options, including --help.
        stage, rest = argv[0], argv[1:]
    else:
        ns = build_parser().parse_args(argv)
        stage, rest = ns.stage, ns.args
    module = importlib.import_module(STAGES[stage][0])
    sys.argv[0] = f"cra {stage}"  # argparse derives the usage prog name from it
    return int(module.main(rest) or 0)


if __name__ == "__main__":
    sys.exit(main())
