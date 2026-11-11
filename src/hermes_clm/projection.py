"""Pure request projection: committed operations applied to request copies.

``project()`` is a pure function of (request messages, UID-bearing history,
committed snapshot). It never writes, never calls a model, never reads the
clock and never consults token-dependent state to *choose* what to edit - the
agent already chose, in a committed operation. Its only outputs are fresh
message dictionaries for this request and diagnostics.

Invariants enforced here (see ``docs/adr/0001-request-projection.md``):

* protected rows (system/developer/user, pinned, newest tool result, non-text
  content) pass through byte-identically;
* an op that targets a protected, ambiguous, multimodal or invalid row aborts
  the whole projection instead of yielding a guessed subset;
* tool groups are atomic: a drop either covers a whole group or is refused;
* every returned message is a deep copy, so caller mutation cannot reach store
  or later projections.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .budget import estimate_messages_tokens
from .identity import (
    align,
    content_sha256,
    group_covered,
    is_editable_content,
    is_protected_message,
    role_of,
    row_signature,
    tool_groups,
)

#: Closed set of operation kinds. Anything else is structural corruption.
CLOSED_KINDS = frozenset({"keep", "drop", "rewrite", "pin", "offload", "restore", "boundary"})
#: Kinds that change provider-visible bytes.
CONTENT_KINDS = frozenset({"drop", "rewrite", "offload", "restore"})
#: Kinds that are bookkeeping only and never touch a request row.
NOOP_KINDS = frozenset({"keep", "pin", "boundary"})
#: Default ceiling for a rewrite/restore payload, in bytes of UTF-8 text.
DEFAULT_MAX_REWRITE_BYTES = 16_384

#: Offload stub: non-imperative, no instructions, span identity and hash only.
OFFLOAD_STUB_TEMPLATE = "[clm.offloaded span={span_id} sha256={content_sha256}]"


def _field(source: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing object."""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def offload_stub(span_id: str, content_sha256_hex: str) -> str:
    """The placeholder text left behind by an offload.

    Deliberately non-imperative: it names the span and its content hash so a
    retrieval call can be constructed, and says nothing a model could mistake
    for an instruction.
    """
    return OFFLOAD_STUB_TEMPLATE.format(span_id=span_id, content_sha256=content_sha256_hex)


