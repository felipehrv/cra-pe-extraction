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
- ``ReplayBackend``: serves stored traces keyed by (model, effort, prompt_hash)
  and raises ``TraceMissError`` on a miss unless ``allow_api`` is set, in which
  case it delegates to a live backend.

Effort: every backend carries an optional ``effort`` level, sent as the Gemini
thinking level, the Anthropic ``output_config.effort`` or the OpenAI-compatible
``reasoning_effort``, and recorded in ``Trace.effort``. ``DEFAULT_EFFORT`` gives
the run stage's default per backend (``low`` for hosted backends, none for Ollama).

The closed-book contamination baseline (``extract_closed_book``) uses its own
prompt and ``CLOSED_BOOK_PROMPT_VERSION`` and records its traces under the field
group ``closedbook``.

Every call goes through a rate limiter (optional) and a retry loop with
exponential backoff that honours ``Retry-After``. ``extract()`` builds the
prompt and schema, calls the backend, validates the JSON, maps it to
``FieldValue`` records and appends a ``Trace`` to
``data/traces/{split}/{backend}.jsonl``. Grounding checks (fuzzy quote
location, scope support) live in ``evaluate.py``; here the quote is only
located by exact substring search to attach a page and scope.

Field groups: ``extract`` takes one of the groups listed in ``fields.yaml`` or the
combined group ``all`` (``prompts.ALL_GROUP``), which asks for every LLM field in one
call. The trace records the group in ``Trace.field_group``, so an ``all`` trace and the
four split traces of the same document are told apart; their prompt hashes differ, so
replay never serves one mode's trace to the other. For ``all`` a field's quote is
searched in its own group's sections first, then in the rest, so page, scope and offsets
come from the same section as in the split call whenever the quote is there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from cra_extract.config import CACHE_DIR, FIXTURES_DIR, TRACE_DIR, field_spec, load_prices
from cra_extract.models import DocRecord, FieldValue, Section, Trace, make_doc_id
from cra_extract.prompts import (
    ALL_GROUP,
    CLOSED_BOOK_FIELDS,
    CLOSED_BOOK_GROUP,
    CLOSED_BOOK_PROMPT_VERSION,
    CLOSED_BOOK_STATUSES,
    CLOSED_BOOK_SYSTEM_PROMPT,
    PROMPT_VERSION,
    STATUSES,
    SYSTEM_PROMPT,
    build_closed_book_prompt,
    build_closed_book_schema,
    build_prompt,
    build_schema,
    field_home_sections,
    llm_field_group,
    portable_schema,
)

# Thinking tokens count toward max_tokens on the hosted APIs (always on for the current
# Claude models, on by default for Gemini), so the API default leaves room for them; the
# local model gets a smaller budget because it shares num_ctx with the prompt.
log = logging.getLogger(__name__)

DEFAULT_MAX_OUTPUT_TOKENS = 16000
DEFAULT_OLLAMA_MAX_OUTPUT_TOKENS = 4096
DEFAULT_OLLAMA_NUM_CTX = 32768
TRUNCATION_STOP_REASONS = {"max_tokens", "refusal", "MAX_TOKENS", "SAFETY", "RECITATION", "length", "content_filter"}
CHARS_PER_TOKEN_LOWER_BOUND = 3.0
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Effort sent by the run stage when --effort is not given: low for the hosted APIs, none
# for the local model (Ollama has no effort setting for the S4 models).
DEFAULT_EFFORT: dict[str, str | None] = {
    "gemini": "low",
    "anthropic": "low",
    "openai_compat": "low",
    "ollama": None,
}
GEMINI_THINKING_LEVELS = ("minimal", "low", "medium", "high")


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
    stop_reason: str | None = None
    response_format: str | None = None
    from_replay: bool = False
    replay_trace_id: str | None = None
    replay_called_at: datetime | None = None
    replay_cost_usd: float | None = None


# ----------------------------------------------------------------------------- hashing


