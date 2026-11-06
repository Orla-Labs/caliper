from __future__ import annotations

import json
from pathlib import Path

import pytest
from caliper_eval.budget import Budget, BudgetExceeded
from caliper_eval.config import load_config
from caliper_eval.loader import load_callable, load_golden
from caliper_eval.preflight import PreflightError, require_preflight, run_preflight
from caliper_eval.pricing import (
    PRICES,
    cost_usd,
    lookup_price,
    price_for,
    pricing_warnings,
)
from caliper_eval.providers import get_provider
from caliper_eval.results import ResultsStore
from caliper_eval.runner import plan_keys, run_eval
from caliper_eval.types import (
    Candidate,
    Decision,
    OutputMode,
    Price,
    ProviderKind,
    Reply,
    RowResult,
    Usage,
    request_sha256,
)

# --------------------------------------------------------------------------- config


def test_load_config(project: Path):
    cfg = load_config(project / "eval.toml")
    assert cfg.config_dir == project.resolve()
    assert cfg.golden_path == project.resolve() / "golden.jsonl"
    assert [c.name for c in cfg.candidates] == ["baseline", "small"]
    assert cfg.reference == "baseline"
    assert cfg.replicas_for("baseline") == 2 and cfg.replicas_for("small") == 1
    assert cfg.pricing["fake-large"].input == 3.0
    assert cfg.ci.max_regression == 0.02
    assert cfg.calibration.thresholds == [0.5, 0.9]
    assert cfg.transition_cost("review", "approve") == 5.0
    assert cfg.transition_cost("approve", "other") == 1.0
    assert cfg.transition_cost("approve", "approve") == 0.0
    # paths stay relative in the model: nothing machine-specific in dumps
    assert cfg.candidates[0].fake_script == "fake_models.py:respond"
    assert str(project) not in json.dumps(cfg.model_dump(mode="json"))


def test_config_fingerprint_is_location_independent(project: Path, tmp_path_factory):
    other = tmp_path_factory.mktemp("copy")
    (other / "eval.toml").write_text((project / "eval.toml").read_text())
    a = load_config(project / "eval.toml").fingerprint()
    assert a == load_config(other / "eval.toml").fingerprint()


def test_config_rejects_bad_reference(project: Path):
    text = (project / "eval.toml").read_text().replace('reference = "baseline"', 'reference = "x"')
    (project / "bad.toml").write_text(text)
    with pytest.raises(ValueError, match="reference"):
        load_config(project / "bad.toml")


def test_config_rejects_duplicate_names_and_bad_cost_keys(project: Path):
    text = (project / "eval.toml").read_text().replace('name = "small"', 'name = "baseline"')
    (project / "dup.toml").write_text(text)
    with pytest.raises(ValueError, match="duplicate"):
        load_config(project / "dup.toml")
    text = (project / "eval.toml").read_text().replace('"approve->review"', '"approve"')
    (project / "cost.toml").write_text(text)
    with pytest.raises(ValueError, match="from->to"):
        load_config(project / "cost.toml")


def test_reference_defaults_to_first_candidate(project: Path):
    text = (project / "eval.toml").read_text().replace('reference = "baseline"\n', "")
    (project / "noref.toml").write_text(text)
    assert load_config(project / "noref.toml").reference == "baseline"


def test_unknown_candidate_field_is_an_error(project: Path):
    text = (
        (project / "eval.toml")
        .read_text()
        .replace("replies_per_row = 3", "replies_per_row = 3\nreplies = 2")
    )
    (project / "typo.toml").write_text(text)
    with pytest.raises(ValueError):
        load_config(project / "typo.toml")


# --------------------------------------------------------------------------- loader


def test_load_golden(project: Path):
    rows = load_golden(project / "golden.jsonl")
    assert [r.id for r in rows] == [f"r{i}" for i in range(6)]
    assert rows[0].request["messages"][0]["content"] == "record 0 is good"


def test_load_golden_errors(tmp_path: Path):
    p = tmp_path / "g.jsonl"
    p.write_text('{"id": "a", "request": {}}\n\n{"id": "a", "request": {}}\n')
    with pytest.raises(ValueError, match=r"g.jsonl:3: duplicate"):
        load_golden(p)
    p.write_text('{"id": "a"}\n')
    with pytest.raises(ValueError, match=r"g.jsonl:1"):
        load_golden(p)


