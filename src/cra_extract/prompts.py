"""Prompt and JSON-schema construction for the LLM extractor (systems S3 and S4).

The schema is derived from ``config/fields.yaml`` so that the model can only
return allowed enum members and set elements. Every field is reported as an
object with ``value``, ``status``, ``quote`` and ``page``; the grounding checks
in ``evaluate.py`` consume the quote. The prompt lists the located sections
with their scope labels and page numbers and states the applicability rules
for the document's (regulator, exam method) cell.

``PROMPT_VERSION`` is bumped whenever the wording changes so that traces from
different prompt versions never get mixed up.
"""

from __future__ import annotations

from typing import Any

from cra_extract.config import applicability, field_spec, load_fields
from cra_extract.models import DocRecord, Section

PROMPT_VERSION = "v1"

STATUSES = ["value", "absent", "abstain"]

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


def llm_field_group(name: str) -> dict[str, Any]:
    """Return the ``llm_field_groups`` entry for ``name`` from ``fields.yaml``."""
    groups = load_fields()["llm_field_groups"]
    if name not in groups:
        raise KeyError(f"unknown llm field group {name!r}; known: {sorted(groups)}")
    return groups[name]


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
    the Anthropic, Gemini and OpenAI-compatible structured-output APIs.
    """
    group = llm_field_group(field_group)
    properties: dict[str, Any] = {}
    for name in group["fields"]:
        spec = field_spec(name)
        properties[name] = {
            "type": "object",
            "description": spec.get("description", name),
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
    """One bullet per field: type, allowed values, description and the applicability rule."""
    lines: list[str] = []
    for name in llm_field_group(field_group)["fields"]:
        spec = field_spec(name)
        parts = [f"- {name} ({_TYPE_NOTES.get(spec['type'], spec['type'])}"]
        if spec.get("allowed"):
            parts.append(f"; allowed: {', '.join(spec['allowed'])}")
        parts.append(f"): {spec.get('description', '').strip()}")
        rule = applicability(name, doc.regulator, doc.method)
        if rule == "absent_from_document":
            parts.append(
                f" For {doc.regulator} {_method_label(doc.method)} evaluations this field is"
                " not printed; return status absent unless the text clearly states it."
            )
        elif rule == "not_applicable":
            parts.append(" Not applicable to this document; return status absent.")
        lines.append("".join(parts))
    return lines


def _method_label(method: str) -> str:
    return "Intermediate Small Bank" if method == "isb" else "Small Bank"


def build_prompt(doc: DocRecord, sections: dict[str, Section], field_group: str) -> str:
    """User prompt: document header, field rules and the located sections.

    ``sections`` is the set of located sections the caller wants the model to
    see (normally those listed for the group in ``fields.yaml``, or a single
    whole-document section when the locator missed). Sections are rendered in
    the order given, each with its scope label and page range.
    """
    header = [
        f"Document: {doc.doc_id}",
        f"Regulator: {doc.regulator}",
        f"Exam method: {_method_label(doc.method)}",
        f"Bank name (from the index, confirm against the text): {doc.bank_name}",
    ]
    if doc.exam_date is not None:
        header.append(f"Evaluation date (from the index): {doc.exam_date.isoformat()}")

    body = [
        "\n".join(header),
        "",
        f"Fields to extract (group {field_group!r}):",
        *_field_lines(field_group, doc),
        "",
        "Sections located in the document. Each section is labeled with its scope "
        "(institution, state, msa, assessment_area, appendix) and page range. "
        "Quotes must be copied verbatim from these sections.",
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
