"""Transactional, per-session context store for Hermes CLM.

One SQLite database per session under ``<hermes_home>/clm/sessions/<key>/``:

* ``session.db``   authoritative state (append-only payload tables, WAL)
* ``store.lock``   sibling ``flock`` file enforcing a single writer
* ``active.json``  **derived** index only, never authoritative

Invariants implemented here (see ``docs/store-format.md`` for the full spec):

1. Derived paths are sanitized + hashed, containment-checked, symlink-refused;
   directories are ``0700`` and files ``0600``.
2. Ingestion redacts before archival, dedupes ``uid``/content versions and
   never aliases caller objects into the archive.
3. Every mutation is one ``BEGIN IMMEDIATE`` transaction covering operation
   append, state change, revision bump, idempotency outcome, audit and token
   accounting.  Triggers abort ``UPDATE``/``DELETE`` on payload tables.
4. Compare-and-swap on ``expected_revision``; ``txn_id`` replay returns the
   original outcome; stale replay with a different payload is refused.
5. Revisions are monotonic.  Revert writes a new head whose state root is an
   earlier revision; nothing is truncated.
6. Agent may pin (one-way); only the operator may unpin.
7. Content hashes and a revision hash chain detect corruption/interruption.
   Recovery rebuilds derived state instead of guessing.
"""

from __future__ import annotations

import base64
import copy
import errno
import fcntl
import json
import os
import re
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from . import schema
from .archive import GENESIS_HASH, RedactionRule, Redactor, chain_entry_hash
from .errors import (
    CLMError,
    ConflictError,
    CorruptStoreError,
    IdempotencyConflictError,
    NotFoundError,
    PathSafetyError,
    PinAuthorityError,
    ProtectedSpanError,
    ReadOnlyStoreError,
    StaleProposalError,
    StaleRevisionError,
    StoreBudgetError,
    StoreBusyError,
    StoreClosedError,
    ValidationError,
)
from .models import (
    PROTECTED_ROLES,
    ActorOrigin,
    ArchivedSpan,
    CommitResult,
    EditKind,
    EditOperation,
    IngestResult,
    PinRecord,
    Proposal,
    Receipt,
    SpanOrigin,
    SpanState,
    StageResult,
    StoreSnapshot,
    canonical_json,
    canonical_sha256,
    coerce_operations,
    origin_for_role,
    sha256_hex,
    validate_span_id,
    validate_txn_id,
)

__all__ = [
    "ContextStore",
    "StoreConfig",
    "STORE_DIRNAME",
    "SESSIONS_DIRNAME",
    "DB_FILENAME",
    "LOCK_FILENAME",
    "INDEX_FILENAME",
    "session_key",
    "sessions_root",
    "session_dir",
]

STORE_DIRNAME = "clm"
SESSIONS_DIRNAME = "sessions"
DB_FILENAME = "session.db"
LOCK_FILENAME = "store.lock"
INDEX_FILENAME = "active.json"
INDEX_FORMAT_VERSION = 1
TOKEN_ESTIMATOR = "chars/4"

_SESSION_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SESSION_ID = 256


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class StoreConfig:
    """Explicit store limits.

    Injected by the engine/tools layer (which owns Hermes config parsing) so
    that this module never reads ``clm.*`` itself and never imports Hermes.
    """

    max_retrieve_bytes: int = 65_536
    max_rewrite_bytes: int = 16_384
    max_aux_spans: int = 32
    max_operations_per_txn: int = 128
    max_operations_in_snapshot: int = 500
    max_query_chars: int = 512
    inspect_limit_max: int = 200
    search_limit_max: int = 100
    archive_redact: bool = True
    retention_sessions: int = 32
    busy_timeout_ms: int = 5_000
    stale_txn_ttl_seconds: int = 3_600
    assume_full_history: bool = True
    snapshot_verify_hashes: bool = True
    clock: Callable[[], str] | None = field(default=None, compare=False)
    extra_redactions: tuple[RedactionRule, ...] = ()


def _coerce_config(config: StoreConfig | Mapping[str, Any] | None) -> StoreConfig:
    if config is None:
        return StoreConfig()
    if isinstance(config, StoreConfig):
        return config
    if isinstance(config, Mapping):
        known = {f for f in StoreConfig.__dataclass_fields__}
        unknown = set(config) - known
        if unknown:
            raise ValidationError(
                f"unknown store config key(s): {sorted(unknown)}",
                code="unknown_config",
                details={"fields": sorted(unknown)},
            )
        return StoreConfig(**dict(config))
    raise ValidationError("config must be StoreConfig, mapping or None", code="invalid_config")


# ---------------------------------------------------------------------------
# path derivation
# ---------------------------------------------------------------------------


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str):
        raise ValidationError("session_id must be a string", code="invalid_session_id")
    if not session_id:
        raise ValidationError("session_id must not be empty", code="invalid_session_id")
    if len(session_id) > _MAX_SESSION_ID:
        raise ValidationError(
            f"session_id longer than {_MAX_SESSION_ID} characters", code="invalid_session_id"
        )
    if "\x00" in session_id:
        raise ValidationError("session_id must not contain NUL", code="invalid_session_id")
    return session_id


def session_key(session_id: str) -> str:
    """Derive a filesystem-safe, collision-resistant directory key.

    The slug keeps readable context; the digest makes collisions and traversal
    impossible even for adversarial session ids (``../../etc``, unicode
    lookalikes, 4096-char ids).
    """

    sid = _validate_session_id(session_id)
    slug = _SESSION_SLUG_RE.sub("-", sid).strip(".-")[:_MAX_SESSION_ID]
    slug = slug.replace("..", "-")[:48].strip(".-") or "session"
    digest = sha256_hex("clm-session\x00" + sid)[:16]
    return f"{slug}-{digest}"


def sessions_root(hermes_home: str | os.PathLike[str]) -> Path:
    home = Path(hermes_home).expanduser()
    if not str(home):
        raise ValidationError("hermes_home is required", code="invalid_home")
    return home / STORE_DIRNAME / SESSIONS_DIRNAME


def session_dir(hermes_home: str | os.PathLike[str], session_id: str) -> Path:
    return sessions_root(hermes_home) / session_key(session_id)


def _assert_contained(root: Path, path: Path) -> None:
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    if os.path.commonpath([root_real, path_real]) != root_real:
        raise PathSafetyError(
            "derived store path escaped its root", code="path_escape", details={"path": path.name}
        )