def prompt_hash(
    system_prompt: str,
    user_prompt: str,
    json_schema: dict[str, Any] | None = None,
    version: str = PROMPT_VERSION,
) -> str:
    """Stable hash of the prompt version, the full prompt (system and user) and the JSON
    schema sent with it, so a schema-only change never replays a stale trace. The
    closed-book baseline passes ``CLOSED_BOOK_PROMPT_VERSION`` as ``version``."""
    h = hashlib.sha256()
    h.update(version.encode())
    h.update(b"\x00")
    h.update(system_prompt.encode())
    h.update(b"\x00")
    h.update(user_prompt.encode())
    if json_schema is not None:
        h.update(b"\x00")
        h.update(json.dumps(json_schema, sort_keys=True).encode())
    return h.hexdigest()


def make_trace_id(
    doc_id: str, field_group: str, model: str, prompt_hash_: str, effort: str | None = None
) -> str:
    """Trace key: sha256 of (doc_id, field_group, model, prompt_hash[, effort]), first 16
    hex chars. ``effort`` joins the key only when one was sent, so ids of calls without
    an effort are unchanged."""
    parts = [doc_id, field_group, model, prompt_hash_]
    if effort is not None:
        parts.append(effort)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


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


def _retry_after(exc: BaseException, now: Callable[[], float] = time.time) -> float | None:
    """Seconds from a ``Retry-After`` header (delay-seconds or an HTTP-date, RFC 9110)
    when the SDK exposes the response."""
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
        pass
    try:
        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - now())


_RETRY_HINT = re.compile(r"retry in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)


def daily_quota_hint(exc: BaseException) -> float | None:
    """Seconds until a daily quota reopens, read from a 429 message such as Gemini's
    ``Quota exceeded for metric: ...free_tier_requests, limit: 500 ... Please retry in
    23h19m52s``. Returns ``None`` when the error is not a daily-quota 429 (per-minute
    429s carry hints of a few seconds and no per-day marker)."""
    if _status_code(exc) != 429:
        return None
    text = str(exc)
    match = _RETRY_HINT.search(text)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    wait = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
    if wait >= 600 or "perday" in text.lower().replace("_", ""):
        return wait
    return None


def is_retryable(exc: BaseException) -> bool:
    """429, 5xx, connection and timeout errors are retried; other errors are not.
    A 429 whose retry hint says the daily quota is spent is not retried: every
    attempt counts against that quota, so retrying only burns it."""
    if daily_quota_hint(exc) is not None:
        return False
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
        reset_tz: tzinfo = UTC,
    ) -> None:
        self.backend = backend
        self.per_minute = per_minute
        self.per_day = per_day
        self.path = Path(cache_dir) / f"ratelimit_{backend}.json"
        self._sleep = sleep
        self._clock = clock
        self._window: list[float] = []
        # The provider's day boundary (Gemini resets at midnight Pacific time).
        self.reset_tz = reset_tz

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=self.reset_tz).date().isoformat()

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

    def mark_exhausted(self) -> None:
        """Record that the server reported the daily quota spent, so later runs on the
        same day halt before sending (and burning) further requests."""
        if not self.per_day:
            return
        daily = self._load_daily()
        daily["count"] = max(int(daily.get("count", 0)), self.per_day)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(daily), encoding="utf-8")

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
        if not self.per_day:
            return
        daily = self._load_daily()
        if daily["count"] >= self.per_day:
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
    which applies the rate limiter, the retry loop and timing. ``effort`` is the
    level sent with every call (``None``: no effort parameter) and is recorded in
    each trace.
    """

    name: str = "base"
    model: str = ""
    effort: str | None = None
    limiter: RateLimiter | None = None
    max_attempts: int = 5
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
        model: str | None = None,
        key: str | None = None,
    ) -> BackendResult:
        """Send one structured-output request and return the parsed result.

        ``key`` is the caller's prompt hash; live backends ignore it and
        ``ReplayBackend`` looks the trace up by it.
        """
        model = model or self.model

        def attempt() -> BackendResult:
            # Every attempt, retries included, counts against the request budget.
            if self.limiter is not None:
                self.limiter.acquire()
            return self._call(system_prompt, user_prompt, json_schema, model)

        start = time.perf_counter()
        try:
            result = call_with_retry(attempt, max_attempts=self.max_attempts, sleep=self.sleep)
        except Exception as exc:
            wait = daily_quota_hint(exc)
            if wait is None:
                raise
            if self.limiter is not None:
                self.limiter.mark_exhausted()
            raise DailyLimitExceeded(
                f"{self.name}: daily quota reported spent by the server; retry in {wait / 3600:.1f} h"
            ) from exc
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


def _enum_name(value: Any) -> str | None:
    """Name of an SDK enum member (or the string itself); ``None`` when unset."""
    if value is None:
        return None
    return str(getattr(value, "name", value))


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (3 characters per token) for context-size assertions."""
    return int(len(text) / CHARS_PER_TOKEN_LOWER_BOUND) + 1


