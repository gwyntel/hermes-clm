"""Contract guard: the documented entry points keep their exact signatures.

If this suite fails, an integration consumer (engine/tools) will break — the
fix belongs here or the change must be reported to the lead as a deviation.
"""

from __future__ import annotations

import inspect
import unittest

import store_testkit as kit

from hermes_clm import models, store


def params(fn) -> list[tuple[str, str, object]]:
    out = []
    for name, parameter in inspect.signature(fn).parameters.items():
        if name == "self":
            continue
        out.append((name, parameter.kind.name, parameter.default))
    return out


EMPTY = inspect.Parameter.empty


class SignatureTests(unittest.TestCase):
    def test_for_session(self) -> None:
        found = params(store.ContextStore.for_session)
        self.assertEqual(
            [name for name, _, _ in found][:3], ["hermes_home", "session_id", "read_only"]
        )
        self.assertEqual(found[2][1], "KEYWORD_ONLY")
        self.assertIs(found[2][2], False)
        # additive config keyword is documented; nothing else may appear
        self.assertEqual([name for name, _, _ in found][3:], ["config"])

    def test_ingest(self) -> None:
        self.assertEqual(
            params(store.ContextStore.ingest), [("messages", "POSITIONAL_OR_KEYWORD", EMPTY)]
        )

    def test_snapshot_and_status(self) -> None:
        self.assertEqual(params(store.ContextStore.snapshot), [])
        self.assertEqual(params(store.ContextStore.status), [])

    def test_stage(self) -> None:
        found = params(store.ContextStore.stage)
        self.assertEqual(
            [(name, kind) for name, kind, _ in found],
            [
                ("operations", "POSITIONAL_OR_KEYWORD"),
                ("expected_revision", "KEYWORD_ONLY"),
                ("txn_id", "KEYWORD_ONLY"),
                ("actor_origin", "KEYWORD_ONLY"),
            ],
        )

    def test_commit_and_discard(self) -> None:
        self.assertEqual(
            [(name, kind) for name, kind, _ in params(store.ContextStore.commit)],
            [("txn_id", "KEYWORD_ONLY"), ("expected_revision", "KEYWORD_ONLY")],
        )
        self.assertEqual(
            [(name, kind) for name, kind, _ in params(store.ContextStore.discard)],
            [("txn_id", "KEYWORD_ONLY")],
        )

    def test_revert(self) -> None:
        self.assertEqual(
            [name for name, _, _ in params(store.ContextStore.revert)],
            ["target_revision", "expected_revision", "txn_id", "actor_origin"],
        )

    def test_read_surfaces(self) -> None:
        self.assertEqual(
            [(name, kind) for name, kind, _ in params(store.ContextStore.inspect)],
            [("cursor", "KEYWORD_ONLY"), ("limit", "KEYWORD_ONLY")],
        )
        self.assertEqual(
            [(name, kind) for name, kind, _ in params(store.ContextStore.search)],
            [
                ("query", "POSITIONAL_OR_KEYWORD"),
                ("cursor", "KEYWORD_ONLY"),
                ("limit", "KEYWORD_ONLY"),
            ],
        )
        self.assertEqual(
            params(store.ContextStore.retrieve), [("span_id", "POSITIONAL_OR_KEYWORD", EMPTY)]
        )
        self.assertEqual(params(store.ContextStore.close), [])

    def test_proposal_surface(self) -> None:
        self.assertEqual(
            [name for name, _, _ in params(store.ContextStore.create_proposal)],
            ["operations", "base_revision"],
        )
        self.assertEqual(
            [name for name, _, _ in params(store.ContextStore.accept_proposal)],
            ["proposal_id", "proposal_sha256", "base_revision", "txn_id"],
        )

    def test_snapshot_fields(self) -> None:
        fields = models.StoreSnapshot.__dataclass_fields__
        for name in ("revision", "operations", "spans", "pins", "head_hash"):
            self.assertIn(name, fields)

    def test_archived_span_fields(self) -> None:
        fields = models.ArchivedSpan.__dataclass_fields__
        for name in (
            "span_id",
            "uid",
            "aliases",
            "message",
            "group_id",
            "origin",
            "protected",
            "content_sha256",
        ):
            self.assertIn(name, fields)

    def test_edit_operation_fields(self) -> None:
        fields = models.EditOperation.__dataclass_fields__
        for name in ("kind", "span_ids", "replacement", "actor_origin", "provenance"):
            self.assertIn(name, fields)

    def test_proposal_fields(self) -> None:
        fields = models.Proposal.__dataclass_fields__
        for name in ("proposal_id", "proposal_sha256", "base_revision", "operations", "origin"):
            self.assertIn(name, fields)

    def test_result_fields(self) -> None:
        for cls in (models.IngestResult, models.StageResult, models.CommitResult):
            fields = cls.__dataclass_fields__
            for name in ("ok", "revision", "errors", "applied", "inert", "receipts"):
                self.assertIn(name, fields, cls.__name__)

    def test_edit_kinds_are_closed(self) -> None:
        self.assertEqual(
            {kind.value for kind in models.EditKind},
            {"keep", "drop", "rewrite", "pin", "offload", "restore", "boundary"},
        )


