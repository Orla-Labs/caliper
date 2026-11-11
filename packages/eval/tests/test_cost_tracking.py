"""Cost tracking: live Bedrock prices, the Converse provider, cost per row, the cost gate.

AWS prices come from trimmed copies of the real us-east-1 offer files
(``tests/data/aws_offers``, published 2026-11-09 / 2026-11-11). Nothing here
touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from caliper_eval.aws_pricing import bedrock_price, normalize_name, split_model_id
from caliper_eval.ci import evaluate_gate
from caliper_eval.config import config_from_dict
from caliper_eval.pricing import lookup_price
from caliper_eval.providers import get_provider
from caliper_eval.providers.bedrock_converse import (
    BedrockConverseProvider,
    ShapeError,
    build_body,
    parse_response,
)
from caliper_eval.report import build_report, render_markdown, render_text
from caliper_eval.types import (
    Candidate,
    Decision,
    GoldenRow,
    OutputMode,
    Price,
    ProviderKind,
    Reply,
    RowResult,
    Usage,
    request_sha256,
)

DATA = Path(__file__).parent / "data" / "aws_offers"
NAMES = {
    "amazon.nova-2-lite-v1:0": "Nova 2 Lite",  # Bedrock's name; the price list says "Nova 2.0 Lite"
    "anthropic.claude-haiku-5-5": "Claude Haiku 5.5",
    "anthropic.claude-sonnet-5-5": "Claude Sonnet 5.5",
    "amazon.nova-premier-v1:0": "Nova Premier",  # not in the trimmed fixture
}


def load_offer(offer: str, region: str) -> dict:
    return json.loads((DATA / f"{offer}-{region}.json").read_text(encoding="utf-8"))


def resolve_name(model_id: str, region: str) -> str | None:
    return NAMES.get(model_id)


def price(model: str, region: str | None = "us-east-1"):
    return bedrock_price(model, region, load_offer=load_offer, resolve_name=resolve_name)


# --------------------------------------------------------------------------- aws price list


def test_model_id_scope_and_name_normalization():
    assert split_model_id("global.amazon.nova-2-lite-v1:0") == ("global", "amazon.nova-2-lite-v1:0")
    assert split_model_id("us.anthropic.claude-haiku-5-5") == (
        "regional",
        "anthropic.claude-haiku-5-5",
    )
    assert split_model_id("amazon.nova-2-lite-v1:0") == ("bare", "amazon.nova-2-lite-v1:0")
    assert normalize_name("Nova 2.0 Lite") == normalize_name("Nova 2 Lite") == "nova 2 lite"
    assert normalize_name("Claude Haiku 5.5") == "claude haiku 5.5"  # only a .0 minor is dropped


def test_nova_2_lite_global_and_regional_prices_per_million():
    # AmazonBedrock offer, priced per 1K tokens in the file
    g = price("global.amazon.nova-2-lite-v1:0")
    assert g.error is None and g.warnings == []
    assert g.price.input == pytest.approx(0.30)
    assert g.price.output == pytest.approx(2.50)
    assert g.price.cache_read == pytest.approx(0.075)
    assert "Nova 2 Lite" in g.price.source and "2026-11-11" in g.price.source
    r = price("us.amazon.nova-2-lite-v1:0")
    assert r.price.input == pytest.approx(0.33) and r.price.output == pytest.approx(2.75)


def test_claude_on_bedrock_prices_and_long_context_warning():
    # AmazonBedrockFoundationModels offer, priced per 1M tokens in the file
    haiku = price("global.anthropic.claude-haiku-5-5")
    assert (haiku.price.input, haiku.price.output) == pytest.approx((0.10, 0.50))
    assert haiku.price.cache_read == pytest.approx(0.01)
    assert haiku.price.cache_write == pytest.approx(0.125)  # 5-minute write, not the 1h one
    assert any("long-context" in w for w in haiku.warnings)
    sonnet = price("us.anthropic.claude-sonnet-5-5")
    assert (sonnet.price.input, sonnet.price.output) == pytest.approx((2.2, 11.0))


def test_bare_id_is_priced_regional_and_flagged():
    bare = price("anthropic.claude-sonnet-5-5")
    assert bare.price.input == pytest.approx(2.2)
    assert any("bare model ID" in w for w in bare.warnings)


def test_unmatched_name_fails_closed_with_suggestions():
    missing = price("global.amazon.nova-premier-v1:0")
    assert missing.price is None
    assert "Nova Premier" in missing.error and "similar names there" in missing.error
    unnamed = price("global.amazon.unknown-v1:0")
    assert unnamed.price is None and "did not return a model name" in unnamed.error
    assert price("global.amazon.nova-2-lite-v1:0", region=None).error.startswith(
        "global.amazon.nova-2-lite-v1:0: no AWS region"
    )


def test_offer_download_failure_is_an_error_not_a_price():
    def broken(offer: str, region: str) -> dict:
        raise OSError("offline")

    out = bedrock_price(
        "global.amazon.nova-2-lite-v1:0", "us-east-1", load_offer=broken, resolve_name=resolve_name
    )
    assert out.price is None and "AWS price list unavailable" in out.error


def test_lookup_order_override_then_live_then_table():
    live = lambda model, region: price(model, region)  # noqa: E731
    nova = lookup_price(
        "global.amazon.nova-2-lite-v1:0",
        provider=ProviderKind.BEDROCK_CONVERSE,
        region="us-east-1",
        bedrock=live,
    )
    assert nova.price.output == pytest.approx(2.5) and nova.warning is None
    override = lookup_price(
        "global.amazon.nova-2-lite-v1:0",
        {"global.amazon.nova-2-lite-v1:0": Price(input=9, output=9)},
        provider=ProviderKind.BEDROCK_CONVERSE,
        bedrock=live,
    )
    assert override.price.input == 9 and override.warning is None
    # live lookup fails for a Claude model -> first-party list price, flagged
    fallback = lookup_price(
        "global.anthropic.claude-haiku-5-5",
        provider=ProviderKind.BEDROCK,
        region=None,
        bedrock=live,
    )
    assert fallback.price.input == pytest.approx(0.10) and "Bedrock may differ" in fallback.warning
    # live lookup fails for a non-Claude model -> unknown
    unknown = lookup_price(
        "global.amazon.nova-premier-v1:0",
        provider=ProviderKind.BEDROCK_CONVERSE,
        region="us-east-1",
        bedrock=live,
    )
    assert unknown.price is None and "$0" in unknown.warning


# --------------------------------------------------------------------------- converse provider

NOVA = Candidate(
    name="nova-reasoning",
    provider="bedrock_converse",
    model="global.amazon.nova-2-lite-v1:0",
    region="us-east-1",
    params={
        "additionalModelRequestFields": {
            "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"}
        },
        "max_tokens": 4000,
    },
)
REQUEST = {
    "system": "Be terse.",
    "messages": [{"role": "user", "content": "Is 17 prime?"}],
    "max_tokens": 400,
    "temperature": 0,
}
# A real Nova 2 Lite reply with reasoning on (2026-11-11), trimmed.
NOVA_RESPONSE = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "[REDACTED]."}}},
                {"text": "Yes."},
            ],
        }
    },
    "stopReason": "end_turn",
    "usage": {"inputTokens": 106, "outputTokens": 754, "totalTokens": 860},
    "metrics": {"latencyMs": 5490},
}


def test_converse_body_maps_request_and_params():
    body = build_body(REQUEST, NOVA)
    assert body["modelId"] == "global.amazon.nova-2-lite-v1:0"
    assert body["system"] == [{"text": "Be terse."}]
    assert body["messages"] == [{"role": "user", "content": [{"text": "Is 17 prime?"}]}]
    assert body["inferenceConfig"] == {"maxTokens": 4000, "temperature": 0}
    assert body["additionalModelRequestFields"]["reasoningConfig"]["maxReasoningEffort"] == "low"
    assert REQUEST["max_tokens"] == 400  # not mutated


def test_converse_structured_modes_and_rejections():
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    tool = NOVA.model_copy(update={"output_mode": OutputMode.TOOL, "output_schema": schema})
    body = build_body(REQUEST, tool)
    assert body["toolConfig"]["toolChoice"] == {"tool": {"name": "caliper_output"}}
    assert body["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {"json": schema}
    js = NOVA.model_copy(update={"output_mode": OutputMode.JSON_SCHEMA, "output_schema": schema})
    fmt = build_body(REQUEST, js)["outputConfig"]["textFormat"]
    assert fmt["type"] == "json_schema" and json.loads(fmt["structure"]["jsonSchema"]["schema"])
    with pytest.raises(ShapeError, match="no portable thinking switch"):
        build_body(REQUEST, NOVA.model_copy(update={"thinking": "on"}))
    with pytest.raises(ShapeError, match="not a Converse field"):
        build_body(REQUEST, NOVA.model_copy(update={"params": {"reasoning_effort": "low"}}))
    with pytest.raises(ShapeError, match="not mapped to Converse"):
        build_body({**REQUEST, "metadata": {}}, NOVA)


def test_converse_parse_counts_reasoning_as_output_and_leaves_served_by_unknown():
    reply = parse_response(NOVA_RESPONSE, NOVA)
    assert reply.text == "Yes." and reply.error is None
    assert reply.usage == Usage(input_tokens=106, output_tokens=754)
    assert reply.served_by is None


class FakeConverseClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def converse(self, **body):
        self.calls.append(body)
        if self.error:
            raise self.error
        return self.response


def nova_price(model, region):
    return price(model, region)


def test_converse_provider_prices_from_aws_and_preflights(monkeypatch):
    from caliper_eval import pricing

    monkeypatch.setattr(pricing, "_cached_bedrock_price", nova_price)
    client = FakeConverseClient(NOVA_RESPONSE)
    provider = BedrockConverseProvider(client_factory=lambda c: client)
    reply = provider.send(REQUEST, NOVA, row_id="r1", replica=0, sample=0)
    assert reply.cost_usd == pytest.approx((106 * 0.30 + 754 * 2.50) / 1e6)
    check = provider.preflight(NOVA)
    assert check.ok and "does not report which model" in check.reason
    bad = BedrockConverseProvider(
        client_factory=lambda c: FakeConverseClient(error=ValueError("ValidationException"))
    )
    assert not bad.preflight(NOVA).ok
    assert isinstance(get_provider(NOVA), BedrockConverseProvider)


# --------------------------------------------------------------------------- report + gate


def _result(cand: str, row: str, replica: int, cost: float, *, tokens: int = 100) -> RowResult:
    req = {"messages": [{"role": "user", "content": row}]}
    return RowResult(
        run_id="test",
        candidate=cand,
        row_id=row,
        replica=replica,
        request_sha256=request_sha256(req),
        replies=[Reply(text="a", usage=Usage(input_tokens=tokens), cost_usd=cost)],
        decision=Decision(outcome="a"),
    )


def _setup(costs: dict[str, float], **extra):
    rows = [f"r{i}" for i in range(10)]
    golden = [GoldenRow(id=r, request={"messages": [{"role": "user", "content": r}]}) for r in rows]
    cands = [{"name": n, "provider": "fake", "model": n} for n in costs]
    config = config_from_dict(
        {"golden": "g.jsonl", "candidates": cands, "reference": "ref", **extra}
    )
    results = []
    for name, cost in costs.items():
        replicas = 2 if name == "ref" else 1  # the reference is replayed for the noise floor
        results += [_result(name, r, k, cost) for r in rows for k in range(replicas)]
    return config, results, golden


def test_cost_per_row_ignores_reference_replays_and_projects_monthly():
    config, results, golden = _setup(
        {"ref": 0.001, "sonnet": 0.004, "haiku": 0.0002},
        cost_projection={"calls_per_day": 100_000},
    )
    rep = build_report(config, results, golden, n_boot=20)
    ref, sonnet, haiku = rep.candidates
    assert ref.dollars == pytest.approx(0.02)  # 2 replicas x 10 rows
    assert ref.cost_per_row == pytest.approx(0.001)  # not 0.002
    assert sonnet.cost_vs_reference == pytest.approx(4.0)
    assert haiku.cost_vs_reference == pytest.approx(0.2)
    assert ref.monthly_cost_usd == pytest.approx(3000.0)  # 0.001 x 100k x 30
    assert sonnet.monthly_delta_usd == pytest.approx(9000.0)
    text = render_text(rep)
    assert "0.004000 (4.00x)" in text
    assert "sonnet $12,000.00/mo (+$9,000.00 vs ref)" in text
    assert "haiku $600.00/mo (-$2,400.00 vs ref)" in text
    assert "Cost projection (100,000 calls/day x 30 days)" in render_markdown(rep)


def test_cost_gate_fails_expensive_and_unpriced_candidates():
    config, results, golden = _setup(
        {"ref": 0.001, "sonnet": 0.004, "haiku": 0.0002, "mystery": 0.0},
        ci={"max_cost_ratio": 1.5},
    )
    rep = build_report(config, results, golden, n_boot=20)
    gate = evaluate_gate(rep)
    reasons = {c.name: c.reasons for c in gate.candidates}
    assert reasons["ref"] == [] and reasons["haiku"] == []
    assert any("cost 4.00x the reference > allowed 1.5x" in r for r in reasons["sonnet"])
    assert any("cost unknown: 10 replies priced at $0" in r for r in reasons["mystery"])
    assert rep.candidate("mystery").cost_vs_reference is None
    assert "unpriced x10" in render_text(rep, gate)
    assert "cost <= 1.5x the reference" in render_text(rep, gate)


def test_unpriced_reference_fails_every_candidate_under_the_cost_gate():
    config, results, golden = _setup({"ref": 0.0, "haiku": 0.0002}, ci={"max_cost_ratio": 2})
    gate = evaluate_gate(build_report(config, results, golden, n_boot=20))
    haiku = next(c for c in gate.candidates if c.name == "haiku")
    assert not haiku.passed and "reference ref" in haiku.reasons[0]


def test_no_cost_gate_without_max_cost_ratio():
    config, results, golden = _setup({"ref": 0.001, "sonnet": 0.004})
    gate = evaluate_gate(build_report(config, results, golden, n_boot=20))
    assert gate.passed and gate.max_cost_ratio is None


def test_errored_replies_are_not_counted_as_unpriced():
    config, results, golden = _setup({"ref": 0.001, "haiku": 0.0002}, ci={"max_cost_ratio": 2})
    failed = Reply(error="simulated 529", usage=Usage(input_tokens=50))
    results = [
        r.model_copy(update={"replies": [*r.replies, failed]}) if r.candidate == "haiku" else r
        for r in results
    ]
    rep = build_report(config, results, golden, n_boot=20)
    assert rep.candidate("haiku").unpriced_replies == 0
    assert evaluate_gate(rep, max_error_rate=1.0).passed