class GeminiBackend(Backend):
    """Google Gemini via the google-genai SDK (``client.models.generate_content``).

    Structured output uses ``response_mime_type="application/json"`` together
    with ``response_json_schema`` (plain JSON schema; both are fields of
    ``GenerateContentConfig`` in google-genai 2.28, and the field doc says
    ``response_mime_type`` is required when the schema is set). The schema is
    passed through ``portable_schema`` because the documented subset has no
    type arrays. The API key comes from ``GEMINI_API_KEY`` or, failing that,
    ``GOOGLE_API_KEY`` (the two names the SDK itself resolves). ``effort`` is sent
    as ``ThinkingConfig.thinking_level`` (minimal, low, medium or high; the Gemini 3
    control, google-genai 2.28 ``types.ThinkingLevel``).
    """

    name = "gemini"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        limiter: RateLimiter | None = None,
        effort: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_attempts: int = 5,
    ) -> None:
        if effort is not None and effort not in GEMINI_THINKING_LEVELS:
            raise LLMError(
                f"gemini effort must be one of {', '.join(GEMINI_THINKING_LEVELS)}; got {effort!r}"
            )
        from google import genai

        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        self.client = genai.Client(api_key=key)
        self.model = model
        self.limiter = limiter
        self.effort = effort
        self.max_output_tokens = max_output_tokens
        self.max_attempts = max_attempts

    def _config(self, system_prompt: str, json_schema: dict[str, Any]) -> Any:
        """``GenerateContentConfig`` for one call (separate so tests can inspect it offline)."""
        from google.genai import types

        thinking = types.ThinkingConfig(thinking_level=self.effort.upper()) if self.effort else None
        return types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_json_schema=portable_schema(json_schema),
            max_output_tokens=self.max_output_tokens,
            thinking_config=thinking,
        )

    def _call(
        self, system_prompt: str, user_prompt: str, json_schema: dict[str, Any], model: str
    ) -> BackendResult:
        config = self._config(system_prompt, json_schema)
        response = self.client.models.generate_content(
            model=model, contents=user_prompt, config=config
        )
        text = response.text or ""
        usage = response.usage_metadata
        output_tokens = None
        if usage is not None:
            output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        return BackendResult(
            text=text,
            parsed_json=_parse_json(text),
            model_reported=response.model_version,
            input_tokens=usage.prompt_token_count if usage is not None else None,
            output_tokens=output_tokens,
            stop_reason=_enum_name(finish),
            response_format="json_schema",
        )


