"""LLM extraction client for API models (S3) and local models (S4).

One ``Backend`` interface with four implementations:

- ``GeminiBackend``: google-genai SDK, ``client.models.generate_content`` with
  ``response_mime_type="application/json"`` and ``response_json_schema``.
- ``OpenAICompatibleBackend``: openai SDK with ``base_url``; covers Ollama
  (``OLLAMA_BASE_URL``) and hosted OpenAI-compatible providers. Uses
  ``response_format={"type": "json_schema"}`` and falls back to ``json_object``
  when the server rejects it.
- ``AnthropicBackend``: anthropic SDK structured outputs via
  ``output_config={"format": {"type": "json_schema", "schema": ...}}``. The
  package is optional; the class imports it lazily.
- ``ReplayBackend``: serves stored traces keyed by (model, prompt_hash) and
  raises ``TraceMissError`` on a miss unless ``allow_api`` is set, in which case
  it delegates to a live backend.

Every call goes through a rate limiter (optional) and a retry loop with
exponential backoff that honours ``Retry-After``. ``extract()`` builds the
prompt and schema, calls the backend, validates the JSON, maps it to
``FieldValue`` records and appends a ``Trace`` to
``data/traces/{split}/{backend}.jsonl``. Grounding checks (fuzzy quote
location, scope support) live in ``evaluate.py``; here the quote is only
located by exact substring search to attach a page and scope.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from cra_extract.config import CACHE_DIR, FIXTURES_DIR, TRACE_DIR, field_spec, load_prices
from cra_extract.models import DocRecord, FieldValue, Section, Trace
from cra_extract.prompts import (
    PROMPT_VERSION,
    STATUSES,
    SYSTEM_PROMPT,
    build_prompt,
    build_schema,
    llm_field_group,
)

DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_OLLAMA_NUM_CTX = 32768
CHARS_PER_TOKEN_LOWER_BOUND = 3.0
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------------- errors


class LLMError(Exception):
    """Base class for errors raised by this module."""


class TraceMissError(LLMError):
    """Raised by ``ReplayBackend`` when no stored trace matches and the API is not allowed."""


class PromptTooLargeError(LLMError):
    """Raised when the estimated prompt tokens do not fit the local model's context window."""


class DailyLimitExceeded(LLMError):
    """Raised by ``RateLimiter`` when the per-day request budget is exhausted."""


# ----------------------------------------------------------------------------- results


@dataclass
class BackendResult:
    """What a backend returns for one call."""

    text: str
    parsed_json: dict[str, Any] | None
    model_reported: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_s: float | None = None
    from_replay: bool = False
    replay_trace_id: str | None = None


# ----------------------------------------------------------------------------- hashing


def prompt_hash(system_prompt: str, user_prompt: str) -> str:
    """Stable hash of the full prompt (system and user) and the prompt version."""
    h = hashlib.sha256()
    h.update(PROMPT_VERSION.encode())
    h.update(b"\x00")
    h.update(system_prompt.encode())
    h.update(b"\x00")
    h.update(user_prompt.encode())
    return h.hexdigest()


def make_trace_id(doc_id: str, field_group: str, model: str, prompt_hash_: str) -> str:
    """Trace key: sha256 of (doc_id, field_group, model, prompt_hash), first 16 hex chars."""
    raw = "|".join([doc_id, field_group, model, prompt_hash_])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- cost


def model_price(model: str) -> dict[str, Any] | None:
    """Price entry for ``model`` from ``config/prices.yaml``, searching every provider.

    Keys match exactly or as the tail after a provider prefix (``ollama/x`` for
    ``x``). Returns ``None`` when the model is not priced.
    """
    prices = load_prices()
    for provider in prices.values():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models") or {}
        for key, entry in models.items():
            if key == model or key.endswith("/" + model):
                return entry
    return None


def compute_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float:
    """USD cost of one call from the usage fields. Unpriced or local models cost 0."""
    entry = model_price(model)
    if not entry:
        return 0.0
    price_in = entry.get("input") or 0.0
    price_out = entry.get("output") or 0.0
    return (input_tokens or 0) * price_in / 1e6 + (output_tokens or 0) * price_out / 1e6


