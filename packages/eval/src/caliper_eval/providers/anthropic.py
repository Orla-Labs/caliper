"""Anthropic Messages API, direct (``anthropic``) and on Amazon Bedrock (``bedrock``).

Install with ``caliper-eval[anthropic]`` or ``caliper-eval[bedrock]``; the SDK is
imported lazily.

Wire shapes (Claude API docs via the claude-api skill, cached 2026-10-06, and
https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock,
fetched 2026-11-06):

* thinking ON  -> ``thinking: {"type": "adaptive"}``; with
  ``thinking_budget_tokens`` -> ``{"type": "enabled", "budget_tokens": N}``
  (older models only).
* thinking OFF -> ``thinking: {"type": "disabled"}``, sent explicitly. Some
  models reject it (e.g. Claude Opus 5.5 and Claude Sonnet 5.5 400). That is
  on purpose: preflight reports it instead of silently evaluating a thinking
  model. ``params = {thinking = {...}}`` is a raw passthrough for other modes
  (e.g. ``{"type": "between_tools"}`` on Claude Sonnet 5.5).
* output JSON_SCHEMA -> ``output_config: {"format": {"type": "json_schema",
  "schema": ...}}``; the first text block holds the JSON.
* output TOOL -> one strict tool named ``caliper_output`` plus forced
  ``tool_choice: {"type": "tool", "name": "caliper_output"}``; the tool_use
  input is the structured output. Models that reject forced tool use
  (Opus 5.5, Sonnet 5.5, Fable 5.1) fail preflight.
* ``params.effort`` -> ``output_config.effort`` (merged with ``format``).
  Other params go to the body as-is. Unknown names (e.g. ``temperature``) go
  through ``extra_body``, so the API, not the SDK, decides.
* usage: ``input_tokens`` (uncached remainder), ``output_tokens``,
  ``cache_read_input_tokens``, ``cache_creation_input_tokens``.
* ServedBy comes from the response ``model`` field.
* Bedrock: ``AnthropicBedrockMantle(aws_region=...)``, model IDs carry an
  ``anthropic.`` prefix (``anthropic.claude-opus-5-5``); same body shape.

Server-side ``fallbacks`` is never added: a fallback is exactly the silent
substitution this tool must surface. If a user opts into it via params, the
response ``model`` differs and ``ServedBy.matches_request`` is False.
"""

from __future__ import annotations

import copy
import inspect
import json
import time
from collections.abc import Callable
from typing import Any

from caliper_eval.providers.base import OUTPUT_TOOL_NAME, Provider, error_text, models_match
from caliper_eval.types import (
    Candidate,
    OutputMode,
    ProviderKind,
    Reply,
    ServedBy,
    ThinkingMode,
    Usage,
)

ClientFactory = Callable[[Candidate], Any]


def build_body(request: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    """Neutral golden request + candidate overlay -> Messages API body (a new dict)."""
    body = copy.deepcopy(request)
    if body.get("system") is None:
        body.pop("system", None)
    body["model"] = candidate.model

    if candidate.thinking == ThinkingMode.ON:
        if candidate.thinking_budget_tokens is not None:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": candidate.thinking_budget_tokens,
            }
        else:
            body["thinking"] = {"type": "adaptive"}
    else:
        body["thinking"] = {"type": "disabled"}

    output_config: dict[str, Any] = dict(body.get("output_config") or {})
    if candidate.output_mode == OutputMode.JSON_SCHEMA:
        output_config["format"] = {
            "type": "json_schema",
            "schema": copy.deepcopy(_require_schema(candidate)),
        }
    elif candidate.output_mode == OutputMode.TOOL:
        tools = list(body.get("tools") or [])
        tools.append(
            {
                "name": OUTPUT_TOOL_NAME,
                "description": "Return the final answer as structured output.",
                "strict": True,
                "input_schema": copy.deepcopy(_require_schema(candidate)),
            }
        )
        body["tools"] = tools
        body["tool_choice"] = {"type": "tool", "name": OUTPUT_TOOL_NAME}

    for key, value in candidate.params.items():
        if key == "effort":
            output_config["effort"] = value
        elif key == "output_config":
            output_config.update(copy.deepcopy(value))
        else:
            body[key] = copy.deepcopy(value)
    if output_config:
        body["output_config"] = output_config
    return body


def _require_schema(candidate: Candidate) -> dict[str, Any]:
    if candidate.output_schema is None:
        raise ValueError(
            f"candidate {candidate.name!r}: output_mode={candidate.output_mode} needs output_schema"
        )
    return candidate.output_schema


