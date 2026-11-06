"""Shared fixtures for compile-target tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Runs inside `python -I -S`: no site-packages (so no caliper, no pydantic),
# no cwd / PYTHONPATH on sys.path. Proves emitted artifacts are self-contained.
_ISOLATED_PRELUDE = """
import importlib.util, json, sys
for _m in ("caliper_sdk", "caliper_rft", "pydantic"):
    try:
        __import__(_m)
    except ImportError:
        pass
    else:
        raise SystemExit("isolation broken: %s importable" % _m)

def load(path, name="emitted"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

ARGS = json.loads(sys.stdin.read())
"""


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def run_isolated() -> Callable[[str, Any], Any]:
    """Run `body` in an isolated interpreter; it must print one JSON line as RESULT."""

    def _run(body: str, args: Any = None) -> Any:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _ISOLATED_PRELUDE + body],
            input=json.dumps(args),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1])

    return _run


@pytest.fixture
def sdk_graders() -> ModuleType:
    """The multi-grader fixture imported normally, against the REAL caliper_sdk."""
    spec = importlib.util.spec_from_file_location(
        "fixture_graders_multi", FIXTURES / "graders_multi.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
