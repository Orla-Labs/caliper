"""Shared fixtures: a tiny fictional golden set with images, and a TestClient factory."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from caliper_label.app import create_app
from caliper_label.config import LabelConfig
from caliper_label.data import load_dataset
from caliper_label.state import Session
from caliper_label.store import open_store
from fastapi.testclient import TestClient

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000030101005d1b2d8d0000000049454e44ae426082"
)


def golden_row(i: int, stratum: str, code: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"g{i}",
        "request": {
            "system": "You assign tax codes (fictional).",
            "messages": [{"role": "user", "content": f"Item: Moonwick widget {i}"}],
            "max_tokens": 50,
        },
        "reference_outcome": "auto_publish",
        "stratum": stratum,
        "metadata": {"code": code, "score": 0.5 + i / 100, "img": f"img/g{i}.png", **extra},
    }


CONFIG: dict[str, Any] = {
    "title": "Test project",
    "fields": [
        {"key": "metadata.img", "type": "image", "label": "Photo"},
        {"key": "metadata.code", "type": "badge"},
        {"key": "metadata.score", "type": "number", "decimals": 2},
        {"key": "request.messages.-1.content", "type": "text", "label": "Listing"},
        {"template": "https://example.invalid/items/{id}", "type": "link", "label": "Open"},
    ],
    "decisions": [
        {
            "key": "t",
            "value": "correct",
            "label": "Correct",
            "approved": True,
            "outcome": "auto_publish",
        },
        {"key": "f", "value": "wrong", "label": "Wrong", "outcome": "needs_review"},
        {"key": "u", "value": "unsure", "label": "Unsure", "score": 0.5},
    ],
    "queue_key": "stratum",
    "category_key": "metadata.code",
    "stage": "triage",
}


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "img").mkdir(parents=True)
    rows = [
        golden_row(0, "common", "CTX-100"),
        golden_row(1, "common", "CTX-100"),
        golden_row(2, "rare", "CTX-520"),
        golden_row(3, "rare", "CTX-520"),
        golden_row(4, "common", "CTX-210"),
    ]
    (d / "golden.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for r in rows:
        (d / r["metadata"]["img"]).write_bytes(PNG_1PX)
    (tmp_path / "secret.png").write_bytes(PNG_1PX)  # outside the data dir
    (tmp_path / "secret.txt").write_text("do not serve")
    return d


@pytest.fixture
def make_session(data_dir: Path, tmp_path: Path) -> Callable[..., Session]:
    def make(
        store: str = "labels.jsonl", rows: list[dict[str, Any]] | None = None, **overrides: Any
    ) -> Session:
        data = data_dir / "golden.jsonl"
        if rows is not None:
            data = data_dir / "custom.jsonl"
            data.write_text("".join(json.dumps(r) + "\n" for r in rows))
        config = LabelConfig.model_validate({**CONFIG, **overrides})
        return Session(load_dataset(data, config), config, open_store(tmp_path / store))

    return make


@pytest.fixture
def make_client(make_session: Callable[..., Session]) -> Callable[..., TestClient]:
    def make(**kw: Any) -> TestClient:
        session = kw.pop("session", None) or make_session(**kw)
        return TestClient(create_app(session))

    return make


@pytest.fixture
def base_config() -> dict[str, Any]:
    """A fresh copy of CONFIG (tests may mutate it)."""
    import copy

    return copy.deepcopy(CONFIG)
