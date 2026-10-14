"""Prompt and JSON-schema construction for the LLM extractor (systems S3 and S4).

The schema is derived from ``config/fields.yaml`` so that the model can only
return allowed enum members and set elements. Every field is reported as an
object with ``value``, ``status``, ``quote`` and ``page``; the grounding checks
in ``evaluate.py`` consume the quote. The prompt lists the located sections
with their scope labels and page numbers and states the applicability rules
for the document's (regulator, exam method) cell.

Field groups: ``fields.yaml`` lists four groups under ``llm_field_groups``
(``cover_and_rating``, ``institution``, ``lending_tables``, ``review``), one call each in the
``split`` mode. The combined group ``all`` (:data:`ALL_GROUP`) is derived here from those four
rather than listed in the file (:func:`combined_field_group`): its sections are their union in
``headings.yaml`` order and its fields their union in ``fields.yaml`` order, so one call per
document asks for every LLM field (the ``all`` mode, the run stage's default). Modules that
iterate ``llm_field_groups`` therefore still see every field exactly once.

``PROMPT_VERSION`` is bumped whenever the wording changes so that traces from
different prompt versions never get mixed up. The split-group prompts are unchanged by the
combined group; an ``all`` prompt differs from every split prompt in its field block and its
schema, so the two modes never share a prompt hash.

The closed-book contamination baseline (analysis plan, "Leakage and contamination
controls") has its own prompt, schema and ``CLOSED_BOOK_PROMPT_VERSION``: the model sees
only the bank name and the exam date, never PE text, and is asked for ``overall_rating``
and ``finding_present``. Changing it leaves ``PROMPT_VERSION`` untouched.
"""

from __future__ import annotations

from typing import Any

from cra_extract.config import applicability, field_spec, load_fields, load_headings
from cra_extract.models import DocRecord, Section

# v2 (2026-10-07): header no longer carries the doc_id, bank name or evaluation date
# (scored Group A answers); field lines use prompt_description; the JSON schema is part
# of the trace key.
# v3 (2026-10-14, dev tuning): field lines of finding_present and rating_lowered say they are
# always true or false, never absent; inside_aa_share_number_pct and inside_aa_share_product
# say which row to read (the combined figure, else the primary product's pooled subtotal) and
# that the product names the row quoted; the JSON schema's per-field description uses
# prompt_description too, so the schema no longer carries data-dictionary wording.
# v4 (2026-10-14): total_assets_usd prefers the FDIC LTD table figure in thousands.
# v5 (2026-10-14, dev tuning): lending_data_period_start and _end are status absent when only
# years are given for the primary product (except "calendar years YYYY through YYYY"), and
# never the evaluation period; total_assets_usd quotes the bank's own LTD table row.
PROMPT_VERSION = "v5"

STATUSES = ["value", "absent", "abstain"]

# The combined field group (every LLM field in one call) and the two field-group modes of the
# run stage: "all" sends one call per document, "split" one call per listed group.
ALL_GROUP = "all"
GROUP_MODES = ("all", "split")

SYSTEM_PROMPT = """You extract structured fields from a Community Reinvestment Act (CRA) \
Performance Evaluation published by a US bank regulator (OCC, FDIC or Federal Reserve).

Rules:
1. Use only the document sections provided. Do not use outside knowledge about the bank.
2. For every field return an object with "value", "status", "quote" and "page".
3. status "value": the document states the value. "quote" must be a short verbatim span \
copied exactly from the provided text (no paraphrase, no ellipsis, keep original spelling) \
and "page" the page number of the section where the quote appears.
4. status "absent": the document does not state this field, or the applicability rules say \
it is not printed for this regulator and exam method. Set value and quote to null.
5. status "abstain": you cannot decide from the provided text. Set value and quote to null.
6. Never guess. A value that is not supported by a verbatim quote is wrong.
7. Institution-level fields must be supported by institution-level text, not by a state, \
metropolitan area or single assessment area section.
8. Dates are ISO strings (YYYY-MM-DD). Ratings use the codes O (Outstanding), \
S (Satisfactory), NI (Needs to Improve), SN (Substantial Noncompliance).
9. Return only the JSON object."""

_TYPE_NOTES: dict[str, str] = {
    "date": "ISO date string YYYY-MM-DD",
    "int": "integer",
    "float": "number",
    "bool": "true or false",
    "str": "string",
    "enum": "one of the allowed codes",
    "set": "list of allowed codes (may be empty only if the document says none)",
}


def split_group_names() -> list[str]:
    """The field groups listed under ``llm_field_groups`` in ``fields.yaml``, in file order
    (the calls of the ``split`` mode). ``all`` is never one of them: it is derived."""
    return [g for g in load_fields()["llm_field_groups"] if g != ALL_GROUP]


