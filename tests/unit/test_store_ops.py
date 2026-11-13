"""Stage/commit/discard: CAS, idempotency, authority, operation semantics."""

from __future__ import annotations

import json
import unittest

import store_testkit as kit

from hermes_clm.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    PinAuthorityError,
    ProtectedSpanError,
    StaleRevisionError,
    StoreBudgetError,
    ValidationError,
)
from hermes_clm.models import EditKind, EditOperation
from hermes_clm.store import StoreConfig


class StageCommitBase(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)


class TransactionLifecycleTests(StageCommitBase):
    def test_commit_applies_state_and_bumps_revision(self) -> None:
        revision = self.store.snapshot().revision
        result = self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertTrue(result.ok)
        self.assertEqual(result.revision, revision + 1)
        self.assertEqual(result.base_revision, revision)
        self.assertEqual(len(result.applied), 1)
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.revision, revision + 1)
        self.assertIs(snapshot.span(self.ids["u-a2"]).state, kit.SpanState.DROPPED)

    def test_stage_does_not_change_head_but_reports_effects(self) -> None:
        revision = self.store.snapshot().revision
        staged = self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertTrue(staged.ok)
        self.assertEqual(staged.revision, revision)
        self.assertEqual([r.span_id for r in staged.applied], [self.ids["u-a2"]])
        self.assertEqual(self.store.snapshot().revision, revision)
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)

    def test_discard_drops_staged_transaction(self) -> None:
        self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.store.discard(txn_id="txn-1")
        self.assertExceptionCode(
            "txn_discarded",
            IdempotencyConflictError,
            self.store.commit,
            txn_id="txn-1",
            expected_revision=1,
        )
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)

    def test_discard_unknown_txn_is_a_noop(self) -> None:
        self.assertIsNone(self.store.discard(txn_id="never-existed"))

    def test_discard_of_committed_txn_is_refused(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertExceptionCode(
            "already_committed", ConflictError, self.store.discard, txn_id="txn-1"
        )

    def test_commit_unknown_txn_is_refused(self) -> None:
        self.assertExceptionCode(
            "unknown_txn", NotFoundError, self.store.commit, txn_id="nope", expected_revision=1
        )

    def test_empty_operation_batch_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.stage([], expected_revision=1, txn_id="txn-1", actor_origin="agent")

    def test_txn_id_validation(self) -> None:
        for bad in ("", "has space", "x" * 200, "tab\tchar"):
            with self.assertRaises(ValidationError):
                self.store.stage(
                    [self.drop([self.ids["u-a2"]])],
                    expected_revision=1,
                    txn_id=bad,
                    actor_origin="agent",
                )


class CompareAndSwapTests(StageCommitBase):
    def test_stage_with_stale_revision_refused(self) -> None:
        self.assertExceptionCode(
            "stale_revision",
            StaleRevisionError,
            self.store.stage,
            [self.drop([self.ids["u-a2"]])],
            expected_revision=0,
            txn_id="txn-1",
            actor_origin="agent",
        )

    def test_commit_with_stale_revision_refused(self) -> None:
        self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertExceptionCode(
            "stale_revision",
            StaleRevisionError,
            self.store.commit,
            txn_id="txn-1",
            expected_revision=99,
        )

    def test_commit_after_head_moved_is_refused(self) -> None:
        self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        moved = self.commit(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-2")
        self.assertEqual(moved.revision, 2)
        # caller still on old head -> plain CAS failure
        self.assertExceptionCode(
            "stale_revision",
            StaleRevisionError,
            self.store.commit,
            txn_id="txn-1",
            expected_revision=1,
        )
        # caller caught up but the staged txn itself is rooted at the old head
        self.assertExceptionCode(
            "stale_stage",
            StaleRevisionError,
            self.store.commit,
            txn_id="txn-1",
            expected_revision=2,
        )

    def test_two_txns_staged_on_same_revision(self) -> None:
        self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.stage(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-2")
        first = self.store.commit(txn_id="txn-1", expected_revision=1)
        self.assertEqual(first.revision, 2)
        self.assertExceptionCode(
            "stale_stage",
            StaleRevisionError,
            self.store.commit,
            txn_id="txn-2",
            expected_revision=2,
        )


class IdempotencyTests(StageCommitBase):
    def test_commit_replay_returns_original_outcome(self) -> None:
        original = self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        replay = self.store.commit(txn_id="txn-1", expected_revision=0)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.revision, original.revision)
        self.assertEqual(replay.head_hash, original.head_hash)
        self.assertEqual([r.span_id for r in replay.applied], [r.span_id for r in original.applied])
        self.assertEqual(
            self.store.snapshot().revision, original.revision, "replay must not add a revision"
        )

    def test_stage_replay_same_payload_is_idempotent(self) -> None:
        first = self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        second = self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertTrue(second.replayed)
        self.assertEqual(first.staged_sha256, second.staged_sha256)

    def test_stage_replay_different_payload_conflicts(self) -> None:
        self.stage(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertExceptionCode(
            "txn_payload_mismatch",
            IdempotencyConflictError,
            self.store.stage,
            [self.drop([self.ids["u-a1"], self.ids["u-t1"]])],
            expected_revision=1,
            txn_id="txn-1",
            actor_origin="agent",
        )

    def test_stage_after_commit_reports_already_committed(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        replay = self.store.stage(
            [self.drop([self.ids["u-a2"]])],
            expected_revision=1,
            txn_id="txn-1",
            actor_origin="agent",
        )
        self.assertTrue(replay.already_committed)
        self.assertEqual(replay.revision, 2)


class OperationSemanticsTests(StageCommitBase):
    def test_drop_then_keep_restores_the_span(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.commit(
            self.store,
            [EditOperation(kind=EditKind.KEEP, span_ids=(self.ids["u-a2"],))],
            "txn-2",
        )
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)

    def test_keep_on_active_span_is_inert(self) -> None:
        result = self.commit(
            self.store, [EditOperation(kind=EditKind.KEEP, span_ids=(self.ids["u-a2"],))], "txn-1"
        )
        self.assertEqual(len(result.applied), 0)
        self.assertEqual(result.inert[0].reason, "already_active")

    def test_drop_of_dropped_span_is_inert(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        result = self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-2")
        self.assertEqual(result.inert[0].reason, "not_active")

    def test_rewrite_preserves_role_and_records_text(self) -> None:
        result = self.commit(
            self.store,
            [
                EditOperation(
                    kind=EditKind.REWRITE,
                    span_ids=(self.ids["u-a2"],),
                    replacement="The file looks fine (summary).",
                    provenance={"origin": "aux"},
                )
            ],
            "txn-1",
        )
        self.assertEqual(result.applied[0].effect, "applied")
        span = self.store.snapshot().span(self.ids["u-a2"])
        self.assertEqual(span.rewrite_text, "The file looks fine (summary).")
        message = span.effective_message()
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "The file looks fine (summary).")
        # archived raw is untouched
        self.assertEqual(span.message["content"], "The file looks fine.")

    def test_rewrite_of_multimodal_or_protected_refused(self) -> None:
        for uid in ("u-q1", "u-sys"):
            with self.assertRaises(ProtectedSpanError):
                self.commit(
                    self.store,
                    [
                        EditOperation(
                            kind=EditKind.REWRITE, span_ids=(self.ids[uid],), replacement="x"
                        )
                    ],
                    f"txn-{uid}",
                )

    def test_rewrite_budget_enforced(self) -> None:
        small = self.open_store("budget-session", config=StoreConfig(max_rewrite_bytes=16))
        ids = self.ingest(small)
        with self.assertRaises(StoreBudgetError):
            self.commit(
                small,
                [
                    EditOperation(
                        kind=EditKind.REWRITE, span_ids=(ids["u-a2"],), replacement="y" * 100
                    )
                ],
                "txn-big",
            )

    def test_offload_keeps_archive_and_produces_stub(self) -> None:
        result = self.commit(
            self.store,
            [EditOperation(kind=EditKind.OFFLOAD, span_ids=(self.ids["u-a2"],))],
            "txn-1",
        )
        self.assertEqual(result.applied[0].effect, "applied")
        span = self.store.snapshot().span(self.ids["u-a2"])
        self.assertIs(span.state, kit.SpanState.OFFLOADED)
        retrieved = self.store.retrieve(self.ids["u-a2"])
        self.assertEqual(retrieved["message"]["content"], "The file looks fine.")
        self.assertIn(self.ids["u-a2"], retrieved["stub"])
        self.assertEqual(span.effective_message()["content"], retrieved["stub"])

    def test_offload_then_restore(self) -> None:
        self.commit(
            self.store,
            [EditOperation(kind=EditKind.OFFLOAD, span_ids=(self.ids["u-a2"],))],
            "txn-1",
        )
        result = self.commit(
            self.store,
            [EditOperation(kind=EditKind.RESTORE, span_ids=(self.ids["u-a2"],))],
            "txn-2",
        )
        self.assertEqual(result.applied[0].effect, "applied")
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)

    def test_restore_of_absent_uid_is_inert(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        # host history no longer contains the message (e.g. /undo)
        remaining = [m for m in kit.sample_messages() if m["message_uid"] != "u-a2"]
        self.store.ingest(remaining)
        result = self.commit(
            self.store,
            [EditOperation(kind=EditKind.RESTORE, span_ids=(self.ids["u-a2"],))],
            "txn-2",
        )
        self.assertEqual(result.inert[0].reason, "uid_absent")
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.DROPPED)

    def test_boundary_creates_audit_revision_without_state_change(self) -> None:
        result = self.commit(
            self.store,
            [EditOperation(kind=EditKind.BOUNDARY, provenance={"label": "task-boundary"})],
            "txn-1",
        )
        self.assertEqual(result.kind, "boundary")
        self.assertEqual(result.applied, ())
        self.assertEqual(dict(self.store.snapshot().counts)["active"], 6)

    def test_offload_of_multimodal_refused(self) -> None:
        store = self.open_store("mm-session")
        store.ingest(
            [
                {
                    "message_uid": "u-img",
                    "role": "tool",
                    "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                }
            ]
        )
        span_id = next(iter(store.snapshot().spans))
        with self.assertRaises(ProtectedSpanError):
            self.commit(
                store, [EditOperation(kind=EditKind.OFFLOAD, span_ids=(span_id,))], "txn-mm"
            )


class OperationPersistenceTests(StageCommitBase):
    """One ``operations`` row per staged operation, not one per affected span."""

    def operations_for(self, revision: int) -> list[dict]:
        raw = self.raw_connect()
        return [
            dict(row)
            for row in raw.execute(
                "SELECT * FROM operations WHERE revision = ? ORDER BY ord", (revision,)
            )
        ]

    def op_count_for(self, revision: int) -> int:
        raw = self.raw_connect()
        row = raw.execute(
            "SELECT op_count FROM revisions WHERE revision = ?", (revision,)
        ).fetchone()
        return int(row["op_count"])

    def test_multi_span_operation_is_persisted_once_and_applies_to_every_span(self) -> None:
        """Regression: a group offload wrote one identical row per span."""

        result = self.commit(
            self.store,
            [
                EditOperation(
                    kind=EditKind.OFFLOAD,
                    span_ids=(self.ids["u-a1"], self.ids["u-t1"]),
                )
            ],
            "txn-group",
        )

        self.assertEqual(len(result.applied), 2, "both spans changed state")
        rows = self.operations_for(result.revision)
        self.assertEqual(len(rows), 1, f"one operation, one row: {rows}")
        self.assertEqual(
            json.loads(rows[0]["span_ids_json"]),
            [self.ids["u-a1"], self.ids["u-t1"]],
            "the single row still names every span the operation targeted",
        )
        self.assertEqual(self.op_count_for(result.revision), 1)
        snapshot = self.store.snapshot()
        for uid in ("u-a1", "u-t1"):
            span = snapshot.span(self.ids[uid])
            assert span is not None
            self.assertIs(span.state, kit.SpanState.OFFLOADED)
        verdict = self.store.verify()
        self.assertTrue(verdict["ok"], verdict["problems"])

    def test_two_separately_staged_identical_operations_stay_two_rows(self) -> None:
        """Identity, not payload: the caller asked for two operations.

        Two boundary markers with the same label are byte-identical operation
        payloads (one span cannot legitimately appear in two operations), so a
        payload-based dedupe would wrongly collapse them into one row.
        """

        ops = [
            EditOperation(
                kind=EditKind.BOUNDARY, actor_origin="agent", provenance={"label": "checkpoint"}
            )
            for _ in range(2)
        ]
        result = self.commit(self.store, ops, "txn-boundary-twice")

        rows = self.operations_for(result.revision)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], rows[1]["kind"])
        self.assertEqual(rows[0]["provenance_json"], rows[1]["provenance_json"])
        self.assertEqual(self.op_count_for(result.revision), 2)
        self.assertTrue(self.store.verify()["ok"])


class AuthorityTests(StageCommitBase):
    def test_unknown_span_refused(self) -> None:
        self.assertExceptionCode(
            "unknown_span",
            NotFoundError,
            self.commit,
            self.store,
            [self.drop(["sp_doesnotexist"])],
            "txn-1",
        )

    def test_duplicate_span_across_ops_refused(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.commit(
                self.store,
                [
                    self.drop([self.ids["u-a2"]]),
                    EditOperation(kind=EditKind.PIN, span_ids=(self.ids["u-a2"],)),
                ],
                "txn-1",
            )
        self.assertEqual(ctx.exception.code, "duplicate_span")

    def test_actor_origin_must_match_caller(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.store.stage(
                [
                    kit.EditOperation(
                        kind=EditKind.DROP, span_ids=(self.ids["u-a2"],), actor_origin="operator"
                    )
                ],
                expected_revision=1,
                txn_id="txn-1",
                actor_origin="agent",
            )
        self.assertEqual(ctx.exception.code, "actor_mismatch")

    def test_incomplete_tool_group_refused(self) -> None:
        with self.assertRaises(ConflictError) as ctx:
            self.commit(self.store, [self.drop([self.ids["u-t1"]])], "txn-1")
        self.assertEqual(ctx.exception.code, "incomplete_group")

    def test_complete_tool_group_allowed(self) -> None:
        result = self.commit(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-1")
        self.assertEqual(len(result.applied), 2)
        snapshot = self.store.snapshot()
        self.assertIs(snapshot.span(self.ids["u-a1"]).state, kit.SpanState.DROPPED)
        self.assertIs(snapshot.span(self.ids["u-t1"]).state, kit.SpanState.DROPPED)

    def test_pin_is_one_way_for_agents(self) -> None:
        self.commit(self.store, [self.pin(self.ids["u-a2"])], "txn-1")
        self.assertIn(self.ids["u-a2"], self.store.snapshot().pins)
        self.assertExceptionCode(
            "pin_authority",
            PinAuthorityError,
            self.store.unpin,
            self.ids["u-a2"],
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-unpin",
            actor_origin="agent",
        )

    def test_pinned_span_cannot_be_dropped(self) -> None:
        self.commit(self.store, [self.pin(self.ids["u-a2"])], "txn-1")
        with self.assertRaises(ConflictError) as ctx:
            self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-2")
        self.assertEqual(ctx.exception.code, "span_pinned")

    def test_operator_unpin_then_drop_allowed(self) -> None:
        self.commit(self.store, [self.pin(self.ids["u-a2"])], "txn-1")
        self.store.operator_unpin(
            self.ids["u-a2"],
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-unpin",
        )
        self.assertNotIn(self.ids["u-a2"], self.store.snapshot().pins)
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-2")
        self.assertIs(self.store.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.DROPPED)

    def test_operator_unpin_of_unpinned_span_is_inert(self) -> None:
        result = self.store.operator_unpin(
            self.ids["u-a2"], expected_revision=1, txn_id="txn-unpin"
        )
        self.assertEqual(result.inert[0].reason, "not_pinned")

    def test_operation_budget_bound(self) -> None:
        store = self.open_store("ops-budget", config=StoreConfig(max_operations_per_txn=2))
        ids = self.ingest(store)
        ops = [
            EditOperation(kind=EditKind.PIN, span_ids=(ids[uid],))
            for uid in ("u-a1", "u-a2", "u-t1")
        ]
        with self.assertRaises(StoreBudgetError):
            self.commit(store, ops, "txn-1")


class AuditAndTokenTests(StageCommitBase):
    def test_audit_and_token_rows_written_per_commit(self) -> None:
        result = self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        raw = self.raw_connect()
        events = [row[0] for row in raw.execute("SELECT event FROM audit ORDER BY seq")]
        self.assertIn("commit:edit", events)
        tokens = raw.execute(
            "SELECT before_est, after_est, delta FROM tokens WHERE revision = ? AND span_id IS NULL",
            (result.revision,),
        ).fetchone()
        self.assertIsNotNone(tokens)
        self.assertLess(tokens[1], tokens[0], "dropping content must reduce estimated tokens")
        self.assertEqual(tokens[2], tokens[1] - tokens[0])

    def test_revision_snapshot_records_token_estimates(self) -> None:
        result = self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        raw = self.raw_connect()
        row = raw.execute(
            "SELECT est_tokens, active_span_count, active_set_sha256 FROM snapshots WHERE revision = ?",
            (result.revision,),
        ).fetchone()
        self.assertEqual(row[1], 5)
        self.assertGreater(row[0], 0)
        self.assertEqual(len(row[2]), 64)

    def test_status_reports_health_and_counts(self) -> None:
        self.commit(self.store, [self.pin(self.ids["u-a2"])], "txn-1")
        status = self.store.status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["health"]["ok"])
        self.assertEqual(status["counts"]["pinned"], 1)
        self.assertEqual(status["revision"], 2)
        self.assertFalse(status["index"]["authoritative"])
        self.assertIn("estimator", status)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