def test_load_callable_by_path_and_module(project: Path):
    fn = load_callable("decide.py:decide", project)
    assert fn([Reply(text="approve")]).outcome == "approve"
    assert load_callable("json:dumps") is json.dumps
    with pytest.raises(AttributeError):
        load_callable("decide.py:nope", project)
    with pytest.raises(ValueError):
        load_callable("decide.py", project)


# --------------------------------------------------------------------------- results


def _rr(cand="c", row="r", replica=0, error=None, reply_error=None) -> RowResult:
    return RowResult(
        run_id="x",
        candidate=cand,
        row_id=row,
        replica=replica,
        request_sha256="0" * 64,
        replies=[Reply(text="t", error=reply_error)],
        decision=None if error else Decision(outcome="a"),
        error=error,
    )


def test_results_store_roundtrip_and_truncated_tail(tmp_path: Path):
    store = ResultsStore(tmp_path / "out" / "results.jsonl")
    assert store.load() == [] and store.done_keys() == set()
    store.append(_rr(row="a"))
    store.append(_rr(row="b", reply_error="429"))
    assert store.done_keys() == {("c", "a", 0), ("c", "b", 0)}
    assert store.done_keys(include_errors=False) == {("c", "a", 0)}
    # simulate a crash mid-write
    with store.path.open("a") as fh:
        fh.write('{"run_id": "x", "candid')
    assert [r.row_id for r in store.load()] == ["a", "b"]
    fresh = ResultsStore(store.path)
    fresh.append(_rr(row="c"))
    assert [r.row_id for r in fresh.load()] == ["a", "b", "c"]
    assert store.path.read_text().endswith("\n")


def test_results_store_corrupt_middle_line_is_an_error(tmp_path: Path):
    p = tmp_path / "r.jsonl"
    p.write_text("not json\n" + json.dumps(_rr().model_dump(mode="json")) + "\n")
    with pytest.raises(ValueError, match=":1:"):
        ResultsStore(p).load()


def test_results_store_latest_wins(tmp_path: Path):
    store = ResultsStore(tmp_path / "r.jsonl")
    store.append(_rr(error="boom"))
    store.append(_rr())
    assert store.latest()[("c", "r", 0)].error is None


# --------------------------------------------------------------------------- budget


def test_budget():
    b = Budget(1.0)
    b.check(0.5)
    b.charge(0.6)
    assert b.remaining == pytest.approx(0.4)
    with pytest.raises(BudgetExceeded, match="re-run to resume"):
        b.check(0.5)
    b.check(0.3)
    unlimited = Budget(None)
    unlimited.charge(1e9)
    unlimited.check(1e9)
    with pytest.raises(BudgetExceeded):
        Budget(0.0).check(0.0)


# --------------------------------------------------------------------------- pricing


def test_pricing_table_is_sourced():
    assert PRICES["claude-opus-5-5"].input == 4.0
    for price in PRICES.values():
        assert price.source and "platform.claude.com" in price.source
        assert price.cache_read is not None and price.cache_write is not None


def test_cost_is_cache_aware():
    price = Price(input=4.0, output=20.0, cache_read=0.2, cache_write=5.0)
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost_usd(usage, price) == pytest.approx(4 + 20 + 0.2 + 5)
    # cache prices default to input when not given
    assert cost_usd(usage, Price(input=1.0, output=2.0)) == pytest.approx(1 + 2 + 1 + 1)
    assert cost_usd(usage, None) == 0.0


def test_price_lookup_overrides_bedrock_and_unknown():
    override = {"claude-opus-5-5": Price(input=1.0, output=1.0)}
    assert price_for("claude-opus-5-5", override).input == 1.0
    assert price_for("claude-opus-5-5").input == 4.0
    bedrock = lookup_price("anthropic.claude-opus-5-5", provider=ProviderKind.BEDROCK)
    assert bedrock.price.input == 4.0 and "Bedrock" in bedrock.warning
    assert lookup_price("us.anthropic.claude-sonnet-5-5").price.input == 2.0
    unknown = lookup_price("gpt-something")
    assert unknown.price is None and "$0" in unknown.warning
    cands = [
        Candidate(name="a", provider="anthropic", model="claude-opus-5-5"),
        Candidate(name="b", provider="openai_compat", model="vendor-model"),
    ]
    assert set(pricing_warnings(cands)) == {"b"}


