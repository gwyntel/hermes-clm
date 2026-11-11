"""Typed error hierarchy for the Hermes CLM context store.

Every error carries a stable machine-readable ``code`` plus an optional
``details`` mapping so tool handlers can return JSON without inspecting
exception text.  Messages are short, human-readable and contain no secrets.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "CLMError",
    "ValidationError",
    "PathSafetyError",
    "StoreBusyError",
    "ReadOnlyStoreError",
    "StoreClosedError",
    "NotFoundError",
    "ProtectedSpanError",
    "PinAuthorityError",
    "ConflictError",
    "StaleRevisionError",
    "IdempotencyConflictError",
    "StaleProposalError",
    "CorruptStoreError",
    "StoreBudgetError",
]


class CLMError(Exception):
    """Base class for every store error."""

    code = "clm_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = str(code)
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.details:
            return f"{self.code}: {self.message} ({self.details!r})"
        return f"{self.code}: {self.message}"


class ValidationError(CLMError):
    """Caller supplied malformed arguments or operations."""

    code = "invalid_argument"


class PathSafetyError(CLMError):
    """A derived store path escaped its root, or traversed a symlink."""

    code = "unsafe_path"


class StoreBusyError(CLMError):
    """Another writer already holds the exclusive store lock."""

    code = "store_busy"


class ReadOnlyStoreError(CLMError):
    """A mutation was attempted on a read-only store handle."""

    code = "read_only_store"


class StoreClosedError(CLMError):
    """The store handle was used after ``close()``."""

    code = "store_closed"


class NotFoundError(CLMError):
    """Requested span, revision, transaction or proposal does not exist."""

    code = "not_found"


class ProtectedSpanError(CLMError):
    """Operation targeted a protected (system/user/prefill/unknown) span."""

    code = "protected_span"


class PinAuthorityError(CLMError):
    """Actor lacks authority for the requested pin transition.

    Agents may pin (one-way).  Only the operator may unpin.
    """

    code = "pin_authority"


class ConflictError(CLMError):
    """Generic state conflict: stale revision, duplicate span, bad txn state."""

    code = "conflict"


class StaleRevisionError(ConflictError):
    """Compare-and-swap failed because the head revision moved."""

    code = "stale_revision"


class IdempotencyConflictError(ConflictError):
    """A txn id was reused with a different payload or terminal state."""

    code = "idempotency_conflict"


class StaleProposalError(ConflictError):
    """Proposal hash/base revision no longer matches the store."""

    code = "stale_proposal"


class CorruptStoreError(CLMError):
    """Structural corruption detected (hash mismatch, bad schema, bad bytes).

    Callers must treat this as "no trustworthy view" (projection returns
    ``None`` rather than a guessed subset) and surface the diagnostics.
    """

    code = "corrupt_store"


class StoreBudgetError(CLMError):
    """A bounded limit (bytes, span count, operation count) was exceeded."""

    code = "budget_exceeded"