def llm_field_group(name: str) -> dict[str, Any]:
    """Return the ``llm_field_groups`` entry for ``name`` from ``fields.yaml``, or the derived
    combined group for ``all`` (:func:`combined_field_group`)."""
    if name == ALL_GROUP:
        return combined_field_group()
    groups = load_fields()["llm_field_groups"]
    if name not in groups:
        raise KeyError(f"unknown llm field group {name!r}; known: {sorted([*groups, ALL_GROUP])}")
    return groups[name]


def combined_field_group() -> dict[str, Any]:
    """The ``all`` group, derived from the listed groups.

    ``sections``: the union of the groups' sections in ``headings.yaml`` order, which is the
    order they appear in an FDIC or FRB PE (cover, rating, description_of_institution,
    description_of_assessment_area, scope_of_evaluation, ltd_ratio, aa_concentration,
    illegal_credit_practices_review; OCC prints the lending tables first, so the prompt
    renders the located sections by their position in each document). ``fields``: the union
    of the groups' fields in ``fields.yaml`` order. ``groups``: the listed groups it combines.
    A section or field missing from those files keeps its group order after the rest.
    """
    groups = split_group_names()
    specs = [llm_field_group(g) for g in groups]
    wanted_sections = {s for spec in specs for s in spec["sections"]}
    sections = [s for s in load_headings().get("sections", {}) if s in wanted_sections]
    wanted_fields = {f for spec in specs for f in spec["fields"]}
    fields = [f for f in load_fields()["fields"] if f in wanted_fields]
    for spec in specs:
        sections += [s for s in spec["sections"] if s not in sections]
        fields += [f for f in spec["fields"] if f not in fields]
    return {"sections": sections, "fields": fields, "groups": groups}


def groups_for_mode(group_mode: str) -> list[str]:
    """Field groups called per document: ``["all"]`` for ``all``, the listed groups for
    ``split``."""
    if group_mode == ALL_GROUP:
        return [ALL_GROUP]
    if group_mode == "split":
        return split_group_names()
    raise ValueError(f"unknown field-group mode {group_mode!r}; use one of {', '.join(GROUP_MODES)}")


def field_home_sections() -> dict[str, list[str]]:
    """Field name to the sections of the listed group that extracts it (first group wins)."""
    out: dict[str, list[str]] = {}
    for g in split_group_names():
        spec = llm_field_group(g)
        for name in spec["fields"]:
            out.setdefault(name, list(spec["sections"]))
    return out


