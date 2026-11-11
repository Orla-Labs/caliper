"""Any Amazon Bedrock model through the Converse API (``bedrock_converse``).

Install with ``caliper-eval[bedrock-converse]`` (boto3); imported lazily. Use it
for models the Messages API on Bedrock does not serve (Amazon Nova, Llama,
Mistral, ...), or to evaluate a Claude model exactly as a Converse-based
production feature calls it.

Wire shape (botocore ``bedrock-runtime`` service model, botocore 1.44.0, and a
live call on 2026-11-11):

* ``modelId`` is the candidate model: a base ID, or an inference profile ID
  (``global.amazon.nova-2-lite-v1:0``). The profile prefix also sets the
  price tier (see :mod:`caliper_eval.aws_pricing`).
* Neutral request -> ``system: [{"text": ...}]``, ``messages`` with
  ``content: [{"text": ...}]`` (string content or ``text`` blocks; base64
  ``image`` blocks become ``{"image": {"format", "source": {"bytes"}}}``).
  ``max_tokens`` / ``temperature`` / ``top_p`` / ``stop_sequences`` go to
  ``inferenceConfig``.
* JSON_SCHEMA -> ``outputConfig.textFormat = {"type": "json_schema",
  "structure": {"jsonSchema": {"schema": "<json string>", "name": ...}}}``.
* TOOL -> one ``toolConfig.tools[].toolSpec`` with ``inputSchema.json`` and
  ``toolChoice: {"tool": {"name": "caliper_output"}}``.
* ``params`` keys that are Converse body fields (``additionalModelRequestFields``,
  ``inferenceConfig``, ``outputConfig``, ``performanceConfig``,
  ``serviceTier``, ``toolConfig``, ...) are deep-merged into the body as
  given; ``max_tokens`` / ``temperature`` / ``top_p`` / ``stop_sequences`` go
  to ``inferenceConfig``. Anything else is an error, never silently dropped.
* Thinking has no portable Converse switch: each model family takes its own
  field under ``additionalModelRequestFields``. ``thinking = "on"`` is
  rejected locally; put the model's field in ``params``. Nova 2 Lite, for
  example::

      params = { additionalModelRequestFields = { reasoningConfig =
                 { type = "enabled", maxReasoningEffort = "low" } } }

  Reasoning tokens are billed and counted in ``usage.outputTokens`` (a live
  Nova 2 Lite call: 59 output tokens without reasoning, 754 with it).
* usage: ``inputTokens`` (uncached), ``outputTokens``,
  ``cacheReadInputTokens``, ``cacheWriteInputTokens``.
* **Who answered:** the Converse response does not name the model that
  served it. ``ServedBy`` is left empty (reported as "unknown") rather than
  copied from the request; preflight accepts the shape on any non-error
  response and says so.
"""

from __future__ import annotations

import base64
import copy
import json
import time
from collections.abc import Callable
from typing import Any

from caliper_eval.providers.base import (
    OUTPUT_TOOL_NAME,
    PreflightResult,
    Provider,
    deep_merge,
    error_text,
    minimal_request,
)
from caliper_eval.types import Candidate, OutputMode, ProviderKind, Reply, ThinkingMode, Usage

ClientFactory = Callable[[Candidate], Any]

_BODY_FIELDS = {
    "additionalModelRequestFields",
    "additionalModelResponseFieldPaths",
    "guardrailConfig",
    "inferenceConfig",
    "outputConfig",
    "performanceConfig",
    "requestMetadata",
    "serviceTier",
    "toolConfig",
}
_INFERENCE = {
    "max_tokens": "maxTokens",
    "temperature": "temperature",
    "top_p": "topP",
    "stop_sequences": "stopSequences",
}


class ShapeError(ValueError):
    """The candidate or golden request asks for something Converse cannot express."""


def _content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    blocks: list[dict[str, Any]] = []
    for block in content or []:
        kind = block.get("type")
        if kind == "text":
            blocks.append({"text": block.get("text", "")})
        elif kind == "image" and (block.get("source") or {}).get("type") == "base64":
            src = block["source"]
            fmt = str(src.get("media_type", "image/png")).split("/")[-1]
            blocks.append(
                {"image": {"format": fmt, "source": {"bytes": base64.b64decode(src["data"])}}}
            )
        else:
            raise ShapeError(f"content block type {kind!r} is not mapped to Converse")
    return blocks


