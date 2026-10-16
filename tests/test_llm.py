"""Tests for the LLM client (``cra_extract.llm``) and prompt builder (``cra_extract.prompts``).

No network. A ``FakeBackend`` returns canned JSON; retry, rate limiting and
replay are exercised with injected clocks and sleepers. Quotes come from the
SouthStar sample (FDIC_12299_SouthStar_2022_ISB_NI_fairlending.txt, lines 56,
67 and 599; pages 3, 3 and 14 under a form-feed page split).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from cra_extract import llm, prompts
from cra_extract.config import load_fields, load_headings
from cra_extract.models import DocRecord, Section, Trace
from cra_extract.prompts import (
    ALL_GROUP,
    CLOSED_BOOK_PROMPT_VERSION,
    CLOSED_BOOK_SYSTEM_PROMPT,
    PROMPT_VERSION,
    STATUSES,
    SYSTEM_PROMPT,
    build_closed_book_prompt,
    build_closed_book_schema,
    build_prompt,
    build_schema,
)

SOUTHSTAR = Path(__file__).parent / "fixtures" / "samples"
SOUTHSTAR_TXT = SOUTHSTAR / "FDIC_12299_SouthStar_2022_ISB_NI_fairlending.txt"

RATING_QUOTE = "This institution is rated Needs to Improve."
LENDING_QUOTE = "The Lending Test is rated Satisfactory."
REVIEW_QUOTE = "The FDIC lowered the CRA rating from Satisfactory to Needs to Improve"


# ----------------------------------------------------------------------------- helpers


class FakeBackend(llm.Backend):
    """Returns a canned response; records the prompts it received."""

    name = "fake"

    def __init__(self, response: dict[str, Any] | str, model: str = "fake-1") -> None:
        self.response = response
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def _call(self, system_prompt, user_prompt, json_schema, model):
        self.calls.append({"system": system_prompt, "user": user_prompt, "schema": json_schema})
        text = self.response if isinstance(self.response, str) else json.dumps(self.response)
        return llm.BackendResult(
            text=text,
            parsed_json=llm._parse_json(text),
            model_reported=model + "-2026",
            input_tokens=1000,
            output_tokens=200,
        )


class FlakyBackend(FakeBackend):
    """Fails ``n_failures`` times with the given exception, then succeeds."""

    def __init__(self, response, exc_factory, n_failures: int) -> None:
        super().__init__(response)
        self.exc_factory = exc_factory
        self.remaining = n_failures
        self.attempts = 0

    def _call(self, *args):
        self.attempts += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.exc_factory()
        return super()._call(*args)


class StatusError(Exception):
    """Duck-typed SDK error with ``status_code`` and an optional Retry-After header."""

    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        headers = {"retry-after": retry_after} if retry_after else {}
        self.response = type("Resp", (), {"headers": headers})()


def entry(value=None, status="value", quote=None, page=None) -> dict[str, Any]:
    return {"value": value, "status": status, "quote": quote, "page": page}


@pytest.fixture
def doc() -> DocRecord:
    return DocRecord(
        doc_id="FDIC_12299_20220912",
        regulator="FDIC",
        bank_id="12299",
        bank_name="SouthStar Bank, S.S.B.",
        exam_date=date(2022, 9, 12),
        method="isb",
        url="https://crapes.fdic.gov/publish/2022/12299_220912.PDF",
        split="dev",
    )


@pytest.fixture
def sections(doc) -> dict[str, Section]:
    """Rating section = page 3 and review section = page 14 of the SouthStar text."""
    text = SOUTHSTAR_TXT.read_text(encoding="utf-8")
    pages = text.split("\f")
    starts = []
    pos = 0
    for p in pages:
        starts.append(pos)
        pos += len(p) + 1
    rating = pages[2]
    review = pages[13]
    assert RATING_QUOTE in rating and LENDING_QUOTE in rating
    assert REVIEW_QUOTE in review
    return {
        "rating": Section(
            name="rating", doc_id=doc.doc_id, page_start=3, page_end=3,
            char_start=starts[2], char_end=starts[2] + len(rating), text=rating,
            scope="institution",
        ),
        "illegal_credit_practices_review": Section(
            name="illegal_credit_practices_review", doc_id=doc.doc_id, page_start=14,
            page_end=14, char_start=starts[13], char_end=starts[13] + len(review),
            text=review, scope="institution",
        ),
    }


# ----------------------------------------------------------------------------- schema


def _check_schema(schema: dict[str, Any], fields: list[str]) -> None:
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(fields) == set(schema["properties"])
    for name in fields:
        prop = schema["properties"][name]
        assert prop["type"] == "object"
        assert prop["additionalProperties"] is False
        assert prop["required"] == ["value", "status", "quote", "page"]
        assert set(prop["properties"]) == {"value", "status", "quote", "page"}
        assert prop["properties"]["status"]["enum"] == STATUSES
        value = prop["properties"]["value"]
        assert "null" in value["type"], f"{name} value must be nullable"
        spec = load_fields()["fields"][name]
        if spec["type"] == "enum":
            assert value["enum"] == spec["allowed"] + [None]
        if spec["type"] == "set":
            assert value["items"]["enum"] == spec["allowed"]


def test_build_schema_every_group():
    groups = load_fields()["llm_field_groups"]
    assert set(groups) == {"cover_and_rating", "institution", "lending_tables", "review"}
    for name, group in groups.items():
        _check_schema(build_schema(name), group["fields"])


def test_build_schema_unknown_group():
    with pytest.raises(KeyError):
        build_schema("nope")


def test_combined_group_is_derived_union_of_listed_groups():
    listed = load_fields()["llm_field_groups"]
    # Derived in code, never listed: modules iterating the block see each field once.
    assert ALL_GROUP not in listed and prompts.split_group_names() == list(listed)
    combined = prompts.llm_field_group(ALL_GROUP)
    union_sections = {s for g in listed.values() for s in g["sections"]}
    union_fields = {f for g in listed.values() for f in g["fields"]}
    # Sections: the union in headings.yaml (document) order; fields: the union in fields.yaml order.
    assert combined["sections"] == [s for s in load_headings()["sections"] if s in union_sections]
    assert set(combined["sections"]) == union_sections and len(combined["sections"]) == len(union_sections)
    assert combined["fields"] == [f for f in load_fields()["fields"] if f in union_fields]
    assert len(combined["fields"]) == len(union_fields) == sum(len(g["fields"]) for g in listed.values())
    assert combined["groups"] == list(listed)
    assert prompts.groups_for_mode("all") == [ALL_GROUP] and prompts.groups_for_mode("split") == list(listed)
    with pytest.raises(ValueError):
        prompts.groups_for_mode("both")
    home = prompts.field_home_sections()
    assert set(home) == union_fields and home["finding_present"] == ["illegal_credit_practices_review"]
    # A fresh copy each time: callers cannot change the derived group.
    combined["fields"].append("x")
    assert "x" not in prompts.llm_field_group(ALL_GROUP)["fields"]


def _schema_stats(node: Any, depth: int = 0, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    """Properties, object nesting depth, enum members and keywords of a JSON schema."""
    stats = stats if stats is not None else {"properties": 0, "depth": 0, "enum": 0, "keywords": set()}
    if isinstance(node, list):
        for item in node:
            _schema_stats(item, depth, stats)
    elif isinstance(node, dict):
        stats["keywords"] |= set(node)
        for key, value in node.items():
            if key == "properties":
                stats["properties"] += len(value)
                stats["depth"] = max(stats["depth"], depth + 1)
                for prop in value.values():
                    _schema_stats(prop, depth + 1, stats)
            elif key == "enum":
                stats["enum"] += len(value)
            else:
                _schema_stats(value, depth, stats)
    return stats


# Keywords google-genai 2.28.0 documents as supported for response_json_schema
# (types.GenerateContentConfig.response_json_schema); it states no size or count limit.
GEMINI_JSON_SCHEMA_KEYWORDS = {
    "$id", "$defs", "$ref", "$anchor", "type", "format", "title", "description", "enum", "items",
    "prefixItems", "minItems", "maxItems", "minimum", "maximum", "anyOf", "oneOf", "properties",
    "additionalProperties", "required", "propertyOrdering",
}


def test_combined_schema_size_and_gemini_keywords():
    combined = prompts.llm_field_group(ALL_GROUP)
    schema = build_schema(ALL_GROUP)
    _check_schema(schema, combined["fields"])
    assert list(schema["properties"]) == combined["fields"] and schema["title"] == "cra_pe_all"
    port = prompts.portable_schema(schema)
    stats = _schema_stats(port)
    n = len(combined["fields"])
    # One object per field with its four slots, two object levels, as in every split schema.
    assert n == 31 and stats["properties"] == 5 * n and stats["depth"] == 2
    split = [_schema_stats(prompts.portable_schema(build_schema(g))) for g in prompts.split_group_names()]
    assert stats["properties"] == sum(x["properties"] for x in split)
    assert stats["enum"] == sum(x["enum"] for x in split)
    assert len(json.dumps(schema)) < 1.05 * sum(len(json.dumps(build_schema(g))) for g in prompts.split_group_names())
    assert stats["keywords"] <= GEMINI_JSON_SCHEMA_KEYWORDS, stats["keywords"] - GEMINI_JSON_SCHEMA_KEYWORDS


def test_schema_is_json_serializable():
    json.dumps(build_schema("review"))


def test_portable_schema_rewrites_nullable_slots():
    """Gemini's documented subset has no type arrays: nullables become anyOf."""
    from cra_extract.prompts import portable_schema

    schema = build_schema("cover_and_rating")
    port = portable_schema(schema)
    assert port["required"] == schema["required"] and port["additionalProperties"] is False
    rating = port["properties"]["overall_rating"]["properties"]["value"]
    assert "type" not in rating
    assert rating["anyOf"] == [
        {"type": "string", "enum": ["O", "S", "NI", "SN"]},
        {"type": "null"},
    ]
    page = port["properties"]["overall_rating"]["properties"]["page"]
    assert page["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert port["properties"]["overall_rating"]["properties"]["status"] == {
        "type": "string", "enum": STATUSES,
    }
    laws = portable_schema(build_schema("review"))["properties"]["laws_cited"]["properties"]["value"]
    assert laws["anyOf"][0]["type"] == "array" and laws["anyOf"][0]["items"]["enum"][0] == "ECOA_RegB"
    # Idempotent and non-mutating.
    assert portable_schema(port) == port
    assert build_schema("cover_and_rating") == schema
    json.dumps(port)


