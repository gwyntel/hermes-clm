"""The single adaptation layer between this suite and the hermes-clm public contract.

Every `hermes_clm` symbol name appears here and nowhere else. If an implementation
delegate chooses a different spelling for a documented entry point, this file is the
only thing that changes (see docs/security-testing.md, "Contract adapter").

Authority note: nothing in this module is a source of truth about behaviour. It only
locates and binds to the API described in docs/integration-contract.md and
docs/architecture.md.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

STORE_MOD = "hermes_clm.store"
MODELS_MOD = "hermes_clm.models"
PROJECTION_MOD = "hermes_clm.projection"
ENGINE_MOD = "hermes_clm.engine"
TOOLS_MOD = "hermes_clm.tools"

SKIP_REASON = (
    "hermes_clm is not importable on this branch. src/hermes_clm lands on feat/store, "
    "feat/engine and feat/tools (phase-1 parallel development); this suite is written "
    "against docs/integration-contract.md and runs unchanged once those merge."
)

CLOSED_OP_KINDS = frozenset({"keep", "drop", "rewrite", "pin", "offload", "restore", "boundary"})
PROTECTED_ORIGINS = frozenset({"operator", "system", "host", "user"})
MODEL_ORIGINS = frozenset({"model", "assistant", "agent", "aux"})
PROJECTION_CANDIDATES = (
    "project",
    "project_messages",
    "project_request",
    "project_view",
    "apply_projection",
)


# --------------------------------------------------------------------------- imports


def need(*modules: str):
    """Module-level gate. Skips the whole module with an explicit reason."""
    import pytest

    loaded = [pytest.importorskip(name, reason=SKIP_REASON) for name in modules]
    return loaded[0] if len(loaded) == 1 else tuple(loaded)


def module(name: str):
    return importlib.import_module(name)


def has_module(name: str) -> bool:
    try:
        module(name)
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- primitives


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", "replace"))


def _bind(fn, values: Mapping[str, Any], *, what: str) -> dict[str, Any]:
    """Bind only the parameters `fn` actually accepts, and never invent one."""
    if not callable(fn):
        raise AssertionError(f"{what} is not callable: {fn!r}")
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C callables
        return dict(values)
    params = sig.parameters
    accepts_any = any(p.kind is p.VAR_KEYWORD for p in params.values())
    kwargs = {k: v for k, v in values.items() if k in params}
    if not accepts_any:
        missing = [
            name
            for name, p in params.items()
            if p.default is p.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            and name not in kwargs
        ]
        if missing:
            raise AssertionError(f"cannot bind {what}: no value for {missing}; signature={sig}")
    return kwargs


def store_cls():
    return module(STORE_MOD).ContextStore


def for_session(hermes_home, session_id, *, read_only: bool = False):
    values = {"hermes_home": str(hermes_home), "session_id": session_id, "read_only": read_only}
    fn = store_cls().for_session  # classmethod bound to the class, so no instance needed
    return fn(**_bind(fn, values, what="ContextStore.for_session"))


def session_dir(hermes_home, session_id) -> Path:
    return Path(hermes_home) / "clm" / "sessions" / str(session_id)


def active_json(hermes_home, session_id) -> Path:
    return session_dir(hermes_home, session_id) / "active.json"


DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def db_path(hermes_home, session_id) -> Path:
    """Locate the per-session database without hard-coding a filename."""
    directory = session_dir(hermes_home, session_id)
    if not directory.is_dir():
        raise AssertionError(f"session directory missing: {directory} (I1 layout)")
    candidates = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix in DB_SUFFIXES and not p.name.startswith(".")
    ]
    if len(candidates) != 1:
        raise AssertionError(
            f"expected exactly one per-session database under {directory}, found "
            f"{[p.name for p in candidates]}"
        )
    return candidates[0]


def session_files(hermes_home, session_id) -> list[Path]:
    directory = session_dir(hermes_home, session_id)
    return sorted(p for p in directory.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- store calls


def _res_get(result, *names, default=None):
    if result is None:
        return default
    for name in names:
        if isinstance(result, Mapping) and name in result:
            return result[name]
        if hasattr(result, name):
            return getattr(result, name)
    return default


def res_ok(result) -> bool:
    value = _res_get(result, "ok", "success")
    return True if value is None else bool(value)


def res_errors(result) -> list[str]:
    value = _res_get(result, "errors", "error", "diagnostics", default=[])
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def res_revision(result):
    return _res_get(result, "revision", "head_revision")


def res_applied(result) -> list:
    value = _res_get(result, "applied", default=[])
    return list(value or [])


def res_text(result) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, sort_keys=True)
    except Exception:
        return repr(result)


def ingest(store, messages):
    return store.ingest(**_bind(store.ingest, {"messages": list(messages)}, what="ingest"))


def snapshot(store):
    return store.snapshot()


def make_op(kind, span_ids=(), replacement=None, actor_origin="model", **extra):
    cls = getattr(module(MODELS_MOD), "EditOperation")
    values = {
        "kind": kind,
        "op_kind": kind,
        "span_ids": tuple(span_ids),
        "spans": tuple(span_ids),
        "replacement": replacement,
        "actor_origin": actor_origin,
        **extra,
    }
    return cls(**_bind(cls, values, what="EditOperation"))


def stage(store, operations, *, expected_revision, txn_id, actor_origin="model"):
    ops = list(operations)
    values = {
        "operations": ops,
        "ops": ops,
        "expected_revision": expected_revision,
        "txn_id": txn_id,
        "actor_origin": actor_origin,
    }
    return store.stage(**_bind(store.stage, values, what="ContextStore.stage"))


def commit(store, *, txn_id, expected_revision):
    values = {"txn_id": txn_id, "expected_revision": expected_revision}
    return store.commit(**_bind(store.commit, values, what="ContextStore.commit"))


def discard(store, *, txn_id):
    return store.discard(**_bind(store.discard, {"txn_id": txn_id}, what="discard"))


def revert(store, *, target_revision, expected_revision, txn_id, actor_origin="model"):
    values = {
        "target_revision": target_revision,
        "expected_revision": expected_revision,
        "txn_id": txn_id,
        "actor_origin": actor_origin,
    }
    return store.revert(**_bind(store.revert, values, what="ContextStore.revert"))


def status(store):
    return store.status()


def inspect_(store, **kwargs):
    return store.inspect(**_bind(store.inspect, kwargs, what="ContextStore.inspect"))


def search(store, query, **kwargs):
    values = {"query": query, **kwargs}
    return store.search(**_bind(store.search, values, what="ContextStore.search"))


def retrieve(store, span_id):
    return store.retrieve(
        **_bind(store.retrieve, {"span_id": span_id}, what="ContextStore.retrieve")
    )


def create_proposal(store, operations, *, base_revision):
    ops = list(operations)
    values = {"operations": ops, "ops": ops, "base_revision": base_revision}
    return store.create_proposal(**_bind(store.create_proposal, values, what="create_proposal"))


def accept_proposal(store, *, proposal_id, proposal_sha256, base_revision, txn_id):
    values = {
        "proposal_id": proposal_id,
        "proposal_sha256": proposal_sha256,
        "base_revision": base_revision,
        "txn_id": txn_id,
    }
    return store.accept_proposal(
        **_bind(store.accept_proposal, values, what="ContextStore.accept_proposal")
    )


def close(store):
    fn = getattr(store, "close", None)
    if callable(fn):
        fn()


# --------------------------------------------------------------------------- snapshot views


def snap_revision(snap) -> int:
    return int(_res_get(snap, "revision", default=-1))


def snap_spans(snap) -> dict[str, Any]:
    spans = _res_get(snap, "spans", default={}) or {}
    return dict(spans)


def snap_ops(snap) -> tuple:
    return tuple(_res_get(snap, "operations", "ops", default=()) or ())


def snap_pins(snap) -> Any:
    return _res_get(snap, "pins", default=()) or ()


def snap_head(snap):
    return _res_get(snap, "head_hash", default=None)


def span_message(span) -> dict:
    msg = _res_get(span, "message", "payload", default={}) or {}
    return dict(msg) if isinstance(msg, Mapping) else {"content": msg}


def span_text(span) -> str:
    return json.dumps(span_message(span), default=str, sort_keys=True)


def span_field(span, name, default=None):
    return _res_get(span, name, default=default)


def all_strings(obj, _depth: int = 0) -> Iterable[str]:
    """Every string reachable from a result object (for leakage assertions)."""
    if _depth > 8:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, bytes):
        yield obj.decode("utf-8", "replace")
    elif isinstance(obj, Mapping):
        for key, value in obj.items():
            yield str(key)
            yield from all_strings(value, _depth + 1)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            yield from all_strings(item, _depth + 1)
    elif hasattr(obj, "__dict__") and not isinstance(obj, type):
        yield from all_strings(vars(obj), _depth + 1)


def flatten_text(obj) -> str:
    return "\n".join(all_strings(obj))


# --------------------------------------------------------------------------- projection


@dataclass
class Outcome:
    """Normalised projection result. `messages is None` means fail-closed decline."""

    raw: Any

    @property
    def messages(self):
        raw = self.raw
        if raw is None:
            return None
        if isinstance(raw, list):
            return raw
        inner = _res_get(raw, "messages", "projected", "result", default=None)
        if inner is None:
            return None
        return list(inner)

    @property
    def errors(self) -> list[str]:
        return res_errors(self.raw)

    @property
    def declined(self) -> bool:
        return self.messages is None

    @property
    def text(self) -> str:
        return flatten_text(self.raw)


def projection_fn():
    mod = module(PROJECTION_MOD)
    for name in PROJECTION_CANDIDATES:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    raise AssertionError(
        f"{PROJECTION_MOD} exposes none of {PROJECTION_CANDIDATES}: the projection "
        "entry point named in docs/architecture.md is missing"
    )


def project(store, request_messages, conversation_messages=None, *, incoming_message=None):
    if conversation_messages is None:
        conversation_messages = request_messages
    snap = snapshot(store)
    values = {
        "snapshot": snap,
        "state": snap,
        "store": store,
        "request_messages": list(request_messages),
        "request": list(request_messages),
        "messages": list(request_messages),
        "conversation_messages": list(conversation_messages),
        "conversation": list(conversation_messages),
        "history": list(conversation_messages),
        "incoming_message": incoming_message
        if incoming_message is not None
        else (list(conversation_messages)[-1] if conversation_messages else None),
        "operations": snap_ops(snap),
        "pins": snap_pins(snap),
        "revision": snap_revision(snap),
        "session_id": _res_get(store, "session_id", default=None),
    }
    fn = projection_fn()
    return Outcome(fn(**_bind(fn, values, what="projection")))


# --------------------------------------------------------------------------- misc helpers


def roles(messages) -> list[str]:
    return [str(m.get("role")) for m in (messages or []) if isinstance(m, Mapping)]


def content_of(message) -> str:
    if not isinstance(message, Mapping):
        return str(message)
    value = message.get("content")
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, sort_keys=True)


def find_message(messages, needle):
    for index, message in enumerate(messages or []):
        if needle in content_of(message):
            return index, message
    return None, None


GAP_MARKER_RE = re.compile(
    r"(tombstone|corrupt|unreadable|unavailable|integrity|checksum|missing span|gap)",
    re.IGNORECASE,
)


def is_gap_marker(text: str) -> bool:
    return bool(GAP_MARKER_RE.search(text or ""))


def sqlite_connect(path):
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    return conn


def tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows]


def columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def find_payload_column(conn):
    pattern = re.compile(r"(content|payload|message|body|text|json)", re.IGNORECASE)
    for table in tables(conn):
        for column in columns(conn, table):
            if pattern.search(column):
                count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL '
                    f'AND length("{column}") > 0'
                ).fetchone()[0]
                if count:
                    yield table, column


def quick_check(path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    finally:
        conn.close()
