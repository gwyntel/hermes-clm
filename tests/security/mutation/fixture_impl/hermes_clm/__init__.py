"""Harness fixture implementation of hermes-clm (NOT PRODUCTION).

Built only for tests/security/mutation. It is loaded into a temporary directory by the
harness so it can never shadow the real package during a normal test run.
"""

from __future__ import annotations

from . import _defect, projection  # noqa: F401
from .models import (  # noqa: F401
    CLOSED_OP_KINDS,
    ArchivedSpan,
    EditOperation,
    Proposal,
    StoreSnapshot,
)
from .store import ContextStore, MountError  # noqa: F401

__all__ = [
    "CLOSED_OP_KINDS",
    "ArchivedSpan",
    "ContextStore",
    "EditOperation",
    "MountError",
    "Proposal",
    "StoreSnapshot",
    "projection",
]
