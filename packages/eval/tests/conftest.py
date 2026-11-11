"""Shared fixtures for the eval engine tests: a tiny offline project on disk."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

FAKE_MODELS = textwrap.dedent(
    """
    import hashlib
    import os

    def _u(*parts):
        h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF

    def supports(candidate):
        if candidate["model"] == "fake-tiny" and candidate["output_mode"] == "tool":
            return "fake-tiny does not support forced tool use"
        return None

    def respond(request, *, model, row_id, replica, sample):
        if row_id == "__preflight__":
            return {"text": "OK", "served_model": "fake-other" if model == "fake-swap" else model}
        if os.environ.get("CALIPER_TEST_OUTAGE") == row_id:
            return {"error": "simulated 429 rate limited"}
        text = request["messages"][-1]["content"]
        if "boom" in text:
            return {"error": "simulated 529 overloaded"}
        noise = 0.05 if model == "fake-large" else 0.3
        flip = _u(model, row_id, replica, sample) < noise
        positive = "good" in text
        label = "approve" if positive != flip else "review"
        if request.get("output_mode") == "json_schema":
            return {"parsed": {"label": label}}
        return label
    """
)

DECIDE = textwrap.dedent(
    """
    from caliper_eval.types import Decision

    def decide(replies):
        votes = [r.text for r in replies if r.error is None]
        if not votes:
            return Decision(outcome="review", detail={"fallback": True})
        approve = votes.count("approve")
        return Decision(outcome="approve" if approve * 2 > len(votes) else "review",
                        score=approve / len(votes))

    def broken(replies):
        raise RuntimeError("bad rule")
    """
)

CONFIG = textwrap.dedent(
    """
    golden = "golden.jsonl"
    decide = "decide.py:decide"
    reference = "baseline"
    noise_replays = 2

    [[candidates]]
    name = "baseline"
    provider = "fake"
    model = "fake-large"
    fake_script = "fake_models.py:respond"
    replies_per_row = 3

    [[candidates]]
    name = "small"
    provider = "fake"
    model = "fake-tiny"
    fake_script = "fake_models.py:respond"

    [pricing."fake-large"]
    input = 3.0
    output = 15.0

    [costs]
    "approve->review" = 1.0
    "review->approve" = 5.0

    [ci]
    max_regression = 0.02

    [calibration]
    thresholds = [0.5, 0.9]
    """
)


def golden_rows(n: int = 6) -> list[dict]:
    rows = []
    for i in range(n):
        word = "good" if i % 2 == 0 else "bad"
        rows.append(
            {
                "id": f"r{i}",
                "request": {
                    "system": "Classify the catalog record.",
                    "messages": [{"role": "user", "content": f"record {i} is {word}"}],
                    "max_tokens": 16,
                },
                "reference_outcome": "approve" if word == "good" else "review",
            }
        )
    return rows


@pytest.fixture(autouse=True)
def _no_live_pricing(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Tests never reach AWS: no region, offline pricing, an empty cache, no memo."""
    from caliper_eval import pricing

    monkeypatch.setenv("CALIPER_PRICING_OFFLINE", "1")
    monkeypatch.setenv("CALIPER_CACHE_DIR", str(tmp_path_factory.mktemp("caliper-cache")))
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    pricing._bedrock_cache.clear()
    yield
    pricing._bedrock_cache.clear()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "fake_models.py").write_text(FAKE_MODELS)
    (tmp_path / "decide.py").write_text(DECIDE)
    (tmp_path / "eval.toml").write_text(CONFIG)
    with (tmp_path / "golden.jsonl").open("w") as fh:
        for row in golden_rows():
            fh.write(json.dumps(row) + "\n")
    return tmp_path
