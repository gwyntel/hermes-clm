"""Immutable value types for the harness fixture implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

CLOSED_OP_KINDS = frozenset({"keep", "drop", "rewrite", "pin", "offload", "restore", "boundary"})
MUTATING_KINDS = frozenset({"drop", "rewrite", "offload", "restore", "boundary"})


@dataclass(frozen=True)
class EditOperation:
    kind: str
    span_ids: tuple[str, ...] = ()
    replacement: Any = None
    actor_origin: str = "model"

    def __post_init__(self):
        if self.kind not in CLOSED_OP_KINDS:
            raise ValueError(f"unknown operation kind {self.kind!r}")
        object.__setattr__(self, "span_ids", tuple(self.span_ids))


@dataclass(frozen=True)
class ArchivedSpan:
    span_id: str
    uid: str
    aliases: tuple[str, ...]
    message: Mapping[str, Any]
    group_id: str | None
    origin: str
    protected: bool
    content_sha256: str


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    proposal_sha256: str
    base_revision: int
    operations: tuple[EditOperation, ...]
    origin: str = "aux"


@dataclass(frozen=True)
class StoreSnapshot:
    revision: int
    operations: tuple[Mapping[str, Any], ...] = ()
    spans: Mapping[str, ArchivedSpan] = field(default_factory=dict)
    pins: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    head_hash: str = ""