def _value_schema(spec: dict[str, Any]) -> dict[str, Any]:
    """JSON schema for the ``value`` slot of one field, always nullable."""
    ftype = spec["type"]
    allowed = spec.get("allowed")
    if ftype == "enum":
        return {"type": ["string", "null"], "enum": list(allowed) + [None]}
    if ftype == "set":
        return {
            "type": ["array", "null"],
            "items": {"type": "string", "enum": list(allowed)},
        }
    if ftype == "bool":
        return {"type": ["boolean", "null"]}
    if ftype == "int":
        return {"type": ["integer", "null"]}
    if ftype == "float":
        return {"type": ["number", "null"]}
    if ftype == "date":
        return {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD"}
    return {"type": ["string", "null"]}


def build_schema(field_group: str) -> dict[str, Any]:
    """JSON schema for one LLM field group.

    The top-level object has one required property per field in the group. Each
    property is an object ``{value, status, quote, page}`` with
    ``additionalProperties: false`` so that it is accepted by the strict modes of
    the Anthropic, Gemini and OpenAI-compatible structured-output APIs. The property
    description is the field's ``prompt_description`` when ``fields.yaml`` defines one, as in
    the prompt's field lines, so the schema never carries data-dictionary wording. The combined
    group ``all`` gets one property per field of every listed group: 31 fields, 155
    properties counting the four slots of each, two levels of objects (plus the items of
    the set fields), 173 enum members and about 15,100 characters of JSON, the size of the
    four split schemas together.
    """
    group = llm_field_group(field_group)
    properties: dict[str, Any] = {}
    for name in group["fields"]:
        spec = field_spec(name)
        properties[name] = {
            "type": "object",
            "description": spec.get("prompt_description", spec.get("description", name)),
            "properties": {
                "value": _value_schema(spec),
                "status": {"type": "string", "enum": list(STATUSES)},
                "quote": {"type": ["string", "null"]},
                "page": {"type": ["integer", "null"]},
            },
            "required": ["value", "status", "quote", "page"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "title": f"cra_pe_{field_group}",
        "properties": properties,
        "required": list(group["fields"]),
        "additionalProperties": False,
    }


def portable_schema(schema: Any) -> Any:
    """Rewrite nullable slots for APIs whose JSON-schema subset lacks type arrays.

    ``{"type": ["string", "null"], "enum": [..., None]}`` becomes
    ``{"anyOf": [{"type": "string", "enum": [...]}, {"type": "null"}]}``. Every
    other key is copied through recursively, so the result is still a valid
    JSON schema and validates the same documents. Used by the Gemini backend;
    the OpenAI-compatible strict mode and the Anthropic API take the canonical
    form from ``build_schema`` as is.
    """
    if isinstance(schema, list):
        return [portable_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: portable_schema(v) for k, v in schema.items()}
    types = out.get("type")
    if isinstance(types, list) and "null" in types:
        non_null = [t for t in types if t != "null"]
        base = {k: v for k, v in out.items() if k not in {"type", "enum", "description"}}
        base["type"] = non_null[0] if len(non_null) == 1 else non_null
        if "enum" in out:
            base["enum"] = [e for e in out["enum"] if e is not None]
        rewritten: dict[str, Any] = {"anyOf": [base, {"type": "null"}]}
        if "description" in out:
            rewritten["description"] = out["description"]
        return rewritten
    return out


def _field_lines(field_group: str, doc: DocRecord) -> list[str]:
    """One bullet per field: type, allowed values, description and the applicability rule.

    The text comes from ``prompt_description`` when ``fields.yaml`` defines one (fields
    whose data-dictionary ``description`` names a fabrication probe or uses the gold null
    vocabulary, and fields whose instructions were tuned on dev) and from ``description``
    otherwise, so the model never reads evaluation metadata.
    """
    return [_field_line(name, doc) for name in llm_field_group(field_group)["fields"]]


def _field_line(name: str, doc: DocRecord) -> str:
    """The bullet of one field, with the applicability rule of the document's cell."""
    spec = field_spec(name)
    parts = [f"- {name} ({_TYPE_NOTES.get(spec['type'], spec['type'])}"]
    if spec.get("allowed"):
        parts.append(f"; allowed: {', '.join(spec['allowed'])}")
    parts.append(f"): {spec.get('prompt_description', spec.get('description', '')).strip()}")
    rule = applicability(name, doc.regulator, doc.method)
    if rule == "absent_from_document":
        parts.append(
            f" For {doc.regulator} {_method_label(doc.method)} evaluations this field is"
            " not printed; return status absent unless the text clearly states it."
        )
    elif rule == "not_applicable":
        parts.append(" Not applicable to this document; return status absent.")
    return "".join(parts)


def _combined_field_block(doc: DocRecord) -> list[str]:
    """Field block of the ``all`` prompt: every field once, under one line per listed group
    naming the sections that usually state its fields."""
    combined = combined_field_group()
    lines = [
        f"Fields to extract (group {ALL_GROUP!r}: all {len(combined['fields'])} fields in one "
        "JSON object). Each list below names the sections that usually state its fields; use "
        "any provided section that states a field."
    ]
    seen: set[str] = set()
    for g in combined["groups"]:
        spec = llm_field_group(g)
        names = [n for n in spec["fields"] if n not in seen]
        if not names:
            continue
        seen.update(names)
        lines.append(f"Fields usually stated in the sections {', '.join(spec['sections'])}:")
        lines += [_field_line(n, doc) for n in names]
    return lines


def _method_label(method: str) -> str:
    return "Intermediate Small Bank" if method == "isb" else "Small Bank"


def build_prompt(doc: DocRecord, sections: dict[str, Section], field_group: str) -> str:
    """User prompt: document header, field rules and the located sections.

    ``sections`` is the set of located sections the caller wants the model to
    see (normally those listed for the group in ``fields.yaml``, or a single
    whole-document section when the locator missed). Sections are rendered in
    the order given, each with its scope label and page range. The header states
    only the regulator and exam method, which the applicability rules depend on;
    the bank name, bank id and evaluation date are scored Group A fields and are
    not shown (design 6.8), so the model must read them from the cover section.

    For the combined group ``all`` the field block lists every field once, grouped by the
    listed group that extracts it (:func:`_combined_field_block`), each field line carrying
    the applicability rule of the (regulator, exam method) cell, and the sections (each name
    once, as a dict) are rendered in document order, by start offset. The split-group
    prompts are byte-identical to the ones before the combined group existed.
    """
    header = [
        "Document: a CRA Performance Evaluation",
        f"Regulator: {doc.regulator}",
        f"Exam method: {_method_label(doc.method)}",
    ]
    if field_group == ALL_GROUP:
        fields_block = _combined_field_block(doc)
        sections_note = (
            "Sections located in the document, each listed once in document order. Each section "
            "is labeled with its scope (institution, state, msa, assessment_area, appendix) and "
            "page range. Quotes must be copied verbatim from these sections."
        )
        sections = dict(sorted(sections.items(), key=lambda kv: (kv[1].char_start, kv[1].char_end)))
    else:
        fields_block = [f"Fields to extract (group {field_group!r}):", *_field_lines(field_group, doc)]
        sections_note = (
            "Sections located in the document. Each section is labeled with its scope "
            "(institution, state, msa, assessment_area, appendix) and page range. "
            "Quotes must be copied verbatim from these sections."
        )

    body = [
        "\n".join(header),
        "",
        *fields_block,
        "",
        sections_note,
    ]
    if not sections:
        body.append("\n(no sections were located; return status abstain for every field)")
    for name, sec in sections.items():
        pages = (
            f"page {sec.page_start}"
            if sec.page_start == sec.page_end
            else f"pages {sec.page_start}-{sec.page_end}"
        )
        body.append(f"\n=== section: {name} | scope: {sec.scope} | {pages} ===")
        body.append(sec.text.strip())
    body.append("\n=== end of sections ===")
    body.append(f"\nReturn the JSON object now. (prompt {PROMPT_VERSION})")
    return "\n".join(body)


# ----------------------------------------------------------------------------- closed book

# cb1 (2026-10-08): first version. Bank name and exam date only; best answer requested,
# abstain allowed when the model has no basis at all.
CLOSED_BOOK_PROMPT_VERSION = "cb1"
CLOSED_BOOK_GROUP = "closedbook"
CLOSED_BOOK_FIELDS = ("overall_rating", "finding_present")
CLOSED_BOOK_STATUSES = ["value", "abstain"]

CLOSED_BOOK_SYSTEM_PROMPT = """You answer questions about Community Reinvestment Act (CRA) \
Performance Evaluations of US banks from your own knowledge. No document is provided.

Rules:
1. For every field return an object with "value" and "status".
2. status "value": give your best answer in "value", even when you are not certain.
3. status "abstain": use it only when you cannot give any answer. Set value to null.
4. Ratings use the codes O (Outstanding), S (Satisfactory), NI (Needs to Improve), \
SN (Substantial Noncompliance).
5. Return only the JSON object."""

_CLOSED_BOOK_QUESTIONS: dict[str, str] = {
    "overall_rating": "(one of O, S, NI, SN): the institution's overall CRA rating in the "
    "Performance Evaluation with this evaluation date.",
    "finding_present": "(true or false): whether that Performance Evaluation states that "
    "examiners identified discriminatory or other illegal credit practices that were "
    "considered in the rating.",
}


def build_closed_book_schema() -> dict[str, Any]:
    """JSON schema for the closed-book call: ``{value, status}`` per field, no quote or page.

    The value slots come from ``fields.yaml`` (the same enum and boolean types as the main
    schema); status is restricted to ``value`` and ``abstain``, since a document-absence
    status has no meaning without a document.
    """
    properties: dict[str, Any] = {}
    for name in CLOSED_BOOK_FIELDS:
        spec = field_spec(name)
        properties[name] = {
            "type": "object",
            "description": _CLOSED_BOOK_QUESTIONS[name],
            "properties": {
                "value": _value_schema(spec),
                "status": {"type": "string", "enum": list(CLOSED_BOOK_STATUSES)},
            },
            "required": ["value", "status"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "title": f"cra_pe_{CLOSED_BOOK_GROUP}",
        "properties": properties,
        "required": list(CLOSED_BOOK_FIELDS),
        "additionalProperties": False,
    }


def build_closed_book_prompt(doc: DocRecord) -> str:
    """User prompt for the closed-book baseline: the bank name and the exam date only.

    No regulator, exam method, doc_id or PE text is shown (analysis plan, "Leakage and
    contamination controls"). Raises ``ValueError`` when the record has no exam date.
    """
    if doc.exam_date is None:
        raise ValueError(f"{doc.doc_id}: the closed-book prompt needs an exam date")
    lines = [
        "No document is provided. Answer from what you know about this bank's CRA "
        "Performance Evaluation.",
        f"Bank: {doc.bank_name.strip()}",
        f"Evaluation date: {doc.exam_date.isoformat()}",
        "",
        "Fields:",
        *(f"- {name} {_CLOSED_BOOK_QUESTIONS[name]}" for name in CLOSED_BOOK_FIELDS),
        "",
        f"Return the JSON object now. (closed-book prompt {CLOSED_BOOK_PROMPT_VERSION})",
    ]
    return "\n".join(lines)
