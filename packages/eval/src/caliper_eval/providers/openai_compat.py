"""Any OpenAI-compatible chat-completions endpoint, over plain HTTP (httpx).

``base_url`` comes from the candidate (default ``https://api.openai.com/v1``).
The API key is read from the env var named by ``params.api_key_env`` (default
``OPENAI_API_KEY``). That key is not sent in the body.

Wire shape (from the widely implemented chat-completions convention; NOT
re-verified against any vendor's docs - check your endpoint with preflight):

* ``POST {base_url}/chat/completions`` with ``Authorization: Bearer <key>``.
* ``messages``: the neutral ``system`` becomes a leading system message.
* JSON_SCHEMA -> ``response_format: {"type": "json_schema", "json_schema":
  {"name": "caliper_output", "schema": ..., "strict": true}}``.
* TOOL -> one function tool + forced ``tool_choice: {"type": "function",
  "function": {"name": "caliper_output"}}``; arguments (a JSON string) parsed.
* usage: ``prompt_tokens`` (includes cached), ``completion_tokens``,
  ``prompt_tokens_details.cached_tokens``. Cached tokens are subtracted from
  input so they are not charged twice.
* ServedBy from the response ``model`` field.

Thinking has no portable switch across vendors. ``thinking = "on"`` is
rejected locally with instructions to set ``thinking = "off"`` and put the
vendor's own parameter (``reasoning_effort``, ...) in ``params``.
"""

from __future__ import annotations

import copy
import json
import os
import time
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

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_LOCAL_PARAMS = {"api_key_env"}


class ShapeError(ValueError):
    """The candidate asks for a shape this provider cannot express."""


def build_body(request: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    if candidate.thinking == ThinkingMode.ON:
        raise ShapeError(
            'openai_compat has no portable thinking switch; set thinking = "off" and put the '
            "vendor parameter (e.g. reasoning_effort) in params"
        )
    req = copy.deepcopy(request)
    system = req.pop("system", None)
    messages = list(req.pop("messages", []))
    if system is not None:
        messages.insert(0, {"role": "system", "content": system})
    body: dict[str, Any] = {**req, "model": candidate.model, "messages": messages}

    if candidate.output_mode != OutputMode.TEXT and candidate.output_schema is None:
        raise ShapeError(f"output_mode={candidate.output_mode} needs output_schema")
    if candidate.output_mode == OutputMode.JSON_SCHEMA:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": OUTPUT_TOOL_NAME,
                "schema": copy.deepcopy(candidate.output_schema),
                "strict": True,
            },
        }
    elif candidate.output_mode == OutputMode.TOOL:
        tools = list(body.get("tools") or [])
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": OUTPUT_TOOL_NAME,
                    "description": "Return the final answer as structured output.",
                    "parameters": copy.deepcopy(candidate.output_schema),
                },
            }
        )
        body["tools"] = tools
        body["tool_choice"] = {"type": "function", "function": {"name": OUTPUT_TOOL_NAME}}

    for key, value in candidate.params.items():
        if key not in _LOCAL_PARAMS:
            body[key] = copy.deepcopy(value)
    if "max_completion_tokens" in body:
        body.pop("max_tokens", None)
    return body


def parse_response(data: dict[str, Any], candidate: Candidate, endpoint: str) -> Reply:
    served_model = data.get("model") or ""
    served = ServedBy(
        provider=ProviderKind.OPENAI_COMPAT,
        model=served_model,
        endpoint=endpoint,
        matches_request=models_match(candidate.model, served_model),
    )
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    usage = Usage(
        input_tokens=max(0, (u.get("prompt_tokens") or 0) - cached),
        output_tokens=u.get("completion_tokens") or 0,
        cache_read_tokens=cached,
    )
    choices = data.get("choices") or []
    if not choices:
        return Reply(served_by=served, usage=usage, error="response has no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    text = message.get("content")
    parsed: Any = None
    error: str | None = None
    finish = choice.get("finish_reason")
    if message.get("refusal"):
        error = f"refusal: {message['refusal']}"
    elif finish == "content_filter":
        error = "finish_reason=content_filter"
    elif candidate.output_mode == OutputMode.TOOL:
        calls = [
            c
            for c in message.get("tool_calls") or []
            if (c.get("function") or {}).get("name") == OUTPUT_TOOL_NAME
        ]
        if not calls:
            error = f"no {OUTPUT_TOOL_NAME} tool call in response (finish_reason={finish})"
        else:
            text = calls[0]["function"].get("arguments")
            try:
                parsed = json.loads(text or "")
            except json.JSONDecodeError as exc:
                error = f"tool arguments are not valid JSON: {exc}"
    elif candidate.output_mode == OutputMode.JSON_SCHEMA:
        try:
            parsed = json.loads(text or "")
        except json.JSONDecodeError as exc:
            error = f"structured output is not valid JSON (finish_reason={finish}): {exc}"
    return Reply(text=text, parsed=parsed, served_by=served, usage=usage, error=error)


class OpenAICompatProvider(Provider):
    kind = ProviderKind.OPENAI_COMPAT

    def __init__(self, *, http_client: Any = None, timeout: float = 120.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http_client
        self.timeout = timeout

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self.timeout)
        return self._http

    def send(
        self,
        request: dict[str, Any],
        candidate: Candidate,
        *,
        row_id: str,
        replica: int,
        sample: int,
    ) -> Reply:
        base_url = (candidate.base_url or DEFAULT_BASE_URL).rstrip("/")
        try:
            body = build_body(request, candidate)
            key_env = candidate.params.get("api_key_env", "OPENAI_API_KEY")
            key = os.environ.get(key_env)
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            started = time.perf_counter()
            resp = self._client().post(f"{base_url}/chat/completions", json=body, headers=headers)
            latency = (time.perf_counter() - started) * 1000
            if resp.status_code >= 400:
                return Reply(error=f"HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
        except Exception as exc:
            return Reply(error=error_text(exc))
        reply = parse_response(data, candidate, base_url)
        served = (reply.served_by.model if reply.served_by else "") or candidate.model
        return reply.model_copy(
            update={"latency_ms": latency, "cost_usd": self.cost(reply.usage, served)}
        )
