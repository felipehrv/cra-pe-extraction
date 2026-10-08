"""Shared data contracts.

Every module reads and writes these records so that index, parse, locate, rules,
LLM and evaluation stages can be developed and tested independently.

Conventions
-----------
- ``doc_id`` is ``{regulator}_{bank_id}_{exam_date:%Y%m%d}`` (e.g. ``FDIC_12299_20220912``).
- Gold labels use the null vocabulary ``not_applicable`` / ``absent_from_document``.
- System outputs use the status vocabulary ``value`` / ``absent`` / ``abstain``.
- Dates are ISO strings in files and ``datetime.date`` in memory.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Regulator = Literal["OCC", "FDIC", "FRB"]
ExamMethod = Literal["small", "isb"]
Rating = Literal["O", "S", "NI", "SN"]
Scope = Literal["institution", "state", "msa", "assessment_area", "appendix", "toc", "unknown"]
Split = Literal["dev", "test", "corpus", "audit", "excluded"]
SystemStatus = Literal["value", "absent", "abstain"]
GoldNull = Literal["not_applicable", "absent_from_document"]

RATING_WORDS: dict[str, Rating] = {
    "outstanding": "O",
    "satisfactory": "S",
    "needs to improve": "NI",
    "substantial noncompliance": "SN",
    "substantial non-compliance": "SN",
}


def make_doc_id(regulator: str, bank_id: str, exam_date: date) -> str:
    return f"{regulator}_{str(bank_id).lstrip('0')}_{exam_date:%Y%m%d}"


class DocRecord(BaseModel):
    """One row of the corpus manifest (``config/corpus.csv``)."""

    doc_id: str
    regulator: Regulator
    bank_id: str
    bank_name: str
    exam_date: date | None = None
    release_date: date | None = None
    method: ExamMethod
    overall_rating: Rating | None = None
    lending_test_rating: Rating | None = None
    cd_test_rating: Rating | None = None
    total_assets_usd: int | None = None
    url: str
    sha256: str | None = None
    pages: int | None = None
    chars_per_page: float | None = None
    text_extractable: bool | None = None
    split: Split = "corpus"
    source_file_id: str | None = None
    notes: str | None = None

    @field_validator("bank_id", mode="before")
    @classmethod
    def _bank_id_str(cls, v: Any) -> str:
        return str(v).strip()


class Page(BaseModel):
    """One page of extracted text with its scope tag."""

    number: int = Field(ge=1, description="1-based page number")
    text: str
    scope: Scope = "unknown"
    is_toc: bool = False
    chars: int = 0


class Section(BaseModel):
    """A located section of a document."""

    name: str
    doc_id: str
    page_start: int
    page_end: int
    char_start: int = Field(description="offset within the concatenated document text")
    char_end: int
    text: str
    scope: Scope = "institution"
    heading_matched: str | None = None
    confidence: float = 1.0


class FieldValue(BaseModel):
    """One extracted value from one system for one (doc, field)."""

    doc_id: str
    field: str
    system: str
    status: SystemStatus = "value"
    value: Any = None
    quote: str | None = None
    page: int | None = None
    scope: Scope | None = None
    char_start: int | None = None
    char_end: int | None = None
    confidence: float | None = None
    notes: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        return None if v == "" else v


class LabelRow(BaseModel):
    """One hand label for one (doc, field)."""

    doc_id: str
    field: str
    value: Any = None
    null_reason: GoldNull | None = None
    evidence_page: int | None = None
    quote: str | None = None
    scope: Scope | None = None
    minutes: float | None = None
    labeler: str | None = None
    labeled_at: datetime | None = None
    notes: str | None = None

    @property
    def is_null(self) -> bool:
        return self.null_reason is not None

    @field_validator("value", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        return None if v == "" else v


class Trace(BaseModel):
    """One logged LLM call. Stored as JSONL under ``data/traces/``."""

    trace_id: str
    doc_id: str
    field_group: str
    backend: str
    model: str
    model_reported: str | None = None
    prompt_hash: str
    section_offsets: list[tuple[int, int]] = Field(default_factory=list)
    response_json: dict[str, Any] | None = None
    response_text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    called_at: datetime
    error: str | None = None
    stop_reason: str | None = None
    response_format: str | None = None
