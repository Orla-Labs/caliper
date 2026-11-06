"""The vendored shim must behave like caliper_sdk for graders."""

from __future__ import annotations

import ast

import pytest
from caliper_rft.targets import _runtime, _shim
from caliper_rft.targets._common import CompileError, GraderSource, py_str_literal

ATTRS = [
    "exact_total",
    "with_components",
    "lazy_import",
    "raises",
    "out_of_range",
    "returns_bool",
    "gold_is_none",
]

CASES = [
    ("Invoice: total?", "Total: $20", "20", {"weight": 0.5}),
    ("Invoice: total?", "total: 21", "20", {}),
    ("p", "no number", None, {"weight": 3}),
    ("ünïcödé ✓", "TOTAL: 22.50", "22.5", {"weight": 0}),
]


def _score_or_exc(fn, rollout):
    try:
        r = fn(rollout)
    except Exception:
        return ("exc",)  # exception class may differ (pydantic ValidationError vs ValueError)
    return ("ok", r.score, dict(r.components), r.explanation)


@pytest.mark.parametrize("attr", ATTRS)
def test_shim_matches_sdk(attr, fixtures_dir, sdk_graders) -> None:
    source = (fixtures_dir / "graders_multi.py").read_text()
    shim_g = _shim.load_grader(source, attr, "graders_multi.py")
    sdk_g = getattr(sdk_graders, attr)
    assert shim_g.name == sdk_g.name
    assert shim_g.version == sdk_g.version
    for prompt, response, gold, meta in CASES:
        rollout = {"prompt": prompt, "response": response, "gold": gold, "metadata": meta}
        assert _score_or_exc(shim_g, rollout) == _score_or_exc(sdk_g, rollout)


def test_shim_rejects_unknown_rollout_fields() -> None:
    with pytest.raises(ValueError):
        _shim.Rollout.model_validate({"prompt": "", "response": "", "bogus": 1})


def test_embedded_modules_are_py310_compatible() -> None:
    from pathlib import Path

    for mod in (_shim, _runtime):
        src = Path(mod.__file__).read_text()
        ast.parse(src, feature_version=(3, 10))
        tree = ast.parse(src)
        # A __future__ import would land mid-file once concatenated -> SyntaxError.
        assert not any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in tree.body
        )


@pytest.mark.parametrize(
    "s",
    ["plain", r"re.compile(r'\d+\.\d')", "ends with backslash\\", "has ''' triple", "x'", "é\n\t"],
)
def test_py_str_literal_round_trips(s: str) -> None:
    assert ast.literal_eval(py_str_literal(s)) == s


def test_grader_source_requires_attr_when_ambiguous(fixtures_dir) -> None:
    with pytest.raises(CompileError, match=r"select one with PATH\.py:ATTR"):
        GraderSource.from_spec(str(fixtures_dir / "graders_multi.py"))


def test_grader_source_single_grader_without_attr(fixtures_dir) -> None:
    g = GraderSource.from_spec(str(fixtures_dir / "graders_single.py"))
    assert (g.attr, g.name, g.filename) == ("only_one", "only_one", "graders_single.py")


def test_grader_source_named_grader(fixtures_dir) -> None:
    g = GraderSource.from_spec(f"{fixtures_dir / 'graders_multi.py'}:with_components")
    assert (g.attr, g.name) == ("with_components", "total_with_parts")


@pytest.mark.parametrize(
    ("spec_suffix", "match"),
    [
        (":nope", "not defined"),
        (":_TOTAL", "not defined|not a @grader"),
        (":json", "not a @grader"),
    ],
)
def test_grader_source_bad_attr(spec_suffix, match, fixtures_dir) -> None:
    with pytest.raises(CompileError, match=match):
        GraderSource.from_spec(str(fixtures_dir / "graders_multi.py") + spec_suffix)


def test_grader_source_syntax_error() -> None:
    with pytest.raises(CompileError, match="syntax error"):
        GraderSource.from_source("def (:\n", None, "bad.py")


def test_grader_source_missing_file(tmp_path) -> None:
    with pytest.raises(CompileError, match="not found"):
        GraderSource.from_spec(str(tmp_path / "missing.py") + ":g")
