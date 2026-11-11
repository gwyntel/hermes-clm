"""Shared helpers for the store test suites (not a test module itself).

Imports are bootstrapped here so the suite runs with plain
``python -m unittest`` from a clean checkout (no install step, no pytest).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(SRC))

from hermes_clm.models import (  # noqa: E402
    ActorOrigin,
    EditKind,
    EditOperation,
    SpanState,
    canonical_json,
    offload_stub,
)
from hermes_clm.store import (  # noqa: E402
    ContextStore,
    DB_FILENAME,
    INDEX_FILENAME,
    LOCK_FILENAME,
    StoreConfig,
    session_dir,
    session_key,
    sessions_root,
)


def sample_messages(secret: str = "sk-abcdefghijklmnopqrstuvwx") -> list[dict[str, Any]]:
    """A small realistic history: system, user, tool group, assistant, user."""

    return [
        {"message_uid": "u-sys", "role": "system", "content": "You are a careful coding agent."},
        {"message_uid": "u-q1", "role": "user", "content": f"my key is {secret} keep it safe"},
        {
            "message_uid": "u-a1",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {
            "message_uid": "u-t1",
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "content of a.py",
        },
        {"message_uid": "u-a2", "role": "assistant", "content": "The file looks fine."},
        {"message_uid": "u-q2", "role": "user", "content": "now refactor it"},
    ]


class StoreTestCase(unittest.TestCase):
    """Temp-home fixture with helpers for the common store choreography."""

    session_id = "sess-1"

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="clm-store-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = Path(self.tmp) / "hermes-home"
        self._stores: list[ContextStore] = []

    def tearDown(self) -> None:
        for store in reversed(self._stores):
            try:
                store.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # -- opening ---------------------------------------------------------
    def open_store(
        self,
        session_id: str | None = None,
        *,
        read_only: bool = False,
        config: StoreConfig | Mapping[str, Any] | None = None,
    ) -> ContextStore:
        store = ContextStore.for_session(
            self.home,
            session_id or self.session_id,
            read_only=read_only,
            config=config,
        )
        self._stores.append(store)
        return store

    def db_path(self, session_id: str | None = None) -> Path:
        return session_dir(self.home, session_id or self.session_id) / DB_FILENAME

    def raw_connect(self, session_id: str | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path(session_id)), isolation_level=None)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    # -- choreography ----------------------------------------------------
    def ingest(
        self, store: ContextStore, messages: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, str]:
        store.ingest(list(messages if messages is not None else sample_messages()))
        return self.span_ids(store)

    @staticmethod
    def span_ids(store: ContextStore) -> dict[str, str]:
        return {span.uid: span.span_id for span in store.snapshot().spans.values()}

    def stage(
        self,
        store: ContextStore,
        ops: Sequence[EditOperation],
        txn_id: str,
        *,
        actor: str = "agent",
    ):
        return store.stage(
            list(ops),
            expected_revision=store.snapshot().revision,
            txn_id=txn_id,
            actor_origin=actor,
        )

    def commit(
        self,
        store: ContextStore,
        ops: Sequence[EditOperation],
        txn_id: str,
        *,
        actor: str = "agent",
    ):
        revision = store.snapshot().revision
        store.stage(list(ops), expected_revision=revision, txn_id=txn_id, actor_origin=actor)
        return store.commit(txn_id=txn_id, expected_revision=revision)

    @staticmethod
    def drop(span_ids: Sequence[str], *, actor: str = "agent") -> EditOperation:
        return EditOperation(kind=EditKind.DROP, span_ids=tuple(span_ids), actor_origin=actor)

    @staticmethod
    def pin(span_id: str, *, actor: str = "agent") -> EditOperation:
        return EditOperation(kind=EditKind.PIN, span_ids=(span_id,), actor_origin=actor)

    def assertExceptionCode(self, code: str, exc_type: type[BaseException], fn, /, *args, **kwargs):
        with self.assertRaises(exc_type) as ctx:
            fn(*args, **kwargs)
        actual = getattr(ctx.exception, "code", None)
        self.assertEqual(actual, code, msg=f"unexpected error code: {ctx.exception!r}")
        return ctx.exception


__all__ = [
    "StoreTestCase",
    "sample_messages",
    "ContextStore",
    "StoreConfig",
    "session_dir",
    "session_key",
    "sessions_root",
    "DB_FILENAME",
    "INDEX_FILENAME",
    "LOCK_FILENAME",
    "canonical_json",
    "offload_stub",
    "EditKind",
    "EditOperation",
    "ActorOrigin",
    "SpanState",
    "json",
    "os",
]
