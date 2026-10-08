"""Paths and configuration loading shared by every module.

All modules import paths from here so that the repo root can move without
breaking anything. Configuration files live in ``config/`` and are versioned.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("CRA_ROOT", Path(__file__).resolve().parents[2]))
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
INDEX_DIR = DATA_DIR / "index"
RAW_INDEX_DIR = INDEX_DIR / "raw"
PDF_DIR = DATA_DIR / "pdfs"
TEXT_DIR = DATA_DIR / "text"
GOLD_DIR = DATA_DIR / "gold"
TRACE_DIR = DATA_DIR / "traces"
CACHE_DIR = DATA_DIR / "cache"
RESULTS_DIR = ROOT / "results"
DOCS_DIR = ROOT / "docs"
TESTS_DIR = ROOT / "tests"
FIXTURES_DIR = TESTS_DIR / "fixtures"

FIELDS_PATH = CONFIG_DIR / "fields.yaml"
HEADINGS_PATH = CONFIG_DIR / "headings.yaml"
PRICES_PATH = CONFIG_DIR / "prices.yaml"
TEMPLATES_PATH = CONFIG_DIR / "review_templates.yaml"
CORPUS_PATH = CONFIG_DIR / "corpus.csv"
STRATA_PATH = CONFIG_DIR / "strata.yaml"

load_dotenv(ROOT / ".env")


def ensure_dirs() -> None:
    """Create the data and results directories if they do not exist."""
    for d in (RAW_INDEX_DIR, PDF_DIR, TEXT_DIR, GOLD_DIR, TRACE_DIR, CACHE_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@cache
def load_fields() -> dict[str, Any]:
    """Return the parsed data dictionary (``config/fields.yaml``)."""
    return _load_yaml(FIELDS_PATH)


@cache
def load_headings() -> dict[str, Any]:
    """Return the per-regulator section heading patterns."""
    return _load_yaml(HEADINGS_PATH)


@cache
def load_prices() -> dict[str, Any]:
    """Return model prices and free-tier limits with their retrieval dates."""
    return _load_yaml(PRICES_PATH)


def field_names(group: str | None = None) -> list[str]:
    """Names of scored scalar fields, optionally restricted to one group (A, B, C, D)."""
    fields = load_fields()["fields"]
    return [k for k, v in fields.items() if group is None or v.get("group") == group]


def field_spec(name: str) -> dict[str, Any]:
    """Specification of one field from the data dictionary."""
    return load_fields()["fields"][name]


def applicability(field: str, regulator: str, method: str) -> str:
    """Return ``value``, ``absent_from_document`` or ``not_applicable`` for a field in a
    (regulator, exam method) cell. Fields without an applicability block are ``value``."""
    table = load_fields().get("applicability", {})
    if field not in table:
        return "value"
    key = f"{regulator}/{method}"
    return table[field].get(key, "value")