@dataclass(eq=False)
class ProjectionResult:
    """Outcome of one projection.

    ``ok`` is ``False`` for a refused projection: the caller must leave the
    request untouched (``messages`` is empty) rather than send a partial edit.
    """

    ok: bool = True
    revision: int = 0
    messages: Tuple[Dict[str, Any], ...] = ()
    applied: Tuple[Dict[str, Any], ...] = ()
    inert: Tuple[Dict[str, Any], ...] = ()
    conflicts: Tuple[Dict[str, Any], ...] = ()
    before_tokens: int = 0
    after_tokens: int = 0
    alignment: Dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        """True when at least one request row was actually rewritten or removed."""
        return bool(self.applied)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable diagnostics (no message payloads)."""
        return {
            "ok": self.ok,
            "revision": self.revision,
            "changed": self.changed,
            "applied": list(self.applied),
            "inert": list(self.inert),
            "conflicts": list(self.conflicts),
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "alignment": dict(self.alignment),
        }


@dataclass(eq=False)
class _SpanState:
    """Resolved final state for one editable span."""

    kind: str
    span_id: str
    uid: str
    content: Optional[str] = None


def _span_index(snapshot: Any) -> Dict[str, Any]:
    """Index spans by span id, uid and absorbed alias."""
    spans = _field(snapshot, "spans", {}) or {}
    index: Dict[str, Any] = {}
    if isinstance(spans, Mapping):
        for key, span in spans.items():
            span_id = _field(span, "span_id", key)
            uid = _field(span, "uid")
            if isinstance(span_id, str) and span_id:
                index.setdefault(span_id, span)
            if isinstance(uid, str) and uid:
                index.setdefault(uid, span)
            for alias in _field(span, "aliases", ()) or ():
                if isinstance(alias, str) and alias:
                    index.setdefault(alias, span)
    return index


def _pin_ids(snapshot: Any) -> frozenset:
    """Pinned span/uid identifiers from a mapping, ``PinRecord`` sequence or ids.

    The store publishes ``pins`` as ``Mapping[span_id, PinRecord]``; a structural
    snapshot may use a sequence instead. ``PinRecord.span_id`` is the identifier
    in both cases — ``str(record)`` would never match a span.
    """
    pins = _field(snapshot, "pins", None)
    if pins is None:
        return frozenset()
    if isinstance(pins, Mapping):
        identifiers = {str(key) for key in pins.keys()}
        for value in pins.values():
            span_id = _field(value, "span_id", None)
            if isinstance(span_id, str) and span_id:
                identifiers.add(span_id)
        return frozenset(identifiers)
    if isinstance(pins, (str, bytes)):
        return frozenset({pins.decode() if isinstance(pins, bytes) else pins})
    identifiers = set()
    try:
        for item in pins:
            if isinstance(item, str):
                identifiers.add(item)
                continue
            span_id = _field(item, "span_id", None)
            if isinstance(span_id, str) and span_id:
                identifiers.add(span_id)
    except TypeError:  # pragma: no cover - defensive, store pins are iterable
        return frozenset()
    return frozenset(identifiers)


def _span_content(span: Any) -> Any:
    """The archived ``content`` of a span's redacted message."""
    message = _field(span, "message", {}) or {}
    return _field(message, "content")


def _span_role(span: Any) -> Optional[str]:
    """The archived role of a span's message."""
    return role_of(_field(span, "message", {}) or {})


def _span_uid(span: Any, fallback: str = "") -> str:
    """The span's durable uid (aliases resolve to the survivor)."""
    uid = _field(span, "uid")
    return uid if isinstance(uid, str) and uid else fallback


def _conflict(
    code: str, *, kind: str = "", span_id: str = "", uid: str = "", detail: str = ""
) -> Dict[str, Any]:
    """Build a refusal receipt."""
    receipt = {"code": code, "kind": kind}
    if span_id:
        receipt["span_id"] = span_id
    if uid:
        receipt["uid"] = uid
    if detail:
        receipt["detail"] = detail
    return receipt


def _applied(
    kind: str, *, span_id: str, uid: str, request_index: Optional[int] = None
) -> Dict[str, Any]:
    """Build an applied receipt."""
    receipt: Dict[str, Any] = {"kind": kind, "span_id": span_id, "uid": uid}
    if request_index is not None:
        receipt["request_index"] = request_index
    return receipt


def _inert(kind: str, *, span_id: str, reason: str, uid: str = "") -> Dict[str, Any]:
    """Build an inert receipt (op could not affect this request)."""
    receipt = {"kind": kind, "span_id": span_id, "reason": reason}
    if uid:
        receipt["uid"] = uid
    return receipt


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


