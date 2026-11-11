"""hermes-clm — model-directed context management for Hermes Agent.

The public package is intentionally importable without Hermes installed. Hermes is an
optional host, never a runtime dependency: host adapters on the integration branches live
behind guarded imports.
"""

from __future__ import annotations

from ._version import __version__

__all__ = ["__version__"]
