"""Single-writer enforcement, WAL, file modes and concurrent CAS behaviour."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
import unittest

import store_testkit as kit

from hermes_clm.errors import ReadOnlyStoreError, StoreBusyError
from hermes_clm.store import ContextStore, DB_FILENAME, LOCK_FILENAME


class WALAndModeTests(kit.StoreTestCase):
    def test_journal_mode_is_wal_and_synchronous_full(self) -> None:
        store = self.open_store()
        self.assertEqual(store._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(store._conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(store._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(store._conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_wal_and_shm_files_are_private(self) -> None:
        store = self.open_store()
        self.commit_ingest = self.ingest(store)
        for name in (DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm", LOCK_FILENAME):
            path = store.directory / name
            if path.exists():
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, name)

    def test_read_only_connection_sets_query_only(self) -> None:
        writer = self.open_store()
        self.ingest(writer)
        reader = self.open_store(read_only=True)
        self.assertEqual(reader._conn.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(ReadOnlyStoreError):
            reader.ingest([])
        with self.assertRaises(ReadOnlyStoreError):
            reader.commit(txn_id="txn", expected_revision=1)
        with self.assertRaises(ReadOnlyStoreError):
            reader.stage([], expected_revision=1, txn_id="t", actor_origin="agent")

    def test_read_only_sees_committed_state(self) -> None:
        writer = self.open_store()
        ids = self.ingest(writer)
        writer.stage(
            [kit.EditOperation(kind=kit.EditKind.DROP, span_ids=(ids["u-a2"],))],
            expected_revision=1,
            txn_id="txn-1",
            actor_origin="agent",
        )
        writer.commit(txn_id="txn-1", expected_revision=1)
        reader = self.open_store(read_only=True)
        self.assertEqual(reader.snapshot().revision, 2)
        self.assertIs(reader.snapshot().span(ids["u-a2"]).state, kit.SpanState.DROPPED)


class SingleWriterTests(kit.StoreTestCase):
    def test_second_writer_is_refused(self) -> None:
        self.open_store()
        with self.assertRaises(StoreBusyError) as ctx:
            ContextStore.for_session(self.home, self.session_id)
        self.assertEqual(ctx.exception.code, "store_busy")

    def test_lock_released_on_close(self) -> None:
        first = ContextStore.for_session(self.home, self.session_id)
        first.close()
        second = ContextStore.for_session(self.home, self.session_id)
        second.close()

    def test_other_process_cannot_write(self) -> None:
        store = self.open_store()
        self.ingest(store)
        script = (
            "import sys;"
            "sys.path.insert(0, %r);"
            "from hermes_clm.store import ContextStore;"
            "ContextStore.for_session(%r, %r)"
        ) % (str(kit.SRC), str(self.home), self.session_id)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("StoreBusyError", proc.stderr)

    def test_readers_are_allowed_alongside_a_writer(self) -> None:
        writer = self.open_store()
        ids = self.ingest(writer)
        readers = [self.open_store(read_only=True) for _ in range(3)]
        self.assertTrue(all(reader.snapshot().revision == 1 for reader in readers))
        writer.stage(
            [kit.EditOperation(kind=kit.EditKind.DROP, span_ids=(ids["u-a2"],))],
            expected_revision=1,
            txn_id="txn-1",
            actor_origin="agent",
        )
        self.assertTrue(all(reader.snapshot().revision == 1 for reader in readers))


class ConcurrencyTests(kit.StoreTestCase):
    def test_concurrent_commits_lose_the_cas_race_cleanly(self) -> None:
        store = self.open_store()
        ids = self.ingest(store)
        store.stage(
            [kit.EditOperation(kind=kit.EditKind.DROP, span_ids=(ids["u-a2"],))],
            expected_revision=1,
            txn_id="txn-1",
            actor_origin="agent",
        )
        store.stage(
            [kit.EditOperation(kind=kit.EditKind.DROP, span_ids=(ids["u-a1"], ids["u-t1"]))],
            expected_revision=1,
            txn_id="txn-2",
            actor_origin="agent",
        )
        outcomes: list[str] = []

        def run(txn_id: str) -> None:
            try:
                store.commit(txn_id=txn_id, expected_revision=1)
                outcomes.append(f"ok:{txn_id}")
            except Exception as exc:  # noqa: BLE001 - recorded for assertion
                outcomes.append(f"{getattr(exc, 'code', type(exc).__name__)}:{txn_id}")

        threads = [threading.Thread(target=run, args=(txn,)) for txn in ("txn-1", "txn-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(outcomes), 2)
        winners = [item for item in outcomes if item.startswith("ok:")]
        self.assertEqual(len(winners), 1, outcomes)
        # the loser always observes head=2 with expected_revision=1, so the
        # plain CAS error is deterministic; a caught-up caller would instead
        # get stale_stage (covered in test_store_ops).
        self.assertEqual(sorted(item.split(":")[0] for item in outcomes), ["ok", "stale_revision"], outcomes)
        self.assertEqual(store.snapshot().revision, 2)

    def test_parallel_ingest_is_serialised_and_idempotent(self) -> None:
        store = self.open_store()
        errors: list[str] = []

        def run() -> None:
            try:
                store.ingest(kit.sample_messages())
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(store.snapshot().spans), 6)
        raw = self.raw_connect()
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM spans").fetchone()[0], 6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