# --------------------------------------------------------------------------- fake provider


def _fake(project: Path, **kw) -> tuple:
    cand = Candidate(
        name="c",
        provider="fake",
        model=kw.pop("model", "fake-large"),
        fake_script="fake_models.py:respond",
        **kw,
    )
    provider = get_provider(
        cand, base_dir=project, pricing={"fake-large": Price(input=3.0, output=15.0)}
    )
    return cand, provider


def test_fake_provider_is_deterministic_and_does_not_mutate(project: Path):
    cand, provider = _fake(project)
    request = load_golden(project / "golden.jsonl")[0].request
    before = json.dumps(request, sort_keys=True)
    a = provider.send(request, cand, row_id="r0", replica=0, sample=0)
    b = provider.send(request, cand, row_id="r0", replica=0, sample=0)
    assert json.dumps(request, sort_keys=True) == before
    assert a == b
    assert a.text in {"approve", "review"}
    assert a.served_by.provider == "fake" and a.served_by.matches_request
    assert a.usage.input_tokens > 0 and a.usage.output_tokens > 0
    assert 50 <= a.latency_ms < 1000
    assert a.cost_usd == pytest.approx(
        (a.usage.input_tokens * 3 + a.usage.output_tokens * 15) / 1e6
    )


def test_fake_provider_structured_and_errors(project: Path):
    cand, provider = _fake(
        project, output_mode=OutputMode.JSON_SCHEMA, output_schema={"type": "object"}
    )
    reply = provider.send(
        {"messages": [{"role": "user", "content": "good"}]}, cand, row_id="x", replica=0, sample=0
    )
    assert reply.parsed in ({"label": "approve"}, {"label": "review"})
    assert json.loads(reply.text) == reply.parsed
    err = provider.send(
        {"messages": [{"role": "user", "content": "boom"}]}, cand, row_id="x", replica=0, sample=0
    )
    assert err.error == "simulated 529 overloaded"
    crash = provider.send({"messages": []}, cand, row_id="x", replica=0, sample=0)
    assert crash.error and "IndexError" in crash.error


def test_fake_text_mode_does_not_parse(project: Path):
    cand, provider = _fake(project)
    reply = provider.send(
        {"messages": [{"role": "user", "content": "good"}]}, cand, row_id="x", replica=0, sample=0
    )
    assert reply.parsed is None


# --------------------------------------------------------------------------- preflight


def test_preflight_ok_rejected_and_swapped(project: Path):
    cfg = load_config(project / "eval.toml")
    cands = [
        *cfg.candidates,
        Candidate(
            name="tiny-tool",
            provider="fake",
            model="fake-tiny",
            output_mode="tool",
            output_schema={"type": "object"},
            fake_script="fake_models.py:respond",
        ),
        Candidate(
            name="swap", provider="fake", model="fake-swap", fake_script="fake_models.py:respond"
        ),
        Candidate(name="noscript", provider="fake", model="m"),
    ]
    res = {r.candidate: r for r in run_preflight(cands, cfg)}
    assert res["baseline"].ok and res["small"].ok
    assert res["baseline"].served_by.model == "fake-large"
    assert not res["tiny-tool"].ok and "forced tool use" in res["tiny-tool"].reason
    assert not res["swap"].ok and "fake-other" in res["swap"].reason
    assert not res["swap"].served_by.matches_request
    assert not res["noscript"].ok
    with pytest.raises(PreflightError, match="tiny-tool"):
        require_preflight(cands, cfg)
    require_preflight(cfg.candidates, cfg)


# --------------------------------------------------------------------------- runner


def test_run_eval_end_to_end(project: Path):
    cfg = load_config(project / "eval.toml")
    out = project / "results.jsonl"
    seen = []
    results = run_eval(cfg, results_path=out, progress=lambda r, d, t: seen.append((r.key, d, t)))
    rows = load_golden(cfg.golden_path)
    # baseline: 6 rows x 2 replicas; small: 6 rows x 1
    assert len(results) == 18
    assert [r.key for r in results] == plan_keys(cfg, rows)
    assert seen[-1][1:] == (18, 18)
    base = [r for r in results if r.candidate == "baseline"]
    assert all(len(r.replies) == 3 for r in base)
    assert all(len(r.replies) == 1 for r in results if r.candidate == "small")
    assert all(r.decision is not None and r.error is None for r in results)
    assert results[0].request_sha256 == request_sha256(rows[0].request)
    assert len({r.run_id for r in results}) == 1
    # replicas differ in randomness but are reproducible
    again = run_eval(cfg, results_path=project / "again.jsonl")
    assert [r.model_dump() for r in again] == [r.model_dump() for r in results]
    assert out.read_bytes() == (project / "again.jsonl").read_bytes()


