"""Shared test support for the CLM engine tests.

Import-safe: adds ``src`` to ``sys.path`` so the src-layout package imports
without an editable install (packaging is a separate delivery branch).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for candidate in (SRC, TESTS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def pytest_configure(config):  # noqa: D401 - pytest hook
    """Fail fast if the package is not importable from src."""
    import importlib

    importlib.import_module("hermes_clm.identity")