# ----------------------------------------------------------------------------- retry


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _retry_after(exc: BaseException) -> float | None:
    """Seconds from a ``Retry-After`` header when the SDK exposes the response."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def is_retryable(exc: BaseException) -> bool:
    """429, 5xx, connection and timeout errors are retried; other errors are not."""
    code = _status_code(exc)
    if code is not None:
        return code == 429 or code >= 500
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "ServerError"}:
        return True
    return isinstance(exc, ConnectionError | TimeoutError)


def call_with_retry(
    fn: Callable[[], Any],
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Call ``fn`` with exponential backoff on retryable errors.

    The delay is ``base_delay * 2**attempt`` capped at ``max_delay``, or the
    ``Retry-After`` value when the server sent one. Non-retryable errors
    propagate immediately; the last error propagates after ``max_attempts``.
    """
    last: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_retryable(exc) or attempt == max_attempts - 1:
                raise
            last = exc
            delay = _retry_after(exc)
            if delay is None:
                delay = min(base_delay * (2**attempt), max_delay)
            sleep(delay)
    assert last is not None
    raise last


# ----------------------------------------------------------------------------- rate limit


class RateLimiter:
    """Requests-per-minute and requests-per-day limiter for free tiers.

    The per-minute window is kept in memory; the per-day count is persisted in
    ``{cache_dir}/ratelimit_{backend}.json`` so that restarts do not reset it.
    ``acquire()`` sleeps when the minute window is full and raises
    ``DailyLimitExceeded`` when the daily budget is spent.
    """

    def __init__(
        self,
        backend: str,
        per_minute: int | None = None,
        per_day: int | None = None,
        cache_dir: Path = CACHE_DIR,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        self.per_minute = per_minute
        self.per_day = per_day
        self.path = Path(cache_dir) / f"ratelimit_{backend}.json"
        self._sleep = sleep
        self._clock = clock
        self._window: list[float] = []

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=UTC).date().isoformat()

    def _load_daily(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("date") != self._today():
            data = {"date": self._today(), "count": 0}
        return data

    def daily_count(self) -> int:
        """Requests recorded today."""
        return int(self._load_daily().get("count", 0))

    def acquire(self) -> None:
        """Block until a request may be sent, then record it."""
        now = self._clock()
        if self.per_minute:
            self._window = [t for t in self._window if now - t < 60.0]
            if len(self._window) >= self.per_minute:
                wait = 60.0 - (now - self._window[0])
                if wait > 0:
                    self._sleep(wait)
                now = self._clock()
                self._window = [t for t in self._window if now - t < 60.0]
            self._window.append(now)
        daily = self._load_daily()
        if self.per_day and daily["count"] >= self.per_day:
            raise DailyLimitExceeded(
                f"{self.backend}: {daily['count']} requests today, limit {self.per_day}"
            )
        daily["count"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(daily), encoding="utf-8")


# ----------------------------------------------------------------------------- backends


class Backend:
    """Interface shared by all backends.

    Subclasses implement ``_call`` (one raw request) and inherit ``complete``,
    which applies the rate limiter, the retry loop and timing.
    """

    name: str = "base"
    model: str = ""
    limiter: RateLimiter | None = None
    max_attempts: int = 5
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
        model: str | None = None,
    ) -> BackendResult:
        """Send one structured-output request and return the parsed result."""
        model = model or self.model
        if self.limiter is not None:
            self.limiter.acquire()
        start = time.perf_counter()
        result = call_with_retry(
            lambda: self._call(system_prompt, user_prompt, json_schema, model),
            max_attempts=self.max_attempts,
            sleep=self.sleep,
        )
        result.latency_s = round(time.perf_counter() - start, 3)
        return result

    def _call(
        self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any], model: str
    ) -> BackendResult:
        raise NotImplementedError


def _parse_json(text: str) -> dict[str, Any] | None:
    """Parse the model text as a JSON object, tolerating a Markdown code fence."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (3 characters per token) for context-size assertions."""
    return int(len(text) / CHARS_PER_TOKEN_LOWER_BOUND) + 1


