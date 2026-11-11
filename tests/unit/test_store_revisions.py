"""Monotonic revisions and revert semantics (no truncation, pins win)."""

from __future__ import annotations

import unittest

import store_testkit as kit

from hermes_clm import projection
from hermes_clm.errors import ConflictError, ValidationError


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

    def test_revert_across_a_pin_is_refused_and_pin_survives(self) -> None:
        self.commit(self.store, [self.pin(self.ids["u-a2"])], "txn-pin")
        self.commit(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-drop")
        head = self.store.snapshot().revision
        with self.assertRaises(ConflictError) as raised:
            self.store.revert(
                target_revision=1,
                expected_revision=head,
                txn_id="txn-revert",
                actor_origin="agent",
            )
        self.assertEqual(raised.exception.code, "span_pinned")
        self.assertEqual(self.store.snapshot().revision, head)
        snapshot = self.store.snapshot()
        self.assertIn(self.ids["u-a2"], snapshot.pins, "pins are current state, not revision state")
        self.assertIs(snapshot.span(self.ids["u-a2"]).state, kit.SpanState.ACTIVE)
        # the pin still protects the span after the refused revert
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

    def test_verify_accepts_rewrite_then_drop(self) -> None:
        """A drop clears the rewrite in the checkpoint; replay must agree."""

        span = self.ids["u-a2"]
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE,
                    span_ids=(span,),
                    replacement="rewritten answer",
                )
            ],
            "txn-rewrite",
        )
        self.commit(self.store, [self.drop([span])], "txn-drop")
        report = self.store.verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(
            [p for p in report["problems"] if p["code"] == "span_state_divergence"], []
        )
        self.assertIsNone(self.store.snapshot().span(span).rewrite_text)
        # recover() must not "repair" the correct checkpoint into the replay's
        # stale answer: nothing to rebuild on a consistent store.
        self.assertEqual(self.store.rebuild_state()["changed"], 0)

    def test_verify_accepts_rewrite_then_offload(self) -> None:
        span = self.ids["u-a2"]
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE,
                    span_ids=(span,),
                    replacement="rewritten answer",
                )
            ],
            "txn-rewrite",
        )
        self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.OFFLOAD, span_ids=(span,))],
            "txn-offload",
        )
        report = self.store.verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertIsNone(self.store.snapshot().span(span).rewrite_text)

    def test_rebuild_state_does_not_resurrect_a_dropped_rewrite(self) -> None:
        span = self.ids["u-a2"]
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE,
                    span_ids=(span,),
                    replacement="rewritten answer",
                )
            ],
            "txn-rewrite",
        )
        self.commit(self.store, [self.drop([span])], "txn-drop")
        rebuilt = self.store.rebuild_state()
        self.assertEqual(rebuilt["changed"], 0, "replay rewrote state the checkpoint had cleared")
        self.assertIsNone(self.store.snapshot().span(span).rewrite_text)

    def test_restore_after_drop_projects_the_archived_original(self) -> None:
        """Documented semantics: drop discards the rewrite, restore brings back the archive."""

        span = self.ids["u-a2"]
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE,
                    span_ids=(span,),
                    replacement="rewritten answer",
                )
            ],
            "txn-rewrite",
        )
        self.commit(self.store, [self.drop([span])], "txn-drop")
        self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.RESTORE, span_ids=(span,))],
            "txn-restore",
        )
        snapshot = self.store.snapshot()
        self.assertIs(snapshot.span(span).state, kit.SpanState.ACTIVE)
        self.assertIsNone(snapshot.span(span).rewrite_text)
        projected = projection.project(
            [dict(message) for message in kit.sample_messages()],
            conversation_messages=kit.sample_messages(),
            snapshot=snapshot,
        )
        self.assertTrue(projected.ok, projected.conflicts)
        contents = [row.get("content") for row in projected.messages]
        self.assertIn("The file looks fine.", contents)
        self.assertNotIn("rewritten answer", contents)

    def test_rewrite_then_restore_keeps_the_rewrite_in_store_and_projection(self) -> None:
        """The store applies a restore to an active span; projection must agree."""

        span = self.ids["u-a2"]
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE,
                    span_ids=(span,),
                    replacement="rewritten answer",
                )
            ],
            "txn-rewrite",
        )
        self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.RESTORE, span_ids=(span,))],
            "txn-restore",
        )
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.span(span).rewrite_text, "rewritten answer")
        projected = projection.project(
            [dict(message) for message in kit.sample_messages()],
            conversation_messages=kit.sample_messages(),
            snapshot=snapshot,
        )
        self.assertTrue(projected.ok, projected.conflicts)
        self.assertIn("rewritten answer", [row.get("content") for row in projected.messages])

    def test_keep_after_drop_projects_the_archived_original(self) -> None:
        """``keep`` is the documented reactivation for a dropped/offloaded span.

        The store applies it, replay/verify agree, and the projector emits the
        row again — with the archived bytes, because the drop cleared the rewrite.
        """

        span = self.ids["u-a2"]
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE,
                    span_ids=(span,),
                    replacement="rewritten answer",
                )
            ],
            "txn-rewrite",
        )
        self.commit(self.store, [self.drop([span])], "txn-drop")
        kept = self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.KEEP, span_ids=(span,))],
            "txn-keep",
        )
        self.assertEqual([receipt.effect for receipt in kept.applied], ["applied"])
        snapshot = self.store.snapshot()
        self.assertIs(snapshot.span(span).state, kit.SpanState.ACTIVE)
        self.assertIsNone(snapshot.span(span).rewrite_text)
        report = self.store.verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(self.store.rebuild_state()["changed"], 0)

        projected = projection.project(
            [dict(message) for message in kit.sample_messages()],
            conversation_messages=kit.sample_messages(),
            snapshot=snapshot,
        )
        self.assertTrue(projected.ok, projected.conflicts)
        contents = [row.get("content") for row in projected.messages]
        self.assertIn("The file looks fine.", contents)
        self.assertNotIn("rewritten answer", contents)
        self.assertIn("keep", [receipt["kind"] for receipt in projected.applied])

    def test_keep_after_offload_projects_the_archived_original(self) -> None:
        span = self.ids["u-a2"]
        self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.OFFLOAD, span_ids=(span,))],
            "txn-offload",
        )
        self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.KEEP, span_ids=(span,))],
            "txn-keep",
        )
        snapshot = self.store.snapshot()
        self.assertIs(snapshot.span(span).state, kit.SpanState.ACTIVE)
        self.assertTrue(self.store.verify()["ok"], self.store.verify()["problems"])
        self.assertEqual(self.store.rebuild_state()["changed"], 0)

        projected = projection.project(
            [dict(message) for message in kit.sample_messages()],
            conversation_messages=kit.sample_messages(),
            snapshot=snapshot,
        )
        self.assertTrue(projected.ok, projected.conflicts)
        self.assertIn("The file looks fine.", [row.get("content") for row in projected.messages])

    def test_keep_on_an_active_span_is_recorded_inert_and_replays_inert(self) -> None:
        span = self.ids["u-a2"]
        kept = self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.KEEP, span_ids=(span,))],
            "txn-keep-active",
        )
        self.assertEqual([receipt.reason for receipt in kept.inert], ["already_active"])
        report = self.store.verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(self.store.rebuild_state()["changed"], 0)
        snapshot = self.store.snapshot()
        projected = projection.project(
            [dict(message) for message in kit.sample_messages()],
            conversation_messages=kit.sample_messages(),
            snapshot=snapshot,
        )
        self.assertTrue(projected.ok, projected.conflicts)
        self.assertFalse(projected.changed)
        self.assertEqual(
            [(item["kind"], item["reason"]) for item in projected.inert],
            [("keep", "already_active")],
        )

    def test_inert_operations_do_not_diverge_from_replay(self) -> None:
        """Ops committed inert are still recorded; replay must not apply them."""

        span = self.ids["u-a2"]
        self.commit(self.store, [self.drop([span])], "txn-drop")
        inert = self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE, span_ids=(span,), replacement="resurrect"
                )
            ],
            "txn-rewrite-inert",
        )
        self.assertEqual([r.effect for r in inert.inert], ["inert"])
        self.assertEqual([r.reason for r in inert.inert], ["not_active"])
        problems = self.store.verify()["problems"]
        self.assertEqual([p for p in problems if p["code"] == "span_state_divergence"], [])
        replayed = self.store.snapshot().span(span)
        self.assertIsNotNone(replayed)
        assert replayed is not None
        self.assertIs(replayed.state, kit.SpanState.DROPPED)
        self.assertIsNone(replayed.rewrite_text)

    def test_offload_after_drop_is_inert_in_replay(self) -> None:
        span = self.ids["u-a2"]
        self.commit(self.store, [self.drop([span])], "txn-drop")
        self.commit(
            self.store,
            [kit.EditOperation(kind=kit.EditKind.OFFLOAD, span_ids=(span,))],
            "txn-offload-inert",
        )
        problems = self.store.verify()["problems"]
        self.assertEqual([p for p in problems if p["code"] == "span_state_divergence"], [])
        replayed = self.store.snapshot().span(span)
        self.assertIsNotNone(replayed)
        assert replayed is not None
        self.assertIs(replayed.state, kit.SpanState.DROPPED)

    def test_truncated_snapshot_refuses_projection(self) -> None:
        """A capped operation log must fail closed, not project a partial edit."""

        capped = self.open_store("sess-capped", config={"max_operations_in_snapshot": 2})
        ids = self.ingest(capped)
        self.commit(capped, [self.drop([ids["u-a2"]])], "txn-cap-0")
        self.commit(capped, [self.drop([ids["u-a1"], ids["u-t1"]])], "txn-cap-1")
        self.commit(
            capped,
            [kit.EditOperation(kind=kit.EditKind.KEEP, span_ids=(ids["u-a2"],))],
            "txn-cap-2",
        )
        snapshot = capped.snapshot()
        self.assertTrue(snapshot.operations_truncated)
        projected = projection.project(
            [dict(message) for message in kit.sample_messages()],
            conversation_messages=kit.sample_messages(),
            snapshot=snapshot,
        )
        self.assertFalse(projected.ok)
        self.assertEqual(projected.messages, ())
        self.assertEqual(projected.conflicts[0]["code"], "operations_truncated")

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
