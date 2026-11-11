"""Real-provider wire shapes, with all network mocked. No keys, no paid calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from caliper_eval.preflight import run_preflight
from caliper_eval.providers import anthropic as anth
from caliper_eval.providers import get_provider
from caliper_eval.providers import openai_compat as oai
from caliper_eval.providers.anthropic import AnthropicProvider
from caliper_eval.providers.openai_compat import OpenAICompatProvider
from caliper_eval.types import Candidate, Price, ProviderKind

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
    "additionalProperties": False,
}
REQUEST = {
    "system": "Classify.",
    "messages": [{"role": "user", "content": "record 1"}],
    "max_tokens": 256,
}


def cand(**kw) -> Candidate:
    base = {"name": "c", "provider": "anthropic", "model": "claude-opus-5-5"}
    return Candidate(**{**base, **kw})


# --------------------------------------------------------------------------- anthropic body


def test_anthropic_body_text_thinking_off_is_explicit():
    body = anth.build_body(REQUEST, cand())
    assert body == {
        "system": "Classify.",
        "messages": [{"role": "user", "content": "record 1"}],
        "max_tokens": 256,
        "model": "claude-opus-5-5",
        "thinking": {"type": "disabled"},
    }
    assert "model" not in REQUEST  # never mutates the stored request


def test_anthropic_body_thinking_modes():
    assert anth.build_body(REQUEST, cand(thinking="on"))["thinking"] == {"type": "adaptive"}
    legacy = anth.build_body(
        REQUEST, cand(model="claude-haiku-4-5", thinking="on", thinking_budget_tokens=2048)
    )
    assert legacy["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    raw = anth.build_body(
        REQUEST, cand(model="claude-sonnet-5-5", params={"thinking": {"type": "between_tools"}})
    )
    assert raw["thinking"] == {"type": "between_tools"}


def test_anthropic_body_json_schema_merges_effort():
    body = anth.build_body(
        {**REQUEST, "system": None},
        cand(
            thinking="on",
            output_mode="json_schema",
            output_schema=SCHEMA,
            params={"effort": "low"},
        ),
    )
    assert "system" not in body
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA},
        "effort": "low",
    }


def test_anthropic_body_tool_mode_forces_tool_and_keeps_existing_tools():
    req = {**REQUEST, "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]}
    body = anth.build_body(req, cand(output_mode="tool", output_schema=SCHEMA))
    assert [t["name"] for t in body["tools"]] == ["lookup", "caliper_output"]
    assert body["tools"][1]["strict"] is True
    assert body["tools"][1]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": "caliper_output"}
    assert len(req["tools"]) == 1


def test_anthropic_body_requires_schema():
    with pytest.raises(ValueError, match="output_schema"):
        anth.build_body(REQUEST, cand(output_mode="json_schema"))


# --------------------------------------------------------------------------- anthropic parse


def _msg(content, model="claude-opus-5-5", stop="end_turn", **usage):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_details": None,
        "usage": {
            "input_tokens": usage.get("i", 100),
            "output_tokens": usage.get("o", 10),
            "cache_read_input_tokens": usage.get("cr", 0),
            "cache_creation_input_tokens": usage.get("cw", 0),
        },
    }


def test_anthropic_parse_text_usage_and_served_by():
    data = _msg(
        [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "approve"}], cr=50, cw=7
    )
    r = anth.parse_response(data, cand(), ProviderKind.ANTHROPIC, "api.anthropic.com")
    assert r.text == "approve" and r.error is None
    assert (r.usage.input_tokens, r.usage.output_tokens) == (100, 10)
    assert (r.usage.cache_read_tokens, r.usage.cache_write_tokens) == (50, 7)
    assert r.served_by.model == "claude-opus-5-5" and r.served_by.matches_request


def test_anthropic_parse_fallback_model_is_flagged():
    data = _msg([{"type": "text", "text": "x"}], model="claude-opus-5")
    r = anth.parse_response(data, cand(), ProviderKind.ANTHROPIC, None)
    assert r.served_by.matches_request is False


def test_anthropic_parse_structured_and_tool_and_refusal():
    js = cand(output_mode="json_schema", output_schema=SCHEMA)
    ok = anth.parse_response(
        _msg([{"type": "text", "text": '{"label": "a"}'}]), js, "anthropic", None
    )
    assert ok.parsed == {"label": "a"}
    bad = anth.parse_response(
        _msg([{"type": "text", "text": '{"lab'}], stop="max_tokens"), js, "anthropic", None
    )
    assert "max_tokens" in bad.error
    tool = cand(output_mode="tool", output_schema=SCHEMA)
    call = {"type": "tool_use", "id": "t", "name": "caliper_output", "input": {"label": "b"}}
    r = anth.parse_response(_msg([call], stop="tool_use"), tool, "anthropic", None)
    assert r.parsed == {"label": "b"} and json.loads(r.text) == {"label": "b"}
    missing = anth.parse_response(_msg([{"type": "text", "text": "hi"}]), tool, "anthropic", None)
    assert "no caliper_output" in missing.error
    refusal = _msg([], stop="refusal")
    refusal["stop_details"] = {"type": "refusal", "category": "cyber"}
    assert "cyber" in anth.parse_response(refusal, cand(), "anthropic", None).error


# --------------------------------------------------------------------------- anthropic client


class StubMessages:
    """Mimics the SDK's keyword-only messages.create signature (a subset)."""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response
        self.exc = exc

    def create(
        self,
        *,
        max_tokens,
        messages,
        model,
        system=None,
        thinking=None,
        output_config=None,
        tools=None,
        tool_choice=None,
        extra_body=None,
    ):
        self.calls.append({k: v for k, v in locals().items() if k != "self" and v is not None})
        if self.exc:
            raise self.exc
        return SimpleNamespace(model_dump=lambda mode="json": self.response)


