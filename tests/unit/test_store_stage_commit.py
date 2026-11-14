from __future__ import annotations

import store_testkit as kit

from hermes_clm.errors import ConflictError, ProtectedSpanError, StaleRevisionError
from hermes_clm.models import EditKind, EditOperation


class StageCommitTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def drop_ops(self, *uids):
        return [
            EditOperation(
                kind=EditKind.DROP,
                span_ids=tuple(self.ids[uid] for uid in uids),
            )
        ]

    def test_atomic_commit_leaves_no_staged_transaction(self):
        result = self.store.stage_commit(
            self.drop_ops("u-a2"),
            expected_revision=1,
            txn_id="txn-fast",
            actor_origin="agent",
        )
        self.assertEqual(result.revision, 2)
        self.assertEqual(self.store.status()["staged_transactions"], [])
        self.assertEqual(
            self.raw_connect()
            .execute("SELECT state FROM txns WHERE txn_id='txn-fast'")
            .fetchone()[0],
            "committed",
        )

    def test_same_payload_replays_idempotently(self):
        operations = self.drop_ops("u-a2")
        first = self.store.stage_commit(
            operations, expected_revision=1, txn_id="txn-replay", actor_origin="agent"
        )
        second = self.store.stage_commit(
            operations, expected_revision=1, txn_id="txn-replay", actor_origin="agent"
        )
        self.assertEqual(first.revision, second.revision)
        self.assertTrue(second.replayed)
        self.assertEqual(self.store.snapshot().revision, 2)

    def test_cas_refusal_leaves_no_transaction(self):
        with self.assertRaises(StaleRevisionError):
            self.store.stage_commit(
                self.drop_ops("u-a2"),
                expected_revision=0,
                txn_id="txn-stale",
                actor_origin="agent",
            )
        self.assertIsNone(
            self.raw_connect().execute("SELECT 1 FROM txns WHERE txn_id='txn-stale'").fetchone()
        )
        self.assertEqual(self.store.snapshot().revision, 1)

    def test_protected_refusal_leaves_no_staged_row(self):
        with self.assertRaises(ProtectedSpanError):
            self.store.stage_commit(
                self.drop_ops("u-q1"),
                expected_revision=1,
                txn_id="txn-protected",
                actor_origin="agent",
            )
        self.assertEqual(self.store.status()["staged_transactions"], [])

    def test_existing_stage_is_not_consumed(self):
        self.store.stage(
            self.drop_ops("u-a2"),
            expected_revision=1,
            txn_id="txn-existing",
            actor_origin="agent",
        )
        with self.assertRaises(ConflictError) as ctx:
            self.store.stage_commit(
                self.drop_ops("u-a2"),
                expected_revision=1,
                txn_id="txn-existing",
                actor_origin="agent",
            )
        self.assertEqual(ctx.exception.code, "txn_already_staged")
        self.assertEqual(len(self.store.status()["staged_transactions"]), 1)
        self.assertEqual(self.store.snapshot().revision, 1)

    def test_committed_id_with_different_payload_refuses(self):
        self.store.stage_commit(
            self.drop_ops("u-a2"),
            expected_revision=1,
            txn_id="txn-mismatch",
            actor_origin="agent",
        )
        with self.assertRaises(ConflictError) as ctx:
            self.store.stage_commit(
                self.drop_ops("u-t1"),
                expected_revision=1,
                txn_id="txn-mismatch",
                actor_origin="agent",
            )
        self.assertEqual(ctx.exception.code, "txn_payload_mismatch")
        self.assertEqual(self.store.snapshot().revision, 2)

    def test_audit_semantics_match_two_phase_commit(self):
        ops_fast = self.drop_ops("u-a2")
        fast = self.store.stage_commit(
            ops_fast, expected_revision=1, txn_id="txn-fast-audit", actor_origin="agent"
        )
        ordinary = self.open_store("ordinary")
        ids = self.ingest(ordinary)
        op = EditOperation(kind=EditKind.DROP, span_ids=(ids["u-a2"],))
        ordinary.stage([op], expected_revision=1, txn_id="txn-two-phase", actor_origin="agent")
        two_phase = ordinary.commit(txn_id="txn-two-phase", expected_revision=1)
        self.assertEqual(fast.revision, two_phase.revision)
        self.assertEqual(fast.applied, two_phase.applied)
        fast_events = (
            self.raw_connect()
            .execute("SELECT event, revision, actor_origin FROM audit ORDER BY seq")
            .fetchall()
        )
        two_events = (
            self.raw_connect("ordinary")
            .execute("SELECT event, revision, actor_origin FROM audit ORDER BY seq")
            .fetchall()
        )
        self.assertEqual(
            [(r["event"], r["revision"], r["actor_origin"]) for r in fast_events],
            [(r["event"], r["revision"], r["actor_origin"]) for r in two_events],
        )

    def test_two_phase_stage_reusing_a_committed_id_with_a_new_payload_is_refused(self):
        """The same honesty rule as the fast path: never echo a batch that was not staged.

        ``stage()`` returns the stored outcome for a committed ``txn_id``. Returning
        it for a *different* operation set would tell the caller its new batch is
        staged (and, for a patch, hand back receipts for text never written) while
        ``commit()`` replays the original.
        """

        self.store.stage(
            self.drop_ops("u-a2"), expected_revision=1, txn_id="txn-reuse", actor_origin="agent"
        )
        self.store.commit(txn_id="txn-reuse", expected_revision=1)

        with self.assertRaises(ConflictError) as ctx:
            self.store.stage(
                [EditOperation(kind=EditKind.KEEP, span_ids=(self.ids["u-a2"],))],
                expected_revision=2,
                txn_id="txn-reuse",
                actor_origin="agent",
            )
        self.assertEqual(ctx.exception.code, "txn_payload_mismatch")

    def test_two_phase_stage_replaying_a_committed_id_with_the_same_payload_replays(self):
        self.store.stage(
            self.drop_ops("u-a2"), expected_revision=1, txn_id="txn-replay", actor_origin="agent"
        )
        self.store.commit(txn_id="txn-replay", expected_revision=1)
        again = self.store.stage(
            self.drop_ops("u-a2"), expected_revision=2, txn_id="txn-replay", actor_origin="agent"
        )
        self.assertTrue(again.replayed)
        self.assertTrue(again.already_committed)
        self.assertEqual(again.revision, 2)
        self.assertEqual([op.kind for op in again.staged_operations], [EditKind.DROP])