class GeminiBackend(Backend):
    """Google Gemini via the google-genai SDK (``client.models.generate_content``).

    Structured output uses ``response_mime_type="application/json"`` together
    with ``response_json_schema`` (plain JSON schema, google-genai 1.x). The API
    key comes from ``GEMINI_API_KEY``.
    """

    name = "gemini"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        limiter: RateLimiter | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_attempts: int = 5,
    ) -> None:
        from google import genai

        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set")
        self.client = genai.Client(api_key=key)
        self.model = model
        self.limiter = limiter
        self.max_output_tokens = max_output_tokens
        self.max_attempts = max_attempts

    def _call(
        self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any], model: str
    ) -> BackendResult:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_json_schema=json_schema,
            max_output_tokens=self.max_output_tokens,
        )
        response = self.client.models.generate_content(
            model=model, contents=user_prompt, config=config
        )
        text = response.text or ""
        usage = response.usage_metadata
        output_tokens = None
        if usage is not None:
            output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        return BackendResult(
            text=text,
            parsed_json=_parse_json(text),
            model_reported=response.model_version,
            input_tokens=usage.prompt_token_count if usage is not None else None,
            output_tokens=output_tokens,
        )


class OpenAICompatibleBackend(Backend):
    """Any OpenAI-compatible chat completions endpoint (Ollama, Groq, Mistral, OpenRouter).

    ``base_url`` selects the provider. For Ollama (``name="ollama"``) the context
    window is set through ``extra_body={"options": {"num_ctx": N}}`` and the
    prompt size is asserted to fit before the call. ``response_format`` starts as
    ``json_schema``; a 400 on that parameter switches the instance to
    ``json_object`` for the rest of its life.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        name: str = "openai_compat",
        limiter: RateLimiter | None = None,
        num_ctx: int | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_attempts: int = 5,
        timeout: float = 600.0,
    ) -> None:
        from openai import OpenAI

        self.name = name
        self.model = model
        self.limiter = limiter
        self.max_attempts = max_attempts
        self.max_output_tokens = max_output_tokens
        self.is_ollama = name == "ollama"
        if self.is_ollama:
            base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            api_key = api_key or "ollama"
            num_ctx = num_ctx or DEFAULT_OLLAMA_NUM_CTX
        self.base_url = base_url
        self.num_ctx = num_ctx
        self.json_mode = "json_schema"
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            max_retries=0,
            timeout=timeout,
        )

    def _response_format(self, json_schema: dict[str, Any]) -> dict[str, Any]:
        if self.json_mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "extraction"),
                    "schema": json_schema,
                    "strict": True,
                },
            }
        return {"type": "json_object"}

    def _call(
        self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any], model: str
    ) -> BackendResult:
        import openai

        extra_body: dict[str, Any] = {}
        if self.num_ctx:
            needed = estimate_tokens(system_prompt + user_prompt) + self.max_output_tokens
            if needed > self.num_ctx:
                raise PromptTooLargeError(
                    f"estimated {needed} tokens exceed num_ctx={self.num_ctx} for {model}"
                )
            extra_body["options"] = {"num_ctx": self.num_ctx}
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            completion = self.client.chat.completions.create(
                model=model,
                messages=messages,
                response_format=self._response_format(json_schema),
                max_tokens=self.max_output_tokens,
                extra_body=extra_body or None,
            )
        except openai.BadRequestError:
            if self.json_mode != "json_schema":
                raise
            self.json_mode = "json_object"
            completion = self.client.chat.completions.create(
                model=model,
                messages=messages,
                response_format=self._response_format(json_schema),
                max_tokens=self.max_output_tokens,
                extra_body=extra_body or None,
            )
        text = completion.choices[0].message.content or ""
        usage = completion.usage
        return BackendResult(
            text=text,
            parsed_json=_parse_json(text),
            model_reported=completion.model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )


class AnthropicBackend(Backend):
    """Claude via the anthropic SDK structured outputs.

    Uses ``client.messages.create`` with
    ``output_config={"format": {"type": "json_schema", "schema": schema}}`` and
    an optional ``effort`` level. No ``temperature`` is sent (current models
    have none); reproducibility is by trace replay. The ``anthropic`` package is
    optional and imported here.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        limiter: RateLimiter | None = None,
        effort: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_attempts: int = 5,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise LLMError("install the 'anthropic' extra to use AnthropicBackend") from exc

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        self.model = model
        self.limiter = limiter
        self.effort = effort
        self.max_output_tokens = max_output_tokens
        self.max_attempts = max_attempts

    def _call(
        self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any], model: str
    ) -> BackendResult:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": json_schema}}
        if self.effort:
            output_config["effort"] = self.effort
        response = self.client.messages.create(
            model=model,
            max_tokens=self.max_output_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config=output_config,
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        usage = response.usage
        input_tokens = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        )
        return BackendResult(
            text=text,
            parsed_json=_parse_json(text),
            model_reported=response.model,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
        )