class OpenAICompatibleBackend(Backend):
    """Any OpenAI-compatible chat completions endpoint (Ollama, Groq, Mistral, OpenRouter).

    ``base_url`` selects the provider. For Ollama (``name="ollama"``) the context
    window is set through ``extra_body={"options": {"num_ctx": N}}`` and the
    prompt size is asserted to fit before the call. ``response_format`` starts as
    ``json_schema``; a 400 whose body names that parameter switches the instance to
    ``json_object`` for the rest of its life (logged, and recorded per trace in
    ``response_format``); any other 400 propagates. ``max_output_tokens`` defaults to
    ``DEFAULT_OLLAMA_MAX_OUTPUT_TOKENS`` for Ollama and ``DEFAULT_MAX_OUTPUT_TOKENS``
    otherwise. ``effort``, when set, is sent as ``reasoning_effort``; a provider whose
    model has no reasoning setting may reject it with a 400, which propagates.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        name: str = "openai_compat",
        limiter: RateLimiter | None = None,
        effort: str | None = None,
        num_ctx: int | None = None,
        max_output_tokens: int | None = None,
        max_attempts: int = 5,
        timeout: float = 600.0,
    ) -> None:
        from openai import OpenAI

        self.name = name
        self.model = model
        self.limiter = limiter
        self.effort = effort
        self.max_attempts = max_attempts
        self.is_ollama = name == "ollama"
        if self.is_ollama:
            base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            api_key = api_key or "ollama"
            num_ctx = num_ctx or DEFAULT_OLLAMA_NUM_CTX
            if max_output_tokens is None:
                max_output_tokens = DEFAULT_OLLAMA_MAX_OUTPUT_TOKENS
        self.max_output_tokens = max_output_tokens if max_output_tokens is not None else DEFAULT_MAX_OUTPUT_TOKENS
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
        extra: dict[str, Any] = {"reasoning_effort": self.effort} if self.effort else {}
        try:
            completion = self.client.chat.completions.create(
                model=model,
                messages=messages,
                response_format=self._response_format(json_schema),
                max_tokens=self.max_output_tokens,
                extra_body=extra_body or None,
                **extra,
            )
        except openai.BadRequestError as exc:
            # Only a rejection of the response_format itself justifies the fallback; any
            # other 400 (context length, unknown model, malformed message) propagates.
            if self.json_mode != "json_schema" or not _rejects_response_format(exc):
                raise
            log.warning(
                "%s rejected response_format=json_schema (%s); falling back to json_object for the rest of the run",
                self.name, exc,
            )
            self.json_mode = "json_object"
            completion = self.client.chat.completions.create(
                model=model,
                messages=messages,
                response_format=self._response_format(json_schema),
                max_tokens=self.max_output_tokens,
                extra_body=extra_body or None,
                **extra,
            )
        choice = completion.choices[0]
        text = choice.message.content or ""
        usage = completion.usage
        return BackendResult(
            text=text,
            parsed_json=_parse_json(text),
            model_reported=completion.model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            stop_reason=getattr(choice, "finish_reason", None),
            response_format=self.json_mode,
        )


def _rejects_response_format(exc: BaseException) -> bool:
    """True when a 400 body talks about the response_format / json_schema parameter."""
    parts = [str(exc), str(getattr(exc, "message", "")), str(getattr(exc, "body", ""))]
    text = " ".join(parts).lower()
    return "response_format" in text or "json_schema" in text or "json schema" in text


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
            stop_reason=getattr(response, "stop_reason", None),
            response_format="json_schema",
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
    by (model, effort, prompt_hash). The prompt hash covers the prompt version,
    the section text and the JSON schema, and the effort level changes what the
    model does, so a trace recorded at another effort is never replayed. A hit
    returns the stored call time and cost so that run logs built from replays
    carry the original values. With ``allow_api=True`` a miss is delegated to
    ``live`` and ``extract()`` records the new trace in the same file and in this
    backend's index (``remember``), so a repeated prompt in the same run is served
    from it. ``name`` equals the live backend's name so that ``FieldValue.system``
    labels stay identical between live and replay. Pointing ``trace_dir`` at an
    empty directory gives a second live run that never replays the first.
    """

    def __init__(
        self,
        backend_name: str,
        split: str,
        model: str,
        live: Backend | None = None,
        allow_api: bool = False,
        trace_dir: Path = TRACE_DIR,
        effort: str | None = None,
    ) -> None:
        if live is not None and live.effort != effort:
            # A live trace would be indexed under an effort it was not called with.
            raise LLMError(f"replay effort {effort!r} differs from the live backend's {live.effort!r}")
        self.name = backend_name
        self.split = split
        self.model = model
        self.live = live
        self.allow_api = allow_api
        self.effort = effort
        self.trace_dir = Path(trace_dir)
        self.path = trace_path(split, backend_name, self.trace_dir)
        self.hits = 0
        self.misses = 0
        self._index: dict[tuple[str, str | None, str], Trace] = {}
        for tr in read_traces(self.path):
            self.remember(tr)

    def remember(self, trace: Trace) -> None:
        """Index a trace (error traces are never served)."""
        if trace.error is None:
            self._index[(trace.model, trace.effort, trace.prompt_hash)] = trace

    def has(self, model: str, key: str) -> bool:
        """True when a stored trace for (model, this effort, prompt hash) exists."""
        return (model, self.effort, key) in self._index

    def stored(self) -> list[Trace]:
        """Every indexed (non-error) trace."""
        return list(self._index.values())

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any],
        model: str | None = None,
        key: str | None = None,
    ) -> BackendResult:
        model = model or self.model
        key = key or prompt_hash(system_prompt, user_prompt, json_schema)
        stored = self._index.get((model, self.effort, key))
        if stored is not None:
            self.hits += 1
            return BackendResult(
                text=stored.response_text or "",
                parsed_json=stored.response_json,
                model_reported=stored.model_reported,
                input_tokens=stored.input_tokens,
                output_tokens=stored.output_tokens,
                latency_s=stored.latency_s,
                stop_reason=stored.stop_reason,
                response_format=stored.response_format,
                from_replay=True,
                replay_trace_id=stored.trace_id,
                replay_called_at=stored.called_at,
                replay_cost_usd=stored.cost_usd,
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


def locate_quote(quote: str, sections: dict[str, Section]) -> tuple[Section, int, int] | None:
    """Exact (whitespace-normalized) substring search over the provided sections.

    Returns the section and the start and end offsets of the matched span within the
    original section text (the span may be longer than the quote when the document
    has extra whitespace), or ``None``. Fuzzy matching belongs to evaluate.py.
    """
    needle = _normalize_ws(quote)
    if not needle:
        return None
    for sec in sections.values():
        idx = sec.text.find(quote)
        if idx >= 0:
            return sec, idx, idx + len(quote)
        # Whitespace-insensitive search: map the normalized hit back to an offset.
        pattern = r"\s+".join(re.escape(tok) for tok in needle.split(" "))
        m = re.search(pattern, sec.text)
        if m:
            return sec, m.start(), m.end()
    return None


def _page_of(sec: Section, offset: int) -> int:
    page = sec.page_start + sec.text[:offset].count("\f")
    return min(page, sec.page_end)


def _quote_search_order(
    name: str, field_group: str, sections: dict[str, Section], home: dict[str, list[str]]
) -> dict[str, Section]:
    """Sections in the order a field's quote is searched: as given for a listed group; for
    ``all``, the field's own group's sections first (as in its split call), then the rest."""
    if field_group != ALL_GROUP:
        return sections
    first = {n: sections[n] for n in home.get(name, []) if n in sections}
    return {**first, **{n: s for n, s in sections.items() if n not in first}}


def map_response(
    doc: DocRecord | str,
    field_group: str,
    data: dict[str, Any] | None,
    sections: dict[str, Section],
    system: str,
) -> list[FieldValue]:
    """Map a validated-or-not response object to one ``FieldValue`` per field.

    Missing fields, malformed entries and values that violate the field spec
    become abstentions with the reason in ``notes``. ``page`` and ``scope`` come
    from the section where the quote is found; when the quote is not found the
    model's page is kept and ``notes`` says ``quote_not_located``. ``doc`` may be
    the record or its doc_id (only the id is used). For the combined group ``all``
    every field of every listed group gets one row, and each quote is searched in
    the field's own group's sections first (:func:`_quote_search_order`).
    """
    doc_id = doc if isinstance(doc, str) else doc.doc_id
    out: list[FieldValue] = []
    data = data or {}
    home = field_home_sections() if field_group == ALL_GROUP else {}
    for name in llm_field_group(field_group)["fields"]:
        spec = field_spec(name)
        entry = data.get(name)
        base = {"doc_id": doc_id, "field": name, "system": system}
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
        hit = locate_quote(quote, _quote_search_order(name, field_group, sections, home)) if quote else None
        if hit is None:
            fv.page = page
            fv.notes = "quote_missing" if not quote else "quote_not_located"
        else:
            sec, offset, end = hit
            fv.page = _page_of(sec, offset)
            fv.scope = sec.scope
            fv.char_start = sec.char_start + offset
            fv.char_end = sec.char_start + end
        out.append(fv)
    return out


# ----------------------------------------------------------------------------- extract


def _call_and_trace(
    doc_id: str,
    field_group: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    version: str,
    backend: Backend,
    split: str,
    trace_dir: Path,
    offsets: list[tuple[int, int]],
    mode: str | None = None,
) -> tuple[BackendResult | None, Trace]:
    """One backend call and its trace, shared by ``extract`` and ``extract_closed_book``.

    Returns ``(None, trace)`` when the call failed after retries (the trace carries the
    error). ``TraceMissError`` and ``DailyLimitExceeded`` propagate. A live trace is
    appended to ``{trace_dir}/{split}/{backend.name}.jsonl`` and, for a replay backend,
    added to its index; a replayed trace is not written again.
    """
    model = backend.model
    effort = backend.effort
    ph = prompt_hash(system_prompt, user_prompt, schema, version)
    called_at = datetime.now(UTC)
    base: dict[str, Any] = {
        "doc_id": doc_id,
        "field_group": field_group,
        "backend": backend.name,
        "model": model,
        "prompt_hash": ph,
        "section_offsets": offsets,
        "effort": effort,
        "mode": mode,
    }
    try:
        result = backend.complete(system_prompt, user_prompt, schema, model, key=ph)
    except (TraceMissError, DailyLimitExceeded):
        # A replay miss is the caller's decision; an exhausted daily budget would fail
        # every later call the same way, so the run must stop rather than record
        # abstentions (run.py halts on it). Other failures become a failed trace.
        raise
    except Exception as exc:
        trace = Trace(
            trace_id=make_trace_id(doc_id, field_group, model, ph, effort),
            called_at=called_at,
            error=f"{type(exc).__name__}: {exc}",
            **base,
        )
        append_trace(trace, split, trace_dir)
        return None, trace

    truncated = result.stop_reason in TRUNCATION_STOP_REASONS
    if truncated:
        error: str | None = str(result.stop_reason).lower()
    elif result.parsed_json is None:
        error = "response_not_json"
    else:
        error = None
    trace = Trace(
        trace_id=result.replay_trace_id or make_trace_id(doc_id, field_group, model, ph, effort),
        model_reported=result.model_reported,
        response_json=result.parsed_json,
        response_text=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.replay_cost_usd if result.from_replay and result.replay_cost_usd is not None
        else compute_cost(model, result.input_tokens, result.output_tokens),
        latency_s=result.latency_s,
        called_at=result.replay_called_at if result.from_replay and result.replay_called_at else called_at,
        error=error,
        stop_reason=result.stop_reason,
        response_format=result.response_format,
        **base,
    )
    if not result.from_replay:
        append_trace(trace, split, trace_dir)
        remember = getattr(backend, "remember", None)
        if callable(remember):
            remember(trace)
    return result, trace


def _all_abstain(doc_id: str, fields: list[str] | tuple[str, ...], system: str, note: str) -> list[FieldValue]:
    return [FieldValue(doc_id=doc_id, field=f, system=system, status="abstain", notes=note) for f in fields]


def system_name(backend: Backend, mode: str | None = None) -> str:
    """``llm:{backend}:{model}``, with ``:{mode}`` appended for the whole-document and
    closed-book variants (``FieldValue.system``)."""
    name = f"llm:{backend.name}:{backend.model}"
    return f"{name}:{mode}" if mode else name


def extract(
    doc: DocRecord,
    sections: dict[str, Section],
    field_group: str,
    backend: Backend,
    split: str,
    trace_dir: Path = TRACE_DIR,
    system: str | None = None,
    mode: str = "located",
) -> tuple[list[FieldValue], Trace]:
    """Run one LLM extraction for one field group and record its trace.

    Builds the prompt and schema, calls ``backend.complete``, validates the
    response and maps it to ``FieldValue`` records with ``system`` (default
    ``llm:{backend.name}:{model}``; the run stage passes the ``:whole`` name for
    the whole-document mode). The trace, with the backend's effort, is appended
    to ``{trace_dir}/{split}/{backend.name}.jsonl`` unless the result came from a
    replay. A failed call (after retries) is recorded with ``error`` set and
    every field is returned as an abstention; the caller decides whether to
    stop. ``mode`` (``located`` or ``whole``) is recorded in ``Trace.mode`` so the
    cost accounting can attribute the call to its run system. ``field_group`` is a
    listed group or ``all`` (every LLM field in one call), recorded in
    ``Trace.field_group``.
    """
    system = system or system_name(backend)
    fields = llm_field_group(field_group)["fields"]
    offsets = [(s.char_start, s.char_end) for s in sections.values()]
    result, trace = _call_and_trace(
        doc.doc_id, field_group, SYSTEM_PROMPT, build_prompt(doc, sections, field_group),
        build_schema(field_group), PROMPT_VERSION, backend, split, trace_dir, offsets, mode,
    )
    if result is None:
        return _all_abstain(doc.doc_id, fields, system, "call_failed"), trace
    if result.stop_reason in TRUNCATION_STOP_REASONS:
        # The output was cut off (or refused): not a model abstention, so every field
        # abstains with a note that evaluate can separate from a real abstention.
        return _all_abstain(doc.doc_id, fields, system, "truncated"), trace
    return map_response(doc, field_group, result.parsed_json, sections, system), trace


# ----------------------------------------------------------------------------- closed book


def map_closed_book(doc_id: str, data: dict[str, Any] | None, system: str) -> list[FieldValue]:
    """Map a closed-book response to one ``FieldValue`` per closed-book field.

    Statuses other than ``value`` and ``abstain``, missing entries and values that
    violate the field spec become abstentions with the reason in ``notes``. Values
    carry no quote, page or scope: the model saw no document.
    """
    out: list[FieldValue] = []
    data = data or {}
    for name in CLOSED_BOOK_FIELDS:
        base = {"doc_id": doc_id, "field": name, "system": system}
        entry = data.get(name)
        if not isinstance(entry, dict):
            out.append(FieldValue(**base, status="abstain", notes="missing_from_response"))
            continue
        status, value = entry.get("status"), entry.get("value")
        if status not in CLOSED_BOOK_STATUSES:
            out.append(FieldValue(**base, status="abstain", notes=f"invalid_status:{status!r}"))
        elif status == "abstain":
            out.append(FieldValue(**base, status="abstain"))
        elif value is None:
            out.append(FieldValue(**base, status="abstain", notes="value_missing"))
        elif err := _check_value(field_spec(name), value):
            out.append(FieldValue(**base, status="abstain", notes=f"invalid_value:{err}"))
        else:
            out.append(FieldValue(**base, status="value", value=value, notes="closed_book"))
    return out


def extract_closed_book(
    doc: DocRecord,
    backend: Backend,
    split: str,
    trace_dir: Path = TRACE_DIR,
    system: str | None = None,
) -> tuple[list[FieldValue], Trace]:
    """Closed-book contamination baseline for one document (analysis plan, "Leakage and
    contamination controls").

    The prompt carries only the bank name and the exam date
    (``prompts.build_closed_book_prompt``), the answers are ``overall_rating`` and
    ``finding_present``, and the trace is recorded under the field group
    ``closedbook`` with no section offsets and the hash of
    ``CLOSED_BOOK_PROMPT_VERSION``. ``system`` defaults to ``llm:{backend}:{model}:closedbook``.
    """
    system = system or system_name(backend, CLOSED_BOOK_GROUP)
    result, trace = _call_and_trace(
        doc.doc_id, CLOSED_BOOK_GROUP, CLOSED_BOOK_SYSTEM_PROMPT, build_closed_book_prompt(doc),
        build_closed_book_schema(), CLOSED_BOOK_PROMPT_VERSION, backend, split, trace_dir, [],
        CLOSED_BOOK_GROUP,
    )
    if result is None:
        return _all_abstain(doc.doc_id, CLOSED_BOOK_FIELDS, system, "call_failed"), trace
    if result.stop_reason in TRUNCATION_STOP_REASONS:
        return _all_abstain(doc.doc_id, CLOSED_BOOK_FIELDS, system, "truncated"), trace
    return map_closed_book(doc.doc_id, result.parsed_json, system), trace


def trace_values(trace: Trace, system: str | None = None) -> list[FieldValue]:
    """The ``FieldValue`` rows a stored trace stands for, as ``extract`` produced them.

    An error trace (failed call, truncation, non-JSON output) gives abstentions for
    every field of its group; a closed-book trace is mapped with ``map_closed_book``.
    Quotes are not located (no sections), so page and scope are not set. Used by
    run-to-run agreement on traces. ``system`` defaults to ``llm:{backend}:{model}``
    (``:closedbook`` for the closed-book group).
    """
    closed = trace.field_group == CLOSED_BOOK_GROUP
    if system is None:
        system = f"llm:{trace.backend}:{trace.model}" + (f":{CLOSED_BOOK_GROUP}" if closed else "")
    fields = list(CLOSED_BOOK_FIELDS) if closed else llm_field_group(trace.field_group)["fields"]
    if trace.error is not None:
        return _all_abstain(trace.doc_id, fields, system, trace.error)
    if closed:
        return map_closed_book(trace.doc_id, trace.response_json, system)
    return map_response(trace.doc_id, trace.field_group, trace.response_json, {}, system)


# ----------------------------------------------------------------------------- CLI


def sections_from_text(doc_id: str, text: str) -> dict[str, Section]:
    """Whole-document fallback: one institution-scope section covering every page
    (``locate.pages_from_text`` drops the empty element after pdftotext's final form feed)."""
    from cra_extract.locate import pages_from_text

    pages = pages_from_text(text)
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
    """Build a ``DocRecord`` for one sample under ``tests/fixtures/samples`` from its stem.

    The doc_id follows the manifest convention (``strata.FIXTURE_SAMPLES`` carries the
    exam dates), so a smoke trace is keyed like a pilot trace for the same document.
    """
    from cra_extract.strata import FIXTURE_SAMPLES

    parts = stem.split("_")
    regulator, bank_id = parts[0], parts[1]
    exam_date = FIXTURE_SAMPLES[stem][2] if stem in FIXTURE_SAMPLES else None
    return DocRecord(
        doc_id=make_doc_id(regulator, bank_id, exam_date) if exam_date else f"{regulator}_{bank_id}_{stem}",
        regulator=regulator,  # type: ignore[arg-type]
        bank_id=bank_id,
        bank_name=" ".join(parts[2:3]),
        exam_date=exam_date,
        method="isb" if "_ISB_" in stem else "small",
        url=f"file://{FIXTURES_DIR / 'samples' / (stem + '.pdf')}",
        split="dev",
    )


def make_backend(name: str, model: str, **kwargs: Any) -> Backend:
    """Instantiate a live backend by CLI name (``kwargs`` such as ``limiter`` and
    ``effort`` go to the backend's constructor)."""
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
    ap.add_argument(
        "--field-group", default=ALL_GROUP,
        help="a field group of fields.yaml, or all (every LLM field in one call; the run default)",
    )
    ap.add_argument("--split", default="dev")
    ap.add_argument(
        "--effort", default=None,
        help="effort level (default: DEFAULT_EFFORT for the backend; 'none' sends no effort parameter)",
    )
    args = ap.parse_args(argv)
    if not args.smoke:
        ap.error("only --smoke is implemented; the pipeline calls extract() from run.py")

    text = (FIXTURES_DIR / "samples" / f"{args.sample}.txt").read_text(encoding="utf-8")
    doc = sample_doc(args.sample)
    sections = sections_from_text(doc.doc_id, text)
    effort = DEFAULT_EFFORT.get(args.backend) if args.effort is None else (None if args.effort == "none" else args.effort)
    backend = make_backend(args.backend, args.model, effort=effort)
    values, trace = extract(doc, sections, args.field_group, backend, args.split)
    for fv in values:
        print(f"{fv.field:28s} {fv.status:8s} {fv.value!r:30s} p{fv.page} {fv.scope} | {fv.quote}")
    print(
        f"\nmodel={trace.model} reported={trace.model_reported} "
        f"in={trace.input_tokens} out={trace.output_tokens} "
        f"cost={'n/a' if trace.cost_usd is None else f'${trace.cost_usd:.4f}'} "
        f"latency={trace.latency_s}s effort={trace.effort} error={trace.error}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
