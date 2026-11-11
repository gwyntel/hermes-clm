"""Hermes plugin entry point for hermes-clm.

Hermes loads this directory as ``hermes_plugins.hermes_clm`` and calls
``register(ctx)``. The implementation lives in the installed ``hermes_clm``
package; when running from a source checkout without an install, ``src/`` is
added to ``sys.path`` so the same entry point works in-place.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["register"]

_REPO_ROOT = Path(__file__).resolve().parent


def _import_register():
    try:
        from hermes_clm.plugin import register as _register
    except ModuleNotFoundError as exc:  # pragma: no cover - dev-checkout path
        if not (exc.name or "").startswith("hermes_clm"):
            raise
        src = _REPO_ROOT / "src"
        if not src.is_dir():
            raise
        sys.path.insert(0, str(src))
        from hermes_clm.plugin import register as _register
    return _register


def register(ctx) -> None:
    """Register the CLM context engine, its tools and the operator command."""

    _import_register()(ctx)
