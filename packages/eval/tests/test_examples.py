"""`caliper example` copies a bundled project that runs as-is."""

from __future__ import annotations

from caliper_eval.cli import EXIT_OK, EXIT_USAGE, main
from caliper_eval.examples import EXAMPLES, NEXT_STEPS


def test_lists_examples(capsys):
    assert main(["example"]) == EXIT_OK
    out = capsys.readouterr().out
    assert all(name in out for name in EXAMPLES)
    assert set(NEXT_STEPS) == set(EXAMPLES)


def test_copied_catalog_example_evaluates(tmp_path, capsys):
    dest = tmp_path / "demo"
    assert main(["example", "catalog_cleanup", str(dest)]) == EXIT_OK
    assert f"caliper eval --config {dest}/caliper.toml" in capsys.readouterr().out
    assert (dest / "golden.jsonl").exists() and (dest / "cards").is_dir()
    assert not (dest / "tests").exists() and not (dest / "expected_output.txt").exists()
    code = main(
        [
            "eval",
            "--config",
            str(dest / "caliper.toml"),
            "--out",
            str(dest / "out"),
            "--n-boot",
            "50",
        ]
    )
    assert code == EXIT_OK
    assert "baseline (ref)" in capsys.readouterr().out


def test_refuses_existing_dir_and_unknown_name(tmp_path, capsys):
    assert main(["example", "catalog_cleanup", str(tmp_path)]) == EXIT_USAGE
    assert "already exists" in capsys.readouterr().err
    assert main(["example", "nope", str(tmp_path / "x")]) == EXIT_USAGE
    assert "unknown example" in capsys.readouterr().err
