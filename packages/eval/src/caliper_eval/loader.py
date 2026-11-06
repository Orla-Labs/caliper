"""Load golden sets and user callables (decide functions, fake model scripts)."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import ValidationError

from caliper_eval.types import GoldenRow

_MODULES: dict[Path, ModuleType] = {}


def load_golden(path: str | Path) -> list[GoldenRow]:
    """Read a golden JSONL file. Blank lines are skipped; ids must be unique."""
    path = Path(path)
    rows: list[GoldenRow] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = GoldenRow.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(f"{path}:{lineno}: invalid golden row: {exc}") from exc
            if row.id in seen:
                raise ValueError(f"{path}:{lineno}: duplicate row id {row.id!r}")
            seen.add(row.id)
            rows.append(row)
    return rows


def _resolve(path: str | Path, base_dir: str | Path | None) -> Path:
    p = Path(path)
    if not p.is_absolute() and base_dir is not None:
        p = Path(base_dir) / p
    return p.resolve()


def load_module(path: str | Path, base_dir: str | Path | None = None) -> ModuleType:
    """Import a Python file by path (cached per resolved path)."""
    resolved = _resolve(path, base_dir)
    if resolved in _MODULES:
        return _MODULES[resolved]
    if not resolved.is_file():
        raise FileNotFoundError(f"no such Python file: {resolved}")
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:12]
    name = f"caliper_user_{resolved.stem}_{digest}"
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    _MODULES[resolved] = module
    return module


def split_spec(spec: str) -> tuple[str, str]:
    if ":" not in spec:
        raise ValueError(f"expected 'file.py:attr' or 'module:attr', got {spec!r}")
    target, attr = spec.rsplit(":", 1)
    if not target or not attr:
        raise ValueError(f"expected 'file.py:attr' or 'module:attr', got {spec!r}")
    return target, attr


def load_callable(spec: str, base_dir: str | Path | None = None) -> Any:
    """Load ``"path/to/file.py:attr"`` (relative to ``base_dir``) or ``"pkg.mod:attr"``."""
    target, attr = split_spec(spec)
    if target.endswith(".py") or "/" in target or "\\" in target:
        module = load_module(target, base_dir)
    else:
        module = importlib.import_module(target)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise AttributeError(f"{target} has no attribute {attr!r}") from exc
