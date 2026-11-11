"""The agent docs on caliper.orlalabs.com/agents/ stay true to the code.

* the generated CLI reference, file reference and JSON Schemas match the code;
* every CALIPER_* environment variable the code reads is documented;
* the smoke test the agent pages promise behaves as they say.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from caliper_eval.cli import EXIT_GATE, EXIT_OK, main

ROOT = Path(__file__).resolve().parents[3]
AGENT_DOCS = ROOT / "site/src/content/docs/agents"


def test_generated_reference_is_current():
    spec = importlib.util.spec_from_file_location(
        "gen_agent_docs", ROOT / "scripts/gen_agent_docs.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.main(["--check"]) == 0, "run: uv run python scripts/gen_agent_docs.py"


def test_every_env_var_is_documented():
    # Every CALIPER_* string literal in the shipped code is an environment variable name.
    read = set()
    for path in (ROOT / "packages").glob("*/src/**/*.py"):
        read |= set(re.findall(r"""["'](CALIPER_[A-Z0-9_]+)["']""", path.read_text()))
    assert {"CALIPER_PRICING_OFFLINE", "CALIPER_STORE_URL", "CALIPER_LABEL_TOKEN"} <= read
    page = (AGENT_DOCS / "troubleshooting.md").read_text()
    missing = sorted(v for v in read if f"`{v}`" not in page)
    assert not missing, f"document in site/src/content/docs/agents/troubleshooting.md: {missing}"


def test_smoke_test_behaves_as_documented(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["example", "catalog_cleanup", "demo"]) == EXIT_OK

    # Without --ci a run that has failing candidates still exits 0.
    assert (
        main(["eval", "--config", "demo/caliper.toml", "--out", "demo/out", "--n-boot", "50"])
        == EXIT_OK
    )
    report = json.loads(Path("demo/out/report.json").read_text())
    by_name = {c["name"]: c for c in report["candidates"]}
    assert set(by_name) == {"baseline", "small", "small-thinking"}
    for c in by_name.values():
        assert c["cost_per_row"] > 0 and c["monthly_cost_usd"] > 0 and c["unpriced_replies"] == 0
    assert by_name["baseline"]["cost_vs_reference"] == 1.0

    # With --ci the example fails on purpose.
    ci = ["eval", "--config", "demo/caliper.toml", "--out", "demo/out-ci", "--ci", "--n-boot", "50"]
    assert main(ci) == EXIT_GATE
    capsys.readouterr()

    # A second --ci run into the same --out fails on the test ledger.
    assert main(ci) == EXIT_GATE
    assert "test split re-touched" in capsys.readouterr().err
