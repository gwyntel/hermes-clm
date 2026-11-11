"""Child process used by the crash-recovery tests.

Modes deliberately terminate the process with ``os._exit`` so no Python
cleanup, no ``close()`` and no SQLite checkpoint runs:

* ``stage-crash``   — inside a ``stage`` write transaction boundary, after it committed
* ``txn-crash``     — mid ``BEGIN IMMEDIATE`` transaction (the interesting one)
* ``commit-crash``  — after a successful commit, before the handle is closed
* ``ingest-crash``  — mid-ingest transaction
"""

from __future__ import annotations

import os
import sqlite3
import sys

SRC = sys.argv[1]
HOME = sys.argv[2]
SESSION = sys.argv[3]
MODE = sys.argv[4]
sys.path.insert(0, SRC)

from hermes_clm.models import EditKind, EditOperation  # noqa: E402
from hermes_clm.store import DB_FILENAME, ContextStore, session_dir  # noqa: E402


def messages() -> list[dict]:
    return [
        {"message_uid": "u-sys", "role": "system", "content": "You are a careful coding agent."},
        {"message_uid": "u-q1", "role": "user", "content": "hello"},
        {
            "message_uid": "u-a1",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"message_uid": "u-t1", "role": "tool", "tool_call_id": "call-1", "content": "body"},
        {"message_uid": "u-a2", "role": "assistant", "content": "done"},
    ]


def span_ids(store: ContextStore) -> dict[str, str]:
    return {span.uid: span.span_id for span in store.snapshot().spans.values()}


def main() -> None:  # pragma: no cover - runs in a subprocess
    store = ContextStore.for_session(HOME, SESSION)
    if MODE == "stage-crash":
        store.ingest(messages())
        ids = span_ids(store)
        store.stage(
            [EditOperation(kind=EditKind.DROP, span_ids=(ids["u-a2"],))],
            expected_revision=1,
            txn_id="txn-crash",
            actor_origin="agent",
        )
        os._exit(9)

    if MODE == "ingest-crash":
        store.ingest(messages())
        # second ingest with a fresh uid, killed from inside the write transaction
        store.close()
        db = session_dir(HOME, SESSION) / DB_FILENAME
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
            "VALUES (1, 'phantom', 'system', '{}', 'now')"
        )
        os._exit(9)

    if MODE == "txn-crash":
        store.ingest(messages())
        store.close()
        db = session_dir(HOME, SESSION) / DB_FILENAME
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO audit (revision, event, actor_origin, detail_json, created_at) "
            "VALUES (1, 'phantom', 'system', '{}', 'now')"
        )
        conn.execute("UPDATE meta SET value = 'tampered' WHERE key = 'session_id'")
        os._exit(9)

    if MODE == "commit-crash":
        store.ingest(messages())
        ids = span_ids(store)
        store.stage(
            [EditOperation(kind=EditKind.DROP, span_ids=(ids["u-a2"],))],
            expected_revision=1,
            txn_id="txn-commit",
            actor_origin="agent",
        )
        store.commit(txn_id="txn-commit", expected_revision=1)
        os._exit(9)

    raise SystemExit(f"unknown mode: {MODE}")


main()
