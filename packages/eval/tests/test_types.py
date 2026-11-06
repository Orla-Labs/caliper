from caliper_eval.types import Candidate, GoldenRow, ProviderKind, request_sha256


def test_request_hash_is_key_order_independent():
    a = {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5}
    b = {"max_tokens": 5, "messages": [{"role": "user", "content": "x"}]}
    assert request_sha256(a) == request_sha256(b)


def test_models_validate():
    GoldenRow(id="r1", request={"messages": []})
    Candidate(name="c", provider=ProviderKind.FAKE, model="m")
