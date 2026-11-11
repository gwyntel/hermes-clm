"""Run the offline benchmark harness with ``python -m benchmarks``."""

from __future__ import annotations

from .runner import main

if __name__ == "__main__":  # pragma: no cover - exercised through the CLI wrapper
    raise SystemExit(main())
