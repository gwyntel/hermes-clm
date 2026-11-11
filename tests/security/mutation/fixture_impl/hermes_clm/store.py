"""Harness fixture implementation of the CLM store contract.

NOT PRODUCTION CODE. It exists only so the mutation harness can run the real security
tests against a correct implementation (green) and against single-defect mutants (red).
It implements the subset of docs/integration-contract.md that the probe tests use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import _defect as D
from .models import (
    CLOSED_OP_KINDS,
    MUTATING_KINDS,
    ArchivedSpan,
    EditOperation,
    Proposal,
    StoreSnapshot,
)

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ROLES = {"system", "user", "assistant", "tool"}
SECRET_PATTERNS = [
    re.compile(r"sk-ant-api03-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+eyJ[A-Za-z0-9_\-\.]{10,}"),
    re.compile(r"postgres://[^\s:@/]+:[^\s@]+@[^\s]+"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS spans(
  span_id TEXT PRIMARY KEY, seq INTEGER, role TEXT, origin TEXT, protected INTEGER,
  content TEXT, content_sha256 TEXT, group_id TEXT);
CREATE TABLE IF NOT EXISTS pins(
  pin_id TEXT PRIMARY KEY, span_id TEXT, revision INTEGER, content_sha256 TEXT,
  actor_origin TEXT);
CREATE TABLE IF NOT EXISTS staged(
  txn_id TEXT PRIMARY KEY, expected_revision INTEGER, payload TEXT, actor_origin TEXT);
CREATE TABLE IF NOT EXISTS outcomes(
  txn_id TEXT PRIMARY KEY, revision INTEGER, applied TEXT);
CREATE TABLE IF NOT EXISTS committed_ops(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, txn_id TEXT, revision INTEGER, kind TEXT,
  span_ids TEXT, actor_origin TEXT);
"""

TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS spans_immutable_update BEFORE UPDATE ON spans
WHEN OLD.protected = 1 OR EXISTS (SELECT 1 FROM pins WHERE pins.span_id = OLD.span_id)
BEGIN SELECT RAISE(ABORT, 'archive row is immutable'); END;
CREATE TRIGGER IF NOT EXISTS spans_immutable_delete BEFORE DELETE ON spans
WHEN OLD.protected = 1 OR EXISTS (SELECT 1 FROM pins WHERE pins.span_id = OLD.span_id)
BEGIN SELECT RAISE(ABORT, 'archive row is immutable'); END;
"""


class MountError(RuntimeError):
    pass


@dataclass
class _Result:
    ok: bool
    revision: int = 0
    errors: list[str] | None = None
    applied: list[str] | None = None
    inert: list[str] | None = None
    replayed: bool = False

    def __post_init__(self):
        self.errors = list(self.errors or [])
        self.applied = list(self.applied or [])
        self.inert = list(self.inert or [])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def redact(text: str) -> str:
    if D.active("M48"):
        return text
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _redact_value(value):
    """Redact string leaves before serialisation, so the stored JSON stays valid."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes):
        return redact(value.decode("utf-8", "replace"))
    if isinstance(value, Mapping):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