# ----------------------------------------------------------------------------- prompt


def test_build_prompt_lists_sections_and_rules(doc, sections):
    text = build_prompt(doc, sections, "cover_and_rating")
    # The header conditions on regulator and method only: no doc_id, bank name or date,
    # which are scored Group A answers (design 6.8).
    assert "Document: a CRA Performance Evaluation" in text and "FDIC_12299_20220912" not in text
    assert "SouthStar" not in text.split("=== section")[0] and "2022-09-12" not in text
    assert "Regulator: FDIC" in text
    assert "Exam method: Intermediate Small Bank" in text
    assert "=== section: rating | scope: institution | page 3 ===" in text
    assert "=== section: illegal_credit_practices_review | scope: institution | page 14 ===" in text
    assert RATING_QUOTE in text
    assert "- cd_test_rating (" in text and "allowed: O, S, NI, SN" in text
    assert PROMPT_VERSION in text
    # FDIC ISB: component ratings are printed, so no "not printed" note for them.
    assert "lending_test_rating" in text
    assert "not printed" not in text.split("- lending_test_rating")[1].split("\n")[0]


def test_build_prompt_applicability_for_small_bank(doc, sections):
    small = doc.model_copy(update={"method": "small"})
    text = build_prompt(small, sections, "cover_and_rating")
    cd_line = text.split("- cd_test_rating")[1].split("\n")[0]
    assert "FDIC Small Bank" in cd_line and "not printed" in cd_line
    lt_line = text.split("- lending_test_rating")[1].split("\n")[0]
    assert "not printed" in lt_line


@pytest.mark.parametrize("regulator,method", [("OCC", "small"), ("OCC", "isb"), ("FDIC", "small"), ("FDIC", "isb"), ("FRB", "small"), ("FRB", "isb")])
def test_prompts_never_leak_evaluation_metadata(doc, sections, regulator, method):
    """The field lines use the system vocabulary and never name a probe or the gold nulls."""
    d = doc.model_copy(update={"regulator": regulator, "method": method})
    for group in ("cover_and_rating", "institution", "lending_tables", "review", ALL_GROUP):
        text = build_prompt(d, sections, group).lower()
        for word in ("probe", "absent_from_document", "not_applicable", "applicability block", "fabrication", "gold"):
            assert word not in text, (group, regulator, method, word)


def test_schema_never_leaks_evaluation_metadata():
    """The per-field schema description is the prompt wording too (v3): before it, the schema
    sent to the model carried the data-dictionary descriptions, including "fabrication probe"
    (laws_cited, cd_test_rating) and the gold null codes."""
    for group in ("cover_and_rating", "institution", "lending_tables", "review", ALL_GROUP):
        text = json.dumps(prompts.portable_schema(build_schema(group))).lower()
        for word in ("probe", "absent_from_document", "not_applicable", "fabrication", "gold", "=="):
            assert word not in text, (group, word)


def test_derived_booleans_are_never_absent(doc, sections):
    """finding_present and rating_lowered are always true or false: the dev pilot (prompt v2)
    returned rating_lowered with status absent on FDIC_6078_20240805, where finding_present is
    false. The optional Group C attributes stay absent when there is no finding."""
    text = build_prompt(doc, sections, ALL_GROUP)
    for name in ("finding_present", "rating_lowered"):
        line = text.split(f"\n- {name} (")[1].split("\n")[0]
        assert "never absent" in line and "true or false" in line, name
    lowered = text.split("\n- rating_lowered (")[1].split("\n")[0]
    assert "including when finding_present is false" in lowered
    for name in ("finding_type", "rating_effect"):
        line = text.split(f"\n- {name} (")[1].split("\n")[0]
        assert "status absent when finding_present is false" in line, name


def test_inside_aa_lines_name_the_row_to_read(doc, sections):
    """OCC_11715_20240408 ("54.5 percent of its total loans": all_reviewed_combined, v2 said
    home_mortgage) and FDIC_12299_20220912 (no Total row; the home mortgage subtotal 75.2:
    home_mortgage, v2 said all_reviewed_combined)."""
    text = build_prompt(doc, sections, ALL_GROUP)
    share = text.split("\n- inside_aa_share_number_pct (")[1].split("\n")[0]
    assert "Total row" in share and "total loans" in share and "primary loan product" in share
    assert "subtotal over all years" in share and "not by dollar amount" in share
    product = text.split("\n- inside_aa_share_product (")[1].split("\n")[0]
    assert "even when only one product was reviewed" in product and "home mortgage subtotal" in product
    assert "quote that same row or sentence" in product


