"""Allow ``python -m hermes_clm`` as the CLI entry point."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(main())