def _assert_plain(path: Path, *, kind: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise NotFoundError(
            f"{kind} does not exist", code="not_found", details={"path": path.name}
        ) from None
    if stat.S_ISLNK(st.st_mode):
        raise PathSafetyError(
            f"{kind} is a symlink", code="symlink_refused", details={"path": path.name}
        )
    if kind == "directory" and not stat.S_ISDIR(st.st_mode):
        raise PathSafetyError(
            f"{kind} is not a directory", code="not_a_directory", details={"path": path.name}
        )
    if kind == "file" and not stat.S_ISREG(st.st_mode):
        raise PathSafetyError(
            f"{kind} is not a regular file", code="not_a_file", details={"path": path.name}
        )


def _ensure_private_dir(path: Path, *, root: Path) -> None:
    """Create ``path`` (and ancestors up to ``root``) with ``0700`` perms."""

    if path.is_symlink():
        raise PathSafetyError(
            "store directory is a symlink", code="symlink_refused", details={"path": path.name}
        )
    _assert_contained(root, path)
    if path.exists():
        _assert_plain(path, kind="directory")
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        if mode != 0o700:
            os.chmod(path, 0o700)
        return
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    _assert_plain(path, kind="directory")


def _assert_single_link(path: Path) -> None:
    """Refuse a store file with another hard link: its bytes are not private."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise PathSafetyError(
            "store file has multiple hard links",
            code="hardlink_refused",
            details={"path": path.name, "nlink": st.st_nlink},
        )


def _harden_file(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise PathSafetyError(
            "store file is a symlink", code="symlink_refused", details={"path": path.name}
        )
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise PathSafetyError(
            "store file has multiple hard links",
            code="hardlink_refused",
            details={"path": path.name, "nlink": st.st_nlink},
        )
    if stat.S_IMODE(st.st_mode) != 0o600:
        os.chmod(path, 0o600)


def _acquire_writer_lock(lock_path: Path) -> int:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    _harden_file(lock_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise StoreBusyError(
                "another writer holds this session store",
                code="store_busy",
                details={"lock": LOCK_FILENAME},
            ) from exc
        raise
    os.write(fd, f"{os.getpid()}\n".encode()[:32])
    return fd


def _connect(db_path: Path, *, read_only: bool, config: StoreConfig) -> sqlite3.Connection:
    timeout = max(0.0, config.busy_timeout_ms / 1000.0)
    old_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(
            str(db_path),
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
    finally:
        os.umask(old_umask)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(config.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA wal_autocheckpoint = 256")
    return conn


# ---------------------------------------------------------------------------
# internal planning structures
# ---------------------------------------------------------------------------


@dataclass
class _Planned:
    op: EditOperation
    span: ArchivedSpan | None
    effect: str  # applied | inert
    reason: str = ""

    @property
    def receipt(self) -> Receipt:
        return Receipt(
            span_id=self.span.span_id if self.span else "",
            kind=self.op.kind.value,
            effect=self.effect,
            reason=self.reason,
        )


_STATE_CHANGING_KINDS = frozenset(
    {EditKind.KEEP, EditKind.DROP, EditKind.OFFLOAD, EditKind.RESTORE}
)
_REFUSE_PROTECTED_KINDS = frozenset({EditKind.DROP, EditKind.OFFLOAD, EditKind.REWRITE})


def _tokens_for_text(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _op_payload_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": row["kind"],
        "span_ids": json.loads(row["span_ids_json"]),
        "actor_origin": row["actor_origin"],
        "provenance": json.loads(row["provenance_json"]),
    }
    if row["replacement"] is not None:
        payload["replacement"] = row["replacement"]
    if row["txn_id"] is not None:
        payload["txn_id"] = row["txn_id"]
    return payload


def _hash_op_payloads(ops: Iterable[EditOperation], txn_id: str | None) -> list[dict[str, Any]]:
    """Operation payloads exactly as they are persisted (used for hashing)."""

    payloads: list[dict[str, Any]] = []
    for op in ops:
        payload: dict[str, Any] = {
            "kind": op.kind.value,
            "span_ids": list(op.span_ids),
            "actor_origin": op.actor_origin.value,
            "provenance": dict(op.provenance),
        }
        if op.replacement is not None:
            payload["replacement"] = op.replacement
        if txn_id is not None:
            payload["txn_id"] = txn_id
        payloads.append(payload)
    return payloads


def _coerce_aux_proposal(
    operations: Iterable[Any],
) -> tuple[tuple[EditOperation, ...], list[dict[str, Any]]]:
    """Normalize aux proposal operations while preserving their raw payloads.

    The digest must be computed over the caller's *raw* operation payloads
    (exactly what :func:`hermes_clm.auxiliary.canonical_proposal_sha256` sees),
    not over store-normalized payloads that add authority/provenance fields.
    Structural validation still runs on normalized :class:`EditOperation`
    values, and a missing ``actor_origin`` defaults to ``aux``.
    """

    if operations is None:
        raise ValidationError("operations are required", code="missing_operations")
    if isinstance(operations, str | bytes | Mapping):
        raise ValidationError("operations must be a sequence", code="invalid_operations")
    ops: list[EditOperation] = []
    raw_payload: list[dict[str, Any]] = []
    for item in operations:
        if isinstance(item, EditOperation):
            payload = item.to_payload()
            op = item
        elif isinstance(item, Mapping):
            payload = dict(item)
            candidate = dict(payload)
            candidate.setdefault("actor_origin", ActorOrigin.AUX.value)
            op = EditOperation.from_mapping(candidate)
        else:
            raise ValidationError(
                "operation must be a mapping or EditOperation", code="invalid_operation"
            )
        if op.actor_origin is not ActorOrigin.AUX:
            raise ValidationError(
                "operation actor_origin does not match the calling actor",
                code="actor_mismatch",
                details={"expected": ActorOrigin.AUX.value, "found": op.actor_origin.value},
            )
        ops.append(op)
        raw_payload.append(payload)
    if not ops:
        raise ValidationError("operations must not be empty", code="missing_operations")
    return tuple(ops), raw_payload


def _unpin_targets(span_ids: str | Sequence[str] | None, span_id: str | None) -> tuple[str, ...]:
    """Normalize ``unpin`` arguments to an ordered, de-duplicated tuple."""

    if span_ids is not None and span_id is not None:
        raise ValidationError("pass either span_id or span_ids, not both", code="invalid_span_id")
    if span_id is not None:
        raw: Any = span_id
    else:
        raw = span_ids
    if raw is None:
        raise ValidationError("span_id or span_ids is required", code="missing_span_ids")
    if isinstance(raw, str):
        items: Sequence[Any] = (raw,)
    elif isinstance(raw, Sequence) and not isinstance(raw, bytes | bytearray):
        items = raw
    else:
        raise ValidationError(
            "span_ids must be a string or a sequence of strings", code="invalid_span_ids"
        )
    if not items:
        raise ValidationError("span_ids must not be empty", code="missing_span_ids")
    out: list[str] = []
    for item in items:
        validate_span_id(item)
        if item not in out:
            out.append(item)
    return tuple(out)


def revision_hash(
    *,
    prev_hash: str,
    revision: int,
    parent_revision: int,
    base_revision: int,
    kind: str,
    actor_origin: str,
    txn_id: str | None,
    operations: Iterable[Mapping[str, Any]],
) -> str:
    """Deterministic link hash for one revision (chain detects tampering)."""

    return sha256_hex(
        canonical_json(
            {
                "prev": prev_hash,
                "revision": revision,
                "parent": parent_revision,
                "base": base_revision,
                "kind": kind,
                "actor": actor_origin,
                "txn": txn_id,
                "ops": list(operations),
            }
        )
    )


def _encode_cursor(after: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"v": 1, "after": int(after)}).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None or cursor == "":
        return -1
    if not isinstance(cursor, str) or len(cursor) > 256:
        raise ValidationError("invalid cursor", code="invalid_cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor.encode() + b"=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode())
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError("invalid cursor", code="invalid_cursor") from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ValidationError("invalid cursor", code="invalid_cursor")
    after = payload.get("after")
    if not isinstance(after, int) or after < -1:
        raise ValidationError("invalid cursor", code="invalid_cursor")
    return after


def _clamp_limit(limit: Any, *, maximum: int, default: int) -> int:
    if limit is None:
        return default
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValidationError("limit must be an int", code="invalid_limit")
    if limit < 1:
        raise ValidationError("limit must be >= 1", code="invalid_limit")
    return min(limit, maximum)


def _preview(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _is_multimodal(content: Any) -> bool:
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                continue
            if isinstance(part, Mapping) and part.get("type") in {"text", "input_text"}:
                continue
            return True
    return False


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class ContextStore:
    """Write-through, transactional archive for exactly one Hermes session."""

    def __init__(
        self,
        *,
        session_id: str,
        db_id: str,
        directory: Path,
        conn: sqlite3.Connection,
        lock_fd: int | None,
        read_only: bool,
        config: StoreConfig,
    ) -> None:
        self._session_id = session_id
        self._db_id = db_id
        self._dir = directory
        self._conn = conn
        self._lock_fd = lock_fd
        self._read_only = read_only
        self._config = config
        self._lock = threading.RLock()
        self._closed = False
        self._in_txn = False
        self._generation = 0
        self._snapshot_cache: StoreSnapshot | None = None
        self._snapshot_generation = -1
        self._redactor = Redactor(
            enabled=config.archive_redact, extra_rules=config.extra_redactions
        )
        self.index_state = "unknown"
        self.index_error: str | None = None

    # -- construction ----------------------------------------------------
    @classmethod
    def for_session(
        cls,
        hermes_home: str | os.PathLike[str],
        session_id: str,
        *,
        read_only: bool = False,
        config: StoreConfig | Mapping[str, Any] | None = None,
    ) -> "ContextStore":
        """Open (creating if needed) the store for ``session_id``.

        ``config`` is an additive keyword beyond the integration contract so
        tests and the engine can inject limits without touching Hermes config.
        """

        cfg = _coerce_config(config)
        sid = _validate_session_id(session_id)
        home = Path(hermes_home).expanduser()
        root = sessions_root(home)
        directory = session_dir(home, sid)
        if not read_only:
            _ensure_private_dir(home / STORE_DIRNAME, root=home)
            _ensure_private_dir(root, root=home / STORE_DIRNAME)
            _ensure_private_dir(directory, root=root)
        else:
            if not directory.exists():
                raise NotFoundError(
                    "no store for this session",
                    code="store_not_found",
                    details={"session": session_key(sid)},
                )
            _assert_plain(directory, kind="directory")
        _assert_contained(root, directory)
        db_path = directory / DB_FILENAME
        _assert_single_link(db_path)
        lock_fd: int | None = None
        conn: sqlite3.Connection | None = None
        try:
            if read_only:
                _assert_plain(db_path, kind="file")
            lock_fd = None if read_only else _acquire_writer_lock(directory / LOCK_FILENAME)
            try:
                conn = _connect(db_path, read_only=read_only, config=cfg)
            except sqlite3.DatabaseError as exc:
                raise CorruptStoreError(
                    "store file is not a readable SQLite database",
                    code="sqlite_open_failed",
                    details={"reason": str(exc)[:200]},
                ) from exc
            store = cls(
                session_id=sid,
                db_id="",
                directory=directory,
                conn=conn,
                lock_fd=lock_fd,
                read_only=read_only,
                config=cfg,
            )
            store._harden_files()
            try:
                if read_only:
                    schema.require_schema(conn)
                    store._db_id = store._read_meta("db_id") or ""
                    if not store._db_id:
                        raise CorruptStoreError("store has no db_id", code="schema_incomplete")
                else:
                    store._bootstrap()
            except sqlite3.DatabaseError as exc:
                if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                    raise
                raise CorruptStoreError(
                    "store file is not a readable SQLite database",
                    code="sqlite_open_failed",
                    details={"reason": str(exc)[:200]},
                ) from exc
            return store
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            raise

    def _harden_files(self) -> None:
        _harden_file(self._dir / DB_FILENAME)
        _harden_file(self._dir / f"{DB_FILENAME}-wal")
        _harden_file(self._dir / f"{DB_FILENAME}-shm")
        os.chmod(self._dir, 0o700)

    def _bootstrap(self) -> None:
        with self._write() as cur:
            tables = schema.table_names(cur)
            version = schema.schema_version(cur) if tables else 0
            fresh = version == 0 or not tables
            if fresh:
                if "meta" in tables:
                    session_row = cur.execute(
                        "SELECT value FROM meta WHERE key = 'session_id'"
                    ).fetchone()
                    if session_row is not None and session_row[0] != self._session_id:
                        raise CorruptStoreError(
                            "store belongs to a different session",
                            code="session_mismatch",
                            details={"expected": self._session_id[:64]},
                        )
                schema.create_schema(cur)
                self._db_id = sha256_hex(f"clm-db\x00{self._session_id}\x00{_utc_now()}")[:32]
                cur.execute(
                    "INSERT INTO meta (key, value) VALUES ('session_id', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (self._session_id,),
                )
                cur.execute(
                    "INSERT INTO meta (key, value) VALUES ('db_id', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (self._db_id,),
                )
            else:
                schema.require_schema(cur)
                stored = self._read_meta("session_id")
                if stored != self._session_id:
                    raise CorruptStoreError(
                        "store belongs to a different session",
                        code="session_mismatch",
                        details={"found": (stored or "")[:64]},
                    )
                self._db_id = self._read_meta("db_id") or ""
                if not self._db_id:
                    raise CorruptStoreError("store has no db_id", code="schema_incomplete")
            head = cur.execute("SELECT MAX(revision) FROM revisions").fetchone()[0]
            if head is None:
                ops: list[dict[str, Any]] = []
                head_hash = revision_hash(
                    prev_hash=GENESIS_HASH,
                    revision=1,
                    parent_revision=0,
                    base_revision=0,
                    kind="init",
                    actor_origin=ActorOrigin.SYSTEM.value,
                    txn_id=None,
                    operations=ops,
                )
                now = self._now()
                cur.execute(
                    "INSERT INTO revisions (revision, parent_revision, base_revision, kind, head_hash, "
                    "txn_id, actor_origin, op_count, applied, inert, est_tokens_before, est_tokens_after, created_at) "
                    "VALUES (1, 0, 0, 'init', ?, NULL, 'system', 0, 0, 0, 0, 0, ?)",
                    (head_hash, now),
                )
                cur.execute(
                    "INSERT INTO snapshots (revision, head_hash, active_span_count, dropped_span_count, "
                    "offloaded_span_count, pinned_span_count, est_tokens, active_set_sha256, created_at) "
                    "VALUES (1, ?, 0, 0, 0, 0, 0, ?, ?)",
                    (head_hash, sha256_hex(canonical_json([])), now),
                )
                cur.execute(
                    "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
                    "VALUES (1, 'store_created', 'system', ?, ?)",
                    (canonical_json({"db_id": self._db_id}), now),
                )
        self._invalidate()
        self._write_index()

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "ContextStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if not self._read_only:
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except sqlite3.Error:
                        pass
            finally:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                if self._lock_fd is not None:
                    fd, self._lock_fd = self._lock_fd, None
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def db_id(self) -> str:
        return self._db_id

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def config(self) -> StoreConfig:
        return self._config

    @property
    def directory(self) -> Path:
        return self._dir

    # -- guards and primitives -------------------------------------------
    def _require_open(self) -> None:
        if self._closed:
            raise StoreClosedError("store handle is closed", code="store_closed")

    def _require_writable(self) -> None:
        self._require_open()
        if self._read_only:
            raise ReadOnlyStoreError("store handle is read-only", code="read_only_store")

    def _now(self) -> str:
        clock = self._config.clock
        return clock() if clock is not None else _utc_now()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Cursor]:
        self._require_writable()
        with self._lock:
            self._require_open()
            if self._in_txn:
                raise CLMError("nested write transaction requested", code="nested_transaction")
            cur = self._conn.cursor()
            try:
                try:
                    cur.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc) or "busy" in str(exc):
                        raise StoreBusyError(
                            "store is busy", code="store_busy", details={"reason": str(exc)}
                        ) from exc
                    raise
                self._in_txn = True
                try:
                    yield cur
                except BaseException:
                    try:
                        cur.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                else:
                    try:
                        cur.execute("COMMIT")
                    except BaseException:
                        try:
                            cur.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        raise
                finally:
                    self._in_txn = False
            finally:
                cur.close()

    def _invalidate(self) -> None:
        self._generation += 1
        self._snapshot_cache = None

    def _read_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _head_row(self, cur: sqlite3.Cursor) -> sqlite3.Row:
        row = cur.execute("SELECT * FROM revisions ORDER BY revision DESC LIMIT 1").fetchone()
        if row is None:
            raise CorruptStoreError("store has no revisions", code="no_revisions")
        return row

    def _head(self, cur: sqlite3.Cursor) -> int:
        return int(self._head_row(cur)["revision"])

    def _txn_row(self, cur: sqlite3.Cursor, txn_id: str) -> sqlite3.Row | None:
        return cur.execute("SELECT * FROM txns WHERE txn_id = ?", (txn_id,)).fetchone()

    # -- span lookup ------------------------------------------------------
    def _current_span_id_for_uid(self, cur: sqlite3.Cursor, uid: str) -> str | None:
        row = cur.execute("SELECT span_id FROM current_spans WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else str(row[0])

    def _resolve_span_id(self, cur: sqlite3.Cursor, span_id: str) -> str | None:
        """Resolve a span id through absorbed aliases to the current version."""

        seen: set[str] = set()
        current: str | None = span_id
        while current is not None and current not in seen:
            seen.add(current)
            row = cur.execute("SELECT uid FROM spans WHERE span_id = ?", (current,)).fetchone()
            if row is not None:
                live = self._current_span_id_for_uid(cur, str(row["uid"]))
                if live is not None and live != current:
                    current = live
                    continue
                return current
            alias = cur.execute(
                "SELECT span_id FROM span_aliases WHERE alias = ?", (current,)
            ).fetchone()
            if alias is None:
                return None
            current = str(alias[0])
        return None

    def _load_span(
        self, cur: sqlite3.Cursor, span_id: str, *, pins: set[str] | None = None
    ) -> ArchivedSpan | None:
        resolved = self._resolve_span_id(cur, span_id)
        if resolved is None:
            return None
        row = cur.execute(
            "SELECT s.*, cs.present AS present, st.state AS state, st.rewrite_text AS rewrite_text, "
            "st.rewrite_sha256 AS rewrite_sha256 "
            "FROM spans s LEFT JOIN current_spans cs ON cs.span_id = s.span_id "
            "LEFT JOIN span_state st ON st.span_id = s.span_id WHERE s.span_id = ?",
            (resolved,),
        ).fetchone()
        if row is None:
            return None
        if pins is None:
            pins = self._pin_ids(cur)
        return self._span_from_row(row, pinned=resolved in pins, current=True)

    def _span_from_row(
        self, row: Mapping[str, Any], *, pinned: bool, current: bool
    ) -> ArchivedSpan:
        message = json.loads(row["message_json"])
        state = SpanState(row["state"] or SpanState.ACTIVE.value)
        rewrite = row["rewrite_text"]
        est = _tokens_for_text(rewrite) if rewrite else _tokens_for_text(row["message_json"])
        return ArchivedSpan(
            span_id=str(row["span_id"]),
            uid=str(row["uid"]),
            message=message,
            origin=SpanOrigin(row["origin"]),
            protected=bool(row["protected"]),
            content_sha256=str(row["content_sha256"]),
            group_id=row["group_id"],
            aliases=(),
            seq=int(row["seq"]),
            version=int(row["version"]),
            created_revision=int(row["created_revision"]),
            editable=bool(row["editable"]),
            present=bool(row["present"]) if row["present"] is not None else False,
            current=current,
            state=state,
            rewrite_text=rewrite,
            rewrite_sha256=row["rewrite_sha256"],
            pinned=pinned,
            est_tokens=est,
        )

    def _tombstone_span(self, row: Mapping[str, Any], *, pinned: bool, reason: str) -> ArchivedSpan:
        """An explicit non-authoritative stand-in for an unreadable payload.

        The row stays archived and its identity/hash are preserved. Only the
        unreadable bytes are replaced, so projection can continue with an
        auditable gap instead of either serving corrupt data or losing every
        other span.
        """

        span_id = str(row["span_id"])
        uid = str(row["uid"])
        marker = f"[clm.unreadable payload span={span_id} reason={reason}]"
        message = {"role": str(row["role"]), "content": marker, "message_uid": uid}
        return ArchivedSpan(
            span_id=span_id,
            uid=uid,
            message=message,
            origin=SpanOrigin(row["origin"]),
            protected=True,
            content_sha256=str(row["content_sha256"]),
            group_id=row["group_id"],
            aliases=(),
            seq=int(row["seq"]),
            version=int(row["version"]),
            created_revision=int(row["created_revision"]),
            editable=False,
            present=bool(row["present"]) if row["present"] is not None else False,
            current=True,
            state=SpanState(row["state"] or SpanState.ACTIVE.value),
            rewrite_text=None,
            rewrite_sha256=None,
            pinned=pinned,
            est_tokens=_tokens_for_text(marker),
        )

    def _pin_ids(self, cur: sqlite3.Cursor) -> set[str]:
        return {str(row[0]) for row in cur.execute("SELECT span_id FROM pins")}

    def _load_pins(self, cur: sqlite3.Cursor) -> dict[str, PinRecord]:
        out: dict[str, PinRecord] = {}
        for row in cur.execute("SELECT * FROM pins ORDER BY span_id"):
            out[str(row["span_id"])] = PinRecord(
                span_id=str(row["span_id"]),
                actor_origin=ActorOrigin(row["actor_origin"]),
                revision=int(row["revision"]),
                pinned_at=str(row["pinned_at"]),
            )
        return out

    def _load_aliases(self, cur: sqlite3.Cursor) -> dict[str, str]:
        out: dict[str, str] = {}
        # Materialise first: alias resolution runs nested queries on the same
        # cursor, which would otherwise invalidate the outer iteration.
        rows = cur.execute("SELECT alias, span_id FROM span_aliases ORDER BY alias").fetchall()
        for row in rows:
            resolved = self._resolve_span_id(cur, str(row["span_id"]))
            if resolved is not None:
                out[str(row["alias"])] = resolved
        return out

    def _load_current_spans(
        self, cur: sqlite3.Cursor, *, verify_hashes: bool | None = None
    ) -> dict[str, ArchivedSpan]:
        pins = self._pin_ids(cur)
        verify = self._config.snapshot_verify_hashes if verify_hashes is None else verify_hashes
        spans: dict[str, ArchivedSpan] = {}
        for row in cur.execute(
            "SELECT s.*, cs.present AS present, st.state AS state, st.rewrite_text AS rewrite_text, "
            "st.rewrite_sha256 AS rewrite_sha256 "
            "FROM spans s JOIN current_spans cs ON cs.span_id = s.span_id "
            "LEFT JOIN span_state st ON st.span_id = s.span_id ORDER BY s.seq"
        ):
            if verify:
                try:
                    self._verify_payload(row)
                except (CorruptStoreError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    reason = getattr(exc, "code", None) or type(exc).__name__
                    spans[str(row["span_id"])] = self._tombstone_span(
                        row,
                        pinned=str(row["span_id"]) in pins,
                        reason=str(reason),
                    )
                    continue
            try:
                span = self._span_from_row(row, pinned=str(row["span_id"]) in pins, current=True)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
                spans[str(row["span_id"])] = self._tombstone_span(
                    row,
                    pinned=str(row["span_id"]) in pins,
                    reason=type(exc).__name__,
                )
                continue
            spans[span.span_id] = span
        return spans

    def _verify_payload(self, row: Mapping[str, Any]) -> None:
        payload = row["message_json"]
        computed = sha256_hex(canonical_json(json.loads(payload)))
        if computed != row["content_sha256"]:
            raise CorruptStoreError(
                "archived payload hash mismatch",
                code="payload_hash_mismatch",
                details={
                    "span_id": row["span_id"],
                    "expected": row["content_sha256"],
                    "computed": computed,
                },
            )

    # -- revision chain ---------------------------------------------------
    def _chain(self, cur: sqlite3.Cursor, revision: int) -> list[int]:
        """Revisions whose operations make up the state at ``revision`` (desc)."""

        chain: list[int] = []
        seen: set[int] = set()
        current = revision
        while current and current not in seen:
            seen.add(current)
            row = cur.execute(
                "SELECT base_revision FROM revisions WHERE revision = ?", (current,)
            ).fetchone()
            if row is None:
                break
            chain.append(current)
            current = int(row[0])
        return chain

    def _operations_for_revision(self, cur: sqlite3.Cursor, revision: int) -> list[EditOperation]:
        out: list[EditOperation] = []
        for row in cur.execute(
            "SELECT * FROM operations WHERE revision = ? ORDER BY ord", (revision,)
        ):
            payload = _op_payload_from_row(row)
            out.append(
                EditOperation(
                    kind=payload["kind"],
                    span_ids=tuple(payload["span_ids"]),
                    replacement=payload.get("replacement"),
                    actor_origin=payload["actor_origin"],
                    provenance=payload["provenance"],
                    txn_id=payload.get("txn_id"),
                )
            )
        return out

    def _verify_head(self, cur: sqlite3.Cursor, rev_row: Mapping[str, Any]) -> None:
        revision = int(rev_row["revision"])
        if revision <= 1:
            prev_hash = GENESIS_HASH
        else:
            parent = cur.execute(
                "SELECT head_hash FROM revisions WHERE revision = ?", (revision - 1,)
            ).fetchone()
            if parent is None:
                raise CorruptStoreError(
                    "revision chain has a gap",
                    code="revision_gap",
                    details={"revision": revision},
                )
            prev_hash = str(parent[0])
        ops = [
            _op_payload_from_row(row)
            for row in cur.execute(
                "SELECT * FROM operations WHERE revision = ? ORDER BY ord", (revision,)
            )
        ]
        computed = revision_hash(
            prev_hash=prev_hash,
            revision=revision,
            parent_revision=int(rev_row["parent_revision"]),
            base_revision=int(rev_row["base_revision"]),
            kind=str(rev_row["kind"]),
            actor_origin=str(rev_row["actor_origin"]),
            txn_id=rev_row["txn_id"],
            operations=ops,
        )
        if computed != rev_row["head_hash"]:
            raise CorruptStoreError(
                "revision hash mismatch",
                code="revision_hash_mismatch",
                details={
                    "revision": revision,
                    "expected": rev_row["head_hash"],
                    "computed": computed,
                },
            )

    # -- snapshot ---------------------------------------------------------
    def snapshot(self) -> StoreSnapshot:
        """Immutable, deep-copy-safe view of the committed revision."""

        self._require_open()
        with self._lock:
            if self._snapshot_cache is not None and self._snapshot_generation == self._generation:
                return self._snapshot_cache
            cur = self._conn.cursor()
            try:
                rev_row = self._head_row(cur)
                self._verify_head(cur, dict(rev_row))
                revision = int(rev_row["revision"])
                spans = self._load_current_spans(cur)
                pins = self._load_pins(cur)
                aliases = self._load_aliases(cur)
                chain = self._chain(cur, revision)
                operations: list[EditOperation] = []
                for rev in sorted(chain):
                    operations.extend(self._operations_for_revision(cur, rev))
                truncated = False
                cap = self._config.max_operations_in_snapshot
                if cap > 0 and len(operations) > cap:
                    operations = operations[-cap:]
                    truncated = True
                counts = {
                    "active": sum(1 for s in spans.values() if s.state is SpanState.ACTIVE),
                    "dropped": sum(1 for s in spans.values() if s.state is SpanState.DROPPED),
                    "offloaded": sum(1 for s in spans.values() if s.state is SpanState.OFFLOADED),
                    "pinned": len(pins),
                    "spans": len(spans),
                }
                est = sum(s.est_tokens for s in spans.values() if s.state is SpanState.ACTIVE)
                snap = StoreSnapshot(
                    session_id=self._session_id,
                    db_id=self._db_id,
                    revision=revision,
                    head_hash=str(rev_row["head_hash"]),
                    operations=tuple(operations),
                    spans=spans,
                    pins=pins,
                    aliases=aliases,
                    counts=counts,
                    est_tokens=est,
                    operations_truncated=truncated,
                )
            finally:
                cur.close()
            self._snapshot_cache = snap
            self._snapshot_generation = self._generation
            return snap

    # -- ingest -----------------------------------------------------------
    def ingest(self, messages: Sequence[Mapping[str, Any]]) -> IngestResult:
        """Archive (redacted) host messages, write-through and idempotent.

        Each call is treated as the caller's view of the *current* host history
        (``assume_full_history``): UIDs seen are marked present, and previously
        archived UIDs absent from the batch are marked absent so revert/restore
        can report inert instead of resurrecting deleted content.

        Input objects are never mutated and never aliased into the archive.
        """

        self._require_writable()
        if messages is None or isinstance(messages, (str, bytes, Mapping)):
            raise ValidationError("messages must be a sequence", code="invalid_messages")
        try:
            batch = list(messages)
        except TypeError:
            raise ValidationError("messages must be a sequence", code="invalid_messages") from None
        prepared: list[tuple[str, Any, Mapping[str, Any]]] = []
        ephemeral = 0
        redactions = 0
        for raw in batch:
            if not isinstance(raw, Mapping):
                raise ValidationError("each message must be a mapping", code="invalid_message")
            uid = raw.get("message_uid", raw.get("uid"))
            if not isinstance(uid, str) or not uid.strip():
                ephemeral += 1
                continue
            uid = uid.strip()
            outcome = self._redactor(raw)
            redactions += outcome.count
            prepared.append((uid, outcome, raw))

        with self._write() as cur:
            head = self._head(cur)
            next_seq_row = cur.execute("SELECT COALESCE(MAX(seq), 0) FROM spans").fetchone()
            next_seq = int(next_seq_row[0])
            archived = duplicates = versions = absorbed = 0
            updated: list[str] = []
            seen: list[str] = []
            group_map: dict[str, str] = {}
            now = self._now()
            chain_row = cur.execute(
                "SELECT entry_hash FROM archive ORDER BY archive_id DESC LIMIT 1"
            ).fetchone()
            prev_hash = GENESIS_HASH if chain_row is None else str(chain_row[0])

            for uid, outcome, raw in prepared:
                seen.append(uid)
                message = outcome.message
                role = str(message.get("role", ""))
                role_key = role
                origin = origin_for_role(role_key)
                content_json = canonical_json(message)
                content_sha = sha256_hex(content_json)

                tool_calls = message.get("tool_calls")
                if role_key == "assistant" and isinstance(tool_calls, list) and tool_calls:
                    group_id: str | None = "grp_" + sha256_hex(uid)[:16]
                    for call in tool_calls:
                        if isinstance(call, Mapping) and call.get("id"):
                            group_map[str(call["id"])] = str(group_id)
                elif role_key in {"tool", "function"}:
                    call_id = message.get("tool_call_id", message.get("tool_use_id"))
                    group_id = group_map.get(str(call_id)) if call_id is not None else None
                else:
                    group_id = None

                multimodal = _is_multimodal(message.get("content"))
                protected = (
                    role_key in PROTECTED_ROLES
                    or origin is SpanOrigin.UNKNOWN
                    or multimodal
                    or raw.get("protected") is True
                )
                existing = cur.execute(
                    "SELECT span_id, version, seq FROM spans WHERE uid = ? AND content_sha256 = ?",
                    (uid, content_sha),
                ).fetchone()
                if existing is not None:
                    duplicates += 1
                    cur.execute(
                        "INSERT INTO current_spans (uid, span_id, present, last_seen_seq, updated_revision) "
                        "VALUES (?, ?, 1, ?, ?) ON CONFLICT (uid) DO UPDATE SET "
                        "span_id = excluded.span_id, present = 1, last_seen_seq = excluded.last_seen_seq, "
                        "updated_revision = excluded.updated_revision",
                        (uid, existing["span_id"], int(existing["seq"]), head),
                    )
                    updated.append(str(existing["span_id"]))
                    continue

                live = cur.execute(
                    "SELECT cs.span_id AS span_id, s.version AS version FROM current_spans cs "
                    "JOIN spans s ON s.span_id = cs.span_id WHERE cs.uid = ?",
                    (uid,),
                ).fetchone()
                version = int(live["version"]) + 1 if live is not None else 1
                span_id = "sp_" + sha256_hex(f"{uid}\x00{content_sha}")[:24]
                next_seq += 1
                cur.execute(
                    "INSERT INTO spans (span_id, uid, version, content_sha256, message_json, origin, role, "
                    "group_id, protected, editable, multimodal, seq, created_revision, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        span_id,
                        uid,
                        version,
                        content_sha,
                        content_json,
                        origin.value,
                        role_key,
                        group_id,
                        1 if protected else 0,
                        0 if protected else 1,
                        1 if multimodal else 0,
                        next_seq,
                        head,
                        now,
                    ),
                )
                cur.execute(
                    "INSERT INTO span_state (span_id, state, rewrite_text, rewrite_sha256, updated_revision) "
                    "VALUES (?, 'active', NULL, NULL, ?)",
                    (span_id, head),
                )
                cur.execute(
                    "INSERT INTO current_spans (uid, span_id, present, last_seen_seq, updated_revision) "
                    "VALUES (?, ?, 1, ?, ?) ON CONFLICT (uid) DO UPDATE SET "
                    "span_id = excluded.span_id, present = 1, last_seen_seq = excluded.last_seen_seq, "
                    "updated_revision = excluded.updated_revision",
                    (uid, span_id, next_seq, head),
                )
                if live is not None and str(live["span_id"]) != span_id:
                    cur.execute(
                        "INSERT INTO span_aliases (alias, span_id, reason, created_revision, created_at) "
                        "VALUES (?, ?, 'content_version', ?, ?) ON CONFLICT (alias) DO NOTHING",
                        (str(live["span_id"]), span_id, head, now),
                    )
                    absorbed += 1
                    versions += 1
                entry_hash = chain_entry_hash(
                    prev_hash=prev_hash,
                    span_id=span_id,
                    content_sha256=content_sha,
                    payload_json=content_json,
                    revision=head,
                )
                cur.execute(
                    "INSERT INTO archive (span_id, uid, content_sha256, payload_json, redacted, "
                    "redaction_count, prev_hash, entry_hash, revision, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        span_id,
                        uid,
                        content_sha,
                        content_json,
                        1 if self._config.archive_redact else 0,
                        outcome.count,
                        prev_hash,
                        entry_hash,
                        head,
                        now,
                    ),
                )
                prev_hash = entry_hash
                archived += 1
                updated.append(span_id)

            if self._config.assume_full_history and seen:
                cur.execute("CREATE TEMP TABLE IF NOT EXISTS _clm_seen (uid TEXT PRIMARY KEY)")
                cur.execute("DELETE FROM _clm_seen")
                cur.executemany(
                    "INSERT OR IGNORE INTO _clm_seen (uid) VALUES (?)", [(u,) for u in seen]
                )
                cur.execute(
                    "UPDATE current_spans SET present = 0 "
                    "WHERE present = 1 AND uid NOT IN (SELECT uid FROM _clm_seen)"
                )
                cur.execute("DELETE FROM _clm_seen")

            cur.execute(
                "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
                "VALUES (?, 'ingest', 'system', ?, ?)",
                (
                    head,
                    canonical_json(
                        {
                            "archived": archived,
                            "duplicates": duplicates,
                            "versions": versions,
                            "ephemeral_skipped": ephemeral,
                            "redactions": redactions,
                        }
                    ),
                    now,
                ),
            )
            total = int(cur.execute("SELECT COUNT(*) FROM current_spans").fetchone()[0])

        self._invalidate()
        self._write_index()
        return IngestResult(
            ok=True,
            revision=head,
            archived=archived,
            duplicates=duplicates,
            versions=versions,
            ephemeral_skipped=ephemeral,
            redactions=redactions,
            aliases_absorbed=absorbed,
            spans_total=total,
            updated_span_ids=tuple(updated),
        )

    # -- planning ---------------------------------------------------------
    def _plan(
        self,
        cur: sqlite3.Cursor,
        ops: Sequence[EditOperation],
        *,
        state_aware: bool = True,
    ) -> list[_Planned]:
        """Validate operations against current state and produce receipts.

        Raises :class:`CLMError` subclasses on refusals.  ``state_aware=False``
        performs structural validation only (used when staging a proposal whose
        base revision may be older than head).
        """

        if len(ops) > self._config.max_operations_per_txn:
            raise StoreBudgetError(
                f"too many operations in one transaction (max {self._config.max_operations_per_txn})",
                code="too_many_operations",
                details={"count": len(ops), "max": self._config.max_operations_per_txn},
            )
        pins = self._pin_ids(cur)
        targeted: dict[str, EditKind] = {}
        planned: list[_Planned] = []
        for op in ops:
            if op.kind is EditKind.BOUNDARY:
                planned.append(_Planned(op=op, span=None, effect="applied", reason="boundary"))
                continue
            for sid in op.span_ids:
                if sid in targeted:
                    raise ConflictError(
                        "span appears in more than one operation",
                        code="duplicate_span",
                        details={"span_id": sid},
                    )
                targeted[sid] = op.kind
            for sid in op.span_ids:
                span = self._load_span(cur, sid, pins=pins)
                if span is None:
                    raise NotFoundError(
                        "unknown span for this session",
                        code="unknown_span",
                        details={"span_id": sid},
                    )
                if op.kind in _REFUSE_PROTECTED_KINDS:
                    if span.protected:
                        raise ProtectedSpanError(
                            "span is protected (system/user/prefill/multimodal/unknown role)",
                            code="protected_span",
                            details={
                                "span_id": span.span_id,
                                "kind": op.kind.value,
                                "origin": span.origin.value,
                            },
                        )
                    if span.pinned:
                        raise ConflictError(
                            "span is pinned; only the operator can unpin",
                            code="span_pinned",
                            details={"span_id": span.span_id, "kind": op.kind.value},
                        )
                    if op.kind is EditKind.REWRITE and not span.editable:
                        raise ProtectedSpanError(
                            "span is not editable",
                            code="not_editable",
                            details={"span_id": span.span_id},
                        )
                if op.kind is EditKind.REWRITE and op.replacement is not None:
                    if len(op.replacement.encode("utf-8")) > self._config.max_rewrite_bytes:
                        raise StoreBudgetError(
                            "rewrite exceeds max_rewrite_bytes",
                            code="rewrite_too_large",
                            details={"limit": self._config.max_rewrite_bytes},
                        )
                effect, reason = ("applied", "")
                if state_aware:
                    effect, reason = self._transition(span, op)
                planned.append(_Planned(op=op, span=span, effect=effect, reason=reason))

        if state_aware:
            self._check_groups(cur, planned, pins=pins)
        return planned

    def _transition(self, span: ArchivedSpan, op: EditOperation) -> tuple[str, str]:
        kind = op.kind
        if kind is EditKind.PIN:
            if span.pinned:
                return "inert", "already_pinned"
            if span.state is not SpanState.ACTIVE:
                return "inert", "not_active"
            return "applied", ""
        if span.state is SpanState.ACTIVE:
            if kind is EditKind.KEEP:
                return "inert", "already_active"
            return "applied", ""
        # span is dropped or offloaded
        if kind in (EditKind.KEEP, EditKind.RESTORE):
            if not span.present and kind is EditKind.RESTORE:
                return "inert", "uid_absent"
            return "applied", ""
        return "inert", "not_active"

    def _check_groups(
        self, cur: sqlite3.Cursor, planned: Sequence[_Planned], *, pins: set[str]
    ) -> None:
        """Refuse operations that would split an atomic tool group."""

        targeted: dict[str, EditKind] = {}
        for item in planned:
            if item.span is None:
                continue
            if item.op.kind in _STATE_CHANGING_KINDS:
                targeted[item.span.span_id] = item.op.kind
        if not targeted:
            return
        for span_id in list(targeted):
            row = cur.execute("SELECT group_id FROM spans WHERE span_id = ?", (span_id,)).fetchone()
            if row is None or row["group_id"] is None:
                continue
            group_id = str(row["group_id"])
            members = cur.execute(
                "SELECT s.span_id AS span_id FROM spans s JOIN current_spans cs ON cs.span_id = s.span_id "
                "WHERE s.group_id = ?",
                (group_id,),
            ).fetchall()
            missing = [
                str(m["span_id"])
                for m in members
                if str(m["span_id"]) not in targeted
                and self._span_state_of(cur, str(m["span_id"])) is SpanState.ACTIVE
            ]
            if missing:
                raise ConflictError(
                    "operation would split an atomic tool group; include every member",
                    code="incomplete_group",
                    details={"group_id": group_id, "span_id": span_id, "missing": sorted(missing)},
                )

    def _span_state_of(self, cur: sqlite3.Cursor, span_id: str) -> SpanState:
        row = cur.execute("SELECT state FROM span_state WHERE span_id = ?", (span_id,)).fetchone()
        if row is None:
            return SpanState.ACTIVE
        return SpanState(row[0])

    # -- staging / commit -------------------------------------------------
    def stage(
        self,
        operations: Sequence[Any],
        *,
        expected_revision: int,
        txn_id: str,
        actor_origin: str,
    ) -> StageResult:
        """Validate and persist an operation set without changing the head."""

        self._require_writable()
        ops = coerce_operations(operations, actor_origin=actor_origin)
        validate_txn_id(txn_id)
        expected = self._validate_revision(expected_revision)
        with self._write() as cur:
            result = self._stage_locked(
                cur,
                ops,
                expected_revision=expected,
                txn_id=txn_id,
                actor_origin=ActorOrigin(actor_origin),
            )
        return result

    def _stage_locked(
        self,
        cur: sqlite3.Cursor,
        ops: Sequence[EditOperation],
        *,
        expected_revision: int,
        txn_id: str,
        actor_origin: ActorOrigin,
    ) -> StageResult:
        head = self._head(cur)
        staged_sha = canonical_sha256([op.to_payload() for op in ops])
        existing = self._txn_row(cur, txn_id)
        if existing is not None:
            state = str(existing["state"])
            if state == "committed":
                return StageResult(
                    ok=True,
                    revision=int(existing["committed_revision"] or head),
                    applied=(),
                    inert=(),
                    txn_id=txn_id,
                    staged_sha256=str(existing["staged_sha256"]),
                    staged_operations=tuple(ops),
                    replayed=True,
                    already_committed=True,
                )
            if state == "discarded":
                raise IdempotencyConflictError(
                    "transaction was discarded", code="txn_discarded", details={"txn_id": txn_id}
                )
            if str(existing["staged_sha256"]) != staged_sha:
                raise IdempotencyConflictError(
                    "txn_id reused with a different payload",
                    code="txn_payload_mismatch",
                    details={"txn_id": txn_id},
                )
            planned = self._plan(cur, ops)
            return self._stage_result(
                planned, txn_id=txn_id, staged_sha=staged_sha, ops=ops, replayed=True
            )
        if expected_revision != head:
            raise StaleRevisionError(
                "expected_revision does not match the current head",
                code="stale_revision",
                details={"expected": expected_revision, "current": head},
            )
        planned = self._plan(cur, ops)
        now = self._now()
        cur.execute(
            "INSERT INTO txns (txn_id, state, base_revision, staged_sha256, operations_json, actor_origin, "
            "created_at, updated_at, committed_revision, result_json) VALUES (?, 'staged', ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (
                txn_id,
                head,
                staged_sha,
                canonical_json([op.to_payload() for op in ops]),
                actor_origin.value,
                now,
                now,
            ),
        )
        cur.execute(
            "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
            "VALUES (?, 'stage', ?, ?, ?)",
            (
                head,
                actor_origin.value,
                canonical_json(
                    {
                        "txn_id": txn_id,
                        "staged_sha256": staged_sha,
                        "applied": [r.to_dict() for r in self._receipts(planned, "applied")],
                        "inert": [r.to_dict() for r in self._receipts(planned, "inert")],
                    }
                ),
                now,
            ),
        )
        return self._stage_result(
            planned, txn_id=txn_id, staged_sha=staged_sha, ops=ops, replayed=False
        )

    def _stage_result(
        self,
        planned: Sequence[_Planned],
        *,
        txn_id: str,
        staged_sha: str,
        ops: Sequence[EditOperation],
        replayed: bool,
    ) -> StageResult:
        applied = self._receipts(planned, "applied")
        inert = self._receipts(planned, "inert")
        return StageResult(
            ok=True,
            revision=self._revision_read(),
            applied=applied,
            inert=inert,
            txn_id=txn_id,
            staged_sha256=staged_sha,
            staged_operations=tuple(ops),
            replayed=replayed,
        )

    @staticmethod
    def _receipts(planned: Sequence[_Planned], effect: str) -> tuple[Receipt, ...]:
        return tuple(
            item.receipt for item in planned if item.effect == effect and item.span is not None
        )

    def commit(self, *, txn_id: str, expected_revision: int) -> CommitResult:
        """Apply a staged transaction atomically; replay is idempotent."""

        self._require_writable()
        validate_txn_id(txn_id)
        expected = self._validate_revision(expected_revision)
        with self._write() as cur:
            txn = self._txn_row(cur, txn_id)
            if txn is None:
                raise NotFoundError(
                    "unknown transaction; stage it first",
                    code="unknown_txn",
                    details={"txn_id": txn_id},
                )
            state = str(txn["state"])
            if state == "committed":
                return self._replay_commit(txn)
            if state == "discarded":
                raise IdempotencyConflictError(
                    "transaction was discarded", code="txn_discarded", details={"txn_id": txn_id}
                )
            head = self._head(cur)
            if expected != head:
                raise StaleRevisionError(
                    "expected_revision does not match the current head",
                    code="stale_revision",
                    details={"expected": expected, "current": head},
                )
            if int(txn["base_revision"]) != head:
                raise StaleRevisionError(
                    "staged transaction is based on an older revision",
                    code="stale_stage",
                    details={"base_revision": int(txn["base_revision"]), "current": head},
                )
            ops = self._decode_staged_ops(dict(txn))
            planned = self._plan(cur, ops)
            result = self._apply(
                cur,
                planned,
                txn_id=txn_id,
                actor_origin=ActorOrigin(str(txn["actor_origin"])),
                kind=self._commit_kind(ops),
                base_revision=head,
                extra_audit={"txn_id": txn_id},
            )
            now = self._now()
            cur.execute(
                "UPDATE txns SET state = 'committed', committed_revision = ?, updated_at = ?, result_json = ? "
                "WHERE txn_id = ? AND state = 'staged'",
                (result.revision, now, canonical_json(result.to_dict()), txn_id),
            )
        self._invalidate()
        self._write_index()
        return result

    @staticmethod
    def _commit_kind(ops: Sequence[EditOperation]) -> str:
        kinds = {op.kind for op in ops}
        if kinds and kinds == {EditKind.BOUNDARY}:
            return "boundary"
        return "edit"

    def _decode_staged_ops(self, txn: Mapping[str, Any]) -> tuple[EditOperation, ...]:
        payload = json.loads(txn["operations_json"])
        ops: list[EditOperation] = []
        for raw in payload:
            try:
                ops.append(EditOperation.from_mapping(raw))
            except CLMError:
                raise
        return tuple(ops)

    def _replay_commit(self, txn: Mapping[str, Any] | sqlite3.Row) -> CommitResult:
        payload_json = txn["result_json"]
        if not payload_json:
            raise CorruptStoreError(
                "committed transaction has no recorded outcome",
                code="missing_txn_result",
                details={"txn_id": txn["txn_id"]},
            )
        payload = json.loads(payload_json)
        payload["replayed"] = True
        return self._commit_result_from_dict(payload)

    @staticmethod
    def _commit_result_from_dict(payload: Mapping[str, Any]) -> CommitResult:
        def _receipts(key: str) -> tuple[Receipt, ...]:
            return tuple(
                Receipt(
                    span_id=str(item.get("span_id", "")),
                    kind=str(item.get("kind", "")),
                    effect=str(item.get("effect", "")),
                    reason=str(item.get("reason", "")),
                )
                for item in payload.get(key, ())
            )

        return CommitResult(
            ok=bool(payload.get("ok", True)),
            revision=int(payload.get("revision", 0)),
            applied=_receipts("applied"),
            inert=_receipts("inert"),
            errors=tuple(payload.get("errors", ()) or ()),
            receipts=_receipts("receipts") or (_receipts("applied") + _receipts("inert")),
            txn_id=str(payload.get("txn_id", "")),
            head_hash=str(payload.get("head_hash", "")),
            kind=str(payload.get("kind", "edit")),
            replayed=bool(payload.get("replayed", False)),
            base_revision=int(payload.get("base_revision", 0)),
        )

    def discard(self, *, txn_id: str) -> None:
        """Drop a staged transaction.  No-op for unknown ids."""

        self._require_writable()
        validate_txn_id(txn_id)
        with self._write() as cur:
            txn = self._txn_row(cur, txn_id)
            if txn is None:
                return
            state = str(txn["state"])
            if state == "committed":
                raise ConflictError(
                    "cannot discard a committed transaction",
                    code="already_committed",
                    details={"txn_id": txn_id},
                )
            if state == "discarded":
                return
            now = self._now()
            cur.execute(
                "UPDATE txns SET state = 'discarded', updated_at = ? WHERE txn_id = ? AND state = 'staged'",
                (now, txn_id),
            )
            cur.execute(
                "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
                "VALUES (?, 'discard', ?, ?, ?)",
                (
                    self._head(cur),
                    str(txn["actor_origin"]),
                    canonical_json({"txn_id": txn_id}),
                    now,
                ),
            )
        self._write_index()

    # -- apply ------------------------------------------------------------
    def _apply(
        self,
        cur: sqlite3.Cursor,
        planned: Sequence[_Planned],
        *,
        txn_id: str | None,
        actor_origin: ActorOrigin,
        kind: str,
        base_revision: int,
        extra_audit: Mapping[str, Any] | None = None,
    ) -> CommitResult:
        head_row = self._head_row(cur)
        head = int(head_row["revision"])
        revision = head + 1
        before_tokens = self._est_active_tokens(cur)
        now = self._now()

        applied = self._receipts(planned, "applied")
        inert = self._receipts(planned, "inert")
        for item in planned:
            span = item.span
            if span is None or item.effect != "applied":
                continue
            op = item.op
            if op.kind is EditKind.DROP:
                self._set_state(cur, span.span_id, SpanState.DROPPED, None, revision)
            elif op.kind is EditKind.OFFLOAD:
                self._set_state(cur, span.span_id, SpanState.OFFLOADED, None, revision)
            elif op.kind is EditKind.KEEP or op.kind is EditKind.RESTORE:
                self._set_state(cur, span.span_id, SpanState.ACTIVE, span.rewrite_text, revision)
            elif op.kind is EditKind.REWRITE:
                assert op.replacement is not None
                self._set_state(
                    cur,
                    span.span_id,
                    SpanState.ACTIVE,
                    op.replacement,
                    revision,
                    rewrite_sha256=sha256_hex(op.replacement),
                )
            elif op.kind is EditKind.PIN:
                cur.execute(
                    "INSERT INTO pins (span_id, actor_origin, revision, pinned_at) VALUES (?, ?, ?, ?)",
                    (span.span_id, actor_origin.value, revision, now),
                )
                cur.execute(
                    "INSERT INTO pin_events (span_id, action, actor_origin, revision, created_at) "
                    "VALUES (?, 'pin', ?, ?, ?)",
                    (span.span_id, actor_origin.value, revision, now),
                )

        op_payloads = _hash_op_payloads((item.op for item in planned), txn_id)
        head_hash = revision_hash(
            prev_hash=str(head_row["head_hash"]),
            revision=revision,
            parent_revision=head,
            base_revision=base_revision,
            kind=kind,
            actor_origin=actor_origin.value,
            txn_id=txn_id,
            operations=op_payloads,
        )
        after_tokens = self._est_active_tokens(cur)
        counts = self._state_counts(cur)
        cur.execute(
            "INSERT INTO revisions (revision, parent_revision, base_revision, kind, head_hash, txn_id, "
            "actor_origin, op_count, applied, inert, est_tokens_before, est_tokens_after, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision,
                head,
                base_revision,
                kind,
                head_hash,
                txn_id,
                actor_origin.value,
                len(planned),
                len(applied),
                len(inert),
                before_tokens,
                after_tokens,
                now,
            ),
        )
        for index, item in enumerate(planned):
            cur.execute(
                "INSERT INTO operations (revision, ord, kind, span_ids_json, replacement, actor_origin, "
                "provenance_json, txn_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    index,
                    item.op.kind.value,
                    canonical_json(list(item.op.span_ids)),
                    item.op.replacement,
                    item.op.actor_origin.value,
                    canonical_json(dict(item.op.provenance)),
                    txn_id,
                ),
            )
        active_ids = [
            str(r[0])
            for r in cur.execute(
                "SELECT s.span_id FROM spans s JOIN span_state st ON st.span_id = s.span_id "
                "WHERE st.state = 'active' ORDER BY s.seq"
            )
        ]
        cur.execute(
            "INSERT INTO snapshots (revision, head_hash, active_span_count, dropped_span_count, "
            "offloaded_span_count, pinned_span_count, est_tokens, active_set_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision,
                head_hash,
                counts["active"],
                counts["dropped"],
                counts["offloaded"],
                counts["pinned"],
                after_tokens,
                sha256_hex(canonical_json(active_ids)),
                now,
            ),
        )
        self._record_tokens(
            cur,
            revision=revision,
            before=before_tokens,
            after=after_tokens,
            planned=planned,
            estimator=TOKEN_ESTIMATOR,
            now=now,
        )
        audit_detail: dict[str, Any] = {
            "kind": kind,
            "txn_id": txn_id,
            "applied": [r.to_dict() for r in applied],
            "inert": [r.to_dict() for r in inert],
            "est_tokens_before": before_tokens,
            "est_tokens_after": after_tokens,
        }
        boundary_labels = [
            item.op.replacement
            for item in planned
            if item.op.kind is EditKind.BOUNDARY and item.op.replacement is not None
        ]
        if boundary_labels:
            audit_detail["boundary_labels"] = boundary_labels
        if extra_audit:
            audit_detail.update(dict(extra_audit))
        cur.execute(
            "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (revision, f"commit:{kind}", actor_origin.value, canonical_json(audit_detail), now),
        )
        return CommitResult(
            ok=True,
            revision=revision,
            applied=applied,
            inert=inert,
            errors=(),
            receipts=applied + inert,
            txn_id=txn_id or "",
            head_hash=head_hash,
            kind=kind,
            replayed=False,
            base_revision=base_revision,
        )

    def _set_state(
        self,
        cur: sqlite3.Cursor,
        span_id: str,
        state: SpanState,
        rewrite_text: str | None,
        revision: int,
        *,
        rewrite_sha256: str | None = None,
    ) -> None:
        if rewrite_text is not None and rewrite_sha256 is None:
            rewrite_sha256 = sha256_hex(rewrite_text)
        cur.execute(
            "UPDATE span_state SET state = ?, rewrite_text = ?, rewrite_sha256 = ?, updated_revision = ? "
            "WHERE span_id = ?",
            (
                state.value,
                rewrite_text,
                rewrite_sha256 if rewrite_text is not None else None,
                revision,
                span_id,
            ),
        )

    def _est_active_tokens(self, cur: sqlite3.Cursor) -> int:
        total = 0
        for row in cur.execute(
            "SELECT s.message_json AS message_json, st.rewrite_text AS rewrite_text "
            "FROM spans s JOIN current_spans cs ON cs.span_id = s.span_id "
            "JOIN span_state st ON st.span_id = s.span_id WHERE st.state = 'active'"
        ):
            text = row["rewrite_text"] or row["message_json"]
            total += _tokens_for_text(text)
        return total

    def _state_counts(self, cur: sqlite3.Cursor) -> dict[str, int]:
        counts = {"active": 0, "dropped": 0, "offloaded": 0}
        for row in cur.execute("SELECT state, COUNT(*) FROM span_state GROUP BY state"):
            counts[str(row[0])] = int(row[1])
        counts["pinned"] = int(cur.execute("SELECT COUNT(*) FROM pins").fetchone()[0])
        counts["spans"] = int(cur.execute("SELECT COUNT(*) FROM current_spans").fetchone()[0])
        return counts

    def _record_tokens(
        self,
        cur: sqlite3.Cursor,
        *,
        revision: int,
        before: int,
        after: int,
        planned: Sequence[_Planned],
        estimator: str,
        now: str,
    ) -> None:
        cur.execute(
            "INSERT INTO tokens (revision, span_id, before_est, after_est, delta, estimator, detail_json) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?)",
            (
                revision,
                before,
                after,
                after - before,
                estimator,
                canonical_json({"scope": "active_total", "revision": revision}),
            ),
        )
        for item in planned:
            if item.span is None or item.effect != "applied":
                continue
            delta = 0
            if item.op.kind is EditKind.REWRITE and item.op.replacement is not None:
                delta = _tokens_for_text(item.op.replacement) - item.span.est_tokens
            elif item.op.kind in (EditKind.DROP, EditKind.OFFLOAD):
                delta = -item.span.est_tokens
            elif (
                item.op.kind in (EditKind.KEEP, EditKind.RESTORE)
                and item.span.state is not SpanState.ACTIVE
            ):
                delta = item.span.est_tokens
            cur.execute(
                "INSERT INTO tokens (revision, span_id, before_est, after_est, delta, estimator, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    item.span.span_id,
                    item.span.est_tokens,
                    item.span.est_tokens + delta,
                    delta,
                    estimator,
                    canonical_json({"kind": item.op.kind.value}),
                ),
            )

    # -- revert -----------------------------------------------------------
    def revert(
        self,
        *,
        target_revision: int,
        expected_revision: int,
        txn_id: str,
        actor_origin: str,
    ) -> CommitResult:
        """Restore the projection state of an earlier revision as a new head."""

        self._require_writable()
        validate_txn_id(txn_id)
        actor = ActorOrigin(actor_origin)
        target = self._validate_revision(target_revision)
        expected = self._validate_revision(expected_revision)
        request_sha = canonical_sha256({"op": "revert", "target": target, "expected": expected})
        with self._write() as cur:
            existing = self._txn_row(cur, txn_id)
            if existing is not None:
                if (
                    str(existing["state"]) == "committed"
                    and str(existing["staged_sha256"]) == request_sha
                ):
                    return self._replay_commit(existing)
                raise IdempotencyConflictError(
                    "txn_id already used by a different transaction",
                    code="txn_payload_mismatch",
                    details={"txn_id": txn_id},
                )
            head = self._head(cur)
            if expected != head:
                raise StaleRevisionError(
                    "expected_revision does not match the current head",
                    code="stale_revision",
                    details={"expected": expected, "current": head},
                )
            if not 1 <= target <= head:
                raise ValidationError(
                    "target_revision must be an existing revision",
                    code="invalid_target_revision",
                    details={"target": target, "head": head},
                )
            desired = self._replay_states(cur, target)
            crossed_pins = [
                str(row["span_id"])
                for row in cur.execute(
                    "SELECT span_id FROM pins WHERE revision > ? ORDER BY span_id", (target,)
                )
            ]
            if crossed_pins:
                raise ConflictError(
                    "revert would cross a pin committed after the target revision",
                    code="span_pinned",
                    details={"target_revision": target, "span_ids": crossed_pins},
                )
            current = {
                str(row["span_id"]): (SpanState(row["state"]), row["rewrite_text"])
                for row in cur.execute("SELECT span_id, state, rewrite_text FROM span_state")
            }
            pins = self._pin_ids(cur)
            revision = head + 1
            before_tokens = self._est_active_tokens(cur)
            applied: list[Receipt] = []
            inert: list[Receipt] = []
            synthetic: list[EditOperation] = []
            for span_id, (state, rewrite) in desired.items():
                cur_state, cur_rewrite = current.get(span_id, (SpanState.ACTIVE, None))
                if state is not SpanState.ACTIVE and span_id in pins:
                    inert.append(
                        Receipt(span_id=span_id, kind="revert", effect="inert", reason="pinned")
                    )
                    continue
                if (state, rewrite) == (cur_state, cur_rewrite):
                    continue
                row = cur.execute("SELECT uid FROM spans WHERE span_id = ?", (span_id,)).fetchone()
                present = True
                if row is not None:
                    pr = cur.execute(
                        "SELECT present FROM current_spans WHERE uid = ?", (str(row["uid"]),)
                    ).fetchone()
                    present = bool(pr[0]) if pr is not None else False
                if state is SpanState.ACTIVE and not present:
                    inert.append(
                        Receipt(span_id=span_id, kind="revert", effect="inert", reason="uid_absent")
                    )
                    continue
                if state is SpanState.DROPPED:
                    synthetic.append(
                        EditOperation(kind=EditKind.DROP, span_ids=(span_id,), actor_origin=actor)
                    )
                elif state is SpanState.OFFLOADED:
                    synthetic.append(
                        EditOperation(
                            kind=EditKind.OFFLOAD, span_ids=(span_id,), actor_origin=actor
                        )
                    )
                elif rewrite is not None:
                    synthetic.append(
                        EditOperation(
                            kind=EditKind.REWRITE,
                            span_ids=(span_id,),
                            replacement=rewrite,
                            actor_origin=actor,
                            provenance={"revert_of": target},
                        )
                    )
                else:
                    synthetic.append(
                        EditOperation(
                            kind=EditKind.RESTORE, span_ids=(span_id,), actor_origin=actor
                        )
                    )
                applied.append(Receipt(span_id=span_id, kind="revert", effect="applied"))
                self._set_state(cur, span_id, state, rewrite, revision)

            head_row = self._head_row(cur)
            op_payloads = _hash_op_payloads(synthetic, txn_id)
            head_hash = revision_hash(
                prev_hash=str(head_row["head_hash"]),
                revision=revision,
                parent_revision=head,
                base_revision=target,
                kind="revert",
                actor_origin=actor.value,
                txn_id=txn_id,
                operations=op_payloads,
            )
            after_tokens = self._est_active_tokens(cur)
            counts = self._state_counts(cur)
            now = self._now()
            cur.execute(
                "INSERT INTO revisions (revision, parent_revision, base_revision, kind, head_hash, txn_id, "
                "actor_origin, op_count, applied, inert, est_tokens_before, est_tokens_after, created_at) "
                "VALUES (?, ?, ?, 'revert', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    head,
                    target,
                    head_hash,
                    txn_id,
                    actor.value,
                    len(synthetic),
                    len(applied),
                    len(inert),
                    before_tokens,
                    after_tokens,
                    now,
                ),
            )
            for index, op in enumerate(synthetic):
                cur.execute(
                    "INSERT INTO operations (revision, ord, kind, span_ids_json, replacement, actor_origin, "
                    "provenance_json, txn_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        revision,
                        index,
                        op.kind.value,
                        canonical_json(list(op.span_ids)),
                        op.replacement,
                        op.actor_origin.value,
                        canonical_json(dict(op.provenance)),
                        txn_id,
                    ),
                )
            active_ids = [
                str(r[0])
                for r in cur.execute(
                    "SELECT s.span_id FROM spans s JOIN span_state st ON st.span_id = s.span_id "
                    "WHERE st.state = 'active' ORDER BY s.seq"
                )
            ]
            cur.execute(
                "INSERT INTO snapshots (revision, head_hash, active_span_count, dropped_span_count, "
                "offloaded_span_count, pinned_span_count, est_tokens, active_set_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    head_hash,
                    counts["active"],
                    counts["dropped"],
                    counts["offloaded"],
                    counts["pinned"],
                    after_tokens,
                    sha256_hex(canonical_json(active_ids)),
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO tokens (revision, span_id, before_est, after_est, delta, estimator, detail_json) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?)",
                (
                    revision,
                    before_tokens,
                    after_tokens,
                    after_tokens - before_tokens,
                    TOKEN_ESTIMATOR,
                    canonical_json({"scope": "active_total", "kind": "revert", "target": target}),
                ),
            )
            cur.execute(
                "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    revision,
                    "commit:revert",
                    actor.value,
                    canonical_json(
                        {
                            "target_revision": target,
                            "txn_id": txn_id,
                            "applied": [r.to_dict() for r in applied],
                            "inert": [r.to_dict() for r in inert],
                        }
                    ),
                    now,
                ),
            )
            result = CommitResult(
                ok=True,
                revision=revision,
                applied=tuple(applied),
                inert=tuple(inert),
                errors=(),
                receipts=tuple(applied) + tuple(inert),
                txn_id=txn_id,
                head_hash=head_hash,
                kind="revert",
                replayed=False,
                base_revision=target,
            )
            cur.execute(
                "INSERT INTO txns (txn_id, state, base_revision, staged_sha256, operations_json, actor_origin, "
                "created_at, updated_at, committed_revision, result_json) "
                "VALUES (?, 'committed', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    txn_id,
                    expected,
                    request_sha,
                    canonical_json([op.to_payload() for op in synthetic]),
                    actor.value,
                    now,
                    now,
                    revision,
                    canonical_json(result.to_dict()),
                ),
            )
        self._invalidate()
        self._write_index()
        return result

    def _replay_states(
        self, cur: sqlite3.Cursor, target_revision: int
    ) -> dict[str, tuple[SpanState, str | None]]:
        """Replay operations up to ``target_revision`` to derive span states.

        Revert revisions themselves are skipped: their recorded operations are
        audit-only (the revision's ``base_revision`` already selects a state
        root), so replay is deterministic and idempotent.
        """

        # Only current span versions participate: historical (absorbed) versions
        # have no span_state row and are addressed through alias resolution.
        desired: dict[str, tuple[SpanState, str | None]] = {
            str(row[0]): (SpanState.ACTIVE, None)
            for row in cur.execute("SELECT span_id FROM current_spans")
        }
        chain = sorted(self._chain(cur, target_revision))
        for revision in chain:
            kind_row = cur.execute(
                "SELECT kind FROM revisions WHERE revision = ?", (revision,)
            ).fetchone()
            if kind_row is None or str(kind_row[0]) == "revert":
                continue
            for op in self._operations_for_revision(cur, revision):
                if op.kind is EditKind.BOUNDARY:
                    continue
                for sid in op.span_ids:
                    resolved = self._resolve_span_id(cur, sid) or sid
                    current_state, rewrite = desired.get(resolved, (SpanState.ACTIVE, None))
                    if op.kind is EditKind.DROP:
                        desired[resolved] = (SpanState.DROPPED, rewrite)
                    elif op.kind is EditKind.OFFLOAD:
                        desired[resolved] = (SpanState.OFFLOADED, rewrite)
                    elif op.kind is EditKind.REWRITE:
                        desired[resolved] = (SpanState.ACTIVE, op.replacement)
                    elif op.kind in (EditKind.KEEP, EditKind.RESTORE):
                        desired[resolved] = (SpanState.ACTIVE, rewrite)
                    elif op.kind is EditKind.PIN:
                        desired.setdefault(resolved, (SpanState.ACTIVE, rewrite))
        return desired

    # -- operator pin transition -----------------------------------------
    def unpin(
        self,
        span_ids: str | Sequence[str] | None = None,
        *,
        span_id: str | None = None,
        expected_revision: int,
        txn_id: str,
        actor_origin: str,
    ) -> CommitResult:
        """Remove one or more pins.  Operator authority only.

        Additive extension: the integration contract has no unpin entry point,
        but the operator manual command needs one.  ``span_ids`` accepts either
        a single span id or a sequence; the ``span_id=`` keyword is an alias.
        Multiple targets are unpinned **atomically in exactly one revision**:
        the whole batch is validated and applied inside one write transaction
        (unknown span ids abort before any pin is removed).

        Agents may pin but never unpin; the authority check runs before any
        state is read or written.
        """

        self._require_writable()
        actor = ActorOrigin(actor_origin)
        if actor is not ActorOrigin.OPERATOR:
            raise PinAuthorityError(
                "only the operator may unpin", code="pin_authority", details={"actor": actor.value}
            )
        targets = _unpin_targets(span_ids, span_id)
        validate_txn_id(txn_id)
        expected = self._validate_revision(expected_revision)
        # Legacy single-target signature is preserved so transactions staged by
        # earlier builds still replay idempotently.
        request_sha = (
            canonical_sha256({"op": "unpin", "span_id": targets[0]})
            if len(targets) == 1
            else canonical_sha256({"op": "unpin", "span_ids": list(targets)})
        )
        with self._write() as cur:
            existing = self._txn_row(cur, txn_id)
            if existing is not None and str(existing["state"]) == "committed":
                if str(existing["staged_sha256"]) == request_sha:
                    return self._replay_commit(existing)
                raise IdempotencyConflictError(
                    "txn_id already used by a different transaction",
                    code="txn_payload_mismatch",
                    details={"txn_id": txn_id},
                )
            if existing is not None:
                raise IdempotencyConflictError(
                    "txn_id already used by a different transaction", code="txn_payload_mismatch"
                )
            head = self._head(cur)
            if expected != head:
                raise StaleRevisionError(
                    "expected_revision does not match the current head",
                    code="stale_revision",
                    details={"expected": expected, "current": head},
                )
            resolved: list[str] = []
            for requested in targets:
                resolved_id = self._resolve_span_id(cur, requested)
                if resolved_id is None:
                    raise NotFoundError(
                        "unknown span",
                        code="unknown_span",
                        details={"span_id": requested},
                    )
                if resolved_id not in resolved:
                    resolved.append(resolved_id)
            all_pins = {str(row[0]) for row in cur.execute("SELECT span_id FROM pins").fetchall()}
            pinned_rows = {span_id for span_id in resolved if span_id in all_pins}
            now = self._now()
            revision = head + 1
            head_row = self._head_row(cur)
            head_hash = revision_hash(
                prev_hash=str(head_row["head_hash"]),
                revision=revision,
                parent_revision=head,
                base_revision=head,
                kind="operator",
                actor_origin=ActorOrigin.OPERATOR.value,
                txn_id=txn_id,
                operations=[],
            )
            applied_receipts: list[Receipt] = []
            inert_receipts: list[Receipt] = []
            removed: list[str] = []
            for resolved_id in resolved:
                if resolved_id not in pinned_rows:
                    inert_receipts.append(
                        Receipt(
                            span_id=resolved_id, kind="unpin", effect="inert", reason="not_pinned"
                        )
                    )
                    continue
                cur.execute("DELETE FROM pins WHERE span_id = ?", (resolved_id,))
                cur.execute(
                    "INSERT INTO pin_events (span_id, action, actor_origin, revision, created_at) "
                    "VALUES (?, 'unpin', ?, ?, ?)",
                    (resolved_id, actor.value, revision, now),
                )
                applied_receipts.append(
                    Receipt(span_id=resolved_id, kind="unpin", effect="applied")
                )
                removed.append(resolved_id)
            cur.execute(
                "INSERT INTO revisions (revision, parent_revision, base_revision, kind, head_hash, txn_id, "
                "actor_origin, op_count, applied, inert, est_tokens_before, est_tokens_after, created_at) "
                "VALUES (?, ?, ?, 'operator', ?, ?, ?, 0, ?, ?, 0, 0, ?)",
                (
                    revision,
                    head,
                    head,
                    head_hash,
                    txn_id,
                    actor.value,
                    len(applied_receipts),
                    len(inert_receipts),
                    now,
                ),
            )
            counts = self._state_counts(cur)
            cur.execute(
                "INSERT INTO snapshots (revision, head_hash, active_span_count, dropped_span_count, "
                "offloaded_span_count, pinned_span_count, est_tokens, active_set_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision,
                    head_hash,
                    counts["active"],
                    counts["dropped"],
                    counts["offloaded"],
                    counts["pinned"],
                    self._est_active_tokens(cur),
                    sha256_hex(canonical_json([])),
                    now,
                ),
            )
            result = CommitResult(
                ok=True,
                revision=revision,
                applied=tuple(applied_receipts),
                inert=tuple(inert_receipts),
                errors=(),
                txn_id=txn_id,
                head_hash=head_hash,
                kind="operator",
                replayed=False,
                base_revision=head,
            )
            detail: dict[str, Any] = {
                "span_ids": list(resolved),
                "removed": removed,
                "not_pinned": [
                    receipt.span_id for receipt in inert_receipts if receipt.reason == "not_pinned"
                ],
                "txn_id": txn_id,
            }
            if len(resolved) == 1:
                detail["span_id"] = resolved[0]
            cur.execute(
                "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    revision,
                    "operator_unpin",
                    actor.value,
                    canonical_json(detail),
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO txns (txn_id, state, base_revision, staged_sha256, operations_json, actor_origin, "
                "created_at, updated_at, committed_revision, result_json) "
                "VALUES (?, 'committed', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    txn_id,
                    expected,
                    request_sha,
                    canonical_json({"op": "unpin", "span_ids": resolved}),
                    actor.value,
                    now,
                    now,
                    revision,
                    canonical_json(result.to_dict()),
                ),
            )
        self._invalidate()
        self._write_index()
        return result

    def operator_unpin(
        self,
        span_ids: str | Sequence[str] | None = None,
        *,
        span_id: str | None = None,
        expected_revision: int,
        txn_id: str,
    ) -> CommitResult:
        """Operator-only convenience wrapper around :meth:`unpin`.

        Accepts a single span id (`span_id=` or positional str) or a sequence
        (`span_ids=` or positional sequence); a batch is unpinned atomically in
        one revision.
        """

        return self.unpin(
            span_ids,
            span_id=span_id,
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=ActorOrigin.OPERATOR.value,
        )

    def operator_unpin_many(
        self, span_ids: Sequence[str], *, expected_revision: int, txn_id: str
    ) -> CommitResult:
        """Explicit batch form of :meth:`operator_unpin` (same single revision)."""

        return self.operator_unpin(span_ids, expected_revision=expected_revision, txn_id=txn_id)

    def pin_requires_operator(self, actor_origin: str) -> None:
        """Guard used by tool layers: agents cannot unpin, only operators."""

        if ActorOrigin(actor_origin) is not ActorOrigin.OPERATOR:
            raise PinAuthorityError(
                "only the operator may unpin", code="pin_authority", details={"actor": actor_origin}
            )

    # -- proposals --------------------------------------------------------
    def create_proposal(self, operations: Sequence[Any], *, base_revision: int) -> Proposal:
        """Persist an immutable aux compression proposal.

        Proposals never change the head; acceptance stages them and a normal
        ``commit`` is still required.
        """

        self._require_writable()
        ops, raw_payload = _coerce_aux_proposal(operations)
        if len(ops) > self._config.max_aux_spans:
            raise StoreBudgetError(
                "proposal touches too many spans",
                code="too_many_aux_spans",
                details={"count": len(ops), "max": self._config.max_aux_spans},
            )
        base = self._validate_revision(base_revision)
        with self._write() as cur:
            head = self._head(cur)
            if base > head:
                raise StaleProposalError(
                    "proposal base revision is ahead of the store",
                    code="stale_proposal",
                    details={"base": base, "head": head},
                )
            self._plan(cur, ops, state_aware=False)
            # Exact compatibility with hermes_clm.auxiliary.canonical_proposal_sha256:
            # sha256(canonical JSON of {base_revision, operations[raw payload], origin}).
            digest = canonical_sha256(
                {"base_revision": base, "operations": raw_payload, "origin": "aux"}
            )
            proposal_id = "prop_" + digest[:24]
            row = cur.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO proposals (proposal_id, proposal_sha256, base_revision, operations_json, "
                    "origin, created_revision, created_at) VALUES (?, ?, ?, ?, 'aux', ?, ?)",
                    (proposal_id, digest, base, canonical_json(raw_payload), head, self._now()),
                )
                cur.execute(
                    "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
                    "VALUES (?, 'proposal_created', 'aux', ?, ?)",
                    (
                        head,
                        canonical_json(
                            {"proposal_id": proposal_id, "base_revision": base, "spans": len(ops)}
                        ),
                        self._now(),
                    ),
                )
            return Proposal(
                proposal_id=proposal_id,
                proposal_sha256=digest,
                base_revision=base,
                operations=ops,
                origin="aux",
                created_revision=head,
            )

    def accept_proposal(
        self,
        *,
        proposal_id: str,
        proposal_sha256: str,
        base_revision: int,
        txn_id: str,
    ) -> StageResult:
        """Stage an accepted proposal; commit remains a separate step."""

        self._require_writable()
        validate_txn_id(txn_id)
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValidationError("proposal_id is required", code="invalid_proposal_id")
        if not isinstance(proposal_sha256, str) or len(proposal_sha256) != 64:
            raise ValidationError(
                "proposal_sha256 must be a 64-char digest", code="invalid_proposal_hash"
            )
        base = self._validate_revision(base_revision)
        with self._write() as cur:
            row = cur.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "unknown proposal",
                    code="unknown_proposal",
                    details={"proposal_id": proposal_id},
                )
            if str(row["proposal_sha256"]) != proposal_sha256:
                raise StaleProposalError(
                    "proposal hash does not match; re-read the proposal",
                    code="proposal_hash_mismatch",
                    details={"proposal_id": proposal_id},
                )
            if int(row["base_revision"]) != base:
                raise StaleProposalError(
                    "proposal base_revision does not match the request",
                    code="proposal_base_mismatch",
                    details={"proposal_base": int(row["base_revision"]), "requested": base},
                )
            head = self._head(cur)
            if base != head:
                raise StaleProposalError(
                    "proposal is stale for the current head",
                    code="stale_proposal",
                    details={"base": base, "head": head},
                )
            stored = json.loads(row["operations_json"])
            accepted: list[EditOperation] = []
            for raw in stored:
                source = EditOperation.from_mapping(raw)
                provenance = dict(source.provenance)
                provenance.update(
                    {
                        "origin": "aux",
                        "proposal_id": proposal_id,
                        "proposal_sha256": proposal_sha256,
                    }
                )
                accepted.append(
                    EditOperation(
                        kind=source.kind,
                        span_ids=source.span_ids,
                        replacement=source.replacement,
                        actor_origin=ActorOrigin.AGENT,
                        provenance=provenance,
                    )
                )
            result = self._stage_locked(
                cur,
                tuple(accepted),
                expected_revision=head,
                txn_id=txn_id,
                actor_origin=ActorOrigin.AGENT,
            )
            cur.execute(
                "UPDATE proposals SET accepted_txn_id = ?, accepted_at = ? WHERE proposal_id = ?",
                (txn_id, self._now(), proposal_id),
            )
        return result

    # -- read surfaces ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Bounded health/state summary (JSON-serializable)."""

        self._require_open()
        with self._lock:
            cur = self._conn.cursor()
            try:
                rev_row = self._head_row(cur)
                counts = self._state_counts(cur)
                health: dict[str, Any] = {
                    "ok": True,
                    "hash_chain": {
                        "ok": True,
                        "checked": "head",
                        "revision": int(rev_row["revision"]),
                        "head_hash": str(rev_row["head_hash"]),
                    },
                }
                try:
                    self._verify_head(cur, dict(rev_row))
                except CorruptStoreError as exc:
                    health = {
                        "ok": False,
                        "hash_chain": {
                            "ok": False,
                            "checked": "head",
                            "revision": int(rev_row["revision"]),
                            "head_hash": str(rev_row["head_hash"]),
                        },
                        "error": exc.to_dict(),
                    }
                staged = cur.execute(
                    "SELECT txn_id, base_revision, created_at FROM txns WHERE state = 'staged' "
                    "ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
                last_audit = cur.execute(
                    "SELECT event, revision, created_at FROM audit ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                revision = int(rev_row["revision"])
                operations = int(cur.execute("SELECT COUNT(*) FROM operations").fetchone()[0])
                archive_count = int(cur.execute("SELECT COUNT(*) FROM archive").fetchone()[0])
                spans_total = int(cur.execute("SELECT COUNT(*) FROM spans").fetchone()[0])
            finally:
                cur.close()
            index = self._index_summary()
            tokens_cur = self._conn.cursor()
            try:
                est_tokens = self._est_active_tokens(tokens_cur)
            finally:
                tokens_cur.close()
            return {
                "ok": True,
                "session_id": self._session_id,
                "db_id": self._db_id,
                "store_key": self._dir.name,
                "revision": revision,
                "head_hash": str(rev_row["head_hash"]),
                "revision_kind": str(rev_row["kind"]),
                "counts": counts,
                "est_tokens": est_tokens,
                "estimator": TOKEN_ESTIMATOR,
                "spans_archived": spans_total,
                "archive_entries": archive_count,
                "operations_committed": operations,
                "staged_transactions": [
                    {
                        "txn_id": str(r["txn_id"]),
                        "base_revision": int(r["base_revision"]),
                        "created_at": str(r["created_at"]),
                    }
                    for r in staged
                ],
                "last_audit": (
                    {
                        "event": str(last_audit["event"]),
                        "revision": int(last_audit["revision"]),
                        "created_at": str(last_audit["created_at"]),
                    }
                    if last_audit
                    else None
                ),
                "health": health,
                "index": index,
                "read_only": self._read_only,
                "redaction": {"enabled": self._config.archive_redact},
                "limits": {
                    "max_retrieve_bytes": self._config.max_retrieve_bytes,
                    "max_rewrite_bytes": self._config.max_rewrite_bytes,
                    "max_aux_spans": self._config.max_aux_spans,
                    "max_operations_per_txn": self._config.max_operations_per_txn,
                },
            }

    def inspect(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Bounded, session-scoped view of current spans (never cross-session)."""

        self._require_open()
        after = _decode_cursor(cursor)
        n = _clamp_limit(limit, maximum=self._config.inspect_limit_max, default=50)
        cur = self._conn.cursor()
        try:
            rows = cur.execute(
                "SELECT s.span_id AS span_id, s.uid AS uid, s.origin AS origin, s.role AS role, "
                "s.group_id AS group_id, s.protected AS protected, s.seq AS seq, s.version AS version, "
                "s.message_json AS message_json, cs.present AS present, st.state AS state, "
                "st.rewrite_text AS rewrite_text, "
                "EXISTS(SELECT 1 FROM pins p WHERE p.span_id = s.span_id) AS pinned "
                "FROM spans s JOIN current_spans cs ON cs.span_id = s.span_id "
                "LEFT JOIN span_state st ON st.span_id = s.span_id "
                "WHERE s.seq > ? ORDER BY s.seq LIMIT ?",
                (after, n + 1),
            ).fetchall()
        finally:
            cur.close()
        items = []
        for row in rows[:n]:
            text = row["rewrite_text"] or row["message_json"]
            items.append(
                {
                    "span_id": str(row["span_id"]),
                    "uid": str(row["uid"]),
                    "origin": str(row["origin"]),
                    "role": str(row["role"]),
                    "group_id": row["group_id"],
                    "protected": bool(row["protected"]),
                    "pinned": bool(row["pinned"]),
                    "state": str(row["state"] or SpanState.ACTIVE.value),
                    "present": bool(row["present"]),
                    "seq": int(row["seq"]),
                    "version": int(row["version"]),
                    "est_tokens": _tokens_for_text(text),
                    "preview": _preview(
                        text if row["rewrite_text"] else _message_text(row["message_json"])
                    ),
                }
            )
        next_cursor = _encode_cursor(int(str(items[-1]["seq"]))) if len(rows) > n else None
        return {
            "ok": True,
            "session_id": self._session_id,
            "revision": self._revision_read(),
            "items": items,
            "next_cursor": next_cursor,
            "limit": n,
        }

    def search(self, query: str, *, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Substring search over this session's archive only."""

        self._require_open()
        if not isinstance(query, str) or not query.strip():
            raise ValidationError("query must be a non-empty string", code="invalid_query")
        if len(query) > self._config.max_query_chars:
            raise ValidationError(
                f"query longer than {self._config.max_query_chars} characters", code="invalid_query"
            )
        after = _decode_cursor(cursor)
        n = _clamp_limit(limit, maximum=self._config.search_limit_max, default=20)
        needle = query.strip().lower()
        cur = self._conn.cursor()
        try:
            rows = cur.execute(
                "SELECT s.span_id AS span_id, s.uid AS uid, s.origin AS origin, s.seq AS seq, "
                "s.message_json AS message_json, st.state AS state, st.rewrite_text AS rewrite_text "
                "FROM spans s JOIN current_spans cs ON cs.span_id = s.span_id "
                "LEFT JOIN span_state st ON st.span_id = s.span_id "
                "WHERE s.seq > ? AND (instr(lower(s.message_json), ?) > 0 "
                "OR instr(lower(COALESCE(st.rewrite_text, '')), ?) > 0) "
                "ORDER BY s.seq LIMIT ?",
                (after, needle, needle, n + 1),
            ).fetchall()
        finally:
            cur.close()
        items = []
        for row in rows[:n]:
            haystack = (
                row["rewrite_text"]
                or _message_text(row["message_json"])
                or str(row["message_json"])
            )
            lowered = haystack.lower()
            index = lowered.find(needle)
            start = max(0, index - 60)
            snippet = haystack[start : start + 200]
            items.append(
                {
                    "span_id": str(row["span_id"]),
                    "uid": str(row["uid"]),
                    "origin": str(row["origin"]),
                    "state": str(row["state"] or SpanState.ACTIVE.value),
                    "seq": int(row["seq"]),
                    "snippet": _preview(snippet, 200),
                }
            )
        next_cursor = _encode_cursor(int(str(items[-1]["seq"]))) if len(rows) > n else None
        return {
            "ok": True,
            "session_id": self._session_id,
            "query": query,
            "items": items,
            "next_cursor": next_cursor,
            "limit": n,
        }

    def retrieve(self, span_id: str) -> dict[str, Any]:
        """Exact archived detail for one span of **this** session only."""

        self._require_open()
        validate_span_id(span_id)
        cur = self._conn.cursor()
        try:
            resolved = self._resolve_span_id(cur, span_id)
            if resolved is None:
                raise NotFoundError(
                    "unknown span for this session",
                    code="unknown_span",
                    details={"span_id": span_id},
                )
            row = cur.execute(
                "SELECT s.*, cs.present AS present, st.state AS state, st.rewrite_text AS rewrite_text, "
                "st.rewrite_sha256 AS rewrite_sha256, "
                "EXISTS(SELECT 1 FROM pins p WHERE p.span_id = s.span_id) AS pinned "
                "FROM spans s LEFT JOIN current_spans cs ON cs.span_id = s.span_id "
                "LEFT JOIN span_state st ON st.span_id = s.span_id WHERE s.span_id = ?",
                (resolved,),
            ).fetchone()
            if row is None:
                raise NotFoundError("unknown span for this session", code="unknown_span")
            self._verify_payload(row)
            payload_bytes = len(str(row["message_json"]).encode("utf-8"))
            if payload_bytes > self._config.max_retrieve_bytes:
                return {
                    "ok": False,
                    "session_id": self._session_id,
                    "span_id": resolved,
                    "bytes": payload_bytes,
                    "limit": self._config.max_retrieve_bytes,
                    "errors": [
                        {
                            "code": "retrieve_too_large",
                            "message": "archived payload exceeds clm.max_retrieve_bytes",
                            "details": {
                                "bytes": payload_bytes,
                                "limit": self._config.max_retrieve_bytes,
                            },
                        }
                    ],
                }
            span = self._span_from_row(row, pinned=bool(row["pinned"]), current=True)
            return {
                "ok": True,
                "session_id": self._session_id,
                "span_id": span.span_id,
                "requested_span_id": span_id,
                "uid": span.uid,
                "origin": span.origin.value,
                "role": span.role,
                "protected": span.protected,
                "state": span.state.value,
                "group_id": span.group_id,
                "pinned": span.pinned,
                "present": span.present,
                "content_sha256": span.content_sha256,
                "rewrite_sha256": span.rewrite_sha256,
                "bytes": payload_bytes,
                "est_tokens": span.est_tokens,
                "message": copy.deepcopy(dict(span.message)),
                "rewrite_text": span.rewrite_text,
                "stub": span.stub() if span.state is SpanState.OFFLOADED else None,
            }
        finally:
            cur.close()

    def _revision_read(self) -> int:
        row = self._conn.execute("SELECT MAX(revision) FROM revisions").fetchone()
        return int(row[0] or 0)

    # -- integrity / recovery primitives ---------------------------------
    def verify(self) -> dict[str, Any]:
        """Full integrity pass: payload hashes, archive chain, revision chain.

        Returns a report; never mutates.  ``ok`` is ``False`` when anything is
        structurally suspect, in which case callers must not project.
        """

        self._require_open()
        cur = self._conn.cursor()
        problems: list[dict[str, Any]] = []
        corrupt_spans: list[str] = []
        try:
            integrity = cur.execute("PRAGMA integrity_check").fetchone()
            pragma_ok = integrity is not None and str(integrity[0]) == "ok"
            if not pragma_ok:
                problems.append(
                    {"code": "integrity_check", "detail": integrity[0] if integrity else None}
                )
            rows = [
                dict(row)
                for row in cur.execute(
                    "SELECT span_id, uid, content_sha256, payload_json, prev_hash, entry_hash, "
                    "archive_id, revision FROM archive ORDER BY archive_id"
                )
            ]
            from .archive import verify_chain

            chain = verify_chain(rows)
            if not chain["ok"]:
                problems.extend(chain["problems"][:20])
            for row in cur.execute("SELECT span_id, content_sha256, message_json FROM spans"):
                computed = sha256_hex(canonical_json(json.loads(row["message_json"])))
                if computed != row["content_sha256"]:
                    corrupt_spans.append(str(row["span_id"]))
                    problems.append(
                        {
                            "code": "payload_hash_mismatch",
                            "span_id": str(row["span_id"]),
                            "expected": str(row["content_sha256"]),
                            "computed": computed,
                        }
                    )
            revision_rows = [
                dict(r) for r in cur.execute("SELECT * FROM revisions ORDER BY revision")
            ]
            prev_hash = GENESIS_HASH
            for rev in revision_rows:
                ops = [
                    _op_payload_from_row(r)
                    for r in cur.execute(
                        "SELECT * FROM operations WHERE revision = ? ORDER BY ord",
                        (rev["revision"],),
                    )
                ]
                computed = revision_hash(
                    prev_hash=prev_hash,
                    revision=int(rev["revision"]),
                    parent_revision=int(rev["parent_revision"]),
                    base_revision=int(rev["base_revision"]),
                    kind=str(rev["kind"]),
                    actor_origin=str(rev["actor_origin"]),
                    txn_id=rev["txn_id"],
                    operations=ops,
                )
                if computed != rev["head_hash"]:
                    problems.append(
                        {
                            "code": "revision_hash_mismatch",
                            "revision": int(rev["revision"]),
                            "expected": rev["head_hash"],
                            "computed": computed,
                        }
                    )
                prev_hash = str(rev["head_hash"])
            state_problems = self._verify_state_consistency(cur)
            problems.extend(state_problems)
        finally:
            cur.close()
        return {
            "ok": not problems and not corrupt_spans,
            "session_id": self._session_id,
            "revision": self._revision_read(),
            "corrupt_spans": corrupt_spans,
            "problems": problems,
            "archive_entries": len(rows),
        }

    def verify_chain(self) -> dict[str, Any]:
        """Bounded hash-chain verification (revision chain + archive chain).

        Unlike :meth:`verify`, this does not re-hash every archived payload or
        compare the derived checkpoint; it is the cheap integrity signal exposed
        by ``status()["health"]["hash_chain"]`` (which checks only the head) and
        by operator health tooling.
        """

        self._require_open()
        cur = self._conn.cursor()
        problems: list[dict[str, Any]] = []
        archive_entries = 0
        revisions_checked = 0
        try:
            head_row = self._head_row(cur)
            revision = int(head_row["revision"])
            head_hash = str(head_row["head_hash"])
            rows = [
                dict(row)
                for row in cur.execute(
                    "SELECT span_id, uid, content_sha256, payload_json, prev_hash, entry_hash, "
                    "archive_id, revision FROM archive ORDER BY archive_id"
                )
            ]
            archive_entries = len(rows)
            from .archive import verify_chain

            chain = verify_chain(rows)
            if not chain["ok"]:
                problems.extend(chain["problems"][:20])
            prev_hash = GENESIS_HASH
            revision_rows = cur.execute("SELECT * FROM revisions ORDER BY revision").fetchall()
            for rev in revision_rows:
                ops = [
                    _op_payload_from_row(r)
                    for r in cur.execute(
                        "SELECT * FROM operations WHERE revision = ? ORDER BY ord",
                        (rev["revision"],),
                    )
                ]
                computed = revision_hash(
                    prev_hash=prev_hash,
                    revision=int(rev["revision"]),
                    parent_revision=int(rev["parent_revision"]),
                    base_revision=int(rev["base_revision"]),
                    kind=str(rev["kind"]),
                    actor_origin=str(rev["actor_origin"]),
                    txn_id=rev["txn_id"],
                    operations=ops,
                )
                if computed != rev["head_hash"]:
                    problems.append(
                        {
                            "code": "revision_hash_mismatch",
                            "revision": int(rev["revision"]),
                            "expected": rev["head_hash"],
                            "computed": computed,
                        }
                    )
                prev_hash = str(rev["head_hash"])
                revisions_checked += 1
        finally:
            cur.close()
        return {
            "ok": not problems,
            "session_id": self._session_id,
            "revision": revision,
            "head_hash": head_hash,
            "revisions_checked": revisions_checked,
            "archive_entries": archive_entries,
            "problems": problems,
        }

    def _verify_state_consistency_from_scratch(self) -> list[dict[str, Any]]:
        """Consistency check against the replayed chain (used by recovery)."""

        cur = self._conn.cursor()
        try:
            return self._verify_state_consistency(cur)
        finally:
            cur.close()

    def expire_staged_transactions(self, *, ttl_seconds: int | None = None) -> tuple[str, ...]:
        """Discard staged transactions orphaned by a crash.

        ``ttl_seconds=0`` expires every currently staged transaction (used by
        recovery tests and explicit operator cleanup); a negative value skips
        expiry entirely.
        """

        self._require_writable()
        ttl = self._config.stale_txn_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if ttl < 0:
            return ()
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        stale: list[str] = []
        with self._write() as cur:
            rows = cur.execute(
                "SELECT txn_id, created_at FROM txns WHERE state = 'staged'"
            ).fetchall()
            now = self._now()
            head = self._head(cur)
            for row in rows:
                if str(row["created_at"]) > cutoff:
                    continue
                txn_id = str(row["txn_id"])
                cur.execute(
                    "UPDATE txns SET state = 'discarded', updated_at = ? WHERE txn_id = ? AND state = 'staged'",
                    (now, txn_id),
                )
                cur.execute(
                    "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
                    "VALUES (?, 'expire_staged', 'system', ?, ?)",
                    (head, canonical_json({"txn_id": txn_id, "ttl_seconds": ttl}), now),
                )
                stale.append(txn_id)
        return tuple(stale)

    def _verify_state_consistency(self, cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        problems: list[dict[str, Any]] = []
        head = self._head(cur)
        desired = self._replay_states(cur, head)
        # Compare only live versions; superseded span rows keep their (unused)
        # state rows as part of the append-only history.
        actual = {
            str(row["span_id"]): (SpanState(row["state"]), row["rewrite_text"])
            for row in cur.execute(
                "SELECT st.span_id AS span_id, st.state AS state, st.rewrite_text AS rewrite_text "
                "FROM span_state st WHERE st.span_id IN (SELECT span_id FROM current_spans)"
            )
        }
        if set(desired) != set(actual):
            missing = sorted(set(desired) - set(actual))[:20]
            extra = sorted(set(actual) - set(desired))[:20]
            problems.append({"code": "span_state_mismatch", "missing": missing, "extra": extra})
        else:
            for span_id, want in desired.items():
                if actual[span_id] != want:
                    problems.append(
                        {
                            "code": "span_state_divergence",
                            "span_id": span_id,
                            "expected": [want[0].value, want[1]],
                            "found": [actual[span_id][0].value, actual[span_id][1]],
                        }
                    )
                    if len(problems) > 20:
                        break
        return problems

    def rebuild_state(self) -> dict[str, Any]:
        """Recompute ``span_state`` from the committed operation chain."""

        self._require_writable()
        with self._write() as cur:
            head = self._head(cur)
            desired = self._replay_states(cur, head)
            changed = 0
            for span_id, (state, rewrite) in desired.items():
                row = cur.execute(
                    "SELECT state, rewrite_text FROM span_state WHERE span_id = ?", (span_id,)
                ).fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO span_state (span_id, state, rewrite_text, rewrite_sha256, updated_revision) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            span_id,
                            state.value,
                            rewrite,
                            sha256_hex(rewrite) if rewrite else None,
                            head,
                        ),
                    )
                    changed += 1
                    continue
                if (str(row["state"]), row["rewrite_text"]) != (state.value, rewrite):
                    self._set_state(cur, span_id, state, rewrite, head)
                    changed += 1
            cur.execute(
                "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
                "VALUES (?, 'state_rebuilt', 'system', ?, ?)",
                (head, canonical_json({"changed": changed}), self._now()),
            )
        self._invalidate()
        return {"changed": changed, "revision": head}

    # -- derived index ----------------------------------------------------
    def _index_payload(self) -> dict[str, Any]:
        cur = self._conn.cursor()
        try:
            rev_row = self._head_row(cur)
            spans = cur.execute(
                "SELECT span_id, content_sha256, seq FROM spans ORDER BY seq"
            ).fetchall()
            operations = cur.execute(
                "SELECT revision, ord, kind FROM operations ORDER BY revision, ord"
            ).fetchall()
            revisions = cur.execute(
                "SELECT revision, head_hash FROM revisions ORDER BY revision"
            ).fetchall()
            counts = self._state_counts(cur)
        finally:
            cur.close()
        row_hashes = {
            "spans": sha256_hex(
                canonical_json([[str(r["span_id"]), str(r["content_sha256"])] for r in spans])
            ),
            "operations": sha256_hex(
                canonical_json(
                    [[int(r["revision"]), int(r["ord"]), str(r["kind"])] for r in operations]
                )
            ),
            "revisions": sha256_hex(
                canonical_json([[int(r["revision"]), str(r["head_hash"])] for r in revisions])
            ),
        }
        return {
            "format": INDEX_FORMAT_VERSION,
            "db_id": self._db_id,
            "session_id": self._session_id,
            "revision": int(rev_row["revision"]),
            "head_hash": str(rev_row["head_hash"]),
            "counts": counts,
            "row_hashes": row_hashes,
            "rows": {
                "spans": len(spans),
                "operations": len(operations),
                "revisions": len(revisions),
            },
            "generated_at": self._now(),
        }

    def _write_index(self) -> None:
        if self._read_only:
            return
        path = self._dir / INDEX_FILENAME
        try:
            payload = self._index_payload()
            tmp = self._dir / f".{INDEX_FILENAME}.{os.getpid()}.tmp"
            data = canonical_json(payload).encode("utf-8")
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
            self.index_state = "ok"
            self.index_error = None
        except (OSError, CLMError, sqlite3.Error) as exc:  # index is derived: never fatal
            self.index_state = "failed"
            self.index_error = str(exc)[:200]

    def _read_index(self) -> dict[str, Any] | None:
        path = self._dir / INDEX_FILENAME
        try:
            raw = path.read_bytes()
        except (OSError, FileNotFoundError):
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("format") != INDEX_FORMAT_VERSION:
            return None
        if payload.get("db_id") != self._db_id or payload.get("session_id") != self._session_id:
            return None
        return payload

    def _index_summary(self) -> dict[str, Any]:
        payload = self._read_index()
        if payload is None:
            return {"state": "missing_or_corrupt", "authoritative": False}
        stale = int(payload.get("revision", -1)) != self._revision_read()
        return {
            "state": "stale" if stale else "ok",
            "authoritative": False,
            "revision": int(payload.get("revision", -1)),
            "generated_at": payload.get("generated_at"),
        }

    def index_status(self) -> dict[str, Any]:
        """Report derived-index health; rebuilds when corrupt/stale."""

        self._require_open()
        payload = self._read_index()
        state = "ok"
        if payload is None:
            state = "rebuilt"
            if not self._read_only:
                self._write_index()
        elif int(payload.get("revision", -1)) != self._revision_read():
            state = "rebuilt"
            if not self._read_only:
                self._write_index()
        return {"state": state, "authoritative": False, "revision": self._revision_read()}

    def rebuild_index(self) -> dict[str, Any]:
        self._require_open()
        if self._read_only:
            raise ReadOnlyStoreError("cannot rebuild index on a read-only handle")
        self._write_index()
        return {"ok": self.index_state == "ok", "state": self.index_state}

    # -- revision validation ---------------------------------------------
    @staticmethod
    def _validate_revision(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("revision must be an int", code="invalid_revision")
        if value < 0:
            raise ValidationError("revision must be >= 0", code="invalid_revision")
        return value

    # -- maintenance ------------------------------------------------------
    @classmethod
    def list_sessions(cls, hermes_home: str | os.PathLike[str]) -> list[dict[str, Any]]:
        """Enumerate session stores (operator tooling: retention/purge).

        Entries include a best-effort ``pinned_count`` and ``revision`` read
        read-only from each SQLite database.  Unreadable stores are still listed
        with ``ok=False`` so retention tooling can refuse instead of guessing.
        """

        root = sessions_root(hermes_home)
        if not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.is_symlink():
                continue
            db = entry / DB_FILENAME
            if not db.is_file():
                continue
            meta = _read_session_meta(db)
            out.append(
                {
                    "store_key": entry.name,
                    "db_bytes": db.stat().st_size,
                    "modified": int(db.stat().st_mtime),
                    "session_id": meta["session_id"],
                    "revision": meta["revision"],
                    "pinned_count": meta["pinned_count"],
                    "ok": meta["ok"],
                }
            )
        return out

    @classmethod
    def purge_expired_sessions(
        cls,
        hermes_home: str | os.PathLike[str],
        *,
        keep: int | None = None,
        retention_sessions: int = 32,
        current_session_id: str | None = None,
        include_pinned: bool = False,
        include_current: bool = False,
        max_purge: int = 64,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Purge the oldest session stores beyond a bounded retention window.

        Exact semantics (deliberate, non-destructive by default):

        * ``keep`` (default: ``retention_sessions``, matching
          ``clm.retention_sessions``) is the number of **newest** sessions by
          database mtime that are retained.  ``keep=0`` makes every otherwise
          eligible session a candidate.
        * Sessions with any pin rows are **never** purged unless
          ``include_pinned=True`` is passed explicitly.  Pinned sessions are
          exempt even when they fall outside the retention window.
        * ``current_session_id`` is never purged unless ``include_current=True``
          is passed explicitly.
        * Unreadable session ids are listed but never purged (the store cannot
          verify what it would delete).
        * At most ``max_purge`` sessions are removed per call (bounded sweep);
          leftovers are reported with ``reason="purge_limit"``.
        * ``dry_run=True`` reports the same decision set without deleting.

        This is operator tooling only: the store never expires sessions on its
        own, and no engine/tool path calls it.
        """

        if isinstance(keep, bool) or (keep is not None and not isinstance(keep, int)):
            raise ValidationError("keep must be an int", code="invalid_retention")
        retain = retention_sessions if keep is None else keep
        if isinstance(retain, bool) or not isinstance(retain, int) or retain < 0:
            raise ValidationError(
                "retention_sessions must be a non-negative int", code="invalid_retention"
            )
        if isinstance(max_purge, bool) or not isinstance(max_purge, int) or max_purge < 1:
            raise ValidationError("max_purge must be a positive int", code="invalid_retention")
        max_purge = min(max_purge, 1000)
        if current_session_id is not None:
            current_session_id = _validate_session_id(current_session_id)

        sessions = cls.list_sessions(hermes_home)
        ordered = sorted(sessions, key=lambda item: (-int(item["modified"]), item["store_key"]))
        purged: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for index, item in enumerate(ordered):
            session_id = item["session_id"]
            if index < retain:
                continue
            if session_id is None or not item["ok"]:
                skipped.append(
                    {"store_key": item["store_key"], "session_id": None, "reason": "unreadable"}
                )
                continue
            if current_session_id is not None and session_id == current_session_id:
                if not include_current:
                    skipped.append(
                        {
                            "store_key": item["store_key"],
                            "session_id": session_id,
                            "reason": "current",
                        }
                    )
                    continue
            if int(item["pinned_count"] or 0) > 0 and not include_pinned:
                skipped.append(
                    {"store_key": item["store_key"], "session_id": session_id, "reason": "pinned"}
                )
                continue
            if len(purged) >= max_purge:
                skipped.append(
                    {
                        "store_key": item["store_key"],
                        "session_id": session_id,
                        "reason": "purge_limit",
                    }
                )
                continue
            if dry_run:
                removed = True
            else:
                try:
                    removed = bool(cls.purge_session(hermes_home, session_id)["removed"])
                except Exception as exc:  # report, never half-sweep silently
                    skipped.append(
                        {
                            "store_key": item["store_key"],
                            "session_id": session_id,
                            "reason": "error",
                            "error": type(exc).__name__,
                        }
                    )
                    continue
            purged.append(
                {
                    "store_key": item["store_key"],
                    "session_id": session_id,
                    "removed": removed,
                    "pinned_count": int(item["pinned_count"] or 0),
                }
            )
        return {
            "ok": True,
            "scanned": len(ordered),
            "retained": retain,
            "include_pinned": bool(include_pinned),
            "include_current": bool(include_current),
            "max_purge": max_purge,
            "dry_run": bool(dry_run),
            "purged": purged,
            "purged_count": len(purged),
            "skipped": skipped,
            "limit_hit": any(item["reason"] == "purge_limit" for item in skipped),
        }

    @classmethod
    def purge_session(cls, hermes_home: str | os.PathLike[str], session_id: str) -> dict[str, Any]:
        """Operator purge: remove one session store directory entirely."""

        import shutil

        sid = _validate_session_id(session_id)
        directory = session_dir(hermes_home, sid)
        if not directory.exists():
            return {"ok": True, "removed": False, "store_key": session_key(sid)}
        lock_path = directory / LOCK_FILENAME
        fd = _acquire_writer_lock(lock_path)
        try:
            shutil.rmtree(directory)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return {"ok": True, "removed": True, "store_key": session_key(sid)}


def _message_text(message_json: str) -> str:
    try:
        message = json.loads(message_json)
    except (ValueError, TypeError):
        return ""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping) and part.get("type") in {"text", "input_text"}:
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return ""


def _read_session_meta(db_path: Path) -> dict[str, Any]:
    """Best-effort read of session id, head revision and pinned count."""

    out: dict[str, Any] = {
        "session_id": None,
        "revision": None,
        "pinned_count": None,
        "ok": False,
    }
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return out
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'session_id'").fetchone()
        out["session_id"] = None if row is None else str(row[0])
        revision = conn.execute("SELECT MAX(revision) FROM revisions").fetchone()
        out["revision"] = None if revision is None else int(revision[0] or 0)
        pins = conn.execute("SELECT COUNT(*) FROM pins").fetchone()
        out["pinned_count"] = 0 if pins is None else int(pins[0] or 0)
        out["ok"] = out["session_id"] is not None
        return out
    except (sqlite3.Error, TypeError, ValueError):
        return out
    finally:
        conn.close()