def test_build_prompt_no_sections(doc):
    text = build_prompt(doc, {}, "review")
    assert "no sections were located" in text
    assert "no sections were located" in build_prompt(doc, {}, ALL_GROUP)


def test_combined_prompt_lists_every_field_and_section_once(doc, sections):
    # Sections given out of document order are rendered in document order.
    reversed_secs = dict(reversed(list(sections.items())))
    text = build_prompt(doc, reversed_secs, ALL_GROUP)
    fields = prompts.llm_field_group(ALL_GROUP)["fields"]
    for name in fields:
        assert text.count(f"\n- {name} (") == 1, name
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert [ln[2:].split(" (")[0] for ln in lines] == fields
    for name, sec in sections.items():
        assert text.count(f"=== section: {name} | scope: {sec.scope} |") == 1
    assert text.index("=== section: rating |") < text.index("=== section: illegal_credit_practices_review |")
    assert text.count(RATING_QUOTE) == 1 and text.count(REVIEW_QUOTE) == 1
    assert "group 'all'" in text and f"all {len(fields)} fields" in text and PROMPT_VERSION in text
    for g in prompts.split_group_names():
        secs = ", ".join(prompts.llm_field_group(g)["sections"])
        assert text.count(f"Fields usually stated in the sections {secs}:") == 1
    # Applicability of the cell: FDIC Small Bank prints neither component rating.
    small = build_prompt(doc.model_copy(update={"method": "small"}), sections, ALL_GROUP)
    for name in ("lending_test_rating", "cd_test_rating"):
        line = small.split(f"\n- {name} (")[1].split("\n")[0]
        assert "FDIC Small Bank" in line and "not printed" in line
    isb_line = text.split("\n- cd_test_rating (")[1].split("\n")[0]
    assert "not printed" not in isb_line
    # The split prompts are unchanged: the split field block is a sub-block of the combined one.
    split = build_prompt(doc, sections, "review")
    assert "Fields usually stated" not in split and "Fields to extract (group 'review'):" in split


def test_system_prompt_mentions_statuses():
    for s in STATUSES:
        assert f'"{s}"' in SYSTEM_PROMPT


# ----------------------------------------------------------------------------- extract


def test_extract_maps_values_absent_and_missing(doc, sections, tmp_path):
    response = {
        "regulator": entry("FDIC", quote="FEDERAL DEPOSIT INSURANCE CORPORATION", page=1),
        "overall_rating": entry("NI", quote=RATING_QUOTE, page=99),
        "lending_test_rating": entry("S", quote=LENDING_QUOTE, page=3),
        "cd_test_rating": entry("S", quote="The Community Development Test is rated Satisfactory.", page=3),
        "prior_rating": entry(status="absent"),
        "prior_evaluation_date": entry(status="abstain"),
        "exam_date": entry("2022-09-12", quote="September 12, 2022", page=1),
        "bank_id": entry("12299", quote="Certificate Number: 12299", page=1),
        # bank_name and exam_method deliberately missing
    }
    backend = FakeBackend(response)
    values, trace = llm.extract(doc, sections, "cover_and_rating", backend, "dev", trace_dir=tmp_path)
    by = {v.field: v for v in values}
    group_fields = load_fields()["llm_field_groups"]["cover_and_rating"]["fields"]
    assert list(by) == group_fields

    assert all(v.system == "llm:fake:fake-1" for v in values)
    rating = by["overall_rating"]
    assert rating.status == "value" and rating.value == "NI"
    assert rating.page == 3, "page comes from the section the quote is found in, not the model"
    assert rating.scope == "institution"
    assert rating.char_start is not None and rating.char_start >= sections["rating"].char_start
    assert rating.notes is None

    assert by["lending_test_rating"].value == "S" and by["lending_test_rating"].page == 3
    assert by["prior_rating"].status == "absent" and by["prior_rating"].value is None
    assert by["prior_evaluation_date"].status == "abstain"
    assert by["bank_name"].status == "abstain" and by["bank_name"].notes == "missing_from_response"
    assert by["exam_method"].status == "abstain"
    # Quote not in the provided sections: keep the model's page, flag it.
    reg = by["regulator"]
    assert reg.status == "value" and reg.page == 1 and reg.scope is None
    assert reg.notes == "quote_not_located"

    assert trace.backend == "fake" and trace.model == "fake-1"
    assert trace.model_reported == "fake-1-2026"
    assert trace.section_offsets == [(s.char_start, s.char_end) for s in sections.values()]
    assert trace.response_json == response
    assert trace.cost_usd == 0.0
    assert trace.error is None
    assert len(trace.trace_id) == 16
    assert trace.trace_id == llm.make_trace_id(doc.doc_id, "cover_and_rating", "fake-1", trace.prompt_hash)
    written = llm.read_traces(tmp_path / "dev" / "fake.jsonl")
    assert len(written) == 1 and written[0].trace_id == trace.trace_id


def test_extract_maps_combined_response_to_every_field(doc, sections, tmp_path):
    response = {
        "overall_rating": entry("NI", quote=RATING_QUOTE, page=99),
        "lending_test_rating": entry("S", quote=LENDING_QUOTE, page=3),
        "total_assets_usd": entry(status="abstain"),
        "avg_ltd_ratio_pct": entry(status="absent"),
        "finding_present": entry(True, quote=REVIEW_QUOTE, page=14),
        "laws_cited": entry(["FHA", "ECOA_RegB", "FHA"], quote=REVIEW_QUOTE, page=14),
        # A quote also printed in an earlier section: located in the field's own section.
        "rating_after": entry("NI", quote="Needs to Improve", page=14),
        "rating_before": entry("X", quote=REVIEW_QUOTE, page=14),
    }
    backend = FakeBackend(response)
    values, trace = llm.extract(doc, sections, ALL_GROUP, backend, "dev", trace_dir=tmp_path)
    fields = prompts.llm_field_group(ALL_GROUP)["fields"]
    by = {v.field: v for v in values}
    assert [v.field for v in values] == fields and len(values) == 31
    assert all(v.system == "llm:fake:fake-1" and v.doc_id == doc.doc_id for v in values)
    assert by["overall_rating"].value == "NI" and by["overall_rating"].page == 3
    assert by["overall_rating"].scope == "institution" and by["overall_rating"].notes is None
    assert by["lending_test_rating"].value == "S"
    assert by["total_assets_usd"].status == "abstain" and by["avg_ltd_ratio_pct"].status == "absent"
    assert by["finding_present"].value is True and by["finding_present"].page == 14
    assert by["laws_cited"].value == ["ECOA_RegB", "FHA"]
    assert "Needs to Improve" in sections["rating"].text
    assert by["rating_after"].page == 14 and by["rating_after"].char_start >= sections["illegal_credit_practices_review"].char_start
    assert by["rating_before"].status == "abstain" and by["rating_before"].notes.startswith("invalid_value")
    # Every field the response left out abstains.
    missing = [f for f in fields if f not in response]
    assert missing and all(by[f].status == "abstain" and by[f].notes == "missing_from_response" for f in missing)
    # One call, one trace in the usual layout, recorded under the combined group.
    call = backend.calls[0]
    assert len(backend.calls) == 1 and call["schema"] == build_schema(ALL_GROUP)
    assert call["user"] == build_prompt(doc, sections, ALL_GROUP) and call["system"] == SYSTEM_PROMPT
    assert trace.field_group == ALL_GROUP and trace.mode == "located"
    assert trace.section_offsets == [(s.char_start, s.char_end) for s in sections.values()]
    assert trace.prompt_hash == llm.prompt_hash(call["system"], call["user"], call["schema"])
    assert trace.trace_id == llm.make_trace_id(doc.doc_id, ALL_GROUP, "fake-1", trace.prompt_hash)
    assert llm.read_traces(tmp_path / "dev" / "fake.jsonl")[0] == trace
    # trace_values rebuilds the 31 rows of a stored combined trace.
    rebuilt = llm.trace_values(trace)
    assert [v.field for v in rebuilt] == fields and {v.field: v.value for v in rebuilt}["laws_cited"] == ["ECOA_RegB", "FHA"]
    # A truncated combined call abstains on every field.
    class Truncating(FakeBackend):
        def _call(self, *args):
            return llm.BackendResult(text="{", parsed_json=None, stop_reason="MAX_TOKENS")

    cut, cut_trace = llm.extract(doc, sections, ALL_GROUP, Truncating({}), "dev", trace_dir=tmp_path)
    assert len(cut) == 31 and all(v.notes == "truncated" for v in cut) and cut_trace.error == "max_tokens"


