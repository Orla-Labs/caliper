"""Bundled example projects, copied out by ``caliper example``.

The published wheel carries ``examples/<name>`` from the repository under
``caliper_eval/_examples/<name>`` (see the root ``pyproject.toml``). In a
source checkout the files are read from ``examples/`` directly, so the command
works the same in both.
"""

from __future__ import annotations

import shutil
from pathlib import Path

EXAMPLES = {
    "catalog_cleanup": "caliper eval: is it safe to switch a catalog feature to a cheaper model?",
    "invoice_totals": "graders: mutation-test a reward grader and measure kappa against humans",
}

# Next steps printed after copying, with {dir} replaced by the destination.
NEXT_STEPS = {
    "catalog_cleanup": [
        "caliper eval --config {dir}/caliper.toml --out {dir}/out --ci "
        "--summary-md {dir}/out/summary.md",
        "caliper sample --in {dir}/logs.jsonl --out {dir}/out/sampled.jsonl "
        "--stratum-field outcome --keep needs_review,fallback --negatives 40 --id-field id",
        "caliper label serve {dir}/golden.jsonl --config {dir}/label.yaml"
        "   # needs orlalabs-caliper[label]",
    ],
    "invoice_totals": [
        "caliper kappa --rollouts {dir}/labeled_rollouts.jsonl "
        "--grader {dir}/graders.py:fixed_grader",
        "caliper-compile --target trl --grader {dir}/graders.py:fixed_grader "
        "--dataset {dir}/prompts.jsonl --out {dir}/trl",
    ],
}

_SKIP = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "tests", "generate.py", "expected_output.txt"
)


def source_dir(name: str) -> Path:
    """Directory holding example ``name``: packaged data first, then a source checkout.

    Raises ``FileNotFoundError`` when neither exists.
    """
    packaged = Path(__file__).parent / "_examples" / name
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[4] / "examples" / name
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError(f"example {name!r} is not bundled with this install")


def copy_example(name: str, dest: str | Path) -> Path:
    """Copy example ``name`` to ``dest`` (which must not exist yet) and return it."""
    if name not in EXAMPLES:
        raise ValueError(f"unknown example {name!r}; choose from {', '.join(EXAMPLES)}")
    target = Path(dest)
    if target.exists():
        raise FileExistsError(f"{target} already exists; pick another directory")
    shutil.copytree(source_dir(name), target, ignore=_SKIP)
    return target
