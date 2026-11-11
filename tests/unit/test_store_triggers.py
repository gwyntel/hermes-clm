"""Database-level guards: append-only payloads, protected rows, txn states."""

from __future__ import annotations

import sqlite3
import unittest

import store_testkit as kit


class AppendOnlyTriggerTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)
        self.raw = self.raw_connect()

    def _assert_abort(self, code_fragment: str, sql: str, params: tuple = ()) -> None:
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute(sql, params)
        self.assertIn(code_fragment, str(ctx.exception))

    def test_spans_cannot_be_updated_or_deleted(self) -> None:
        self._assert_abort(
            "E_APPEND_ONLY:spans", "UPDATE spans SET content_sha256 = 'x' WHERE span_id = ?", (self.ids["u-a2"],)
        )
        self._assert_abort("E_APPEND_ONLY:spans", "DELETE FROM spans WHERE span_id = ?", (self.ids["u-a2"],))

    def test_archive_cannot_be_updated_or_deleted(self) -> None:
        self._assert_abort("E_APPEND_ONLY:archive", "UPDATE archive SET payload_json = '{}'")
        self._assert_abort("E_APPEND_ONLY:archive", "DELETE FROM archive")

    def test_operations_and_revisions_cannot_be_rewritten(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self._assert_abort("E_APPEND_ONLY:operations", "UPDATE operations SET kind = 'keep'")
        self._assert_abort("E_APPEND_ONLY:operations", "DELETE FROM operations")
        self._assert_abort("E_APPEND_ONLY:revisions", "UPDATE revisions SET head_hash = 'x'")
        self._assert_abort("E_APPEND_ONLY:revisions", "DELETE FROM revisions WHERE revision = 2")

    def test_audit_tokens_snapshots_are_append_only(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self._assert_abort("E_APPEND_ONLY:audit", "DELETE FROM audit")
        self._assert_abort("E_APPEND_ONLY:tokens", "DELETE FROM tokens")
        self._assert_abort("E_APPEND_ONLY:snapshots", "DELETE FROM snapshots")

    def test_alias_rows_are_append_only(self) -> None:
        self.store.ingest([{"message_uid": "u-a2", "role": "assistant", "content": "changed"}])
        self._assert_abort("E_APPEND_ONLY:span_aliases", "DELETE FROM span_aliases")

    def test_revision_gap_is_refused(self) -> None:
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute(
                "INSERT INTO revisions (revision, parent_revision, base_revision, kind, head_hash, txn_id, "
                "actor_origin, op_count, applied, inert, est_tokens_before, est_tokens_after, created_at) "
                "VALUES (9, 2, 2, 'edit', 'h', 't', 'agent', 0, 0, 0, 0, 0, 'now')"
            )
        self.assertIn("E_REVISION_GAP", str(ctx.exception))


class ProtectedRowTriggerTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)
        self.raw = self.raw_connect()

    def test_protected_span_cannot_be_moved_off_active(self) -> None:
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute(
                "UPDATE span_state SET state = 'dropped' WHERE span_id = ?", (self.ids["u-q1"],)
            )
        self.assertIn("E_PROTECTED:span_state_offmove", str(ctx.exception))

    def test_protected_span_cannot_receive_a_rewrite(self) -> None:
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute(
                "UPDATE span_state SET rewrite_text = 'x', rewrite_sha256 = 'y' WHERE span_id = ?",
                (self.ids["u-sys"],),
            )
        self.assertIn("E_PROTECTED:span_state_rewrite", str(ctx.exception))

    def test_protected_state_row_cannot_be_deleted(self) -> None:
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute("DELETE FROM span_state WHERE span_id = ?", (self.ids["u-q2"],))
        self.assertIn("E_PROTECTED:span_state_delete", str(ctx.exception))

    def test_unprotected_span_state_is_still_mutable_by_the_store(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.DROPPED)

    def test_committed_txn_cannot_regress(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute("UPDATE txns SET state = 'staged' WHERE txn_id = 'txn-1'")
        self.assertIn("E_TXN_STATE:regress", str(ctx.exception))

    def test_pins_are_not_updatable(self) -> None:
        self.commit(self.store, [self.pin(self.ids["u-a2"])], "txn-1")
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute("UPDATE pins SET actor_origin = 'agent'")
        self.assertIn("E_IMMUTABLE:pins", str(ctx.exception))

    def test_current_span_uid_cannot_be_reassigned(self) -> None:
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.raw.execute("UPDATE current_spans SET uid = 'hijacked' WHERE uid = 'u-a2'")
        self.assertIn("E_IMMUTABLE:current_spans_uid", str(ctx.exception))


class SchemaShapeTests(kit.StoreTestCase):
    def test_strict_tables_reject_wrong_types(self) -> None:
        store = self.open_store()
        self.ingest(store)
        raw = self.raw_connect()
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute(
                "INSERT INTO snapshots (revision, head_hash, active_span_count, dropped_span_count, "
                "offloaded_span_count, pinned_span_count, est_tokens, active_set_sha256, created_at) "
                "VALUES ('not-an-int', 'h', 0, 0, 0, 0, 0, 'x', 'now')"
            )

    def test_unknown_revision_kind_rejected(self) -> None:
        store = self.open_store()
        raw = self.raw_connect()
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute(
                "INSERT INTO revisions (revision, parent_revision, base_revision, kind, head_hash, txn_id, "
                "actor_origin, op_count, applied, inert, est_tokens_before, est_tokens_after, created_at) "
                "VALUES (2, 1, 1, 'sneaky', 'h', 't', 'agent', 0, 0, 0, 0, 0, 'now')"
            )

    def test_schema_version_recorded(self) -> None:
        store = self.open_store()
        raw = self.raw_connect()
        row = raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        self.assertEqual(row[0], "1")
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