def project(
    request_messages: Sequence[Mapping[str, Any]],
    *,
    conversation_messages: Optional[Sequence[Mapping[str, Any]]] = None,
    snapshot: Any = None,
    incoming_message: Optional[Mapping[str, Any]] = None,
    max_rewrite_bytes: Optional[int] = DEFAULT_MAX_REWRITE_BYTES,
) -> ProjectionResult:
    """Project committed operations onto a fresh copy of ``request_messages``.

    ``conversation_messages`` is the host's UID-bearing history; without it no
    row can be identified, so nothing is editable and the request passes
    through unchanged. ``snapshot`` is a structural view of the store snapshot
    (``revision``, ``operations``, ``spans``, ``pins``); it is only read.
    """
    requests = list(request_messages or ())
    if not requests or not all(isinstance(row, Mapping) for row in requests):
        return ProjectionResult(
            ok=False,
            conflicts=(
                _conflict("invalid_request", detail="request rows must be non-empty mappings"),
            ),
        )

    history = list(conversation_messages or ())
    alignment = align(requests, history)
    revision = int(_field(snapshot, "revision", 0) or 0)

    before_tokens = estimate_messages_tokens(requests)
    result = ProjectionResult(
        ok=True,
        revision=revision,
        before_tokens=before_tokens,
        alignment=alignment.summary(),
    )

    operations = tuple(_field(snapshot, "operations", ()) or ())
    index = _span_index(snapshot)
    pinned = _pin_ids(snapshot)
    groups = tool_groups(history)

    # Rows the host owns by position, not just by role.
    newest_tool_index = -1
    for position, row in enumerate(requests):
        if role_of(row) == "tool":
            newest_tool_index = position
    incoming_signature = (
        row_signature(incoming_message) if isinstance(incoming_message, Mapping) else None
    )
    incoming_indices: List[int] = []
    if incoming_signature is not None:
        for position, row in enumerate(requests):
            if row_signature(row) == incoming_signature:
                incoming_indices.append(position)

    protected_indices = set()
    for position, row in enumerate(requests):
        uid = alignment.uid_for(position)
        span = index.get(uid) if uid else None
        span_pinned = bool(
            span is not None and (_field(span, "span_id") in pinned or uid in pinned)
        )
        if is_protected_message(
            row,
            pinned=span_pinned,
            newest_tool_result=position == newest_tool_index,
            incoming=position in incoming_indices,
        ):
            protected_indices.add(position)

    # Fold the committed operation log, last write wins per span.
    states: Dict[str, _SpanState] = {}
    requested_drops: List[str] = []
    conflicts: List[Dict[str, Any]] = []

    for operation in operations:
        kind = _field(operation, "kind")
        raw_span_ids = _field(operation, "span_ids", ()) or ()
        span_ids: Tuple[str, ...] = tuple(
            str(item) for item in raw_span_ids if isinstance(item, str) and item
        )
        if not isinstance(kind, str) or kind not in CLOSED_KINDS:
            conflicts.append(_conflict("unknown_operation_kind", kind=str(kind)))
            continue
        if not span_ids:
            conflicts.append(_conflict("empty_operation", kind=kind))
            continue
        if kind in NOOP_KINDS:
            for span_id in span_ids:
                result.inert += (_inert(kind, span_id=span_id, reason="noop_operation"),)
            continue
        replacement = _field(operation, "replacement")
        for span_id in span_ids:
            span = index.get(span_id)
            if span is None:
                result.inert += (_inert(kind, span_id=span_id, reason="unknown_span"),)
                continue
            uid = _span_uid(span, span_id)
            if alignment.is_ambiguous(uid):
                conflicts.append(
                    _conflict(
                        "ambiguous_identity",
                        kind=kind,
                        span_id=span_id,
                        uid=uid,
                        detail="row identity is not unique in this request",
                    )
                )
                continue
            if uid not in alignment.uid_to_request:
                result.inert += (_inert(kind, span_id=span_id, uid=uid, reason="not_in_request"),)
                continue
            request_index = alignment.uid_to_request[uid]
            if request_index in protected_indices:
                conflicts.append(
                    _conflict(
                        "protected_row",
                        kind=kind,
                        span_id=span_id,
                        uid=uid,
                        detail="row is host-owned in this request",
                    )
                )
                continue
            if bool(_field(span, "protected", False)) or _span_role(span) in (
                "system",
                "developer",
                "user",
            ):
                conflicts.append(
                    _conflict(
                        "protected_span",
                        kind=kind,
                        span_id=span_id,
                        uid=uid,
                        detail="archived span is marked protected",
                    )
                )
                continue
            if _field(span, "span_id") in pinned or uid in pinned:
                conflicts.append(
                    _conflict(
                        "pinned_span",
                        kind=kind,
                        span_id=span_id,
                        uid=uid,
                        detail="pinned spans are immutable until an operator unpins",
                    )
                )
                continue
            archived_content = _span_content(span)
            if not is_editable_content(archived_content):
                conflicts.append(
                    _conflict(
                        "non_text_content",
                        kind=kind,
                        span_id=span_id,
                        uid=uid,
                        detail="multimodal or unknown content parts are never flattened",
                    )
                )
                continue
            if kind == "rewrite":
                if not isinstance(replacement, str):
                    conflicts.append(
                        _conflict("replacement_not_text", kind=kind, span_id=span_id, uid=uid)
                    )
                    continue
                if max_rewrite_bytes is not None and _byte_length(replacement) > int(
                    max_rewrite_bytes
                ):
                    conflicts.append(
                        _conflict(
                            "replacement_too_large",
                            kind=kind,
                            span_id=span_id,
                            uid=uid,
                            detail=f"limit={int(max_rewrite_bytes)} bytes",
                        )
                    )
                    continue
                states[uid] = _SpanState(kind=kind, span_id=span_id, uid=uid, content=replacement)
            elif kind == "restore":
                restored = archived_content if isinstance(archived_content, str) else ""
                if max_rewrite_bytes is not None and _byte_length(restored) > int(
                    max_rewrite_bytes
                ):
                    conflicts.append(
                        _conflict(
                            "restore_too_large",
                            kind=kind,
                            span_id=span_id,
                            uid=uid,
                            detail=f"limit={int(max_rewrite_bytes)} bytes",
                        )
                    )
                    continue
                states[uid] = _SpanState(kind=kind, span_id=span_id, uid=uid, content=restored)
            elif kind == "offload":
                digest = _field(span, "content_sha256") or content_sha256(archived_content)
                span_id_value = _field(span, "span_id", span_id)
                states[uid] = _SpanState(
                    kind=kind,
                    span_id=span_id_value,
                    uid=uid,
                    content=offload_stub(span_id_value, str(digest)),
                )
            elif kind == "drop":
                states[uid] = _SpanState(kind=kind, span_id=span_id, uid=uid)
                requested_drops.append(uid)

    # Tool groups are atomic: a drop covers a whole group or is refused.
    if requested_drops:
        dropped = set(requested_drops)
        touched: List[str] = []
        for uid in requested_drops:
            group_id = groups.group_of(uid)
            if group_id is not None and group_id not in touched:
                touched.append(group_id)
        for group_id in sorted(touched):
            members = groups.members.get(group_id, ())
            # A member the request no longer shows cannot be dropped, which is
            # exactly the partial-coverage case: refuse rather than orphan a call.
            if not group_covered(members, dropped):
                conflicts.append(_conflict("partial_tool_group", kind="drop", detail=group_id))

    if conflicts:
        result.ok = False
        result.conflicts = tuple(conflicts)
        result.messages = ()
        result.after_tokens = before_tokens
        return result

    # Apply, always on deep copies: the caller owns the result, the store does not.
    output: List[Dict[str, Any]] = []
    for position, row in enumerate(requests):
        uid = alignment.uid_for(position)
        state = states.get(uid) if uid else None
        if state is not None and state.kind == "drop":
            request_index = alignment.uid_to_request.get(state.uid, position)
            result.applied += (
                _applied("drop", span_id=state.span_id, uid=state.uid, request_index=request_index),
            )
            continue
        fresh = copy.deepcopy(dict(row))
        if state is not None:
            fresh["content"] = state.content
            # A stale replay sidecar would re-assert the pre-edit bytes upstream.
            fresh.pop("api_content", None)
            result.applied += (
                _applied(state.kind, span_id=state.span_id, uid=state.uid, request_index=position),
            )
        output.append(fresh)

    result.messages = tuple(output)
    result.after_tokens = estimate_messages_tokens(output)
    return result
