"""Operator unpin accepts one id or a batch and applies it atomically."""

from __future__ import annotations

import unittest

import store_testkit as kit

from hermes_clm.errors import (
    IdempotencyConflictError,
    NotFoundError,
    PinAuthorityError,
    ValidationError,
)
from hermes_clm.models import EditKind, EditOperation


class UnpinManyTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)
        self.a = self.ids["u-a1"]
        self.b = self.ids["u-a2"]

    def _pin(self, span_ids, txn_id: str = "txn-pin"):
        revision = self.store.snapshot().revision
        self.store.stage(
            [EditOperation(kind=EditKind.PIN, span_ids=(span_id,)) for span_id in span_ids],
            expected_revision=revision,
            txn_id=txn_id,
            actor_origin="agent",
        )
        return self.store.commit(txn_id=txn_id, expected_revision=revision)

    def test_batch_unpin_is_one_revision_and_removes_all(self) -> None:
        self._pin([self.a, self.b])
        before = self.store.snapshot().revision
        result = self.store.unpin(
            [self.a, self.b],
            expected_revision=before,
            txn_id="txn-unpin",
            actor_origin="operator",
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.revision, before + 1, "batch unpin must be a single revision")
        self.assertEqual({r.span_id for r in result.applied}, {self.a, self.b})
        self.assertEqual(result.inert, ())
        self.assertEqual(dict(self.store.snapshot().pins), {})

        raw = self.raw_connect()
        events = raw.execute(
            "SELECT span_id FROM pin_events WHERE action = 'unpin' ORDER BY seq"
        ).fetchall()
        self.assertEqual({row[0] for row in events}, {self.a, self.b})

    def test_operator_unpin_accepts_span_id_keyword(self) -> None:
        self._pin([self.a])
        result = self.store.operator_unpin(
            span_id=self.a, expected_revision=self.store.snapshot().revision, txn_id="txn-one"
        )
        self.assertEqual([r.span_id for r in result.applied], [self.a])
        self.assertNotIn(self.a, self.store.snapshot().pins)

    def test_operator_unpin_many_is_the_explicit_batch_form(self) -> None:
        self._pin([self.a, self.b])
        before = self.store.snapshot().revision
        result = self.store.operator_unpin_many(
            [self.a, self.b],
            expected_revision=before,
            txn_id="txn-many",
        )
        self.assertEqual(result.revision, before + 1)
        self.assertEqual(dict(self.store.snapshot().pins), {})

    def test_mixed_pinned_and_unpinned_is_applied_plus_inert(self) -> None:
        self._pin([self.a])
        result = self.store.operator_unpin(
            [self.a, self.b],
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-mixed",
        )
        self.assertEqual([r.span_id for r in result.applied], [self.a])
        self.assertEqual([r.span_id for r in result.inert], [self.b])
        self.assertEqual(result.inert[0].reason, "not_pinned")

    def test_unknown_span_aborts_before_removing_any_pin(self) -> None:
        self._pin([self.a])
        head = self.store.snapshot().revision
        with self.assertRaises(NotFoundError) as ctx:
            self.store.unpin(
                [self.a, "sp_does_not_exist"],
                expected_revision=head,
                txn_id="txn-atomic",
                actor_origin="operator",
            )
        self.assertEqual(ctx.exception.code, "unknown_span")
        self.assertIn(self.a, self.store.snapshot().pins, "atomic refusal must not unpin")
        self.assertEqual(self.store.snapshot().revision, head)

    def test_agent_cannot_unpin_a_batch(self) -> None:
        self._pin([self.a, self.b])
        with self.assertRaises(PinAuthorityError) as ctx:
            self.store.unpin(
                [self.a, self.b],
                expected_revision=self.store.snapshot().revision,
                txn_id="txn-agent",
                actor_origin="agent",
            )
        self.assertEqual(ctx.exception.code, "pin_authority")
        self.assertIn(self.a, self.store.snapshot().pins)
        self.assertIn(self.b, self.store.snapshot().pins)

    def test_unpin_replay_is_idempotent_and_payload_bound(self) -> None:
        self._pin([self.a, self.b])
        revision = self.store.snapshot().revision
        first = self.store.operator_unpin(
            [self.a, self.b], expected_revision=revision, txn_id="txn-replay"
        )
        replay = self.store.operator_unpin(
            [self.a, self.b], expected_revision=revision + 99, txn_id="txn-replay"
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.revision, first.revision)
        self.assertEqual(self.store.snapshot().revision, first.revision)
        with self.assertRaises(IdempotencyConflictError) as ctx:
            self.store.operator_unpin(
                [self.a], expected_revision=first.revision, txn_id="txn-replay"
            )
        self.assertEqual(ctx.exception.code, "txn_payload_mismatch")

    def test_span_id_and_span_ids_together_are_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.store.unpin(
                [self.a],
                span_id=self.a,
                expected_revision=1,
                txn_id="txn-both",
                actor_origin="operator",
            )
        self.assertEqual(ctx.exception.code, "invalid_span_id")

    def test_empty_targets_are_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.store.unpin([], expected_revision=1, txn_id="txn-empty", actor_origin="operator")
        self.assertEqual(ctx.exception.code, "missing_span_ids")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
