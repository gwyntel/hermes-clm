"""Monotonic revisions and revert semantics (no truncation, pins win)."""

from __future__ import annotations

import unittest

import store_testkit as kit

from hermes_clm.errors import ValidationError


class RevisionTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def test_init_revision_is_one(self) -> None:
        self.assertEqual(self.store.snapshot().revision, 1)
        status = self.store.status()
        self.assertEqual(status["revision_kind"], "init")

    def test_revisions_increase_by_one_and_never_rewrite(self) -> None:
        revisions = []
        for index, uid in enumerate(("u-a2", "u-a1", "u-a1")):
            if uid == "u-a1":
                ops = [
                    kit.EditOperation(
                        kind=kit.EditKind.DROP, span_ids=(self.ids["u-a1"], self.ids["u-t1"])
                    )
                ]
            else:
                ops = [self.drop([self.ids[uid]])]
            revisions.append(self.commit(self.store, ops, f"txn-{index}").revision)
        self.assertEqual(revisions, [2, 3, 4])
        raw = self.raw_connect()
        stored = [row[0] for row in raw.execute("SELECT revision FROM revisions ORDER BY revision")]
        self.assertEqual(stored, [1, 2, 3, 4])

    def test_revert_creates_new_head_without_deleting_history(self) -> None:
        target = self.store.snapshot().revision
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.commit(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-2")
        before = dict(self.store.snapshot().counts)
        self.assertEqual(before["dropped"], 3)

        result = self.store.revert(
            target_revision=target,
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-revert",
            actor_origin="agent",
        )
        self.assertEqual(result.kind, "revert")
        self.assertEqual(result.revision, 4)
        self.assertEqual(result.base_revision, target)
        snapshot = self.store.snapshot()
        self.assertEqual(dict(snapshot.counts)["dropped"], 0)
        self.assertEqual(dict(snapshot.counts)["active"], 6)
        raw = self.raw_connect()
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 4)
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM operations").fetchone()[0] >= 3, True)
        self.assertIsNotNone(
            raw.execute("SELECT 1 FROM audit WHERE event = 'commit:revert'").fetchone()
        )

    def test_revert_reports_receipts_for_restored_spans(self) -> None:
        target = self.store.snapshot().revision
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        result = self.store.revert(
            target_revision=target,
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-revert",
            actor_origin="agent",
        )
        self.assertEqual([r.span_id for r in result.applied], [self.ids["u-a2"]])

    def test_pins_survive_revert(self) -> None:
        self.commit(self.store, [self.pin(self.ids["u-a2"])], "txn-pin")
        self.commit(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-drop")
        self.store.revert(
            target_revision=1,
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-revert",
            actor_origin="agent",
        )
        snapshot = self.store.snapshot()
        self.assertIn(self.ids["u-a2"], snapshot.pins, "pins are current state, not revision state")
        self.assertIs(snapshot.span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)
        # the pin still protects the span after the revert
        with self.assertRaises(Exception) as ctx:
            self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-after")
        self.assertEqual(getattr(ctx.exception, "code", None), "span_pinned")

    def test_revert_to_absent_uid_is_inert(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-drop")
        # host history no longer contains the message (e.g. /rewind)
        self.store.ingest([m for m in kit.sample_messages() if m["message_uid"] != "u-a2"])
        result = self.store.revert(
            target_revision=1,
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-revert",
            actor_origin="agent",
        )
        self.assertIn("uid_absent", {r.reason for r in result.inert})
        self.assertIs(
            self.store.snapshot().span(self.ids["u-a2"]).state,
            kit.SpanState.DROPPED,
            "absent content must not be resurrected",
        )

    def test_revert_validates_target_and_cas(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.revert(
                target_revision=99,
                expected_revision=1,
                txn_id="txn-revert",
                actor_origin="agent",
            )
        with self.assertRaises(Exception) as ctx:
            self.store.revert(
                target_revision=1, expected_revision=0, txn_id="txn-revert2", actor_origin="agent"
            )
        self.assertEqual(getattr(ctx.exception, "code", None), "stale_revision")

    def test_revert_replay_is_idempotent(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        first = self.store.revert(
            target_revision=1, expected_revision=2, txn_id="txn-revert", actor_origin="agent"
        )
        replay = self.store.revert(
            target_revision=1, expected_revision=2, txn_id="txn-revert", actor_origin="agent"
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.revision, first.revision)
        self.assertEqual(self.store.snapshot().revision, first.revision)

    def test_edit_after_revert_builds_on_revert_state(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.store.revert(
            target_revision=1, expected_revision=2, txn_id="txn-r", actor_origin="agent"
        )
        self.commit(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-2")
        snapshot = self.store.snapshot()
        self.assertIs(snapshot.span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)
        self.assertIs(snapshot.span(self.ids["u-a1"]).state, kit.SpanState.DROPPED)
        self.assertIs(snapshot.span(self.ids["u-t1"]).state, kit.SpanState.DROPPED)

    def test_revert_restores_rewrite_text(self) -> None:
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE, span_ids=(self.ids["u-a2"],), replacement="short"
                )
            ],
            "txn-rewrite",
        )
        target = self.store.snapshot().revision
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE, span_ids=(self.ids["u-a2"],), replacement="different"
                )
            ],
            "txn-rewrite2",
        )
        self.store.revert(
            target_revision=target,
            expected_revision=self.store.snapshot().revision,
            txn_id="txn-revert",
            actor_origin="agent",
        )
        self.assertEqual(self.store.snapshot().span(self.ids["u-a2"]).rewrite_text, "short")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
