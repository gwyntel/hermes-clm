"""Crash and interruption recovery: WAL rollback, stale txns, replay."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import store_testkit as kit

from hermes_clm.errors import IdempotencyConflictError
from hermes_clm.recovery import recover_session
from hermes_clm.store import DB_FILENAME

CHILD = Path(__file__).with_name("store_crash_child.py")


class CrashRecoveryTests(kit.StoreTestCase):
    session_id = "crash-session"

    def _run_child(self, mode: str, *, expect_exit: int = 9) -> None:
        proc = subprocess.run(
            [sys.executable, str(CHILD), str(kit.SRC), str(self.home), self.session_id, mode],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, expect_exit, f"{mode} stderr: {proc.stderr}")

    def test_crash_mid_transaction_leaves_no_partial_rows(self) -> None:
        self._run_child("txn-crash")
        store = self.open_store()
        raw = self.raw_connect()
        self.assertEqual(
            raw.execute("SELECT COUNT(*) FROM audit WHERE event = 'phantom'").fetchone()[0], 0
        )
        self.assertEqual(
            raw.execute("SELECT value FROM meta WHERE key = 'session_id'").fetchone()[0],
            self.session_id,
        )
        report = store.verify()
        self.assertTrue(report["ok"], report)
        self.assertEqual(store.snapshot().revision, 1)

    def test_crash_between_stage_and_commit_expires_and_refuses_late_commit(self) -> None:
        self._run_child("stage-crash")
        store = self.open_store()
        self.assertEqual(store.snapshot().revision, 1, "staging must never advance the head")
        status = store.status()
        self.assertEqual([t["txn_id"] for t in status["staged_transactions"]], ["txn-crash"])

        discarded = store.expire_staged_transactions(ttl_seconds=0)
        self.assertEqual(discarded, ("txn-crash",))
        self.assertExceptionCode(
            "txn_discarded",
            IdempotencyConflictError,
            store.commit,
            txn_id="txn-crash",
            expected_revision=1,
        )
        self.assertEqual(store.snapshot().revision, 1)

    def test_crash_after_commit_keeps_result_and_replays_idempotently(self) -> None:
        self._run_child("commit-crash")
        store = self.open_store()
        snapshot = store.snapshot()
        self.assertEqual(snapshot.revision, 2)
        by_uid = {span.uid: span for span in snapshot.spans.values()}
        self.assertIs(by_uid["u-a2"].state, kit.SpanState.DROPPED)
        replay = store.commit(txn_id="txn-commit", expected_revision=2)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.revision, 2)
        self.assertEqual(store.snapshot().revision, 2)

    def test_recovery_report_is_clean_after_crash(self) -> None:
        self._run_child("txn-crash")
        report = recover_session(self.home, self.session_id)
        self.assertTrue(report.ok, report.to_dict())
        self.assertIn("verified_hash_chains", report.actions)
        self.assertIn("rebuilt_index", report.actions)
        self.assertEqual(report.discarded_txns, ())

    def test_recovery_expires_stale_staged_txn(self) -> None:
        self._run_child("stage-crash")
        report = recover_session(
            self.home, self.session_id, config=kit.StoreConfig(stale_txn_ttl_seconds=0)
        )
        self.assertIn("txn-crash", report.discarded_txns)
        self.assertIn("expired_staged_transactions", report.actions)
        store = self.open_store()
        self.assertEqual(store.status()["staged_transactions"], [])

    def test_restart_recovers_revision_and_preserves_archive(self) -> None:
        store = self.open_store()
        ids = self.ingest(store)
        store.stage(
            [kit.EditOperation(kind=kit.EditKind.OFFLOAD, span_ids=(ids["u-a2"],))],
            expected_revision=1,
            txn_id="txn-1",
            actor_origin="agent",
        )
        store.commit(txn_id="txn-1", expected_revision=1)
        store.close()

        reopened = self.open_store()
        snapshot = reopened.snapshot()
        self.assertEqual(snapshot.revision, 2)
        self.assertIs(snapshot.span(ids["u-a2"]).state, kit.SpanState.OFFLOADED)
        self.assertEqual(
            reopened.retrieve(ids["u-a2"])["message"]["content"], "The file looks fine."
        )
        self.assertTrue(reopened.verify()["ok"])

    def test_stale_txn_from_previous_process_does_not_block_new_work(self) -> None:
        self._run_child("stage-crash")
        store = self.open_store()
        ids = self.ingest(store)
        # a new transaction on the same head still works
        result = self.commit(store, [self.drop([ids["u-a2"]])], "txn-new")
        self.assertEqual(result.revision, 2)

    def test_wal_files_cleaned_when_last_writer_closes(self) -> None:
        store = self.open_store()
        self.ingest(store)
        store.close()
        directory = self.store_dir()
        wal = directory / f"{DB_FILENAME}-wal"
        # after a clean checkpoint the WAL is truncated to zero bytes (file may remain)
        if wal.exists():
            self.assertEqual(os.stat(wal).st_size, 0)

    def store_dir(self):
        from hermes_clm.store import session_dir

        return session_dir(self.home, self.session_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