class ClosedHandleTests(kit.StoreTestCase):
    def test_closed_handle_refuses_use(self) -> None:
        store = self.open_store()
        self.ingest(store)
        store.close()
        self.assertTrue(store.closed)
        from hermes_clm.errors import StoreClosedError

        for call in (
            store.snapshot,
            store.status,
            lambda: store.ingest([]),
            lambda: store.inspect(),
            lambda: store.search("x"),
            lambda: store.retrieve("sp_x"),
            lambda: store.commit(txn_id="t", expected_revision=1),
            lambda: store.discard(txn_id="t"),
            lambda: store.revert(
                target_revision=1, expected_revision=1, txn_id="t", actor_origin="agent"
            ),
            store.verify,
        ):
            with self.assertRaises(StoreClosedError):
                call()
        store.close()  # idempotent

    def test_context_manager_closes(self) -> None:
        with store_kit_context(self.home, "ctx-session") as store:
            store.ingest([])
        self.assertTrue(store.closed)


def store_kit_context(home, session_id):
    return kit.ContextStore.for_session(home, session_id)


class EdgeCaseTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def test_bool_revision_rejected(self) -> None:
        from hermes_clm.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.store.commit(txn_id="txn-1", expected_revision=True)

    def test_unpin_unknown_span(self) -> None:
        from hermes_clm.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.store.operator_unpin("sp_nope", expected_revision=1, txn_id="txn-unpin")

    def test_revert_txn_id_reuse_with_different_target_refused(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.store.revert(
            target_revision=1, expected_revision=2, txn_id="txn-r", actor_origin="agent"
        )
        from hermes_clm.errors import IdempotencyConflictError

        with self.assertRaises(IdempotencyConflictError):
            self.store.revert(
                target_revision=2, expected_revision=3, txn_id="txn-r", actor_origin="agent"
            )

    def test_ingest_tolerates_odd_uid_types(self) -> None:
        result = self.store.ingest([{"message_uid": 42, "role": "assistant", "content": "x"}])
        self.assertEqual(result.ephemeral_skipped, 1)

    def test_operation_with_long_span_id_rejected(self) -> None:
        from hermes_clm.errors import ValidationError

        with self.assertRaises(ValidationError):
            kit.EditOperation(kind=kit.EditKind.DROP, span_ids=("x" * 500,))

    def test_boundary_mixed_with_edits_is_an_edit(self) -> None:
        result = self.commit(
            self.store,
            [
                kit.EditOperation(kind=kit.EditKind.BOUNDARY),
                self.drop([self.ids["u-a2"]]),
            ],
            "txn-mixed",
        )
        self.assertEqual(result.kind, "edit")
        self.assertEqual(len(result.applied), 1)

    def test_repeated_close_and_reopen_preserves_state(self) -> None:
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.store.close()
        reopened = self.open_store()
        self.assertEqual(reopened.snapshot().revision, 2)
        self.assertIs(reopened.snapshot().span(self.ids["u-a2"]).state, kit.SpanState.DROPPED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