def parse_response(
    data: dict[str, Any], candidate: Candidate, kind: ProviderKind, endpoint: str | None
) -> Reply:
    """Messages API response (as a dict) -> Reply (cost not filled in)."""
    served_model = data.get("model") or ""
    served = ServedBy(
        provider=kind,
        model=served_model,
        endpoint=endpoint,
        matches_request=models_match(candidate.model, served_model),
    )
    u = data.get("usage") or {}
    usage = Usage(
        input_tokens=u.get("input_tokens") or 0,
        output_tokens=u.get("output_tokens") or 0,
        cache_read_tokens=u.get("cache_read_input_tokens") or 0,
        cache_write_tokens=u.get("cache_creation_input_tokens") or 0,
    )
    blocks = data.get("content") or []
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    text: str | None = "".join(texts) if texts else None
    parsed: Any = None
    error: str | None = None

    stop = data.get("stop_reason")
    if stop == "refusal":
        details = data.get("stop_details") or {}
        error = f"refusal (category={details.get('category')})"
    elif candidate.output_mode == OutputMode.TOOL:
        call = next(
            (
                b
                for b in blocks
                if b.get("type") == "tool_use" and b.get("name") == OUTPUT_TOOL_NAME
            ),
            None,
        )
        if call is None:
            error = f"no {OUTPUT_TOOL_NAME} tool call in response (stop_reason={stop})"
        else:
            parsed = call.get("input")
            text = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    elif candidate.output_mode == OutputMode.JSON_SCHEMA:
        try:
            parsed = json.loads(texts[0]) if texts else None
            if parsed is None:
                error = f"no structured output text block (stop_reason={stop})"
        except json.JSONDecodeError as exc:
            error = f"structured output is not valid JSON (stop_reason={stop}): {exc}"
    return Reply(text=text, parsed=parsed, served_by=served, usage=usage, error=error)


def default_client(candidate: Candidate) -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on install
        extra = "bedrock" if candidate.provider == ProviderKind.BEDROCK else "anthropic"
        raise ImportError(
            f"provider {candidate.provider!r} needs the SDK: pip install 'caliper-eval[{extra}]'"
        ) from exc
    if candidate.provider == ProviderKind.BEDROCK:
        return anthropic.AnthropicBedrockMantle(aws_region=candidate.region)
    if candidate.base_url:
        return anthropic.Anthropic(base_url=candidate.base_url)
    return anthropic.Anthropic()


def _to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    dump = getattr(message, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise TypeError(f"unexpected response type {type(message).__name__}")


def _split_known(create: Callable[..., Any], body: dict[str, Any]) -> dict[str, Any]:
    """Pass params the SDK signature knows by name; the rest via ``extra_body``."""
    try:
        params = inspect.signature(create).parameters
    except (TypeError, ValueError):
        return body
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return body
    known = {k: v for k, v in body.items() if k in params}
    extra = {k: v for k, v in body.items() if k not in params}
    if extra:
        known["extra_body"] = extra
    return known


class AnthropicProvider(Provider):
    def __init__(
        self,
        *,
        kind: ProviderKind = ProviderKind.ANTHROPIC,
        client_factory: ClientFactory | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.kind = kind
        self._factory = client_factory or default_client
        self._clients: dict[tuple[str | None, str | None], Any] = {}

    def _client(self, candidate: Candidate) -> Any:
        key = (candidate.base_url, candidate.region)
        if key not in self._clients:
            self._clients[key] = self._factory(candidate)
        return self._clients[key]

    def _endpoint(self, candidate: Candidate) -> str:
        if self.kind == ProviderKind.BEDROCK:
            return f"bedrock-mantle:{candidate.region or 'env'}"
        return candidate.base_url or "api.anthropic.com"

    def send(
        self,
        request: dict[str, Any],
        candidate: Candidate,
        *,
        row_id: str,
        replica: int,
        sample: int,
    ) -> Reply:
        endpoint = self._endpoint(candidate)
        try:
            body = build_body(request, candidate)
            create = self._client(candidate).messages.create
            started = time.perf_counter()
            message = create(**_split_known(create, body))
            latency = (time.perf_counter() - started) * 1000
        except Exception as exc:
            return Reply(error=error_text(exc))
        reply = parse_response(_to_dict(message), candidate, self.kind, endpoint)
        served = reply.served_by.model if reply.served_by else candidate.model
        return reply.model_copy(
            update={"latency_ms": latency, "cost_usd": self.cost(reply.usage, served or "")}
        )