def test_combined_and_split_prompts_never_share_a_hash_or_a_trace(doc, sections, tmp_path):
    def key(group: str) -> str:
        return llm.prompt_hash(SYSTEM_PROMPT, build_prompt(doc, sections, group), build_schema(group))

    split_keys = {key(g) for g in prompts.split_group_names()}
    assert len(split_keys) == 4 and key(ALL_GROUP) not in split_keys
    # The same response under the split groups is stored; replaying the combined call misses.
    for g in prompts.split_group_names():
        llm.extract(doc, sections, g, FakeBackend({}), "dev", trace_dir=tmp_path)
    replay = llm.ReplayBackend("fake", "dev", "fake-1", trace_dir=tmp_path)
    assert {t.prompt_hash for t in replay.stored()} == split_keys
    with pytest.raises(llm.TraceMissError):
        llm.extract(doc, sections, ALL_GROUP, replay, "dev", trace_dir=tmp_path)
    llm.extract(doc, sections, ALL_GROUP, FakeBackend({}), "dev", trace_dir=tmp_path)
    replay = llm.ReplayBackend("fake", "dev", "fake-1", trace_dir=tmp_path)
    values, served = llm.extract(doc, sections, ALL_GROUP, replay, "dev", trace_dir=tmp_path)
    assert replay.hits == 1 and served.prompt_hash == key(ALL_GROUP) and len(values) == 31


def test_extract_propagates_daily_limit(doc, sections, tmp_path):
    class Exhausted(FakeBackend):
        def _call(self, *args):
            raise llm.DailyLimitExceeded("fake: 1 requests today, limit 1")

    with pytest.raises(llm.DailyLimitExceeded):
        llm.extract(doc, sections, "cover_and_rating", Exhausted({}), "dev", trace_dir=tmp_path)
    assert not (tmp_path / "dev" / "fake.jsonl").exists()


def test_extract_marks_truncated_output(doc, sections, tmp_path):
    class Truncating(FakeBackend):
        def _call(self, *args):
            return llm.BackendResult(text="{\"regulator\": {\"value\": \"FD", parsed_json=None, stop_reason="max_tokens")

    values, trace = llm.extract(doc, sections, "cover_and_rating", Truncating({}), "dev", trace_dir=tmp_path)
    assert trace.error == "max_tokens" and trace.stop_reason == "max_tokens"
    assert all(v.status == "abstain" and v.notes == "truncated" for v in values)


def test_openai_backend_only_falls_back_on_response_format_errors(monkeypatch):
    import openai

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    backend = llm.OpenAICompatibleBackend("m", name="ollama", num_ctx=100_000)
    backend.sleep = lambda s: None
    assert backend.max_output_tokens == llm.DEFAULT_OLLAMA_MAX_OUTPUT_TOKENS
    calls: list[dict[str, Any]] = []

    def make_client(messages):
        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return messages(kwargs)

        return type("C", (), {"chat": type("Chat", (), {"completions": Completions()})()})()

    def bad(message):
        resp = type("R", (), {"status_code": 400, "headers": {}, "request": None})()
        return openai.BadRequestError(message, response=resp, body={"error": {"message": message}})

    def ctx_error(kwargs):
        raise bad("context length exceeded")

    backend.client = make_client(ctx_error)
    with pytest.raises(openai.BadRequestError):
        backend.complete("s", "u", {"type": "object"}, "m")
    assert backend.json_mode == "json_schema" and len(calls) == 1

    def schema_error(kwargs):
        if kwargs["response_format"]["type"] == "json_schema":
            raise bad("response_format json_schema is not supported")
        msg = type("M", (), {"content": "{}"})()
        choice = type("Ch", (), {"message": msg, "finish_reason": "stop"})()
        return type("Done", (), {"choices": [choice], "usage": None, "model": "m"})()

    calls.clear()
    backend.client = make_client(schema_error)
    result = backend.complete("s", "u", {"type": "object"}, "m")
    assert backend.json_mode == "json_object" and len(calls) == 2
    assert result.response_format == "json_object" and result.stop_reason == "stop"


def test_extract_rejects_invalid_values(doc, sections, tmp_path):
    response = {
        "finding_present": entry(True, quote=REVIEW_QUOTE, page=14),
        "finding_type": entry("fraud", quote=REVIEW_QUOTE, page=14),
        "rating_effect": entry("substantive_lowered", quote=REVIEW_QUOTE, page=14),
        "laws_cited": entry(["ECOA_RegB", "FHA", "FHA"], quote=REVIEW_QUOTE, page=14),
        "practice_basis": entry(["redlining", "HMDA"], quote=REVIEW_QUOTE, page=14),
        "rating_before": entry("S", quote=REVIEW_QUOTE, page=14),
        "rating_after": entry(None, status="value"),
        "rating_lowered": entry("yes", quote=REVIEW_QUOTE, page=14),
        "rating_lowered_level": entry("overall", status="bogus"),
        "finding_source": "not an object",
    }
    values, trace = llm.extract(doc, sections, "review", FakeBackend(response), "dev", trace_dir=tmp_path)
    by = {v.field: v for v in values}
    assert by["finding_present"].status == "value" and by["finding_present"].value is True
    assert by["finding_present"].page == 14 and by["finding_present"].scope == "institution"
    assert by["finding_type"].status == "abstain" and "invalid_value" in by["finding_type"].notes
    assert by["laws_cited"].value == ["ECOA_RegB", "FHA"]
    assert by["practice_basis"].status == "abstain"
    assert by["rating_before"].value == "S"
    assert by["rating_after"].status == "abstain" and by["rating_after"].notes == "value_missing"
    assert by["rating_lowered"].status == "abstain"
    assert by["rating_lowered_level"].status == "abstain" and "invalid_status" in by["rating_lowered_level"].notes
    assert by["finding_source"].status == "abstain" and by["finding_source"].notes == "missing_from_response"