class ContextStore:
    def __init__(self, hermes_home: Path, session_id: str, conn: sqlite3.Connection):
        self.hermes_home = Path(hermes_home)
        self.session_id = session_id
        self._conn = conn

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def for_session(cls, hermes_home, session_id, *, read_only: bool = False):
        home = Path(hermes_home)
        valid = isinstance(session_id, str) and bool(SESSION_ID_RE.match(session_id))
        if not valid and not D.active("M1"):
            raise ValueError(f"invalid session id {session_id!r}")
        base = home / "clm" / "sessions"
        base.mkdir(parents=True, exist_ok=True)
        _tighten_dir(base)
        _tighten_dir(home / "clm")
        target = base / str(session_id)
        if not D.active("M1"):
            if target.is_symlink():
                raise MountError(f"session directory is a symlink: {target}")
        target.mkdir(parents=True, exist_ok=True)
        if not D.active("M41"):
            _tighten_dir(target, strict=True)

        db = target / "store.sqlite3"
        if not D.active("M1") and not D.active("M3"):
            if db.is_symlink():
                raise MountError(f"database path is a symlink: {db}")
        fresh = (not db.exists()) or db.stat().st_size == 0
        conn = sqlite3.connect(str(db), timeout=10, isolation_level=None)
        if fresh:
            conn.executescript(SCHEMA)
            if not D.active("M25"):
                conn.executescript(TRIGGERS)
        else:
            present = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = {"meta", "spans", "pins", "staged", "outcomes", "committed_ops"} - present
            if missing:
                conn.close()
                raise MountError(f"store structure missing tables: {sorted(missing)}")
        existing = conn.execute("SELECT value FROM meta WHERE key='session_id'").fetchone()
        if existing is None:
            conn.execute("INSERT INTO meta(key,value) VALUES('session_id',?)", (session_id,))
            conn.execute("INSERT INTO meta(key,value) VALUES('db_id',?)", (_sha(str(target)),))
            conn.execute("INSERT INTO meta(key,value) VALUES('revision','0')")
            conn.execute("INSERT INTO meta(key,value) VALUES('tick','0')")
        elif existing[0] != session_id and not D.active("M3"):
            conn.close()
            raise MountError(f"db belongs to session {existing[0]!r}, not {session_id!r}")
        store = cls(home, session_id, conn)
        if not D.active("M41"):
            for path in (db, Path(str(db) + "-journal")):
                if path.exists():
                    os.chmod(path, 0o600)
        store._write_index()
        return store

    def close(self):
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ---------------------------------------------------------------- helpers
    def _revision(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        return int(row[0]) if row else 0

    def _set_revision(self, value: int):
        self._conn.execute("UPDATE meta SET value=? WHERE key='revision'", (str(value),))

    def _write_index(self):
        index = self.hermes_home / "clm" / "sessions" / self.session_id / "active.json"
        rows = {}
        for span_id, digest in self._conn.execute(
            "SELECT span_id, content_sha256 FROM spans"
        ).fetchall():
            rows[span_id] = digest
        payload = {
            "db_id": self._conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0],
            "session_id": self.session_id,
            "revision": self._revision(),
            "rows": rows,
        }
        tmp = index.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, index)
        if not D.active("M41"):
            os.chmod(index, 0o600)

    def _pin_for(self, span_id: str):
        row = self._conn.execute(
            "SELECT pin_id FROM pins WHERE span_id=? ORDER BY revision DESC LIMIT 1",
            (span_id,),
        ).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- contract
    def ingest(self, messages: Sequence[Mapping[str, Any]]):
        revision = self._revision()
        for message in messages:
            role = message.get("role")
            if role not in ROLES:
                if D.active("M9"):
                    role = "user"
                else:
                    raise ValueError(f"unknown role {role!r}")
            content = json.dumps(_redact_value(dict(message)), sort_keys=True)
            digest = _sha(content)
            seq = self._conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
            span_id = "sp-" + _sha(f"{self.session_id}:{digest}:{seq}")[:24]
            origin = {
                "system": "system",
                "user": "operator",
                "assistant": "model",
                "tool": "tool",
            }[role]
            self._conn.execute(
                "INSERT INTO spans(span_id,seq,role,origin,protected,content,content_sha256,group_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    span_id,
                    seq,
                    role,
                    origin,
                    1 if role in ("system", "user") else 0,
                    content,
                    digest,
                    None,
                ),
            )
        self._set_revision(revision)
        return _Result(ok=True, revision=self._revision())

    def snapshot(self) -> StoreSnapshot:
        if D.active("M39"):
            self._conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='tick'"
            )
        spans = {}
        for row in self._conn.execute(
            "SELECT span_id, role, origin, protected, content, content_sha256 FROM spans ORDER BY seq"
        ):
            span_id, role, origin, protected, content, digest = row
            spans[span_id] = ArchivedSpan(
                span_id=span_id,
                uid=span_id,
                aliases=(),
                message=json.loads(content),
                group_id=None,
                origin=origin,
                protected=bool(protected),
                content_sha256=digest,
            )
        ops = tuple(
            {
                "kind": kind,
                "span_ids": tuple(json.loads(span_ids)),
                "actor_origin": actor_origin,
                "revision": revision,
                "txn_id": txn_id,
            }
            for txn_id, revision, kind, span_ids, actor_origin in self._conn.execute(
                "SELECT txn_id, revision, kind, span_ids, actor_origin FROM committed_ops ORDER BY seq"
            )
        )
        pins = {
            pin_id: {
                "pin_id": pin_id,
                "span_id": span_id,
                "revision": revision,
                "content_sha256": digest,
                "actor_origin": actor,
            }
            for pin_id, span_id, revision, digest, actor in self._conn.execute(
                "SELECT pin_id, span_id, revision, content_sha256, actor_origin FROM pins"
            )
        }
        head = (
            _sha(json.dumps([[op["kind"], list(op["span_ids"]), op["revision"]] for op in ops]))
            if ops
            else ""
        )
        return StoreSnapshot(
            revision=self._revision(), operations=ops, spans=spans, pins=pins, head_hash=head
        )

    def stage(
        self, operations, *, expected_revision: int, txn_id: str, actor_origin: str = "model"
    ):
        ops = []
        for op in operations:
            if isinstance(op, Mapping):
                op = EditOperation(
                    **{
                        k: op.get(k)
                        for k in ("kind", "span_ids", "replacement", "actor_origin")
                        if op.get(k) is not None
                    }
                )
            kind = getattr(op, "kind", None)
            if kind not in CLOSED_OP_KINDS:
                return _Result(
                    ok=False, errors=[f"unknown operation kind {kind!r}"], revision=self._revision()
                )
            span_ids = list(getattr(op, "span_ids", ()) or ())
            known = {row[0] for row in self._conn.execute("SELECT span_id FROM spans")}
            missing = [sid for sid in span_ids if sid not in known]
            if missing:
                return _Result(
                    ok=False, errors=[f"unknown span ids {missing}"], revision=self._revision()
                )
            if not D.active("M17") and kind in MUTATING_KINDS:
                blocked = [(sid, self._pin_for(sid)) for sid in span_ids if self._pin_for(sid)]
                if blocked:
                    return _Result(
                        ok=False,
                        errors=[f"blocked by pin {pin} on span {sid}" for sid, pin in blocked],
                        revision=self._revision(),
                    )
            ops.append(
                {
                    "kind": kind,
                    "span_ids": span_ids,
                    "replacement": getattr(op, "replacement", None),
                    "actor_origin": getattr(op, "actor_origin", actor_origin),
                }
            )
        self._conn.execute(
            "INSERT OR REPLACE INTO staged(txn_id, expected_revision, payload, actor_origin)"
            " VALUES(?,?,?,?)",
            (txn_id, expected_revision, json.dumps(ops), actor_origin),
        )
        return _Result(ok=True, revision=self._revision())

    def commit(self, *, txn_id: str, expected_revision: int):
        if not D.active("M21"):
            row = self._conn.execute(
                "SELECT revision, applied FROM outcomes WHERE txn_id=?", (txn_id,)
            ).fetchone()
            if row:
                return _Result(ok=True, revision=row[0], applied=json.loads(row[1]), replayed=True)
        row = self._conn.execute(
            "SELECT expected_revision, payload FROM staged WHERE txn_id=?", (txn_id,)
        ).fetchone()
        if row is None:
            return _Result(
                ok=False, errors=[f"unknown transaction {txn_id!r}"], revision=self._revision()
            )
        staged_revision, payload = row
        current = self._revision()
        if not D.active("M20") and current != expected_revision:
            return _Result(
                ok=False,
                errors=[f"stale revision: expected {expected_revision}, head {current}"],
                revision=current,
            )
        ops = json.loads(payload)
        applied = []
        next_revision = current + 1
        for op in ops:
            kind = op["kind"]
            if kind == "drop":
                for span_id in op["span_ids"]:
                    self._conn.execute(
                        "DELETE FROM spans WHERE span_id=? AND protected=0", (span_id,)
                    )
                    applied.append(span_id)
            elif kind == "pin":
                for span_id in op["span_ids"]:
                    content = self._conn.execute(
                        "SELECT content_sha256 FROM spans WHERE span_id=?", (span_id,)
                    ).fetchone()[0]
                    pin_id = (
                        "pin-" + _sha(f"{self.session_id}:{next_revision}:{span_id}:{content}")[:24]
                    )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO pins(pin_id, span_id, revision, content_sha256, actor_origin)"
                        " VALUES(?,?,?,?,?)",
                        (pin_id, span_id, next_revision, content, op.get("actor_origin", "model")),
                    )
                    applied.append(pin_id)
            elif kind == "rewrite":
                for span_id in op["span_ids"]:
                    replacement = op.get("replacement")
                    text = replacement if isinstance(replacement, str) else json.dumps(replacement)
                    self._conn.execute(
                        "UPDATE spans SET content=?, content_sha256=? WHERE span_id=?",
                        (json.dumps({"role": "assistant", "content": text}), _sha(text), span_id),
                    )
                    applied.append(span_id)
            elif kind in ("keep", "boundary", "offload", "restore"):
                applied.extend(op["span_ids"])
            self._conn.execute(
                "INSERT INTO committed_ops(txn_id, revision, kind, span_ids, actor_origin)"
                " VALUES(?,?,?,?,?)",
                (
                    txn_id,
                    next_revision,
                    kind,
                    json.dumps(op["span_ids"]),
                    op.get("actor_origin", "model"),
                ),
            )
        self._set_revision(next_revision)
        self._conn.execute("DELETE FROM staged WHERE txn_id=?", (txn_id,))
        self._conn.execute(
            "INSERT OR REPLACE INTO outcomes(txn_id, revision, applied) VALUES(?,?,?)",
            (txn_id, next_revision, json.dumps(applied)),
        )
        self._write_index()
        return _Result(ok=True, revision=next_revision, applied=applied)

    def discard(self, *, txn_id: str):
        self._conn.execute("DELETE FROM staged WHERE txn_id=?", (txn_id,))
        return _Result(ok=True, revision=self._revision())

    def revert(
        self,
        *,
        target_revision: int,
        expected_revision: int,
        txn_id: str,
        actor_origin: str = "model",
    ):
        if self._revision() != expected_revision:
            return _Result(ok=False, errors=["stale revision"], revision=self._revision())
        return self.commit(txn_id=txn_id, expected_revision=expected_revision)

    # ---------------------------------------------------------------- read side
    def status(self):
        return {
            "revision": self._revision(),
            "session_id": self.session_id,
            "spans": self._conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0],
            "pins": self._conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0],
            "integrity": "ok",
            "chain": "unverified",
        }

    def inspect(self, *, cursor: str | None = None, limit: int = 50):
        rows = self._conn.execute(
            "SELECT span_id, role, origin, protected, content FROM spans ORDER BY seq LIMIT ?",
            (limit,),
        ).fetchall()
        return {
            "revision": self._revision(),
            "spans": [
                {
                    "span_id": span_id,
                    "role": role,
                    "origin": origin,
                    "protected": bool(protected),
                    "content": content,
                }
                for span_id, role, origin, protected, content in rows
            ],
        }

    def search(self, query: str, *, cursor: str | None = None, limit: int = 20):
        rows = self._conn.execute(
            "SELECT span_id, content FROM spans WHERE content LIKE ? LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return {"query": query, "matches": [{"span_id": r[0], "content": r[1]} for r in rows]}

    def retrieve(self, span_id: str):
        row = self._conn.execute(
            "SELECT span_id, content, origin, content_sha256 FROM spans WHERE span_id=?",
            (span_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown span {span_id!r}")
        return {
            "span_id": row[0],
            "content": row[1],
            "origin": row[2],
            "content_sha256": row[3],
        }

    def create_proposal(self, operations, *, base_revision: int):
        ops = [op for op in operations]
        material = json.dumps(
            [
                {
                    "kind": getattr(o, "kind", None),
                    "span_ids": list(getattr(o, "span_ids", ()) or ()),
                }
                for o in ops
            ],
            sort_keys=True,
        )
        digest = _sha(f"{self.session_id}:{base_revision}:{material}")
        return Proposal(
            proposal_id="prop-" + digest[:24],
            proposal_sha256=digest,
            base_revision=base_revision,
            operations=tuple(
                o if isinstance(o, EditOperation) else EditOperation(**_op_kwargs(o)) for o in ops
            ),
            origin="aux",
        )

    def accept_proposal(self, *, proposal_id, proposal_sha256, base_revision, txn_id):
        if self._revision() != base_revision:
            return _Result(ok=False, errors=["stale base revision"], revision=self._revision())
        return _Result(ok=True, revision=self._revision())


def _op_kwargs(op):
    if isinstance(op, Mapping):
        return {
            "kind": op["kind"],
            "span_ids": tuple(op.get("span_ids") or ()),
            "replacement": op.get("replacement"),
            "actor_origin": op.get("actor_origin", "aux"),
        }
    return {
        "kind": getattr(op, "kind"),
        "span_ids": tuple(getattr(op, "span_ids", ()) or ()),
        "replacement": getattr(op, "replacement", None),
        "actor_origin": getattr(op, "actor_origin", "aux"),
    }


def _tighten_dir(path: Path, *, strict: bool = False):
    try:
        os.chmod(path, 0o700)
    except OSError:
        if strict:
            raise
