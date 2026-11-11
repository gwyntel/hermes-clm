"""SQLite schema for the per-session CLM store.

Design notes (authoritative copy in ``docs/store-format.md``):

* ``STRICT`` tables plus ``CHECK`` constraints reject type drift.
* Payload tables are **append-only**: triggers abort every ``UPDATE``/``DELETE``
  on spans, archive entries, operations, revisions, audit, tokens and
  snapshots, so a bug or a hostile tool cannot rewrite history.
* ``span_state`` is a *derived* checkpoint (rebuildable from ``operations``),
  and triggers refuse to move a protected span off ``active`` or to attach a
  rewrite to it.
* ``pins`` is mutable only through operator unpin; ``UPDATE`` is aborted.
* ``meta``/``current_spans``/``txns``/``proposals`` are the only other mutable
  tables, each with a narrow trigger-enforced transition.
"""

from __future__ import annotations

import sqlite3

from .errors import CorruptStoreError

__all__ = [
    "SCHEMA_VERSION",
    "TABLES",
    "APPEND_ONLY_TABLES",
    "DDL",
    "INDEXES",
    "TRIGGERS",
    "create_schema",
    "schema_version",
    "require_schema",
    "table_names",
]

SCHEMA_VERSION = 1

APPEND_ONLY_TABLES = (
    "spans",
    "span_aliases",
    "archive",
    "operations",
    "revisions",
    "audit",
    "tokens",
    "snapshots",
    "pin_events",
)

TABLES = (
    "meta",
    "spans",
    "span_aliases",
    "current_spans",
    "span_state",
    "revisions",
    "operations",
    "txns",
    "pins",
    "pin_events",
    "archive",
    "audit",
    "tokens",
    "snapshots",
    "proposals",
)

DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS spans (
        span_id          TEXT PRIMARY KEY,
        uid              TEXT NOT NULL,
        version          INTEGER NOT NULL CHECK (version >= 1),
        content_sha256   TEXT NOT NULL,
        message_json     TEXT NOT NULL,
        origin           TEXT NOT NULL,
        role             TEXT NOT NULL,
        group_id         TEXT,
        protected        INTEGER NOT NULL CHECK (protected IN (0, 1)),
        editable         INTEGER NOT NULL CHECK (editable IN (0, 1)),
        multimodal       INTEGER NOT NULL CHECK (multimodal IN (0, 1)),
        seq              INTEGER NOT NULL,
        created_revision INTEGER NOT NULL,
        created_at       TEXT NOT NULL,
        UNIQUE (uid, content_sha256)
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_spans_uid_version ON spans (uid, version)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_spans_seq ON spans (seq)
    """,
    """
    CREATE TABLE IF NOT EXISTS span_aliases (
        alias            TEXT PRIMARY KEY,
        span_id          TEXT NOT NULL REFERENCES spans (span_id),
        reason           TEXT NOT NULL,
        created_revision INTEGER NOT NULL,
        created_at       TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS current_spans (
        uid              TEXT PRIMARY KEY,
        span_id          TEXT NOT NULL REFERENCES spans (span_id),
        present          INTEGER NOT NULL CHECK (present IN (0, 1)),
        last_seen_seq    INTEGER NOT NULL,
        updated_revision INTEGER NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS span_state (
        span_id          TEXT PRIMARY KEY REFERENCES spans (span_id),
        state            TEXT NOT NULL CHECK (state IN ('active', 'dropped', 'offloaded')),
        rewrite_text     TEXT,
        rewrite_sha256   TEXT,
        updated_revision INTEGER NOT NULL,
        CHECK ((rewrite_text IS NULL) = (rewrite_sha256 IS NULL))
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS revisions (
        revision           INTEGER PRIMARY KEY,
        parent_revision    INTEGER NOT NULL,
        base_revision      INTEGER NOT NULL,
        kind               TEXT NOT NULL CHECK (kind IN ('init', 'edit', 'revert', 'boundary', 'ingest', 'operator')),
        head_hash          TEXT NOT NULL,
        txn_id             TEXT,
        actor_origin       TEXT NOT NULL,
        op_count           INTEGER NOT NULL CHECK (op_count >= 0),
        applied            INTEGER NOT NULL CHECK (applied >= 0),
        inert              INTEGER NOT NULL CHECK (inert >= 0),
        est_tokens_before  INTEGER NOT NULL CHECK (est_tokens_before >= 0),
        est_tokens_after   INTEGER NOT NULL CHECK (est_tokens_after >= 0),
        created_at         TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS operations (
        revision        INTEGER NOT NULL REFERENCES revisions (revision),
        ord             INTEGER NOT NULL,
        kind            TEXT NOT NULL,
        span_ids_json   TEXT NOT NULL,
        replacement     TEXT,
        actor_origin    TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        txn_id          TEXT,
        PRIMARY KEY (revision, ord)
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS txns (
        txn_id             TEXT PRIMARY KEY,
        state              TEXT NOT NULL CHECK (state IN ('staged', 'committed', 'discarded')),
        base_revision      INTEGER NOT NULL,
        staged_sha256      TEXT NOT NULL,
        operations_json    TEXT NOT NULL,
        actor_origin       TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        committed_revision INTEGER,
        result_json        TEXT
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS pins (
        span_id      TEXT PRIMARY KEY REFERENCES spans (span_id),
        actor_origin TEXT NOT NULL,
        revision     INTEGER NOT NULL,
        pinned_at    TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS pin_events (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        span_id      TEXT NOT NULL,
        action       TEXT NOT NULL CHECK (action IN ('pin', 'unpin')),
        actor_origin TEXT NOT NULL,
        revision     INTEGER NOT NULL,
        created_at   TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS archive (
        archive_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        span_id         TEXT NOT NULL REFERENCES spans (span_id),
        uid             TEXT NOT NULL,
        content_sha256  TEXT NOT NULL,
        payload_json    TEXT NOT NULL,
        redacted        INTEGER NOT NULL CHECK (redacted IN (0, 1)),
        redaction_count INTEGER NOT NULL CHECK (redaction_count >= 0),
        prev_hash       TEXT NOT NULL,
        entry_hash      TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        created_at      TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_archive_span ON archive (span_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        revision     INTEGER NOT NULL,
        event        TEXT NOT NULL,
        actor_origin TEXT NOT NULL,
        detail_json  TEXT NOT NULL,
        created_at   TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_audit_revision ON audit (revision)
    """,
    """
    CREATE TABLE IF NOT EXISTS tokens (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        revision    INTEGER NOT NULL,
        span_id     TEXT,
        before_est  INTEGER NOT NULL,
        after_est   INTEGER NOT NULL,
        delta       INTEGER NOT NULL,
        estimator   TEXT NOT NULL,
        detail_json TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_tokens_revision ON tokens (revision)
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        revision            INTEGER PRIMARY KEY,
        head_hash           TEXT NOT NULL,
        active_span_count   INTEGER NOT NULL CHECK (active_span_count >= 0),
        dropped_span_count  INTEGER NOT NULL CHECK (dropped_span_count >= 0),
        offloaded_span_count INTEGER NOT NULL CHECK (offloaded_span_count >= 0),
        pinned_span_count   INTEGER NOT NULL CHECK (pinned_span_count >= 0),
        est_tokens          INTEGER NOT NULL CHECK (est_tokens >= 0),
        active_set_sha256   TEXT NOT NULL,
        created_at          TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        proposal_id      TEXT PRIMARY KEY,
        proposal_sha256  TEXT NOT NULL,
        base_revision    INTEGER NOT NULL,
        operations_json  TEXT NOT NULL,
        origin           TEXT NOT NULL CHECK (origin = 'aux'),
        created_revision INTEGER NOT NULL,
        created_at       TEXT NOT NULL,
        accepted_txn_id  TEXT,
        accepted_at      TEXT
    ) STRICT
    """,
)


# ---------------------------------------------------------------------------
# triggers: append-only payload tables
# ---------------------------------------------------------------------------

_APPEND_ONLY_TRIGGERS: tuple[str, ...] = tuple(
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
    BEFORE UPDATE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'E_APPEND_ONLY:{table}');
    END
    """
    for table in APPEND_ONLY_TABLES
) + tuple(
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'E_APPEND_ONLY:{table}');
    END
    """
    for table in APPEND_ONLY_TABLES
)

_PROTECTED_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS trg_span_state_protected_no_offmove
    BEFORE UPDATE ON span_state
    WHEN NEW.state <> 'active'
     AND (SELECT protected FROM spans WHERE span_id = NEW.span_id) = 1
    BEGIN
        SELECT RAISE(ABORT, 'E_PROTECTED:span_state_offmove');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_span_state_protected_no_rewrite
    BEFORE UPDATE ON span_state
    WHEN NEW.rewrite_text IS NOT NULL
     AND (SELECT protected FROM spans WHERE span_id = NEW.span_id) = 1
    BEGIN
        SELECT RAISE(ABORT, 'E_PROTECTED:span_state_rewrite');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_span_state_protected_no_delete
    BEFORE DELETE ON span_state
    WHEN (SELECT protected FROM spans WHERE span_id = OLD.span_id) = 1
    BEGIN
        SELECT RAISE(ABORT, 'E_PROTECTED:span_state_delete');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_pins_no_update
    BEFORE UPDATE ON pins
    BEGIN
        SELECT RAISE(ABORT, 'E_IMMUTABLE:pins');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_current_spans_uid_stable
    BEFORE UPDATE ON current_spans
    WHEN NEW.uid <> OLD.uid
    BEGIN
        SELECT RAISE(ABORT, 'E_IMMUTABLE:current_spans_uid');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_txns_no_regress
    BEFORE UPDATE ON txns
    WHEN OLD.state = 'committed' AND NEW.state <> 'committed'
    BEGIN
        SELECT RAISE(ABORT, 'E_TXN_STATE:regress');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_revisions_no_gap
    BEFORE INSERT ON revisions
    WHEN NEW.revision > 1
     AND (SELECT COUNT(*) FROM revisions WHERE revision = NEW.revision - 1) = 0
    BEGIN
        SELECT RAISE(ABORT, 'E_REVISION_GAP');
    END
    """,
)

TRIGGERS: tuple[str, ...] = _APPEND_ONLY_TRIGGERS + _PROTECTED_TRIGGERS

INDEXES: tuple[str, ...] = ()  # indexes are inline above


def create_schema(conn: sqlite3.Connection) -> None:
    """Create every table, index and trigger idempotently.

    Used at first open and by recovery when a crash interrupted creation.
    """

    for statement in DDL:
        conn.execute(statement)
    for statement in TRIGGERS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('created_at', datetime('now')) "
        "ON CONFLICT (key) DO NOTHING"
    )


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return -1


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def require_schema(conn: sqlite3.Connection) -> None:
    """Raise :class:`CorruptStoreError` when the schema is absent or newer."""

    found = schema_version(conn)
    if found == 0:
        raise CorruptStoreError(
            "store schema is missing", code="schema_missing", details={"schema_version": found}
        )
    if found > SCHEMA_VERSION:
        raise CorruptStoreError(
            "store schema is newer than this package",
            code="schema_too_new",
            details={"found": found, "supported": SCHEMA_VERSION},
        )
    missing = set(TABLES) - table_names(conn)
    if missing:
        raise CorruptStoreError(
            "store schema is incomplete",
            code="schema_incomplete",
            details={"missing": sorted(missing)},
        )
