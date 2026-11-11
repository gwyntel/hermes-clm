"""Deterministic offline benchmark harness for hermes-clm.

The harness is a *model*, not a measurement of Hermes or of any provider:

* token counts come from a fixed character-based estimator, never a tokenizer;
* latency and cost come from explicit constants, never wall-clock timing or billing;
* the four strategies are reference policies, not the shipped engine;
* no network call is possible from this package.

Every number it prints is reproducible from the committed fixtures and this source.
"""

from __future__ import annotations

SCHEMA_VERSION = 1
EVIDENCE_CLASS = "offline-simulation"
