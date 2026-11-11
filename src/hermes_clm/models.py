"""Immutable value types shared by the store, engine and tool layers.

Nothing in this module imports Hermes, SQLite or the filesystem: it is the
stdlib-only data contract described in ``docs/integration-contract.md``.

Design rules encoded here:

* every public value type is frozen and returns a fresh copy when serialized;
* mappings are read-only proxies and sequences are tuples;
* ``copy.deepcopy`` on a value returns the value itself (safe by immutability);
* hashing is canonical JSON with sorted keys and no incidental whitespace.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .errors import ValidationError

__all__ = [
    "canonical_json",
    "sha256_hex",
    "canonical_sha256",
    "offload_stub",
    "EditKind",
    "SpanOrigin",
    "ActorOrigin",
    "SpanState",
    "PinAction",
    "EditOperation",
    "ArchivedSpan",
    "PinRecord",
    "Proposal",
    "StoreSnapshot",
    "IngestResult",
    "StageResult",
    "CommitResult",
    "Receipt",
    "OperationResult",
]


# ---------------------------------------------------------------------------
# canonical hashing
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Serialize ``value`` deterministically (sorted keys, compact, utf-8 safe)."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    """sha256 of the canonical JSON encoding of ``value``."""

    return sha256_hex(canonical_json(value))


def _frozen_map(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not value:
        return MappingProxyType({})
    if isinstance(value, MappingProxyType):
        return value
    return MappingProxyType(dict(value))


def _frozen_deep(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Freeze a nested mapping by deep-copying it first (caller data isolated)."""

    if not value:
        return MappingProxyType({})
    return MappingProxyType(copy.deepcopy(dict(value)))


class _Immutable:
    """Mixin: deep-copying an immutable value is a no-op."""

    __slots__ = ()

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Any:  # noqa: D401
        return self

    def __copy__(self) -> Any:
        return self


# ---------------------------------------------------------------------------
# closed value sets
# ---------------------------------------------------------------------------


class EditKind(StrEnum):
    """Closed set of operation kinds. Unknown kinds are rejected."""

    KEEP = "keep"
    DROP = "drop"
    REWRITE = "rewrite"
    PIN = "pin"
    OFFLOAD = "offload"
    RESTORE = "restore"
    BOUNDARY = "boundary"


ALLOWED_EDIT_KINDS = frozenset(kind.value for kind in EditKind)

#: Kinds that may not carry a ``replacement`` payload.
_NO_REPLACEMENT_KINDS = frozenset(
    {
        EditKind.KEEP,
        EditKind.DROP,
        EditKind.PIN,
        EditKind.OFFLOAD,
        EditKind.RESTORE,
        EditKind.BOUNDARY,
    }
)


