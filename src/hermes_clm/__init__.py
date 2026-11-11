"""Hermes CLM: durable, editable request projection for Hermes Agent context.

The package is stdlib-only at import time. Host-coupled modules (``engine``,
``plugin``, ``auxiliary.default_aux_call``) import Hermes lazily so the core can
be imported, tested and benchmarked without a Hermes install.
"""

from __future__ import annotations

__all__: list[str] = []