# ----------------------------------------------------------------------------- traces


def trace_path(split: str, backend_name: str, trace_dir: Path = TRACE_DIR) -> Path:
    """``data/traces/{split}/{backend}.jsonl``."""
    return Path(trace_dir) / split / f"{backend_name}.jsonl"


def append_trace(trace: Trace, split: str, trace_dir: Path = TRACE_DIR) -> Path:
    """Append one trace as a JSON line and return the file path."""
    path = trace_path(split, trace.backend, trace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(trace.model_dump_json() + "\n")
    return path


def read_traces(path: Path) -> list[Trace]:
    """Read every trace in a JSONL file (missing file gives an empty list)."""
    if not Path(path).exists():
        return []
    traces: list[Trace] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                traces.append(Trace.model_validate_json(line))
    return traces


class ReplayBackend(Backend):
    """Serve stored traces; raise ``TraceMissError`` on a miss unless ``allow_api``.

    Traces are read from ``{trace_dir}/{split}/{backend_name}.jsonl`` and keyed
    by (model, prompt_hash). The prompt hash already covers the document id and
    the section text, so no other key is needed. With ``allow_api=True`` a miss
    is delegated to ``live`` and ``extract()`` records the new trace in the same
    file, so the next replay serves it. ``name`` equals the live backend's name
    so that ``FieldValue.system`` labels stay identical between live and replay.
    """

    def __init__(
        self,
        backend_name: str,
        split: str,
        model: str,
        live: Backend | None = None,
        allow_api: bool = False,
        trace_dir: Path = TRACE_DIR,
    ) -> None:
        self.name = backend_name
        self.split = split
        self.model = model
        self.live = live
        self.allow_api = allow_api
        self.trace_dir = Path(trace_dir)
        self.path = trace_path(split, backend_name, self.trace_dir)
        self.hits = 0
        self.misses = 0
        self._index: dict[tuple[str, str], Trace] = {}
        for tr in read_traces(self.path):
            if tr.error is None:
                self._index[(tr.model, tr.prompt_hash)] = tr

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
        model: str | None = None,
    ) -> BackendResult:
        model = model or self.model
        stored = self._index.get((model, prompt_hash(system_prompt, user_prompt)))
        if stored is not None:
            self.hits += 1
            return BackendResult(
                text=stored.response_text or "",
                parsed_json=stored.response_json,
                model_reported=stored.model_reported,
                input_tokens=stored.input_tokens,
                output_tokens=stored.output_tokens,
                latency_s=stored.latency_s,
                from_replay=True,
                replay_trace_id=stored.trace_id,
            )
        self.misses += 1
        if not (self.allow_api and self.live is not None):
            raise TraceMissError(
                f"no trace for model={model} in {self.path}; rerun with --allow-api to call the API"
            )
        return self.live.complete(system_prompt, user_prompt, json_schema, model)


# ----------------------------------------------------------------------------- validation


def _check_value(spec: dict[str, Any], value: Any) -> str | None:
    """Return an error string when ``value`` does not fit the field spec, else None."""
    ftype = spec["type"]
    allowed = spec.get("allowed") or []
    if ftype == "enum":
        if value not in allowed:
            return f"value {value!r} not in allowed {allowed}"
    elif ftype == "set":
        if not isinstance(value, list):
            return "set value must be a list"
        bad = [v for v in value if v not in allowed]
        if bad:
            return f"set elements {bad} not in allowed {allowed}"
    elif ftype == "bool":
        if not isinstance(value, bool):
            return "expected a boolean"
    elif ftype == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                return None
            return "expected an integer"
    elif ftype == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return "expected a number"
    elif ftype == "date":
        if not isinstance(value, str) or not DATE_RE.match(value):
            return "expected an ISO date string"
        try:
            date.fromisoformat(value)
        except ValueError:
            return f"invalid date {value!r}"
    elif ftype == "str":
        if not isinstance(value, str) or not value.strip():
            return "expected a non-empty string"
    return None


