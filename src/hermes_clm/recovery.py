"""Crash, interruption and corruption recovery for session stores.

Recovery is **derived-state reconstruction**, never payload rewriting:

* SQLite's WAL + ``BEGIN IMMEDIATE`` already guarantee that an interrupted
  transaction leaves no partial commit.  Recovery therefore verifies rather
  than repairs: payload tables are append-only and triggers refuse fixes.
* ``span_state`` is a rebuildable checkpoint of the committed operation chain;
  a divergence is recomputed, not patched by hand.
* ``active.json`` is a derived index; it is rewritten from SQLite, which stays
  authoritative.
* Stale staged transactions (crash between ``stage`` and ``commit``) expire
  after ``stale_txn_ttl_seconds`` so a later replay cannot resurrect them.
* Payload/revision hash mismatches are reported, not "fixed": callers must
  treat the store as untrustworthy (projection returns ``None``) instead of
  receiving a guessed subset.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import schema
from .errors import CLMError, CorruptStoreError, StoreClosedError
from .store import (
    DB_FILENAME,
    INDEX_FILENAME,
    ContextStore,
    StoreConfig,
    _assert_plain,
    _coerce_config,
    _harden_file,
    _validate_session_id,
    session_dir,
    session_key,
)

__all__ = [
    "RecoveryReport",
    "recover",
    "recover_session",
    "verify_session",
    "rebuild_index",
]


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    ok: bool
    session_id: str
    store_key: str
    head_revision: int = 0
    actions: tuple[str, ...] = ()
    problems: tuple[Mapping[str, Any], ...] = ()
    corrupt_spans: tuple[str, ...] = ()
    index_state: str = "unknown"
    discarded_txns: tuple[str, ...] = ()
    schema_repaired: bool = False
    state_rebuilt: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "store_key": self.store_key,
            "head_revision": self.head_revision,
            "actions": list(self.actions),
            "problems": [dict(p) for p in self.problems],
            "corrupt_spans": list(self.corrupt_spans),
            "index_state": self.index_state,
            "discarded_txns": list(self.discarded_txns),
            "schema_repaired": self.schema_repaired,
            "state_rebuilt": self.state_rebuilt,
        }


def _now_iso(offset_seconds: int = 0) -> str:
    stamp = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def recover(store: ContextStore) -> RecoveryReport:
    """Run every recovery step against an open, writable store."""

    if store.closed:
        raise StoreClosedError("store handle is closed", code="store_closed")
    if store.read_only:
        raise CLMError("recovery requires a writable store handle", code="read_only_store")
    actions: list[str] = []
    problems: list[dict[str, Any]] = []
    corrupt_spans: list[str] = []

    # 1. SQLite structural check (page-level corruption, truncated file).
    cur = store._conn.cursor()
    try:
        integrity = cur.execute("PRAGMA integrity_check").fetchone()
    finally:
        cur.close()
    if integrity is None or str(integrity[0]) != "ok":
        problems.append(
            {
                "code": "integrity_check_failed",
                "detail": None if integrity is None else str(integrity[0]),
            }
        )

    # 2. Payload hashes, archive chain and revision chain.
    verification = store.verify()
    problems.extend(verification["problems"])
    corrupt_spans.extend(verification["corrupt_spans"])
    if verification["ok"]:
        actions.append("verified_hash_chains")

    # 3. Derived checkpoint repair: span_state must equal the replayed chain.
    consistency = store._verify_state_consistency_from_scratch()
    if consistency:
        rebuilt = store.rebuild_state()
        actions.append("rebuilt_span_state")
        problems.extend(consistency[:10])
        state_rebuilt = int(rebuilt["changed"])
    else:
        actions.append("state_consistent")
        state_rebuilt = 0

    # 4. Expire staged transactions orphaned by a crash.
    discarded = store.expire_staged_transactions()
    if discarded:
        actions.append("expired_staged_transactions")

    # 5. Drop temp files left by an interrupted index write.
    for stale_tmp in store.directory.glob(f".{INDEX_FILENAME}.*.tmp"):
        try:
            stale_tmp.unlink()
            actions.append("removed_stale_index_tmp")
        except OSError:
            problems.append({"code": "index_tmp_not_removed", "path": stale_tmp.name})

    # 6. Rebuild the derived index (authoritative state is SQLite).
    index = store.rebuild_index()
    actions.append("rebuilt_index" if index.get("ok") else "index_rebuild_failed")
    if not index.get("ok"):
        problems.append({"code": "index_rebuild_failed", "detail": store.index_error})

    ok = not problems
    return RecoveryReport(
        ok=ok,
        session_id=store.session_id,
        store_key=store.directory.name,
        head_revision=store._revision_read(),
        actions=tuple(actions),
        problems=tuple(problems),
        corrupt_spans=tuple(corrupt_spans),
        index_state=str(index.get("state", "unknown")),
        discarded_txns=tuple(discarded),
        schema_repaired=False,
        state_rebuilt=state_rebuilt,
    )


def _schema_is_incomplete(db_path: Path) -> bool:
    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=1.0)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return bool(set(schema.TABLES) - tables)
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def _repair_schema(db_path: Path) -> bool:
    """Create missing tables/indexes/triggers without touching existing rows."""

    conn = sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            schema.create_schema(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def recover_session(
    hermes_home: str | os.PathLike[str],
    session_id: str,
    *,
    config: StoreConfig | Mapping[str, Any] | None = None,
) -> RecoveryReport:
    """Open (repairing schema if interrupted) and recover one session store."""

    cfg = _coerce_config(config)
    sid = _validate_session_id(session_id)
    directory = session_dir(hermes_home, sid)
    if not directory.exists():
        raise CorruptStoreError(
            "no store for this session", code="store_not_found", details={"store_key": session_key(sid)}
        )
    _assert_plain(directory, kind="directory")
    db_path = directory / DB_FILENAME
    schema_repaired = False
    try:
        store = ContextStore.for_session(hermes_home, sid, config=cfg)
    except CorruptStoreError as exc:
        if exc.code not in {"schema_missing", "schema_incomplete"}:
            raise
        _repair_schema(db_path)
        schema_repaired = True
        store = ContextStore.for_session(hermes_home, sid, config=cfg)
    with store:
        report = recover(store)
    _harden_file(directory / INDEX_FILENAME)
    if schema_repaired:
        return RecoveryReport(
            ok=report.ok,
            session_id=report.session_id,
            store_key=report.store_key,
            head_revision=report.head_revision,
            actions=("schema_repaired",) + report.actions,
            problems=report.problems,
            corrupt_spans=report.corrupt_spans,
            index_state=report.index_state,
            discarded_txns=report.discarded_txns,
            schema_repaired=True,
            state_rebuilt=report.state_rebuilt,
        )
    return report


def verify_session(
    hermes_home: str | os.PathLike[str],
    session_id: str,
    *,
    config: StoreConfig | Mapping[str, Any] | None = None,
    read_only: bool = True,
) -> dict[str, Any]:
    """Read-only verification entry point for diagnostics/CLI."""

    cfg = _coerce_config(config)
    with ContextStore.for_session(hermes_home, session_id, read_only=read_only, config=cfg) as store:
        report = store.verify()
        report["index"] = store.index_status()
        return report


def rebuild_index(
    hermes_home: str | os.PathLike[str],
    session_id: str,
    *,
    config: StoreConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rewrite ``active.json`` from authoritative SQLite state."""

    cfg = _coerce_config(config)
    with ContextStore.for_session(hermes_home, session_id, config=cfg) as store:
        return store.rebuild_index()