def test_extract_date_and_number_checks(doc, sections, tmp_path):
    response = {
        "total_assets_usd": entry(1.0e9, quote="average total assets of $1.0", page=9),
        "total_assets_asof": entry("2022-13-01", quote="x", page=9),
        "lending_data_period_start": entry("2020-01-01", status="value"),
        "n_assessment_areas": entry(3.0, quote="three assessment areas", page=9),
        "n_full_scope": entry(2.5, quote="two", page=9),
        "loan_products_evaluated": entry([], quote="no loans", page=9),
    }
    values, _ = llm.extract(doc, sections, "institution", FakeBackend(response), "dev", trace_dir=tmp_path)
    by = {v.field: v for v in values}
    assert by["total_assets_usd"].status == "value" and by["total_assets_usd"].value == 1_000_000_000
    assert by["total_assets_asof"].status == "abstain"
    assert by["lending_data_period_start"].status == "value" and by["lending_data_period_start"].notes == "quote_missing"
    assert by["n_assessment_areas"].value == 3
    assert by["n_full_scope"].status == "abstain"
    assert by["loan_products_evaluated"].status == "value" and by["loan_products_evaluated"].value == []


def test_extract_non_json_response_abstains(doc, sections, tmp_path):
    values, trace = llm.extract(doc, sections, "review", FakeBackend("not json at all"), "dev", trace_dir=tmp_path)
    assert all(v.status == "abstain" for v in values)
    assert trace.error == "response_not_json" and trace.response_text == "not json at all"


def test_extract_code_fenced_json_is_parsed(doc, sections, tmp_path):
    fenced = "```json\n" + json.dumps({"overall_rating": entry("NI", quote=RATING_QUOTE, page=3)}) + "\n```"
    values, trace = llm.extract(doc, sections, "cover_and_rating", FakeBackend(fenced), "dev", trace_dir=tmp_path)
    by = {v.field: v for v in values}
    assert by["overall_rating"].value == "NI" and trace.error is None


def test_extract_call_failure_recorded(doc, sections, tmp_path):
    backend = FlakyBackend({}, lambda: StatusError(400), n_failures=1)
    backend.sleep = lambda s: None
    values, trace = llm.extract(doc, sections, "review", backend, "dev", trace_dir=tmp_path)
    assert all(v.status == "abstain" and v.notes == "call_failed" for v in values)
    assert trace.error.startswith("StatusError")
    assert llm.read_traces(tmp_path / "dev" / "fake.jsonl")[0].error == trace.error


def test_locate_quote_tolerates_whitespace(sections):
    hit = llm.locate_quote("The Lending   Test is\nrated Satisfactory.", sections)
    assert hit is not None
    sec, offset, end = hit
    assert sec.name == "rating" and sec.text[offset:].startswith("The Lending Test")
    assert sec.text[offset:end].endswith("Satisfactory.")
    # The matched span, not len(quote), sets the end when the document has extra whitespace.
    wide = Section(name="w", doc_id="d", page_start=1, page_end=1, char_start=0, char_end=50,
                   text="This institution is rated Needs to  Improve.", scope="institution")
    sec2, start2, end2 = llm.locate_quote("rated Needs to Improve.", {"w": wide})
    assert sec2.text[start2:end2] == "rated Needs to  Improve."
    assert llm.locate_quote("not in any section", sections) is None
    assert llm.locate_quote("   ", sections) is None


def test_page_from_form_feeds():
    sec = Section(name="x", doc_id="d", page_start=5, page_end=7, char_start=0, char_end=20,
                  text="aaa\fbbb\fccc", scope="institution")
    assert llm._page_of(sec, 0) == 5
    assert llm._page_of(sec, 5) == 6
    assert llm._page_of(sec, 9) == 7


# ----------------------------------------------------------------------------- replay


def test_replay_serves_stored_trace_and_raises_on_miss(doc, sections, tmp_path):
    response = {"overall_rating": entry("NI", quote=RATING_QUOTE, page=3)}
    live = FakeBackend(response, model="fake-1")
    values_live, trace_live = llm.extract(doc, sections, "cover_and_rating", live, "dev", trace_dir=tmp_path)

    replay = llm.ReplayBackend("fake", "dev", "fake-1", trace_dir=tmp_path)
    values_rep, trace_rep = llm.extract(doc, sections, "cover_and_rating", replay, "dev", trace_dir=tmp_path)
    assert replay.hits == 1 and replay.misses == 0
    assert [v.model_dump() for v in values_rep] == [v.model_dump() for v in values_live]
    assert trace_rep.trace_id == trace_live.trace_id
    assert trace_rep.response_json == response
    # A replayed trace carries the original call time and cost, not the replay's.
    assert trace_rep.called_at == trace_live.called_at and trace_rep.cost_usd == trace_live.cost_usd
    # Replay does not append a second trace.
    assert len(llm.read_traces(tmp_path / "dev" / "fake.jsonl")) == 1

    # Different field group -> different prompt -> miss.
    with pytest.raises(llm.TraceMissError):
        llm.extract(doc, sections, "review", replay, "dev", trace_dir=tmp_path)
    # Different model -> miss.
    with pytest.raises(llm.TraceMissError):
        llm.ReplayBackend("fake", "dev", "fake-2", trace_dir=tmp_path).complete("s", "u", {}, None)


def test_replay_allow_api_delegates_and_records(doc, sections, tmp_path):
    response = {"finding_present": entry(True, quote=REVIEW_QUOTE, page=14)}
    live = FakeBackend(response)
    replay = llm.ReplayBackend("fake", "dev", "fake-1", live=live, allow_api=True, trace_dir=tmp_path)
    values, trace = llm.extract(doc, sections, "review", replay, "dev", trace_dir=tmp_path)
    assert replay.misses == 1 and len(live.calls) == 1
    assert {v.field: v.value for v in values}["finding_present"] is True
    assert len(llm.read_traces(tmp_path / "dev" / "fake.jsonl")) == 1

    # The recorded trace is served on the next replay without the live backend.
    replay2 = llm.ReplayBackend("fake", "dev", "fake-1", trace_dir=tmp_path)
    _, trace2 = llm.extract(doc, sections, "review", replay2, "dev", trace_dir=tmp_path)
    assert replay2.hits == 1 and trace2.trace_id == trace.trace_id


def test_replay_skips_error_traces(doc, sections, tmp_path):
    backend = FlakyBackend({}, lambda: StatusError(400), n_failures=1)
    llm.extract(doc, sections, "review", backend, "dev", trace_dir=tmp_path)
    replay = llm.ReplayBackend("fake", "dev", "fake-1", trace_dir=tmp_path)
    with pytest.raises(llm.TraceMissError):
        llm.extract(doc, sections, "review", replay, "dev", trace_dir=tmp_path)


# ----------------------------------------------------------------------------- retry


def test_retry_fails_twice_then_succeeds():
    sleeps: list[float] = []
    backend = FlakyBackend({"a": 1}, lambda: StatusError(503), n_failures=2)
    backend.sleep = sleeps.append
    result = backend.complete("s", "u", {}, "fake-1")
    assert result.parsed_json == {"a": 1}
    assert backend.attempts == 3
    assert sleeps == [1.0, 2.0]


def test_retry_honours_retry_after():
    sleeps: list[float] = []
    backend = FlakyBackend({"a": 1}, lambda: StatusError(429, retry_after="7"), n_failures=1)
    backend.sleep = sleeps.append
    backend.complete("s", "u", {}, "fake-1")
    assert sleeps == [7.0]


