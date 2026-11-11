"""`caliper label` without the label extra prints an install hint instead of a traceback."""

from __future__ import annotations

import argparse

import pytest
from caliper_label import cli


def test_missing_label_extra_is_an_install_hint(capsys):
    def needs_fastapi(ns):
        raise ModuleNotFoundError("No module named 'fastapi'", name="fastapi")

    assert cli._call(argparse.Namespace(func=needs_fastapi)) == cli.EXIT_USAGE
    assert "orlalabs-caliper[label]" in capsys.readouterr().err


def test_other_missing_modules_still_raise():
    def broken(ns):
        raise ModuleNotFoundError("No module named 'caliper_typo'", name="caliper_typo")

    with pytest.raises(ModuleNotFoundError):
        cli._call(argparse.Namespace(func=broken))
