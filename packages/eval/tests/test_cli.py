import json

import pytest
from caliper_eval.cli import main


@pytest.fixture
def in_project(project, monkeypatch):
    monkeypatch.chdir(project)
    return project


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_eval_ci_fails_on_regression_and_is_deterministic(in_project, capsys):
    code, out, err = run(capsys, "eval", "--config", "eval.toml", "--ci", "--n-boot", "200",
                         "--summary-md", "pr/summary.md")
    assert code == 1
    assert "preflight ok" in err
    assert "CI gate: FAIL" in out and "small: cost-weighted regression" in out
    assert "results: caliper-out/results.jsonl" in out
    assert str(in_project) not in out
    md = (in_project / "pr" / "summary.md").read_text()
    assert md.startswith("## Caliper eval: FAIL") and str(in_project) not in md
    report = json.loads((in_project / "caliper-out" / "report.json").read_text())
    assert report["reference"] == "baseline"

    # resume: nothing re-run, identical report
    code2, out2, _ = run(capsys, "eval", "--config", "eval.toml", "--ci", "--n-boot", "200",
                         "--summary-md", "pr/summary.md")
    assert code2 == 1 and out2 == out
    lines = (in_project / "caliper-out" / "results.jsonl").read_text().splitlines()
    assert len(lines) == 6 * 2 + 6  # reference x2 + small x1


def test_eval_without_ci_exits_zero(in_project, capsys):
    code, out, _ = run(capsys, "eval", "--config", "eval.toml", "--out", "o", "--n-boot", "50")
    assert code == 0
    assert "CI gate" not in out and "noise floor (baseline x2" in out


def test_candidates_subset_keeps_reference(in_project, capsys):
    code, out, err = run(capsys, "eval", "--config", "eval.toml", "--candidates", "small",
                         "--n-boot", "50", "--skip-preflight")
    assert code == 0
    assert "added reference 'baseline'" in err
    assert "baseline (ref)" in out


def test_unknown_candidate_is_usage_error(in_project, capsys):
    code, _, err = run(capsys, "eval", "--config", "eval.toml", "--candidates", "nope")
    assert code == 2 and "unknown candidate" in err


def test_preflight_failure_exits_3(in_project, capsys):
    cfg = (in_project / "eval.toml").read_text()
    cfg += '\n[[candidates]]\nname = "tiny-tool"\nprovider = "fake"\nmodel = "fake-tiny"\n'
    cfg += 'output_mode = "tool"\nfake_script = "fake_models.py:respond"\n'
    (in_project / "bad.toml").write_text(cfg)
    code, _, err = run(capsys, "eval", "--config", "bad.toml")
    assert code == 3
    assert "tiny-tool" in err and "forced tool use" in err
    assert not (in_project / "caliper-out" / "results.jsonl").exists()


def test_budget_exceeded_exits_4_with_resume_hint(in_project, capsys):
    code, _, err = run(capsys, "eval", "--config", "eval.toml", "--budget-usd", "0.0001")
    assert code == 4
    assert "budget exceeded" in err and "Resume with the same command" in err
    assert (in_project / "caliper-out" / "results.jsonl").exists()
    code2, _, _ = run(capsys, "eval", "--config", "eval.toml", "--n-boot", "50")
    assert code2 == 0


def test_max_error_rate_allowance(in_project, capsys):
    rows = (in_project / "golden.jsonl").read_text()
    boom = json.loads(rows.splitlines()[0])
    boom["id"] = "boom1"
    boom["request"]["messages"][0]["content"] = "boom record"
    (in_project / "golden.jsonl").write_text(rows + json.dumps(boom) + "\n")
    (in_project / "strict.py").write_text(
        "def decide(replies):\n"
        "    ok = [r.text for r in replies if r.error is None]\n"
        "    if not ok:\n"
        "        raise ValueError('no usable reply')\n"
        "    return ok[0]\n"
    )
    cfg = (in_project / "eval.toml").read_text()
    cfg = cfg.replace("max_regression = 0.02", "max_regression = 10.0")
    cfg = cfg.replace('decide = "decide.py:decide"', 'decide = "strict.py:decide"')
    (in_project / "eval.toml").write_text(cfg)
    code, out, _ = run(capsys, "eval", "--config", "eval.toml", "--ci", "--n-boot", "50")
    assert code == 1
    assert "small: error rate 14.3% > allowed 0.0%" in out
    code, out, _ = run(capsys, "eval", "--config", "eval.toml", "--ci", "--n-boot", "50",
                       "--max-error-rate", "0.2")
    assert code == 0, out
    cfg2 = cfg.replace("[ci]\n", "[ci]\nmax_error_rate = 0.2\n")
    (in_project / "eval.toml").write_text(cfg2)
    code, _, _ = run(capsys, "eval", "--config", "eval.toml", "--ci", "--n-boot", "50")
    assert code == 0


def test_sample_command(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with open("logs.jsonl", "w") as fh:
        for i in range(50):
            outcome = "positive" if i % 10 == 0 else "negative"
            fh.write(json.dumps({"outcome": outcome, "request": {"messages": [i]}}) + "\n")
        fh.write("{broken\n")
    code, out, err = run(capsys, "sample", "--in", "logs.jsonl", "--out", "g/golden.jsonl",
                         "--stratum-field", "outcome", "--keep", "positive", "--negatives", "5")
    assert code == 0
    assert "sampled 10 of 51 log lines" in out
    assert "wrote g/golden.jsonl" in out
    assert "skipped 1 lines" in err
    first = (tmp_path / "g" / "golden.jsonl").read_text()
    run(capsys, "sample", "--in", "logs.jsonl", "--out", "g/golden.jsonl",
        "--stratum-field", "outcome", "--keep", "positive", "--negatives", "5")
    assert (tmp_path / "g" / "golden.jsonl").read_text() == first


def test_sample_missing_input(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code, _, err = run(capsys, "sample", "--in", "nope.jsonl", "--out", "g.jsonl",
                       "--stratum-field", "o", "--keep", "p", "--negatives", "1")
    assert code == 2 and "caliper sample" in err
