"""hermes-clm — model-directed context management for Hermes Agent.

The public package is intentionally importable without Hermes installed. Hermes is an
optional host, never a runtime dependency: host adapters live behind guarded imports in
:mod:`hermes_clm.engine` and :mod:`hermes_clm.plugin`.
"""

from __future__ import annotations

from ._version import __version__

__all__ = ["__version__"]