def test_retry_after_http_date():
    exc = StatusError(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
    base = llm.datetime(2026, 10, 21, 7, 27, 30, tzinfo=llm.UTC).timestamp()
    assert llm._retry_after(exc, now=lambda: base) == pytest.approx(30.0)
    assert llm._retry_after(StatusError(429, retry_after="garbage")) is None


def test_limiter_counts_every_attempt(tmp_path):
    rl = llm.RateLimiter("x", per_day=2, cache_dir=tmp_path, clock=lambda: 1_700_000_000.0)
    backend = FlakyBackend({"a": 1}, lambda: StatusError(503), n_failures=1)
    backend.limiter = rl
    backend.sleep = lambda s: None
    backend.complete("s", "u", {}, "fake-1")  # one failure plus one success: two requests
    assert rl.daily_count() == 2
    with pytest.raises(llm.DailyLimitExceeded):
        backend.complete("s", "u", {}, "fake-1")


def test_retry_gives_up_after_max_attempts():
    sleeps: list[float] = []
    backend = FlakyBackend({"a": 1}, lambda: StatusError(500), n_failures=10)
    backend.sleep = sleeps.append
    backend.max_attempts = 3
    with pytest.raises(StatusError):
        backend.complete("s", "u", {}, "fake-1")
    assert backend.attempts == 3 and len(sleeps) == 2


def test_retry_does_not_retry_client_errors():
    backend = FlakyBackend({"a": 1}, lambda: StatusError(401), n_failures=1)
    backend.sleep = lambda s: None
    with pytest.raises(StatusError):
        backend.complete("s", "u", {}, "fake-1")
    assert backend.attempts == 1


def test_retry_connection_errors_and_sdk_names():
    assert llm.is_retryable(ConnectionError("boom"))
    assert llm.is_retryable(TimeoutError())
    assert llm.is_retryable(type("APIConnectionError", (Exception,), {})())
    assert not llm.is_retryable(ValueError("x"))
    assert not llm.is_retryable(StatusError(404))
    assert llm.is_retryable(StatusError(429))
    # google-genai errors expose ``code`` rather than ``status_code``.
    assert llm.is_retryable(type("ServerError", (Exception,), {"code": 503})())
    sleeps: list[float] = []
    assert llm.call_with_retry(lambda: 42, sleep=sleeps.append) == 42 and sleeps == []


def test_retry_delay_is_capped():
    sleeps: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 5:
            raise StatusError(502)
        return "ok"

    assert llm.call_with_retry(fn, max_attempts=6, base_delay=10.0, max_delay=25.0, sleep=sleeps.append) == "ok"
    assert sleeps == [10.0, 20.0, 25.0, 25.0]


# ----------------------------------------------------------------------------- rate limiter


def test_rate_limiter_sleeps_and_persists_daily_count(tmp_path):
    clock = {"t": 1_700_000_000.0}
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock["t"] += s

    rl = llm.RateLimiter("gem", per_minute=2, per_day=3, cache_dir=tmp_path, sleep=sleep, clock=lambda: clock["t"])
    rl.acquire()
    clock["t"] += 10
    rl.acquire()
    assert sleeps == []
    rl.acquire()  # third request within the minute: must wait for the first to age out
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(50.0)
    data = json.loads((tmp_path / "ratelimit_gem.json").read_text())
    assert data["count"] == 3 and data["date"] == "2023-11-14"
    # Without a daily limit nothing is persisted; the day boundary follows reset_tz.
    rl2 = llm.RateLimiter("nolimit", per_minute=5, cache_dir=tmp_path, clock=lambda: clock["t"])
    rl2.acquire()
    assert not (tmp_path / "ratelimit_nolimit.json").exists()
    from zoneinfo import ZoneInfo
    rl3 = llm.RateLimiter("pt", per_day=1, cache_dir=tmp_path, clock=lambda: 1_700_000_000.0, reset_tz=ZoneInfo("America/Los_Angeles"))
    assert rl3._today() == "2023-11-14"
    rl4 = llm.RateLimiter("utc", per_day=1, cache_dir=tmp_path, clock=lambda: 1_700_020_000.0, reset_tz=ZoneInfo("America/Los_Angeles"))
    assert rl4._today() == "2023-11-14" and llm.RateLimiter("u", per_day=1, cache_dir=tmp_path, clock=lambda: 1_700_020_000.0)._today() == "2023-11-15"
    assert rl.daily_count() == 3

    # Daily budget spent: a fresh limiter reading the same file refuses.
    rl2 = llm.RateLimiter("gem", per_minute=None, per_day=3, cache_dir=tmp_path, sleep=sleep, clock=lambda: clock["t"])
    with pytest.raises(llm.DailyLimitExceeded):
        rl2.acquire()
    # Next day the count resets.
    clock["t"] += 86_400
    rl2.acquire()
    assert rl2.daily_count() == 1


def test_backend_uses_limiter(tmp_path):
    acquired: list[int] = []
    rl = llm.RateLimiter("x", per_minute=100, cache_dir=tmp_path, sleep=lambda s: None)
    rl.acquire = lambda: acquired.append(1)  # type: ignore[method-assign]
    backend = FakeBackend({"a": 1})
    backend.limiter = rl
    backend.complete("s", "u", {}, "fake-1")
    assert acquired == [1]


# ----------------------------------------------------------------------------- traces and cost


def test_trace_jsonl_round_trip(tmp_path):
    trace = Trace(
        trace_id="abc", doc_id="d", field_group="review", backend="fake", model="m",
        prompt_hash="h" * 64, section_offsets=[(0, 10), (20, 30)],
        response_json={"x": {"value": 1}}, response_text="{}", input_tokens=10, output_tokens=5,
        cost_usd=0.0, latency_s=0.1, called_at=llm.datetime(2026, 10, 5, tzinfo=llm.UTC),
    )
    path = llm.append_trace(trace, "dev", tmp_path)
    assert path == tmp_path / "dev" / "fake.jsonl"
    llm.append_trace(trace.model_copy(update={"trace_id": "def"}), "dev", tmp_path)
    back = llm.read_traces(path)
    assert [t.trace_id for t in back] == ["abc", "def"]
    assert back[0] == trace
    assert llm.read_traces(tmp_path / "missing.jsonl") == []


def test_prompt_hash_and_trace_id_are_stable():
    h1 = llm.prompt_hash("s", "u")
    assert h1 == llm.prompt_hash("s", "u") and h1 != llm.prompt_hash("s", "u2")
    # The schema is part of the key: a schema-only change never replays a stale trace.
    assert llm.prompt_hash("s", "u", {"a": 1}) == llm.prompt_hash("s", "u", {"a": 1})
    assert llm.prompt_hash("s", "u", {"a": 1}) != llm.prompt_hash("s", "u", {"a": 2}) != h1
    tid = llm.make_trace_id("doc", "review", "m", h1)
    assert len(tid) == 16 and tid == llm.make_trace_id("doc", "review", "m", h1)
    assert tid != llm.make_trace_id("doc", "review", "m2", h1)


def test_cost_from_prices_yaml():
    # anthropic: claude-sonnet-5-5 input 2.00, output 10.00 USD per million tokens (prices.yaml lines 10).
    assert llm.compute_cost("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(2.0 + 1.0)
    assert llm.compute_cost("claude-haiku-4-5", 7_300, 1_000) == pytest.approx(0.0073 + 0.005)
    # local models are priced at 0 under a provider prefix (prices.yaml line 29).
    assert llm.model_price("llama3.1:8b-instruct-q4_K_M") == llm.model_price("ollama/llama3.1:8b-instruct-q4_K_M")
    assert llm.compute_cost("llama3.1:8b-instruct-q4_K_M", 10_000, 1_000) == 0.0
    # gemini prices are null until filled in: cost 0, no crash.
    assert llm.compute_cost("gemini-3.8-flash", 10_000, 1_000) == 0.0
    assert llm.compute_cost("unknown-model", 10_000, 1_000) == 0.0
    assert llm.compute_cost("claude-sonnet-5-5", None, None) == 0.0


# ----------------------------------------------------------------------------- backends (offline)


def test_openai_compatible_prompt_size_assertion(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    backend = llm.OpenAICompatibleBackend("llama3.1:8b-instruct-q4_K_M", name="ollama", num_ctx=2000)
    assert backend.is_ollama and backend.base_url.startswith("http://localhost")
    backend.sleep = lambda s: None
    with pytest.raises(llm.PromptTooLargeError):
        backend.complete("s", "x" * 10_000, {}, None)
    fmt = backend._response_format(build_schema("review"))
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    backend.json_mode = "json_object"
    assert backend._response_format({}) == {"type": "json_object"}


def test_estimate_tokens_is_conservative():
    assert llm.estimate_tokens("a" * 300) >= 100


def test_gemini_backend_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError):
        llm.GeminiBackend("gemini-3.8-flash")


@pytest.mark.skipif(importlib.util.find_spec("anthropic") is None, reason="anthropic not installed")
def test_anthropic_backend_builds_output_config(monkeypatch):
    """Offline: patch the SDK client so the request shape can be inspected."""
    captured: dict[str, Any] = {}

    class FakeUsage:
        input_tokens = 100
        output_tokens = 20
        cache_creation_input_tokens = 5
        cache_read_input_tokens = 0

    class FakeBlock:
        type = "text"
        text = json.dumps({"ok": {"value": 1}})

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Msg", (), {"content": [FakeBlock()], "usage": FakeUsage(), "model": "claude-x"})()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    backend = llm.AnthropicBackend("claude-sonnet-5-5", effort="low")
    backend.client = type("C", (), {"messages": FakeMessages()})()
    result = backend.complete("sys", "user", {"type": "object"}, None)
    assert captured["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "low"}
    assert captured["system"] == "sys" and captured["model"] == "claude-sonnet-5-5"
    assert "temperature" not in captured
    assert result.parsed_json == {"ok": {"value": 1}}
    assert result.input_tokens == 105 and result.output_tokens == 20 and result.model_reported == "claude-x"


def test_make_backend_unknown():
    with pytest.raises(ValueError):
        llm.make_backend("nope", "m")


def test_sample_doc_and_whole_document_sections():
    doc = llm.sample_doc("FDIC_12299_SouthStar_2022_ISB_NI_fairlending")
    assert doc.regulator == "FDIC" and doc.bank_id == "12299" and doc.method == "isb"
    # Manifest-style doc_id so the smoke trace is reusable by run.py.
    assert doc.doc_id == "FDIC_12299_20220912" and doc.exam_date == date(2022, 9, 12)
    assert llm.sample_doc("OCC_11715_LemontNB_2024_SN").method == "small"
    secs = llm.sections_from_text(doc.doc_id, "p1\fp2\fp3")
    assert secs["document"].page_end == 3 and secs["document"].char_end == 8
    # pdftotext output ends with a form feed: the trailing empty page is not counted.
    assert llm.sections_from_text(doc.doc_id, "p1\fp2\fp3\f")["document"].page_end == 3


@pytest.mark.network
@pytest.mark.skipif("CRA_LIVE_LLM" not in __import__("os").environ, reason="set CRA_LIVE_LLM=1 to run")
def test_smoke_live_backend():
    import os

    backend = os.environ.get("CRA_SMOKE_BACKEND", "anthropic")
    model = os.environ.get("CRA_SMOKE_MODEL", "claude-haiku-4-5")
    assert llm.main(["--smoke", "--backend", backend, "--model", model]) == 0


# ----------------------------------------------------------------------------- effort


def test_trace_round_trip_with_effort(tmp_path):
    trace = Trace(
        trace_id="abc", doc_id="d", field_group="review", backend="fake", model="m",
        prompt_hash="h" * 64, called_at=llm.datetime(2026, 10, 8, tzinfo=llm.UTC), effort="low",
    )
    path = llm.append_trace(trace, "dev", tmp_path)
    back = llm.read_traces(path)[0]
    assert back == trace and back.effort == "low"
    assert json.loads(path.read_text().splitlines()[0])["effort"] == "low"
    # A trace written before the field existed reads back with effort None.
    old = json.loads(trace.model_dump_json())
    del old["effort"]
    assert Trace.model_validate_json(json.dumps(old)).effort is None


def test_extract_records_effort_and_replay_keys_on_it(doc, sections, tmp_path):
    response = {"overall_rating": entry("NI", quote=RATING_QUOTE, page=3)}
    live = FakeBackend(response)
    live.effort = "low"
    replay = llm.ReplayBackend("fake", "dev", "fake-1", live=live, allow_api=True, trace_dir=tmp_path, effort="low")
    _, trace = llm.extract(doc, sections, "cover_and_rating", replay, "dev", trace_dir=tmp_path)
    assert trace.effort == "low" and len(live.calls) == 1
    assert trace.trace_id == llm.make_trace_id(doc.doc_id, "cover_and_rating", "fake-1", trace.prompt_hash, "low")
    assert trace.trace_id != llm.make_trace_id(doc.doc_id, "cover_and_rating", "fake-1", trace.prompt_hash)
    # The live trace joined the replay index: the same prompt in the same run is not resent.
    _, again = llm.extract(doc, sections, "cover_and_rating", replay, "dev", trace_dir=tmp_path)
    assert len(live.calls) == 1 and replay.hits == 1 and again.trace_id == trace.trace_id
    assert len(llm.read_traces(tmp_path / "dev" / "fake.jsonl")) == 1
    # A replay at another effort never serves the low-effort trace.
    high = llm.ReplayBackend("fake", "dev", "fake-1", trace_dir=tmp_path, effort="high")
    with pytest.raises(llm.TraceMissError):
        llm.extract(doc, sections, "cover_and_rating", high, "dev", trace_dir=tmp_path)
    low = llm.ReplayBackend("fake", "dev", "fake-1", trace_dir=tmp_path, effort="low")
    values, served = llm.extract(doc, sections, "cover_and_rating", low, "dev", trace_dir=tmp_path)
    assert served.effort == "low" and low.hits == 1 and low.has("fake-1", trace.prompt_hash)
    assert {v.field: v.value for v in values}["overall_rating"] == "NI"
    # The live backend must run at the replay's effort.
    with pytest.raises(llm.LLMError):
        llm.ReplayBackend("fake", "dev", "fake-1", live=FakeBackend(response), allow_api=True, trace_dir=tmp_path, effort="low")


def test_extract_system_name_override(doc, sections, tmp_path):
    backend = FakeBackend({"overall_rating": entry("NI", quote=RATING_QUOTE, page=3)})
    values, _ = llm.extract(doc, sections, "cover_and_rating", backend, "dev", trace_dir=tmp_path, system="llm:fake:fake-1:whole")
    assert {v.system for v in values} == {"llm:fake:fake-1:whole"}
    assert llm.system_name(backend) == "llm:fake:fake-1" and llm.system_name(backend, "whole") == "llm:fake:fake-1:whole"


def test_default_effort_per_backend():
    assert llm.DEFAULT_EFFORT == {"gemini": "low", "anthropic": "low", "openai_compat": "low", "ollama": None}


@pytest.mark.skipif(importlib.util.find_spec("google.genai") is None, reason="google-genai not installed")
def test_gemini_backend_sends_effort_as_thinking_level(monkeypatch):
    """Offline: the client is built with a dummy key and never called."""
    from google.genai import types

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    backend = llm.GeminiBackend("gemini-3.8-flash", effort="low")
    config = backend._config("sys", build_schema("review"))
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config.response_mime_type == "application/json" and config.system_instruction == "sys"
    assert llm.GeminiBackend("gemini-3.8-flash")._config("s", {}).thinking_config is None
    with pytest.raises(llm.LLMError):
        llm.GeminiBackend("gemini-3.8-flash", effort="max")


def test_openai_compatible_sends_reasoning_effort():
    captured: list[dict[str, Any]] = []

    class FakeCompletions:
        def create(self, **kwargs):
            captured.append(kwargs)
            msg = type("M", (), {"content": "{}"})()
            choice = type("C", (), {"message": msg, "finish_reason": "stop"})()
            usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 2})()
            return type("R", (), {"choices": [choice], "usage": usage, "model": "m"})()

    backend = llm.OpenAICompatibleBackend("m", base_url="http://localhost:9/v1", api_key="test-key-not-real", effort="low")
    backend.client = type("Cl", (), {"chat": type("Ch", (), {"completions": FakeCompletions()})()})()
    backend.complete("s", "u", {"title": "t"}, None)
    assert captured[-1]["reasoning_effort"] == "low"
    backend.effort = None
    backend.complete("s", "u", {"title": "t"}, None)
    assert "reasoning_effort" not in captured[-1]


# ----------------------------------------------------------------------------- closed book


def test_closed_book_prompt_has_only_bank_name_and_date(doc, sections):
    text = build_closed_book_prompt(doc)
    assert "Bank: SouthStar Bank, S.S.B." in text and "Evaluation date: 2022-09-12" in text
    assert CLOSED_BOOK_PROMPT_VERSION in text and PROMPT_VERSION == "v5"
    for leaked in (doc.doc_id, "FDIC", "Intermediate", "Regulator", "Certificate", "12299"):
        assert leaked not in text and leaked not in CLOSED_BOOK_SYSTEM_PROMPT
    for sec in sections.values():
        lines = [ln.strip() for ln in sec.text.splitlines() if len(ln.strip()) >= 25]
        assert lines and not any(ln in text for ln in lines)
    schema = build_closed_book_schema()
    assert schema["required"] == ["overall_rating", "finding_present"] and schema["additionalProperties"] is False
    for prop in schema["properties"].values():
        assert set(prop["properties"]) == {"value", "status"} and prop["properties"]["status"]["enum"] == ["value", "abstain"]
    with pytest.raises(ValueError):
        build_closed_book_prompt(doc.model_copy(update={"exam_date": None}))


def test_extract_closed_book_maps_and_traces(doc, tmp_path):
    backend = FakeBackend({
        "overall_rating": {"value": "NI", "status": "value"},
        "finding_present": {"value": None, "status": "absent"},
    })
    values, trace = llm.extract_closed_book(doc, backend, "test", trace_dir=tmp_path)
    by = {v.field: v for v in values}
    assert set(by) == {"overall_rating", "finding_present"}
    assert by["overall_rating"].status == "value" and by["overall_rating"].value == "NI"
    assert by["overall_rating"].quote is None and by["overall_rating"].system == "llm:fake:fake-1:closedbook"
    assert by["finding_present"].status == "abstain" and by["finding_present"].notes.startswith("invalid_status")
    call = backend.calls[0]
    assert call["system"] == CLOSED_BOOK_SYSTEM_PROMPT and call["user"] == build_closed_book_prompt(doc)
    assert trace.field_group == "closedbook" and trace.section_offsets == []
    assert trace.prompt_hash == llm.prompt_hash(call["system"], call["user"], call["schema"], CLOSED_BOOK_PROMPT_VERSION)
    assert trace.prompt_hash != llm.prompt_hash(call["system"], call["user"], call["schema"])
    assert llm.read_traces(tmp_path / "test" / "fake.jsonl")[0].trace_id == trace.trace_id
    # Replay serves the closed-book trace by its own hash.
    replay = llm.ReplayBackend("fake", "test", "fake-1", trace_dir=tmp_path)
    again, _ = llm.extract_closed_book(doc, replay, "test", trace_dir=tmp_path)
    assert replay.hits == 1 and [v.value for v in again] == [v.value for v in values]
    bad = llm.map_closed_book(doc.doc_id, {"overall_rating": {"value": "X", "status": "value"}}, "s")
    assert bad[0].status == "abstain" and bad[0].notes.startswith("invalid_value")
    assert bad[1].notes == "missing_from_response"


def test_trace_values_rebuilds_rows(doc, sections, tmp_path):
    response = {"overall_rating": entry("NI", quote=RATING_QUOTE, page=3)}
    _, trace = llm.extract(doc, sections, "cover_and_rating", FakeBackend(response), "dev", trace_dir=tmp_path)
    rows = {v.field: v for v in llm.trace_values(trace)}
    assert rows["overall_rating"].value == "NI" and rows["overall_rating"].system == "llm:fake:fake-1"
    assert rows["bank_name"].status == "abstain"
    failed = trace.model_copy(update={"error": "max_tokens"})
    assert all(v.status == "abstain" and v.notes == "max_tokens" for v in llm.trace_values(failed))
    _, cb = llm.extract_closed_book(doc, FakeBackend({"overall_rating": {"value": "S", "status": "value"}}), "dev", trace_dir=tmp_path)
    cb_rows = llm.trace_values(cb)
    assert [v.field for v in cb_rows] == ["overall_rating", "finding_present"]
    assert cb_rows[0].system == "llm:fake:fake-1:closedbook" and cb_rows[0].value == "S"


DAILY_429 = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 500, model: gemini-3.5-flash-lite "
    "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier. Please retry in 23h19m52.36s."
)
MINUTE_429 = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 15 quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier. "
    "Please retry in 12.5s."
)


