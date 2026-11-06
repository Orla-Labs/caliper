import json

import pytest
from caliper_eval.sampler import MISSING, get_path, golden_jsonl, stratified_sample
from caliper_eval.types import GoldenRow


def logs():
    out = []
    for i in range(100):
        outcome = "positive" if i % 25 == 0 else ("unsure" if i % 30 == 1 else "negative")
        rec = {
            "id": f"evt-{i}",
            "meta": {"outcome": outcome},
            "request": {"system": "s", "messages": [{"role": "user", "content": f"item {i}"}],
                        "max_tokens": 16},
        }
        out.append(json.dumps(rec))
    out.append("not json")
    out.append(json.dumps({"meta": {"outcome": "negative"}}))  # no request
    out.append(json.dumps({"meta": {}, "request": {}}))  # no stratum
    out.append("")
    return out


def test_get_path():
    assert get_path({"a": {"b": [1, {"c": 2}]}}, "a.b.1.c") == 2
    assert get_path({"a": 1}, "a.x") is MISSING


def test_keeps_rare_samples_negatives_in_log_order():
    rows, summary = stratified_sample(
        logs(), stratum_field="meta.outcome", keep=["positive", "unsure"], negatives=10, seed=0
    )
    strata = [r.stratum for r in rows]
    assert strata.count("positive") == 4
    assert strata.count("unsure") == 4
    assert strata.count("negative") == 10
    idx = [int(r.id.split("-")[1]) for r in rows]
    assert idx == sorted(idx)
    assert summary.n_skipped == 3
    assert summary.skipped_reasons == {
        "invalid json": 1,
        "missing or non-object request": 1,
        "missing stratum": 1,
    }
    assert summary.by_stratum == {"negative": 92, "positive": 4, "unsure": 4}


def test_deterministic_and_seed_sensitive():
    a, _ = stratified_sample(logs(), stratum_field="meta.outcome", keep=["positive"],
                             negatives=5, seed=1)
    b, _ = stratified_sample(logs(), stratum_field="meta.outcome", keep=["positive"],
                             negatives=5, seed=1)
    c, _ = stratified_sample(logs(), stratum_field="meta.outcome", keep=["positive"],
                             negatives=5, seed=2)
    assert golden_jsonl(a) == golden_jsonl(b)
    assert golden_jsonl(a) != golden_jsonl(c)


def test_cap_ids_reference_and_verbatim_request():
    rows, s = stratified_sample(
        logs(), stratum_field="meta.outcome", keep=["positive", "unsure"], negatives=0,
        id_field="id", reference_field="meta.outcome", cap_per_stratum=2,
    )
    assert s.selected_by_stratum == {"positive": 2, "unsure": 2}
    assert all(r.id.startswith("evt-") and r.reference_outcome == r.stratum for r in rows)
    line = golden_jsonl(rows).splitlines()[0]
    # request keys keep logged order
    assert '"request":{"system":"s","messages":' in line
    GoldenRow.model_validate_json(line)


def test_duplicate_ids_rejected():
    dup = [json.dumps({"id": "x", "o": "p", "request": {}})] * 2
    with pytest.raises(ValueError, match="duplicate"):
        stratified_sample(dup, stratum_field="o", keep=["p"], negatives=0, id_field="id")
