"""``restore``/``keep`` must not be able to poison the request they reactivate.

A reactivation emits bytes the projector re-checks (`max_rewrite_bytes`, protected
rows). When the store accepted an operation the projector would refuse, the commit
succeeded and *every later projection* was refused — context editing silently died
for the session until a revert. These are the store-side gates that keep a refusal
at the commit boundary where it belongs.
"""

from __future__ import annotations

import unittest

import store_testkit as kit

from hermes_clm.errors import ProtectedSpanError, StoreBudgetError
from hermes_clm.models import EditKind, EditOperation
from hermes_clm.store import StoreConfig


class RestoreSafetyTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.span_ids_of(kit.sample_messages())

    def span_ids_of(self, messages) -> dict[str, str]:
        self.store.ingest(list(messages))
        return self.span_ids(self.store)

    def reactivate(self, kind: EditKind, span_id: str, txn_id: str):
        return self.store.stage(
            [EditOperation(kind=kind, span_ids=(span_id,))],
            expected_revision=self.store.snapshot().revision,
            txn_id=txn_id,
            actor_origin="agent",
        )

    # -- protected rows ---------------------------------------------------

    def test_restore_of_a_protected_row_is_refused_without_a_write(self) -> None:
        revision = self.store.snapshot().revision
        self.assertExceptionCode(
            "protected_span",
            ProtectedSpanError,
            self.reactivate,
            EditKind.RESTORE,
            self.ids["u-q1"],
            "txn-restore-protected",
        )
        self.assertEqual(self.store.snapshot().revision, revision)
        self.assertEqual(self.store.snapshot().span(self.ids["u-q1"]).state, kit.SpanState.ACTIVE)

    def test_restore_of_a_system_row_is_refused(self) -> None:
        revision = self.store.snapshot().revision
        self.assertExceptionCode(
            "protected_span",
            ProtectedSpanError,
            self.reactivate,
            EditKind.RESTORE,
            self.ids["u-sys"],
            "txn-restore-system",
        )
        self.assertEqual(self.store.snapshot().revision, revision)

    # -- active spans -----------------------------------------------------

    def test_restore_of_an_active_span_is_inert_like_keep(self) -> None:
        """No state change, so the projector must not be handed a reactivation."""

        staged = self.reactivate(EditKind.RESTORE, self.ids["u-a2"], "txn-restore-active")
        self.assertTrue(staged.ok)
        self.assertEqual(list(staged.applied), [])
        self.assertEqual(
            [(r.span_id, r.effect, r.reason) for r in staged.inert],
            [(self.ids["u-a2"], "inert", "already_active")],
        )
        # And the keep spelling of the same no-op behaves identically.
        kept = self.reactivate(EditKind.KEEP, self.ids["u-a2"], "txn-keep-active")
        self.assertEqual([(r.effect, r.reason) for r in kept.inert], [("inert", "already_active")])

    # -- payload ceiling --------------------------------------------------

    def test_restore_of_an_oversized_payload_is_refused_not_deferred(self) -> None:
        """The projector would refuse the whole request; the store must refuse first."""

        big = "log line\n" * 400  # ~3.6 KB, over the 1024-byte ceiling below
        messages = kit.sample_messages()
        messages[3] = {
            "message_uid": "u-t1",
            "role": "tool",
            "tool_call_id": "call-1",
            "content": big,
        }
        self.store.close()
        self.store = self.open_store(config=StoreConfig(max_rewrite_bytes=1024))
        self.addCleanup(self.store.close)
        ids = self.span_ids_of(messages)
        group = (ids["u-a1"], ids["u-t1"])
        self.commit(
            self.store,
            [EditOperation(kind=EditKind.OFFLOAD, span_ids=group)],
            "txn-offload-big",
        )
        revision = self.store.snapshot().revision
        self.assertExceptionCode(
            "restore_too_large",
            StoreBudgetError,
            self.store.stage,
            [EditOperation(kind=EditKind.RESTORE, span_ids=group)],
            expected_revision=revision,
            txn_id="txn-restore-big",
            actor_origin="agent",
        )
        self.assertEqual(self.store.snapshot().revision, revision)
        self.assertEqual(self.store.snapshot().span(ids["u-t1"]).state, kit.SpanState.OFFLOADED)

    def test_restore_within_the_ceiling_still_applies(self) -> None:
        group = (self.ids["u-a1"], self.ids["u-t1"])
        self.commit(
            self.store,
            [EditOperation(kind=EditKind.OFFLOAD, span_ids=group)],
            "txn-offload-small",
        )
        result = self.commit(
            self.store,
            [EditOperation(kind=EditKind.RESTORE, span_ids=group)],
            "txn-restore-small",
        )
        self.assertEqual([r.effect for r in result.applied], ["applied", "applied"])
        self.assertEqual(self.store.snapshot().span(self.ids["u-t1"]).state, kit.SpanState.ACTIVE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