def _code_error(message: str, code: int = 429):
    return type("ClientError", (Exception,), {"code": code})(message)


def test_daily_quota_hint_reads_gemini_messages():
    assert llm.daily_quota_hint(_code_error(DAILY_429)) == pytest.approx(23 * 3600 + 19 * 60 + 52.36)
    assert llm.daily_quota_hint(_code_error(MINUTE_429)) is None
    assert llm.daily_quota_hint(_code_error(DAILY_429, code=503)) is None
    assert llm.daily_quota_hint(StatusError(429)) is None
    assert not llm.is_retryable(_code_error(DAILY_429))
    assert llm.is_retryable(_code_error(MINUTE_429))


def test_daily_quota_429_halts_without_retrying(tmp_path):
    rl = llm.RateLimiter("x", per_day=500, cache_dir=tmp_path, clock=lambda: 1_700_000_000.0)
    backend = FlakyBackend({"a": 1}, lambda: _code_error(DAILY_429), n_failures=10)
    backend.limiter = rl
    backend.sleep = lambda s: None
    with pytest.raises(llm.DailyLimitExceeded):
        backend.complete("s", "u", {}, "fake-1")
    assert backend.attempts == 1  # the daily-cap 429 was not retried
    assert rl.daily_count() == 500  # later runs on the same day halt before sending