def _normalize_value(spec: dict[str, Any], value: Any) -> Any:
    if spec["type"] == "int" and isinstance(value, float):
        return int(value)
    if spec["type"] == "set":
        return sorted(set(value))
    return value


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def locate_quote(quote: str, sections: dict[str, Section]) -> tuple[Section, int] | None:
    """Exact (whitespace-normalized) substring search over the provided sections.

    Returns the section and the character offset of the match within the
    original section text, or ``None``. Fuzzy matching belongs to evaluate.py.
    """
    needle = _normalize_ws(quote)
    if not needle:
        return None
    for sec in sections.values():
        idx = sec.text.find(quote)
        if idx >= 0:
            return sec, idx
        # Whitespace-insensitive search: map the normalized hit back to an offset.
        pattern = r"\s+".join(re.escape(tok) for tok in needle.split(" "))
        m = re.search(pattern, sec.text)
        if m:
            return sec, m.start()
    return None


def _page_of(sec: Section, offset: int) -> int:
    page = sec.page_start + sec.text[:offset].count("\f")
    return min(page, sec.page_end)


def map_response(
    doc: DocRecord,
    field_group: str,
    data: dict[str, Any] | None,
    sections: dict[str, Section],
    system: str,
) -> list[FieldValue]:
    """Map a validated-or-not response object to one ``FieldValue`` per field.

    Missing fields, malformed entries and values that violate the field spec
    become abstentions with the reason in ``notes``. ``page`` and ``scope`` come
    from the section where the quote is found; when the quote is not found the
    model's page is kept and ``notes`` says ``quote_not_located``.
    """
    out: list[FieldValue] = []
    data = data or {}
    for name in llm_field_group(field_group)["fields"]:
        spec = field_spec(name)
        entry = data.get(name)
        base = {"doc_id": doc.doc_id, "field": name, "system": system}
        if not isinstance(entry, dict):
            out.append(FieldValue(**base, status="abstain", notes="missing_from_response"))
            continue
        status = entry.get("status")
        value = entry.get("value")
        quote = entry.get("quote") if isinstance(entry.get("quote"), str) else None
        page = entry.get("page") if isinstance(entry.get("page"), int) else None
        if status not in STATUSES:
            out.append(FieldValue(**base, status="abstain", notes=f"invalid_status:{status!r}"))
            continue
        if status != "value":
            out.append(FieldValue(**base, status=status, quote=quote, page=page))
            continue
        if value is None:
            out.append(FieldValue(**base, status="abstain", notes="value_missing"))
            continue
        err = _check_value(spec, value)
        if err:
            out.append(FieldValue(**base, status="abstain", quote=quote, notes=f"invalid_value:{err}"))
            continue
        fv = FieldValue(**base, status="value", value=_normalize_value(spec, value), quote=quote)
        hit = locate_quote(quote, sections) if quote else None
        if hit is None:
            fv.page = page
            fv.notes = "quote_missing" if not quote else "quote_not_located"
        else:
            sec, offset = hit
            fv.page = _page_of(sec, offset)
            fv.scope = sec.scope
            fv.char_start = sec.char_start + offset
            fv.char_end = fv.char_start + len(quote or "")
        out.append(fv)
    return out


# ----------------------------------------------------------------------------- extract


def extract(
    doc: DocRecord,
    sections: dict[str, Section],
    field_group: str,
    backend: Backend,
    split: str,
    trace_dir: Path = TRACE_DIR,
) -> tuple[list[FieldValue], Trace]:
    """Run one LLM extraction for one field group and record its trace.

    Builds the prompt and schema, calls ``backend.complete``, validates the
    response and maps it to ``FieldValue`` records with
    ``system = "llm:{backend.name}:{model}"``. The trace is appended to
    ``{trace_dir}/{split}/{backend.name}.jsonl`` unless the result came from a
    replay. A failed call (after retries) is recorded with ``error`` set and
    every field is returned as an abstention; the caller decides whether to
    stop.
    """
    model = backend.model
    system_prompt = SYSTEM_PROMPT
    user_prompt = build_prompt(doc, sections, field_group)
    schema = build_schema(field_group)
    ph = prompt_hash(system_prompt, user_prompt)
    system = f"llm:{backend.name}:{model}"
    offsets = [(s.char_start, s.char_end) for s in sections.values()]
    called_at = datetime.now(UTC)
    try:
        result = backend.complete(system_prompt, user_prompt, schema, model)
    except TraceMissError:
        raise
    except Exception as exc:
        trace = Trace(
            trace_id=make_trace_id(doc.doc_id, field_group, model, ph),
            doc_id=doc.doc_id,
            field_group=field_group,
            backend=backend.name,
            model=model,
            prompt_hash=ph,
            section_offsets=offsets,
            called_at=called_at,
            error=f"{type(exc).__name__}: {exc}",
        )
        append_trace(trace, split, trace_dir)
        values = [
            FieldValue(doc_id=doc.doc_id, field=f, system=system, status="abstain", notes="call_failed")
            for f in llm_field_group(field_group)["fields"]
        ]
        return values, trace

    trace = Trace(
        trace_id=result.replay_trace_id or make_trace_id(doc.doc_id, field_group, model, ph),
        doc_id=doc.doc_id,
        field_group=field_group,
        backend=backend.name,
        model=model,
        model_reported=result.model_reported,
        prompt_hash=ph,
        section_offsets=offsets,
        response_json=result.parsed_json,
        response_text=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=compute_cost(model, result.input_tokens, result.output_tokens),
        latency_s=result.latency_s,
        called_at=called_at,
        error=None if result.parsed_json is not None else "response_not_json",
    )
    if not result.from_replay:
        append_trace(trace, split, trace_dir)
    values = map_response(doc, field_group, result.parsed_json, sections, system)
    return values, trace