def build_body(request: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    """Neutral golden request + candidate overlay -> Converse kwargs (a new dict)."""
    if candidate.thinking == ThinkingMode.ON:
        raise ShapeError(
            'bedrock_converse has no portable thinking switch; set thinking = "off" and put the '
            "model's field in params.additionalModelRequestFields (e.g. Nova reasoningConfig)"
        )
    req = copy.deepcopy(request)
    body: dict[str, Any] = {"modelId": candidate.model}
    system = req.pop("system", None)
    if isinstance(system, str):
        body["system"] = [{"text": system}]
    elif system:
        body["system"] = [{"text": b.get("text", "")} for b in system]
    body["messages"] = [
        {"role": m["role"], "content": _content(m.get("content"))} for m in req.pop("messages", [])
    ]
    inference: dict[str, Any] = {}
    for key, wire in _INFERENCE.items():
        if key in req:
            inference[wire] = req.pop(key)
    if req:
        raise ShapeError(f"golden request fields not mapped to Converse: {sorted(req)}")

    if candidate.output_mode != OutputMode.TEXT and candidate.output_schema is None:
        raise ShapeError(f"output_mode={candidate.output_mode} needs output_schema")
    if candidate.output_mode == OutputMode.JSON_SCHEMA:
        body["outputConfig"] = {
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "schema": json.dumps(candidate.output_schema),
                        "name": OUTPUT_TOOL_NAME,
                    }
                },
            }
        }
    elif candidate.output_mode == OutputMode.TOOL:
        body["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": OUTPUT_TOOL_NAME,
                        "description": "Return the final answer as structured output.",
                        "inputSchema": {"json": copy.deepcopy(candidate.output_schema)},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": OUTPUT_TOOL_NAME}},
        }

    for key, value in candidate.params.items():
        if key in _INFERENCE:
            inference[_INFERENCE[key]] = copy.deepcopy(value)
        elif key in _BODY_FIELDS:
            body = deep_merge(body, {key: value})
        else:
            raise ShapeError(
                f"param {key!r} is not a Converse field; use one of "
                f"{sorted(_BODY_FIELDS | set(_INFERENCE))}"
            )
    if inference:
        body = deep_merge(body, {"inferenceConfig": inference})
    return body


def parse_response(data: dict[str, Any], candidate: Candidate) -> Reply:
    """Converse response -> Reply (no ServedBy: Converse does not report it)."""
    u = data.get("usage") or {}
    usage = Usage(
        input_tokens=u.get("inputTokens") or 0,
        output_tokens=u.get("outputTokens") or 0,
        cache_read_tokens=u.get("cacheReadInputTokens") or 0,
        cache_write_tokens=u.get("cacheWriteInputTokens") or 0,
    )
    blocks = ((data.get("output") or {}).get("message") or {}).get("content") or []
    texts = [b["text"] for b in blocks if "text" in b]
    text: str | None = "".join(texts) if texts else None
    parsed: Any = None
    error: str | None = None
    stop = data.get("stopReason")
    if stop in ("guardrail_intervened", "content_filtered"):
        error = f"stopReason={stop}"
    elif candidate.output_mode == OutputMode.TOOL:
        call = next(
            (
                b["toolUse"]
                for b in blocks
                if "toolUse" in b and b["toolUse"].get("name") == OUTPUT_TOOL_NAME
            ),
            None,
        )
        if call is None:
            error = f"no {OUTPUT_TOOL_NAME} tool call in response (stopReason={stop})"
        else:
            parsed = call.get("input")
            text = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    elif candidate.output_mode == OutputMode.JSON_SCHEMA:
        try:
            parsed = json.loads(text or "")
        except json.JSONDecodeError as exc:
            error = f"structured output is not valid JSON (stopReason={stop}): {exc}"
    return Reply(text=text, parsed=parsed, served_by=None, usage=usage, error=error)


def default_client(candidate: Candidate) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ImportError(
            "provider 'bedrock_converse' needs boto3: pip install 'caliper-eval[bedrock-converse]'"
        ) from exc
    return boto3.client("bedrock-runtime", region_name=candidate.region)


class BedrockConverseProvider(Provider):
    kind = ProviderKind.BEDROCK_CONVERSE

    def __init__(self, *, client_factory: ClientFactory | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._factory = client_factory or default_client
        self._clients: dict[str | None, Any] = {}

    def _client(self, candidate: Candidate) -> Any:
        if candidate.region not in self._clients:
            self._clients[candidate.region] = self._factory(candidate)
        return self._clients[candidate.region]

    def send(
        self,
        request: dict[str, Any],
        candidate: Candidate,
        *,
        row_id: str,
        replica: int,
        sample: int,
    ) -> Reply:
        try:
            body = build_body(request, candidate)
            started = time.perf_counter()
            data = self._client(candidate).converse(**body)
            latency = (time.perf_counter() - started) * 1000
        except Exception as exc:
            return Reply(error=error_text(exc))
        reply = parse_response(data, candidate)
        return reply.model_copy(
            update={"latency_ms": latency, "cost_usd": self.cost(reply.usage, candidate)}
        )

    def preflight(
        self, candidate: Candidate, *, sample_request: dict[str, Any] | None = None
    ) -> PreflightResult:
        """Shape accepted when a response comes back without a provider error. Converse
        does not name the serving model, so a substitution cannot be detected here."""
        reply = self.send(
            minimal_request(candidate), candidate, row_id="__preflight__", replica=0, sample=0
        )
        if reply.error is not None and reply.usage.output_tokens == 0:
            return PreflightResult(
                candidate=candidate.name,
                ok=False,
                reason=f"bedrock_converse rejected the request shape (model={candidate.model}, "
                f"output_mode={candidate.output_mode}): {reply.error}",
            )
        note = "shape accepted; Converse does not report which model served the request"
        if reply.error:
            note += f"; preflight answer note: {reply.error}"
        return PreflightResult(candidate=candidate.name, ok=True, reason=note)
