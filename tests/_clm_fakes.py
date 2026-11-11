"""Fakes for the CLM engine tests: a fake store, snapshot, spans and host.

These model the *public* shapes in ``docs/integration-contract.md``, so the
engine and projection are exercised against the contract rather than against a
particular store implementation (which lives on a sibling branch).
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_clm.identity import content_sha256  # noqa: E402

#: Fields the host strips from every provider-bound request copy.
PERSISTENCE_FIELDS = frozenset(
    {
        "timestamp",
        "display_kind",
        "display_metadata",
        "_row_id",
        "message_uid",
        "_absorbed_message_uids",
        "_tool_call_uids",
        "_tool_call_uid",
        "api_content",
    }
)


def message(role: str, content: Any, uid: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """Build a host-shaped message dict."""
    row: Dict[str, Any] = {"role": role, "content": content}
    if uid:
        row["message_uid"] = uid
    row.update(extra)
    return row


def assistant_call(uid: str, *call_ids: str, content: str = "") -> Dict[str, Any]:
    """An assistant row that declares tool calls."""
    return {
        "role": "assistant",
        "content": content,
        "message_uid": uid,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "shell", "arguments": "{}"}}
            for call_id in call_ids
        ],
    }


def tool_result(uid: str, call_id: str, content: str) -> Dict[str, Any]:
    """A tool row answering ``call_id``."""
    return {"role": "tool", "content": content, "message_uid": uid, "tool_call_id": call_id}


def assemble(
    history: Sequence[Mapping[str, Any]],
    *,
    system: str = "SYSTEM PROMPT",
    prefill: Sequence[Mapping[str, Any]] = (),
    trailing: Sequence[Mapping[str, Any]] = (),
    injections: Optional[Mapping[int, str]] = None,
) -> List[Dict[str, Any]]:
    """Mimic the host's assembled request: system + prefill + stripped history + tail.

    Persistence-only fields (``message_uid`` included) are stripped exactly as
    ``agent/turn_context.py`` does, and an optional per-row injection rewrites a
    user row's content the way memory/plugin prefetch does.
    """
    request: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    for row in prefill:
        request.append(
            {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key not in PERSISTENCE_FIELDS
            }
        )
    for index, row in enumerate(history):
        clean = {
            key: copy.deepcopy(value) for key, value in row.items() if key not in PERSISTENCE_FIELDS
        }
        if injections and index in injections:
            clean["content"] = str(clean.get("content", "")) + injections[index]
        request.append(clean)
    for row in trailing:
        request.append(
            {
                key: copy.deepcopy(value)
                for key, value in row.items()
                if key not in PERSISTENCE_FIELDS
            }
        )
    return request


def coding_turns() -> List[Dict[str, Any]]:
    """Two turns with one tool group each: the first group is editable, the second newest."""
    return [
        message("user", "list the files", "u1"),
        assistant_call("a1", "call_1"),
        tool_result("t1", "call_1", "OLD TOOL OUTPUT\n" * 40),
        message("assistant", "listed six files", "a2"),
        message("user", "now run the tests", "u2"),
        assistant_call("a3", "call_2"),
        tool_result("t2", "call_2", "NEWEST TOOL OUTPUT"),
    ]


@dataclass
class FakeSpan:
    """Structural stand-in for ``models.ArchivedSpan``."""

    span_id: str
    uid: str
    message: Mapping[str, Any]
    aliases: Tuple[str, ...] = ()
    group_id: Optional[str] = None
    origin: str = "agent"
    protected: bool = False
    content_sha256: Optional[str] = None
    #: Current rewrite for this span, when the snapshot carries one.
    rewrite_text: Optional[str] = None
    #: Span state the snapshot reports (``None`` means "no state": active).
    #: The store's snapshot uses a ``SpanState`` enum here; these fakes use the
    #: string spelling the projector also accepts.
    state: Optional[str] = None

    def __post_init__(self) -> None:
        if self.content_sha256 is None:
            self.content_sha256 = content_sha256(self.message.get("content"))


@dataclass
class FakeOperation:
    """Structural stand-in for ``models.EditOperation``."""

    kind: str
    span_ids: Tuple[str, ...]
    replacement: Optional[str] = None
    actor_origin: str = "agent"
    provenance: Optional[Mapping[str, Any]] = None


@dataclass(eq=False)
class FakeSnapshot:
    """Structural stand-in for ``models.StoreSnapshot``."""

    revision: int = 0
    operations: Tuple[Any, ...] = ()
    spans: Dict[str, FakeSpan] = field(default_factory=dict)
    pins: Tuple[str, ...] = ()
    head_hash: str = ""
    #: True when the store capped its replayed operation log.
    operations_truncated: bool = False


def spans_from_history(
    history: Sequence[Mapping[str, Any]], *, protected: Iterable[str] = ()
) -> Dict[str, FakeSpan]:
    """Archive a history list into spans keyed by span id (== uid for the fake)."""
    protected_set = set(protected)
    spans: Dict[str, FakeSpan] = {}
    for row in history:
        uid = row.get("message_uid")
        if not uid:
            continue
        spans[uid] = FakeSpan(
            span_id=uid,
            uid=uid,
            message={key: copy.deepcopy(value) for key, value in row.items()},
            protected=uid in protected_set,
        )
    return spans


def snapshot_for(
    history: Sequence[Mapping[str, Any]],
    operations: Sequence[Any] = (),
    *,
    revision: int = 1,
    pins: Sequence[str] = (),
    protected: Iterable[str] = (),
) -> FakeSnapshot:
    """A snapshot archiving ``history`` with ``operations`` committed at ``revision``."""
    return FakeSnapshot(
        revision=revision,
        operations=tuple(operations),
        spans=spans_from_history(history, protected=protected),
        pins=tuple(pins),
        head_hash=f"hash-{revision}",
    )


@dataclass(eq=False)
class FakeStoreResult:
    """Store result shape (JSON-serializable, ``ok`` gated)."""

    ok: bool = True
    revision: int = 0
    errors: Tuple[str, ...] = ()
    applied: Tuple[Any, ...] = ()
    inert: Tuple[Any, ...] = ()
    receipts: Tuple[Any, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "revision": self.revision,
            "errors": list(self.errors),
            "applied": list(self.applied),
            "inert": list(self.inert),
            "receipts": list(self.receipts),
        }


class FakeStore:
    """A store that satisfies the engine-facing contract and records calls."""

    def __init__(
        self,
        snapshot: Optional[FakeSnapshot] = None,
        *,
        supports_commit: bool = True,
        family: str = "fake",
    ) -> None:
        self._snapshot = snapshot or FakeSnapshot()
        self.ingest_calls: List[List[Dict[str, Any]]] = []
        self.staged: List[Any] = []
        self.discarded: List[str] = []
        self.commits: List[Dict[str, Any]] = []
        self.closed = False
        self.supports_commit = supports_commit
        self.family = family

    # --- contract surface used by the engine
    def ingest(self, messages: Sequence[Mapping[str, Any]]) -> FakeStoreResult:
        self.ingest_calls.append([copy.deepcopy(dict(row)) for row in messages])
        return FakeStoreResult(ok=True, revision=self._snapshot.revision)

    def snapshot(self) -> FakeSnapshot:
        return self._snapshot

    def status(self) -> Dict[str, Any]:
        return {"revision": self._snapshot.revision, "session": self.family}

    def inspect(self, *, cursor: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        return {"cursor": cursor, "limit": limit, "spans": sorted(self._snapshot.spans)}

    def search(
        self, query: str, *, cursor: Optional[str] = None, limit: int = 20
    ) -> Dict[str, Any]:
        return {"query": query, "cursor": cursor, "limit": limit, "results": []}

    def retrieve(self, span_id: str) -> Dict[str, Any]:
        span = self._snapshot.spans.get(span_id)
        return {"span_id": span_id, "found": span is not None}

    def close(self) -> None:
        self.closed = True

    # --- commit surface (absent when supports_commit is False)
    def stage_operations(self, operations, *, expected_revision, txn_id, actor_origin):
        self.staged.append((list(operations), expected_revision, txn_id, actor_origin))
        return FakeStoreResult(ok=True, revision=expected_revision)

    def discard_txn(self, *, txn_id):
        self.discarded.append(txn_id)
        self.staged = [entry for entry in self.staged if entry[2] != txn_id]

    def commit_txn(self, *, txn_id, expected_revision):
        self.commits.append({"txn_id": txn_id, "expected_revision": expected_revision})
        # Apply this transaction's staged operations the way a real store would:
        # append to the immutable operation log and bump the head revision.
        for operations, revision, recorded_txn, _origin in self.staged:
            if recorded_txn != txn_id:
                continue
            self._snapshot.operations = tuple(self._snapshot.operations) + tuple(operations)
            self._snapshot.revision = max(self._snapshot.revision, int(revision)) + 1
            self._snapshot.head_hash = f"hash-{self._snapshot.revision}"
        return FakeStoreResult(ok=True, revision=self._snapshot.revision)


def _install_commit_surface(store: FakeStore) -> None:
    store.stage = store.stage_operations  # type: ignore[attr-defined]
    store.commit = store.commit_txn  # type: ignore[attr-defined]
    store.discard = store.discard_txn  # type: ignore[attr-defined]


def make_store(
    snapshot: Optional[FakeSnapshot] = None, *, supports_commit: bool = True, family: str = "fake"
) -> FakeStore:
    """A FakeStore with the commit surface bound when requested."""
    store = FakeStore(snapshot, supports_commit=True, family=family)
    if supports_commit:
        _install_commit_surface(store)
    else:
        store.stage = None  # type: ignore[assignment]
        store.commit = None  # type: ignore[assignment]
    return store


class StoreFactory:
    """Records every store the engine opens; returns the same store per session."""

    def __init__(
        self, snapshot: Optional[FakeSnapshot] = None, *, supports_commit: bool = True
    ) -> None:
        self.snapshot = snapshot or FakeSnapshot()
        self.supports_commit = supports_commit
        self.opened: List[Dict[str, Any]] = []
        self.stores: List[FakeStore] = []

    def __call__(self, hermes_home, session_id, *, read_only: bool = False) -> FakeStore:
        self.opened.append(
            {"hermes_home": hermes_home, "session_id": session_id, "read_only": read_only}
        )
        store = make_store(
            self.snapshot, supports_commit=self.supports_commit, family=str(session_id)
        )
        self.stores.append(store)
        return store

    @property
    def store(self) -> FakeStore:
        return self.stores[-1]