# ----------------------------------------------------------------------------- CLI


def sections_from_text(doc_id: str, text: str) -> dict[str, Section]:
    """Whole-document fallback: one institution-scope section covering every page."""
    pages = text.split("\f")
    return {
        "document": Section(
            name="document",
            doc_id=doc_id,
            page_start=1,
            page_end=max(1, len(pages)),
            char_start=0,
            char_end=len(text),
            text=text,
            scope="institution",
            heading_matched=None,
            confidence=0.0,
        )
    }


def sample_doc(stem: str) -> DocRecord:
    """Build a ``DocRecord`` for one sample under ``tests/fixtures/samples`` from its stem."""
    parts = stem.split("_")
    regulator, bank_id = parts[0], parts[1]
    return DocRecord(
        doc_id=f"{regulator}_{bank_id}_{stem}",
        regulator=regulator,  # type: ignore[arg-type]
        bank_id=bank_id,
        bank_name=" ".join(parts[2:3]),
        method="isb" if "_ISB_" in stem else "small",
        url=f"file://{FIXTURES_DIR / 'samples' / (stem + '.pdf')}",
        split="dev",
    )


def make_backend(name: str, model: str, **kwargs: Any) -> Backend:
    """Instantiate a live backend by CLI name."""
    if name == "gemini":
        return GeminiBackend(model, **kwargs)
    if name == "anthropic":
        return AnthropicBackend(model, **kwargs)
    if name in {"openai_compat", "ollama"}:
        return OpenAICompatibleBackend(model, name=name, **kwargs)
    raise ValueError(f"unknown backend {name!r}")


def main(argv: list[str] | None = None) -> int:
    """``python -m cra_extract.llm --smoke --backend X --model Y``: one live extraction."""
    ap = argparse.ArgumentParser(description="LLM extraction smoke test (network).")
    ap.add_argument("--smoke", action="store_true", help="run one live extraction on a sample PE")
    ap.add_argument(
        "--backend",
        choices=["gemini", "openai_compat", "ollama", "anthropic"],
        default="anthropic",
    )
    ap.add_argument("--model", required=True)
    ap.add_argument("--sample", default="FDIC_12299_SouthStar_2022_ISB_NI_fairlending")
    ap.add_argument("--field-group", default="cover_and_rating")
    ap.add_argument("--split", default="dev")
    args = ap.parse_args(argv)
    if not args.smoke:
        ap.error("only --smoke is implemented; the pipeline calls extract() from run.py")

    text = (FIXTURES_DIR / "samples" / f"{args.sample}.txt").read_text(encoding="utf-8")
    doc = sample_doc(args.sample)
    sections = sections_from_text(doc.doc_id, text)
    backend = make_backend(args.backend, args.model)
    values, trace = extract(doc, sections, args.field_group, backend, args.split)
    for fv in values:
        print(f"{fv.field:28s} {fv.status:8s} {fv.value!r:30s} p{fv.page} {fv.scope} | {fv.quote}")
    print(
        f"\nmodel={trace.model} reported={trace.model_reported} "
        f"in={trace.input_tokens} out={trace.output_tokens} "
        f"cost=${trace.cost_usd:.4f} latency={trace.latency_s}s error={trace.error}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