class SpanOrigin(StrEnum):
    """Structural provenance of an archived message (never inferred from text)."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    AUX = "aux"
    OPERATOR = "operator"
    UNKNOWN = "unknown"


class ActorOrigin(StrEnum):
    """Who is performing a store mutation."""

    AGENT = "agent"
    OPERATOR = "operator"
    AUX = "aux"
    SYSTEM = "system"


class SpanState(StrEnum):
    ACTIVE = "active"
    DROPPED = "dropped"
    OFFLOADED = "offloaded"


class PinAction(StrEnum):
    PIN = "pin"
    UNPIN = "unpin"


ROLE_ORIGIN = {
    "system": SpanOrigin.SYSTEM,
    "developer": SpanOrigin.SYSTEM,
    "user": SpanOrigin.USER,
    "assistant": SpanOrigin.ASSISTANT,
    "model": SpanOrigin.ASSISTANT,
    "tool": SpanOrigin.TOOL,
    "function": SpanOrigin.TOOL,
}

#: Roles whose archived spans are protected by default.
PROTECTED_ROLES = frozenset({"system", "developer", "user"})


def origin_for_role(role: str) -> SpanOrigin:
    return ROLE_ORIGIN.get(str(role).strip().lower(), SpanOrigin.UNKNOWN)


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------

_OP_FIELDS = frozenset(
    {"kind", "span_ids", "replacement", "actor_origin", "provenance", "expected_revision", "txn_id"}
)

_TXN_ID_MAX = 128


def validate_txn_id(txn_id: Any) -> str:
    if not isinstance(txn_id, str) or not txn_id:
        raise ValidationError("txn_id must be a non-empty string", code="invalid_txn_id")
    if len(txn_id) > _TXN_ID_MAX:
        raise ValidationError(
            f"txn_id longer than {_TXN_ID_MAX} characters", code="invalid_txn_id"
        )
    if any(ch.isspace() or ord(ch) < 0x20 for ch in txn_id):
        raise ValidationError("txn_id must not contain whitespace/control chars", code="invalid_txn_id")
    return txn_id


def validate_span_id(span_id: Any) -> str:
    if not isinstance(span_id, str) or not span_id:
        raise ValidationError("span_id must be a non-empty string", code="invalid_span_id")
    if len(span_id) > 128:
        raise ValidationError("span_id too long", code="invalid_span_id")
    return span_id


@dataclass(frozen=True, slots=True)
class EditOperation(_Immutable):
    """One immutable projection edit.

    ``actor_origin`` is the authority that issued the edit; ``provenance``
    records where rewritten content came from (``aux``, ``model``) so that
    untrusted origin survives a rewrite.
    """

    kind: EditKind
    span_ids: tuple[str, ...] = ()
    replacement: str | None = None
    actor_origin: ActorOrigin = ActorOrigin.AGENT
    provenance: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    expected_revision: int | None = None
    txn_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EditKind):
            try:
                object.__setattr__(self, "kind", EditKind(self.kind))
            except ValueError as exc:  # pragma: no cover - defensive
                raise ValidationError(f"unknown edit kind: {self.kind!r}", code="invalid_kind") from exc
        if not isinstance(self.actor_origin, ActorOrigin):
            try:
                object.__setattr__(self, "actor_origin", ActorOrigin(self.actor_origin))
            except ValueError as exc:
                raise ValidationError(
                    f"unknown actor_origin: {self.actor_origin!r}", code="invalid_actor"
                ) from exc
        span_ids = tuple(self.span_ids or ())
        for item in span_ids:
            validate_span_id(item)
        object.__setattr__(self, "span_ids", span_ids)
        if self.kind is EditKind.REWRITE:
            if not isinstance(self.replacement, str):
                raise ValidationError(
                    "rewrite requires a text replacement (multimodal content is not rewritable)",
                    code="invalid_replacement",
                )
        else:
            if self.replacement is not None:
                raise ValidationError(
                    f"{self.kind.value} does not accept a replacement payload",
                    code="invalid_replacement",
                )
        if self.kind is not EditKind.BOUNDARY and not span_ids:
            raise ValidationError(
                f"{self.kind.value} requires at least one span_id", code="missing_span_ids"
            )
        if self.kind is EditKind.BOUNDARY and span_ids:
            raise ValidationError("boundary does not accept span_ids", code="invalid_span_ids")
        if self.expected_revision is not None and (
            not isinstance(self.expected_revision, int) or self.expected_revision < 0
        ):
            raise ValidationError("expected_revision must be a non-negative int", code="invalid_revision")
        if self.txn_id is not None:
            validate_txn_id(self.txn_id)
        if not isinstance(self.provenance, Mapping):
            raise ValidationError("provenance must be a mapping", code="invalid_provenance")
        object.__setattr__(self, "provenance", _frozen_map(self.provenance))

    # -- serialization ---------------------------------------------------
    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "span_ids": list(self.span_ids),
            "actor_origin": self.actor_origin.value,
            "provenance": dict(self.provenance),
        }
        if self.replacement is not None:
            payload["replacement"] = self.replacement
        if self.expected_revision is not None:
            payload["expected_revision"] = self.expected_revision
        if self.txn_id is not None:
            payload["txn_id"] = self.txn_id
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_payload())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EditOperation":
        if not isinstance(raw, Mapping):
            raise ValidationError("operation must be a mapping", code="invalid_operation")
        unknown = set(raw) - _OP_FIELDS
        if unknown:
            raise ValidationError(
                f"unknown operation field(s): {sorted(unknown)}", code="unknown_field", details={"fields": sorted(unknown)}
            )
        span_ids = raw.get("span_ids", ())
        if isinstance(span_ids, str):
            span_ids = (span_ids,)
        elif isinstance(span_ids, Iterable):
            span_ids = tuple(span_ids)
        else:
            raise ValidationError("span_ids must be a sequence", code="invalid_span_ids")
        try:
            kind = EditKind(raw.get("kind"))
        except ValueError as exc:
            raise ValidationError(
                f"unknown edit kind: {raw.get('kind')!r}", code="invalid_kind"
            ) from exc
        return cls(
            kind=kind,
            span_ids=span_ids,
            replacement=raw.get("replacement"),
            actor_origin=raw.get("actor_origin", ActorOrigin.AGENT),
            provenance=raw.get("provenance") or {},
            expected_revision=raw.get("expected_revision"),
            txn_id=raw.get("txn_id"),
        )


def coerce_operations(operations: Iterable[Any], *, actor_origin: str) -> tuple[EditOperation, ...]:
    """Normalize a caller-supplied operation list, forcing actor authority."""

    if operations is None:
        raise ValidationError("operations are required", code="missing_operations")
    if isinstance(operations, (str, bytes, Mapping)):
        raise ValidationError("operations must be a sequence", code="invalid_operations")
    actor = ActorOrigin(actor_origin)
    out: list[EditOperation] = []
    for raw in operations:
        op = raw if isinstance(raw, EditOperation) else EditOperation.from_mapping(raw)
        if op.actor_origin is not actor:
            raise ValidationError(
                "operation actor_origin does not match the calling actor",
                code="actor_mismatch",
                details={"expected": actor.value, "found": op.actor_origin.value},
            )
        out.append(op)
    if not out:
        raise ValidationError("operations must not be empty", code="missing_operations")
    return tuple(out)


# ---------------------------------------------------------------------------
# archived spans
# ---------------------------------------------------------------------------


def offload_stub(span_id: str, content_sha256: str) -> str:
    """Deterministic, non-imperative placeholder text for an offloaded span.

    Contains only identifiers and hash metadata: never instructions, never
    reconstructed content.
    """

    digest = (content_sha256 or "")[:16]
    return f"[archived span {span_id} sha256:{digest} — content offloaded from context]"


@dataclass(frozen=True, slots=True)
class ArchivedSpan(_Immutable):
    """Redacted, archived raw message plus its current projection state.

    ``message`` is the archived raw structure after ingestion redaction.  It is
    never a live host object: each snapshot hands out its own deep copy.
    """

    span_id: str
    uid: str
    message: Mapping[str, Any]
    origin: SpanOrigin
    protected: bool
    content_sha256: str
    group_id: str | None = None
    aliases: tuple[str, ...] = ()
    seq: int = 0
    version: int = 1
    created_revision: int = 0
    editable: bool = True
    present: bool = True
    current: bool = True
    state: SpanState = SpanState.ACTIVE
    rewrite_text: str | None = None
    rewrite_sha256: str | None = None
    pinned: bool = False
    est_tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.origin, SpanOrigin):
            object.__setattr__(self, "origin", SpanOrigin(self.origin))
        if not isinstance(self.state, SpanState):
            object.__setattr__(self, "state", SpanState(self.state))
        object.__setattr__(self, "aliases", tuple(self.aliases or ()))
        object.__setattr__(self, "message", _frozen_deep(self.message))
        object.__setattr__(self, "protected", bool(self.protected))
        object.__setattr__(self, "editable", bool(self.editable))
        object.__setattr__(self, "present", bool(self.present))
        object.__setattr__(self, "current", bool(self.current))
        object.__setattr__(self, "pinned", bool(self.pinned))

    # -- derived views ---------------------------------------------------
    @property
    def role(self) -> str:
        return str(self.message.get("role", "")) if self.message else ""

    @property
    def text(self) -> str:
        """Best-effort text of the archived message (no structure synthesis)."""

        content = self.message.get("content") if self.message else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, Mapping) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
            return "".join(parts)
        return ""

    def effective_message(self) -> dict[str, Any]:
        """Fresh mutable copy of the message as it should be projected.

        Offloaded spans become a stub message carrying only id/hash metadata;
        rewritten spans keep role/structure and replace ``content`` only.
        """

        base = copy.deepcopy(dict(self.message))
        if self.state is SpanState.OFFLOADED:
            base["content"] = offload_stub(self.span_id, self.content_sha256)
            return base
        if self.rewrite_text is not None:
            base["content"] = self.rewrite_text
        return base

    def stub(self) -> str:
        return offload_stub(self.span_id, self.content_sha256)

    def to_dict(self, *, include_message: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "span_id": self.span_id,
            "uid": self.uid,
            "origin": self.origin.value,
            "protected": self.protected,
            "content_sha256": self.content_sha256,
            "group_id": self.group_id,
            "aliases": list(self.aliases),
            "seq": self.seq,
            "version": self.version,
            "created_revision": self.created_revision,
            "editable": self.editable,
            "present": self.present,
            "current": self.current,
            "state": self.state.value,
            "rewrite_sha256": self.rewrite_sha256,
            "pinned": self.pinned,
            "est_tokens": self.est_tokens,
        }
        if include_message:
            out["message"] = copy.deepcopy(dict(self.message))
            out["rewrite_text"] = self.rewrite_text
        return out


@dataclass(frozen=True, slots=True)
class PinRecord(_Immutable):
    span_id: str
    actor_origin: ActorOrigin
    revision: int
    pinned_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.actor_origin, ActorOrigin):
            object.__setattr__(self, "actor_origin", ActorOrigin(self.actor_origin))

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "actor_origin": self.actor_origin.value,
            "revision": self.revision,
            "pinned_at": self.pinned_at,
        }


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Proposal(_Immutable):
    proposal_id: str
    proposal_sha256: str
    base_revision: int
    operations: tuple[EditOperation, ...]
    origin: str = "aux"
    created_revision: int = 0

    __hash__ = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))
        if self.origin != "aux":
            raise ValidationError("proposals may only originate from aux", code="invalid_origin")

    @property
    def span_ids(self) -> tuple[str, ...]:
        return tuple(sid for op in self.operations for sid in op.span_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_sha256": self.proposal_sha256,
            "base_revision": self.base_revision,
            "origin": self.origin,
            "operations": [op.to_payload() for op in self.operations],
            "created_revision": self.created_revision,
            "span_ids": list(self.span_ids),
        }


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoreSnapshot(_Immutable):
    """Immutable, deep-copy-safe view of a committed revision.

    Contains no SQLite handles and no absolute paths.  ``spans`` holds the
    current span for every ingested UID; ``aliases`` maps any absorbed span id
    to its current span id.  Historical span versions stay in the archive and
    are reachable through :meth:`ContextStore.retrieve`.
    """

    session_id: str
    db_id: str
    revision: int
    head_hash: str
    operations: tuple[EditOperation, ...] = ()
    spans: Mapping[str, ArchivedSpan] = field(default_factory=lambda: MappingProxyType({}))
    pins: Mapping[str, PinRecord] = field(default_factory=lambda: MappingProxyType({}))
    aliases: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    counts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    est_tokens: int = 0
    operations_truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))
        object.__setattr__(self, "spans", _frozen_map(self.spans))
        object.__setattr__(self, "pins", _frozen_map(self.pins))
        object.__setattr__(self, "aliases", _frozen_map(self.aliases))
        object.__setattr__(self, "counts", _frozen_map(self.counts))

    # -- lookups ---------------------------------------------------------
    def resolve(self, span_id: str) -> str:
        """Resolve a possibly-absorbed span id to the current one."""

        seen: set[str] = set()
        current = span_id
        while current not in self.spans and current not in seen:
            seen.add(current)
            nxt = self.aliases.get(current)
            if nxt is None:
                break
            current = nxt
        return current

    def span(self, span_id: str) -> ArchivedSpan | None:
        return self.spans.get(self.resolve(span_id))

    def active_spans(self) -> tuple[ArchivedSpan, ...]:
        return tuple(
            sorted(
                (span for span in self.spans.values() if span.state is SpanState.ACTIVE),
                key=lambda span: span.seq,
            )
        )

    def to_dict(self, *, include_messages: bool = False) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "db_id": self.db_id,
            "revision": self.revision,
            "head_hash": self.head_hash,
            "counts": dict(self.counts),
            "est_tokens": self.est_tokens,
            "pins": [pin.to_dict() for pin in self.pins.values()],
            "aliases": dict(self.aliases),
            "operations": [op.to_payload() for op in self.operations],
            "operations_truncated": self.operations_truncated,
            "spans": [span.to_dict(include_message=include_messages) for span in self.spans.values()],
        }


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Receipt(_Immutable):
    """Per-span outcome receipt: applied, inert or refused."""

    span_id: str
    kind: str
    effect: str  # "applied" | "inert"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"span_id": self.span_id, "kind": self.kind, "effect": self.effect}
        if self.reason:
            out["reason"] = self.reason
        return out


Receipts = tuple[Receipt, ...]


def _receipts_to_list(receipts: Iterable[Receipt]) -> list[dict[str, Any]]:
    return [receipt.to_dict() for receipt in receipts]


@dataclass(frozen=True, slots=True)
class OperationResult(_Immutable):
    ok: bool
    revision: int
    applied: tuple[Receipt, ...] = ()
    inert: tuple[Receipt, ...] = ()
    errors: tuple[Mapping[str, Any], ...] = ()
    receipts: Receipts = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "applied", tuple(self.applied))
        object.__setattr__(self, "inert", tuple(self.inert))
        object.__setattr__(self, "errors", tuple(_frozen_map(err) for err in self.errors))
        object.__setattr__(self, "receipts", tuple(self.receipts) or tuple(self.applied) + tuple(self.inert))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "revision": self.revision,
            "applied": _receipts_to_list(self.applied),
            "inert": _receipts_to_list(self.inert),
            "errors": [dict(err) for err in self.errors],
            "receipts": _receipts_to_list(self.receipts),
        }


@dataclass(frozen=True, slots=True)
class IngestResult(_Immutable):
    ok: bool
    revision: int
    archived: int = 0
    duplicates: int = 0
    versions: int = 0
    ephemeral_skipped: int = 0
    redactions: int = 0
    aliases_absorbed: int = 0
    spans_total: int = 0
    updated_span_ids: tuple[str, ...] = ()
    errors: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "updated_span_ids", tuple(self.updated_span_ids))
        object.__setattr__(self, "errors", tuple(_frozen_map(err) for err in self.errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "revision": self.revision,
            "archived": self.archived,
            "duplicates": self.duplicates,
            "versions": self.versions,
            "ephemeral_skipped": self.ephemeral_skipped,
            "redactions": self.redactions,
            "aliases_absorbed": self.aliases_absorbed,
            "spans_total": self.spans_total,
            "updated_span_ids": list(self.updated_span_ids),
            "errors": [dict(err) for err in self.errors],
        }


@dataclass(frozen=True, slots=True)
class StageResult(OperationResult):
    txn_id: str = ""
    staged_sha256: str = ""
    staged_operations: tuple[EditOperation, ...] = ()
    replayed: bool = False
    already_committed: bool = False

    def __post_init__(self) -> None:
        OperationResult.__post_init__(self)
        object.__setattr__(self, "staged_operations", tuple(self.staged_operations))

    def to_dict(self) -> dict[str, Any]:
        out = OperationResult.to_dict(self)
        out.update(
            {
                "txn_id": self.txn_id,
                "staged_sha256": self.staged_sha256,
                "staged_operations": [op.to_payload() for op in self.staged_operations],
                "replayed": self.replayed,
                "already_committed": self.already_committed,
            }
        )
        return out


@dataclass(frozen=True, slots=True)
class CommitResult(OperationResult):
    txn_id: str = ""
    head_hash: str = ""
    kind: str = "edit"
    replayed: bool = False
    base_revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        out = OperationResult.to_dict(self)
        out.update(
            {
                "txn_id": self.txn_id,
                "head_hash": self.head_hash,
                "kind": self.kind,
                "replayed": self.replayed,
                "base_revision": self.base_revision,
            }
        )
        return out
