"""Defect gate for the mutation harness fixture implementation.

`CLM_FIXTURE_DEFECT` selects one injected defect. With no value (or `none`) the fixture
is the correct implementation, which is what makes the harness meaningful: the same test
node must be green with no defect and red with the defect active.
"""

from __future__ import annotations

import os

DEFECT = os.environ.get("CLM_FIXTURE_DEFECT", "none").strip().upper() or "NONE"
DEFECTS = {
    "M1": "session id validation removed (path traversal, I1/I2)",
    "M3": "mount identity check removed (db.session_id mismatch, I3)",
    "M7": "role inferred from message text (role spoofing, I12/I13)",
    "M9": "unknown role folded to user (F1/I14)",
    "M10": "tool_calls synthesised from message text (I13)",
    "M17": "pin not enforced at commit (I23)",
    "M20": "compare-and-swap replaced by last-writer-wins (I7)",
    "M21": "idempotency table not consulted on replay (I8)",
    "M25": "immutability enforced in code, no SQL triggers (I10)",
    "M39": "snapshot writes to the store, projection is not pure (I42)",
    "M41": "store files created with umask modes (I43)",
    "M48": "no redaction at write (I46)",
}


def active(name: str) -> bool:
    return DEFECT == name.strip().upper()
