"""Corruption detection and derived-state recovery (no payload rewriting)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path

import store_testkit as kit

from hermes_clm import schema
from hermes_clm.errors import CorruptStoreError
from hermes_clm.recovery import RecoveryReport, recover, recover_session, rebuild_index, verify_session
from hermes_clm.store import DB_FILENAME, INDEX_FILENAME, ContextStore, session_dir


class VerificationTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def test_clean_store_verifies(self) -> None:
        report = self.store.verify()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["corrupt_spans"], [])

    def test_payload_tamper_is_reported_not_repaired(self) -> None:
        raw = self.raw_connect()
        raw.execute("DROP TRIGGER trg_spans_no_update")
        raw.execute(
            "UPDATE spans SET message_json = ? WHERE span_id = ?",
            (kit.canonical_json({"role": "assistant", "content": "tampered"}), self.ids["u-a2"]),
        )
        report = self.store.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(report["corrupt_spans"], [self.ids["u-a2"]])
        self.assertIn("payload_hash_mismatch", {p["code"] for p in report["problems"]})
        # recovery must report, not rewrite
        rec = recover(self.store)
        self.assertFalse(rec.ok)
        self.assertEqual(rec.corrupt_spans, (self.ids["u-a2"],))
        still = raw.execute("SELECT message_json FROM spans WHERE span_id = ?", (self.ids["u-a2"],)).fetchone()[0]
        self.assertIn("tampered", still)
        with self.assertRaises(CorruptStoreError):
            self.store.retrieve(self.ids["u-a2"])
        # status only walks the cheap revision chain; full payload auditing is
        # verify()/recover() (documented in docs/store-format.md)
        self.assertTrue(self.store.status()["health"]["ok"])
        self.assertFalse(self.store.verify()["ok"])

    def test_archive_tamper_breaks_the_chain(self) -> None:
        raw = self.raw_connect()
        raw.execute("DROP TRIGGER trg_archive_no_update")
        raw.execute("UPDATE archive SET payload_json = ? WHERE archive_id = 2", (kit.canonical_json({"x": 1}),))
        report = self.store.verify()
        self.assertFalse(report["ok"])
        self.assertIn("entry_hash_mismatch", {p["code"] for p in report["problems"]})

    def test_revision_hash_tamper_is_detected(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        raw = self.raw_connect()
        raw.execute("DROP TRIGGER trg_revisions_no_update")
        raw.execute("UPDATE revisions SET head_hash = ? WHERE revision = 2", ("f" * 64,))
        self.assertFalse(self.store.verify()["ok"])
        with self.assertRaises(CorruptStoreError):
            self.store.snapshot()

    def test_span_state_divergence_is_rebuilt(self) -> None:
        raw = self.raw_connect()
        raw.execute(
            "UPDATE span_state SET state = 'dropped' WHERE span_id = ?", (self.ids["u-a2"],)
        )
        self.assertIsNotNone(raw.execute("SELECT 1").fetchone())
        # snapshot reflects the corrupted checkpoint, verify notices the divergence
        self.assertFalse(self.store.verify()["ok"])
        rec = recover(self.store)
        self.assertEqual(rec.state_rebuilt, 1)
        self.assertIn("rebuilt_span_state", rec.actions)
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)

    def test_recovering_a_clean_store_is_idempotent(self) -> None:
        first = recover(self.store)
        second = recover(self.store)
        self.assertEqual(first.actions, second.actions)
        self.assertEqual(first.head_revision, second.head_revision)

    def test_versioned_spans_do_not_look_like_divergence(self) -> None:
        # historical (absorbed) versions must not be mistaken for missing state
        self.store.ingest([{"message_uid": "u-a2", "role": "assistant", "content": "v2 of the reply"}])
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-version")
        report = self.store.verify()
        self.assertTrue(report["ok"], report)
        rebuilt = self.store.rebuild_state()
        self.assertEqual(rebuilt["changed"], 0, "checkpoint already matches replayed chain")
        self.assertTrue(self.store.verify()["ok"])

    def test_recovery_removes_interrupted_index_temp_file(self) -> None:
        tmp = self.store.directory / f".{INDEX_FILENAME}.1234.tmp"
        tmp.write_text("partial")
        report = recover(self.store)
        self.assertIn("removed_stale_index_tmp", report.actions)
        self.assertFalse(tmp.exists())


class SchemaRepairTests(kit.StoreTestCase):
    session_id = "repair-session"

    def _make_partial_store(self) -> Path:
        directory = session_dir(self.home, self.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        db = directory / DB_FILENAME
        conn = sqlite3.connect(str(db), isolation_level=None)
        try:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT")
            conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
            conn.execute("INSERT INTO meta VALUES ('session_id', ?)", (self.session_id,))
            conn.execute("INSERT INTO meta VALUES ('db_id', 'db-partial')")
        finally:
            conn.close()
        return db

    def test_missing_tables_are_reported_then_repaired(self) -> None:
        self._make_partial_store()
        with self.assertRaises(CorruptStoreError) as ctx:
            ContextStore.for_session(self.home, self.session_id)
        self.assertEqual(ctx.exception.code, "schema_incomplete")

        report = recover_session(self.home, self.session_id)
        self.assertTrue(report.schema_repaired)
        self.assertIn("schema_repaired", report.actions)
        self.assertTrue(report.ok, report.to_dict())

        store = self.open_store()
        self.assertEqual(store.snapshot().revision, 1)
        self.assertTrue(store.verify()["ok"])

    def test_garbage_database_file_is_refused(self) -> None:
        directory = session_dir(self.home, self.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / DB_FILENAME).write_bytes(b"this is definitely not a sqlite database" * 10)
        with self.assertRaises(CorruptStoreError) as ctx:
            ContextStore.for_session(self.home, self.session_id)
        self.assertIn(ctx.exception.code, {"sqlite_open_failed", "schema_missing", "schema_incomplete"})

    def test_truncated_database_is_detected(self) -> None:
        store = self.open_store()
        ids = self.ingest(store)
        store.close()
        db = self.db_path()
        size = db.stat().st_size
        with db.open("r+b") as handle:
            handle.truncate(max(1, size // 2))
        try:
            reopened = ContextStore.for_session(self.home, self.session_id)
        except CorruptStoreError:
            return
        try:
            report = reopened.verify()
        finally:
            reopened.close()
        self.assertFalse(report["ok"])

    def test_recover_session_requires_an_existing_store(self) -> None:
        with self.assertRaises(CorruptStoreError) as ctx:
            recover_session(self.home, "never-existed")
        self.assertEqual(ctx.exception.code, "store_not_found")


class RecoveryReportTests(kit.StoreTestCase):
    def test_report_serialises(self) -> None:
        store = self.open_store()
        self.ingest(store)
        report = recover(store)
        payload = report.to_dict()
        self.assertIn("actions", payload)
        json.dumps(payload)
        self.assertIsInstance(report, RecoveryReport)

    def test_verify_session_read_only(self) -> None:
        store = self.open_store()
        self.ingest(store)
        store.close()
        report = verify_session(self.home, self.session_id)
        self.assertTrue(report["ok"], report)
        self.assertIn("index", report)

    def test_rebuild_index_entrypoint(self) -> None:
        store = self.open_store()
        self.ingest(store)
        store.close()
        index = session_dir(self.home, self.session_id) / INDEX_FILENAME
        index.write_text("corrupt")
        result = rebuild_index(self.home, self.session_id)
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(index.read_text())["session_id"], self.session_id)


class RetentionHelpersTests(kit.StoreTestCase):
    def test_list_and_purge_sessions(self) -> None:
        first = self.open_store("session-a")
        first.ingest([{"message_uid": "u1", "role": "assistant", "content": "a"}])
        first.close()
        second = self.open_store("session-b")
        second.ingest([{"message_uid": "u1", "role": "assistant", "content": "b"}])
        second.close()

        sessions = ContextStore.list_sessions(self.home)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(
            sorted(item["session_id"] for item in sessions), ["session-a", "session-b"]
        )

        purged = ContextStore.purge_session(self.home, "session-a")
        self.assertTrue(purged["removed"])
        self.assertEqual([item["session_id"] for item in ContextStore.list_sessions(self.home)], ["session-b"])
        self.assertTrue(ContextStore.purge_session(self.home, "session-a")["removed"] is False)

    def test_purge_refuses_while_a_writer_holds_the_lock(self) -> None:
        store = self.open_store("locked-session")
        store.ingest([])
        with self.assertRaises(Exception) as ctx:
            ContextStore.purge_session(self.home, "locked-session")
        self.assertEqual(getattr(ctx.exception, "code", None), "store_busy")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
