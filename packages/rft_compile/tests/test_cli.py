"""caliper-compile CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from caliper_rft.cli import main

EXPECTED_FILES = {
    "trl": ["train.jsonl", "reward_funcs.py"],
    "bedrock": ["train.jsonl", "lambda_function.py"],
    "openai": ["rft_job.json", "train.jsonl"],
}


def _argv(target, fixtures_dir, out, attr="exact_total"):
    return [
        "--target", target,
        "--grader", f"{fixtures_dir / 'graders_multi.py'}:{attr}",
        "--dataset", str(fixtures_dir / "dataset.jsonl"),
        "--out", str(out),
    ]


@pytest.mark.parametrize("target", ["trl", "bedrock", "openai"])
def test_cli_writes_files_and_summary(target, tmp_path, fixtures_dir, capsys) -> None:
    out = tmp_path / "out"
    assert main(_argv(target, fixtures_dir, out)) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(EXPECTED_FILES[target])
    stdout, stderr = capsys.readouterr()
    assert f"target={target} grader=exact_total (name=exact_total) rows=3" in stdout
    for name in EXPECTED_FILES[target]:
        assert f"wrote {out.as_posix()}/{name}" in stdout
    if target == "openai":
        assert "deprecated" in stderr and "developers.openai.com" in stderr
    elif target == "bedrock":
        assert "at least 100 training records" in stderr
    else:
        assert stderr == ""


@pytest.mark.parametrize("target", ["trl", "bedrock", "openai"])
def test_cli_output_is_byte_identical(target, tmp_path, fixtures_dir) -> None:
    a, b = tmp_path / "a", tmp_path / "nested" / "b"
    assert main(_argv(target, fixtures_dir, a)) == 0
    assert main(_argv(target, fixtures_dir, b)) == 0
    for name in EXPECTED_FILES[target]:
        assert (a / name).read_bytes() == (b / name).read_bytes()
        assert str(tmp_path).encode() not in (a / name).read_bytes()


def test_cli_relative_out_shown_as_typed(tmp_path, fixtures_dir, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(_argv("trl", fixtures_dir, "build/trl")) == 0
    assert "  wrote build/trl/reward_funcs.py" in capsys.readouterr().out


def test_cli_single_grader_without_attr(tmp_path, fixtures_dir, capsys) -> None:
    argv = ["--target", "trl", "--grader", str(fixtures_dir / "graders_single.py"),
            "--dataset", str(fixtures_dir / "dataset.jsonl"), "--out", str(tmp_path)]
    assert main(argv) == 0
    assert "grader=only_one" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda a, d, t: a.__setitem__(3, str(d / "graders_multi.py")), "select one with"),
        (lambda a, d, t: a.__setitem__(3, str(d / "graders_multi.py") + ":nope"), "not defined"),
        (lambda a, d, t: a.__setitem__(5, str(t / "missing.jsonl")), "dataset not found"),
        (lambda a, d, t: (t / "bad.jsonl").write_text('{"id": "x"}\n')
         and a.__setitem__(5, str(t / "bad.jsonl")), "bad.jsonl:1: not a valid EvalRow"),
        (lambda a, d, t: (t / "empty.jsonl").write_text("\n")
         and a.__setitem__(5, str(t / "empty.jsonl")), "has no rows"),
    ],
)
def test_cli_errors(mutate, match, tmp_path, fixtures_dir, capsys) -> None:
    argv = _argv("trl", fixtures_dir, tmp_path / "out")
    mutate(argv, fixtures_dir, tmp_path)
    assert main(argv) == 2
    assert match in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_on_error_none_only_for_trl(tmp_path, fixtures_dir, capsys) -> None:
    assert main([*_argv("bedrock", fixtures_dir, tmp_path), "--on-error", "none"]) == 2
    assert main([*_argv("trl", fixtures_dir, tmp_path / "t"), "--on-error", "none"]) == 0
    assert "ON_ERROR = None" in (tmp_path / "t" / "reward_funcs.py").read_text()


def test_cli_rejects_unknown_target(fixtures_dir, tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(_argv("vertex", fixtures_dir, tmp_path))


def test_console_script_entry_point(tmp_path, fixtures_dir) -> None:
    """`caliper-compile` is installed next to the interpreter by uv sync."""
    exe = os.path.join(os.path.dirname(sys.executable), "caliper-compile")
    if not os.path.exists(exe):
        pytest.skip("console script not installed in this environment")
    proc = subprocess.run([exe, *_argv("trl", fixtures_dir, tmp_path)], capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((tmp_path / "train.jsonl").read_text().splitlines()[0])["id"] == "r1"
