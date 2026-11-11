"""Hermes plugin entry point for hermes-clm.

Hermes has two ways to reach this directory, and both land on the same
``register(ctx)`` below:

* as a **context engine** — ``$HERMES_HOME/plugins/hermes-clm/`` is discovered
  by ``plugins/context_engine/__init__.py``: its directory heuristic loads an
  ``__init__.py`` whose source mentions ``ContextEngine`` or
  ``register_context_engine``, then calls ``register(ctx)`` with an
  ``_EngineCollector`` that only supports those two registrations, and takes the
  instance passed to ``register_context_engine``.  The tools of a
  ``ContextEngine`` arrive through ``get_tool_schemas()`` (host
  ``agent/agent_init.py:_inject_context_engine_tools``), never through
  ``register_tool``;
* as a **general plugin** — ``register_tool`` is the only lane, used as a
  fallback when the host has no context-engine collector.

The implementation lives in the installed ``hermes_clm`` package; when running
from a source checkout without an install, ``src/`` is added to ``sys.path`` so
the same entry point works in-place.

This docstring is load-bearing: the discovery heuristic only reads the first
8 KiB of this file, so the ``ContextEngine`` / ``register_context_engine``
markers must stay above any future code.
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
    """Register the CLM ``ContextEngine``, its tools and the ``/clm`` command."""

    _import_register()(ctx)