def test_run_eval_resume_skips_done(project: Path):
    cfg = load_config(project / "eval.toml")
    out = project / "results.jsonl"
    rows = load_golden(cfg.golden_path)
    run_eval(cfg, results_path=out, rows=rows[:3])
    n_lines = len(out.read_text().splitlines())
    assert n_lines == 9
    calls = []
    results = run_eval(cfg, results_path=out, progress=lambda r, d, t: calls.append(r.key))
    assert len(results) == 18
    assert len(calls) == 9  # only the new keys ran
    assert len(out.read_text().splitlines()) == 18
    # resume=False starts over
    run_eval(cfg, results_path=out, resume=False)
    assert len(out.read_text().splitlines()) == 18


def test_run_eval_decide_errors_and_reply_errors(project: Path):
    cfg = load_config(project / "eval.toml")
    rows = load_golden(cfg.golden_path)
    rows[1].request["messages"][0]["content"] = "boom"
    broken = load_callable("decide.py:broken", project)
    res = run_eval(cfg, results_path=project / "a.jsonl", rows=rows, decide=broken)
    assert all(r.decision is None and "bad rule" in r.error for r in res)
    res = run_eval(cfg, results_path=project / "b.jsonl", rows=rows)
    boom = [r for r in res if r.row_id == "r1"]
    # decide still runs on error replies: the feature's fallback is under test
    assert all(r.decision.outcome == "review" and r.decision.detail["fallback"] for r in boom)
    assert all(rep.error for r in boom for rep in r.replies)


def test_run_eval_retry_errors(project: Path):
    cfg = load_config(project / "eval.toml")
    rows = load_golden(cfg.golden_path)
    rows[1].request["messages"][0]["content"] = "boom"
    out = project / "r.jsonl"
    run_eval(cfg, results_path=out, rows=rows)
    fixed = load_golden(cfg.golden_path)
    calls = []
    run_eval(cfg, results_path=out, rows=fixed, progress=lambda r, d, t: calls.append(r.key))
    assert calls == []  # errored rows count as done by default
    run_eval(
        cfg,
        results_path=out,
        rows=fixed,
        retry_errors=True,
        progress=lambda r, d, t: calls.append(r.key),
    )
    assert sorted(calls) == [("baseline", "r1", 0), ("baseline", "r1", 1), ("small", "r1", 0)]
    assert not any(rep.error for r in ResultsStore(out).latest().values() for rep in r.replies)


def test_run_eval_budget_stops_and_resumes(project: Path):
    cfg = load_config(project / "eval.toml")
    out = project / "r.jsonl"
    with pytest.raises(BudgetExceeded):
        run_eval(cfg, results_path=out, budget=Budget(0.0001))
    partial = ResultsStore(out).load()
    assert 0 < len(partial) < 18
    spent = sum(rep.cost_usd for r in partial for rep in r.replies)
    assert spent <= 0.0001 + max(rep.cost_usd * 3 for r in partial for rep in r.replies)
    results = run_eval(cfg, results_path=out, budget=Budget(10.0))
    assert len(results) == 18


def test_run_eval_budget_from_config(project: Path):
    cfg = load_config(project / "eval.toml").model_copy(update={"budget_usd": 0.0})
    with pytest.raises(BudgetExceeded):
        run_eval(cfg, results_path=project / "r.jsonl")


def test_decide_may_return_str_or_dict(project: Path):
    cfg = load_config(project / "eval.toml")
    res = run_eval(cfg, results_path=project / "s.jsonl", decide=lambda replies: "x")
    assert {r.decision.outcome for r in res} == {"x"}
    res = run_eval(
        cfg, results_path=project / "d.jsonl", decide=lambda replies: {"outcome": "y", "score": 1}
    )
    assert {r.decision.score for r in res} == {1.0}