class StatusError(Exception):
    status_code = 400


def _provider(messages, kind=ProviderKind.ANTHROPIC):
    made = []

    def factory(c):
        made.append(c.name)
        return SimpleNamespace(messages=messages)

    return AnthropicProvider(kind=kind, client_factory=factory), made


def test_anthropic_send_cost_latency_and_extra_body():
    messages = StubMessages(response=_msg([{"type": "text", "text": "ok"}], i=1_000_000, o=0))
    provider, made = _provider(messages)
    c = cand(params={"temperature": 0.0})
    r1 = provider.send(REQUEST, c, row_id="r", replica=0, sample=0)
    provider.send(REQUEST, c, row_id="r", replica=0, sample=1)
    assert made == ["c"]  # client reused
    assert r1.cost_usd == pytest.approx(4.0)  # 1M input tokens of claude-opus-5-5
    assert r1.latency_ms >= 0
    sent = messages.calls[0]
    assert sent["extra_body"] == {"temperature": 0.0}
    assert sent["thinking"] == {"type": "disabled"}


def test_anthropic_errors_become_reply_errors_and_fail_preflight():
    messages = StubMessages(exc=StatusError("thinking.type disabled is not supported"))
    provider, _ = _provider(messages)
    r = provider.send(REQUEST, cand(), row_id="r", replica=0, sample=0)
    assert r.error.startswith("StatusError (HTTP 400)")
    pf = provider.preflight(cand())
    assert not pf.ok and "rejected the request shape" in pf.reason
    assert "thinking=off" in pf.reason
    sent = messages.calls[-1]
    assert sent["max_tokens"] == 64 and sent["thinking"] == {"type": "disabled"}


def test_anthropic_preflight_detects_substitution():
    messages = StubMessages(response=_msg([{"type": "text", "text": "OK"}], model="claude-opus-5"))
    provider, _ = _provider(messages)
    pf = provider.preflight(cand(thinking="on"))
    assert not pf.ok and "claude-opus-5" in pf.reason


def test_bedrock_model_ids_and_endpoint():
    messages = StubMessages(
        response=_msg([{"type": "text", "text": "OK"}], model="anthropic.claude-opus-5-5")
    )
    provider, _ = _provider(messages, kind=ProviderKind.BEDROCK)
    c = cand(
        provider="bedrock", model="anthropic.claude-opus-5-5", region="us-east-1", thinking="on"
    )
    pf = provider.preflight(c)
    assert pf.ok
    assert pf.served_by.provider == "bedrock"
    assert pf.served_by.endpoint == "bedrock-mantle:us-east-1"
    assert messages.calls[0]["model"] == "anthropic.claude-opus-5-5"
    # served without the prefix still counts as the requested model
    assert anth.parse_response(
        _msg([], model="claude-opus-5-5"), c, ProviderKind.BEDROCK, None
    ).served_by.matches_request


def test_missing_sdk_is_a_preflight_failure_not_a_crash(monkeypatch):
    def boom(c):
        raise ImportError("provider 'anthropic' needs the SDK")

    monkeypatch.setattr(anth, "default_client", boom)
    res = run_preflight([cand()])
    assert not res[0].ok and "needs the SDK" in res[0].reason


def test_get_provider_kinds():
    assert isinstance(get_provider(cand()), AnthropicProvider)
    assert get_provider(cand(provider="bedrock")).kind == ProviderKind.BEDROCK
    assert isinstance(get_provider(cand(provider="openai_compat")), OpenAICompatProvider)


# --------------------------------------------------------------------------- openai-compatible


def oc(**kw) -> Candidate:
    base = {
        "name": "o",
        "provider": "openai_compat",
        "model": "vendor-model",
        "base_url": "https://llm.example.test/v1/",
    }
    return Candidate(**{**base, **kw})


def test_openai_body_shapes():
    body = oai.build_body(REQUEST, oc(params={"api_key_env": "X", "temperature": 0}))
    assert body["messages"][0] == {"role": "system", "content": "Classify."}
    assert body["model"] == "vendor-model" and body["max_tokens"] == 256
    assert body["temperature"] == 0 and "api_key_env" not in body
    js = oai.build_body(REQUEST, oc(output_mode="json_schema", output_schema=SCHEMA))
    assert js["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "caliper_output", "schema": SCHEMA, "strict": True},
    }
    tool = oai.build_body(REQUEST, oc(output_mode="tool", output_schema=SCHEMA))
    assert tool["tool_choice"] == {"type": "function", "function": {"name": "caliper_output"}}
    assert tool["tools"][-1]["function"]["parameters"] == SCHEMA
    mct = oai.build_body(REQUEST, oc(params={"max_completion_tokens": 5}))
    assert "max_tokens" not in mct and mct["max_completion_tokens"] == 5
    with pytest.raises(oai.ShapeError, match="reasoning_effort"):
        oai.build_body(REQUEST, oc(thinking="on"))


def _chat(content=None, model="vendor-model", tool_args=None, cached=0):
    msg = {"role": "assistant", "content": content}
    if tool_args is not None:
        msg["tool_calls"] = [
            {
                "id": "1",
                "type": "function",
                "function": {"name": "caliper_output", "arguments": tool_args},
            }
        ]
    return {
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


def test_openai_send_over_mock_transport(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat('{"label": "a"}', cached=100))

    monkeypatch.setenv("VENDOR_KEY", "sk-test")
    provider = OpenAICompatProvider(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        pricing={"vendor-model": Price(input=1.0, output=2.0, cache_read=0.5)},
    )
    c = oc(output_mode="json_schema", output_schema=SCHEMA, params={"api_key_env": "VENDOR_KEY"})
    r = provider.send(REQUEST, c, row_id="r", replica=0, sample=0)
    assert str(seen[0].url) == "https://llm.example.test/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert json.loads(seen[0].content)["response_format"]["type"] == "json_schema"
    assert r.parsed == {"label": "a"} and r.error is None
    assert (r.usage.input_tokens, r.usage.cache_read_tokens) == (20, 100)
    assert r.cost_usd == pytest.approx((20 * 1.0 + 8 * 2.0 + 100 * 0.5) / 1e6)
    assert r.served_by.endpoint == "https://llm.example.test/v1"


def test_openai_http_error_and_preflight():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "response_format not supported"}})

    provider = OpenAICompatProvider(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    c = oc(output_mode="json_schema", output_schema=SCHEMA)
    r = provider.send(REQUEST, c, row_id="r", replica=0, sample=0)
    assert r.error.startswith("HTTP 400") and "response_format" in r.error
    pf = provider.preflight(c)
    assert not pf.ok and "response_format" in pf.reason
    think = provider.preflight(oc(thinking="on"))
    assert not think.ok and "thinking" in think.reason


def test_openai_parse_tool_refusal_and_substitution():
    tool = oc(output_mode="tool", output_schema=SCHEMA)
    r = oai.parse_response(_chat(tool_args='{"label": "z"}'), tool, "e")
    assert r.parsed == {"label": "z"}
    bad = oai.parse_response(_chat(tool_args="{"), tool, "e")
    assert "not valid JSON" in bad.error
    refused = _chat("x")
    refused["choices"][0]["message"]["refusal"] = "no"
    assert oai.parse_response(refused, oc(), "e").error == "refusal: no"
    swapped = oai.parse_response(_chat("x", model="other-model"), oc(), "e")
    assert swapped.served_by.matches_request is False
    assert oai.parse_response({"model": "m"}, oc(), "e").error == "response has no choices"


def test_anthropic_preflight_passes_when_tiny_answer_is_truncated():
    """A response from the requested model means the shape was accepted."""
    messages = StubMessages(response=_msg([{"type": "text", "text": '{"lab'}], stop="max_tokens"))
    provider, _ = _provider(messages)
    c = cand(thinking="on", output_mode="json_schema", output_schema=SCHEMA)
    pf = provider.preflight(c)
    assert pf.ok and "max_tokens" in pf.reason
    assert messages.calls[0]["max_tokens"] == 1024


def test_openai_preflight_passes_on_unparseable_answer():
    def handler(request):
        return httpx.Response(200, json=_chat("{"))

    provider = OpenAICompatProvider(
        http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    pf = provider.preflight(oc(output_mode="json_schema", output_schema=SCHEMA))
    assert pf.ok and "not valid JSON" in pf.reason
