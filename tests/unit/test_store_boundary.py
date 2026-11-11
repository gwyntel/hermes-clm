"""Boundary markers may carry a string label without becoming content."""

from __future__ import annotations

import json
import unittest

import store_testkit as kit

from hermes_clm.errors import ValidationError
from hermes_clm.models import ActorOrigin, EditKind, EditOperation


class BoundaryLabelModelTests(unittest.TestCase):
    def test_boundary_accepts_string_replacement_as_label(self) -> None:
        op = EditOperation.from_mapping(
            {"kind": "boundary", "span_ids": [], "replacement": "before refactor"}
        )
        self.assertEqual(op.label, "before refactor")
        self.assertEqual(op.replacement, "before refactor")
        self.assertEqual(op.provenance["label"], "before refactor")
        self.assertEqual(op.to_payload()["replacement"], "before refactor")

    def test_boundary_accepts_label_alias(self) -> None:
        op = EditOperation.from_mapping({"kind": "boundary", "label": "task boundary"})
        self.assertEqual(op.label, "task boundary")
        self.assertEqual(op.span_ids, ())

    def test_boundary_rejects_non_string_label(self) -> None:
        for bad in ({"nested": "object"}, 42, ["list"], True):
            with self.subTest(label=bad):
                with self.assertRaises(ValidationError) as ctx:
                    EditOperation.from_mapping(
                        {"kind": "boundary", "span_ids": [], "replacement": bad}
                    )
                self.assertEqual(ctx.exception.code, "invalid_replacement")

    def test_non_boundary_rejects_label_alias(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            EditOperation.from_mapping({"kind": "drop", "span_ids": ["sp_1"], "label": "x"})
        self.assertEqual(ctx.exception.code, "invalid_replacement")

    def test_boundary_rejects_conflicting_provenance_label(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            EditOperation.from_mapping(
                {
                    "kind": "boundary",
                    "replacement": "new",
                    "provenance": {"label": "old"},
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_provenance")

    def test_boundary_still_rejects_span_ids(self) -> None:
        with self.assertRaises(ValidationError):
            EditOperation(kind=EditKind.BOUNDARY, span_ids=("sp_1",), replacement="x")


class BoundaryLabelStoreTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def test_tool_shaped_boundary_stage_commit_records_label_only(self) -> None:
        before = self.store.snapshot()
        # Exact operation shape sent by ToolHandlers.boundary().
        staged = self.store.stage(
            [{"kind": "boundary", "span_ids": [], "replacement": "before refactor"}],
            expected_revision=before.revision,
            txn_id="txn-boundary",
            actor_origin="agent",
        )
        self.assertTrue(staged.ok)
        self.assertEqual(staged.staged_operations[0].label, "before refactor")
        self.assertEqual(self.store.snapshot().revision, before.revision, "stage is not a head")

        result = self.store.commit(txn_id="txn-boundary", expected_revision=before.revision)
        self.assertEqual(result.kind, "boundary")
        self.assertEqual(result.revision, before.revision + 1)
        self.assertEqual(result.applied, ())

        after = self.store.snapshot()
        self.assertEqual(dict(after.counts), dict(before.counts))
        for span_id, span in before.spans.items():
            self.assertEqual(after.span(span_id).state, span.state)
            self.assertIsNone(after.span(span_id).rewrite_text)

        recorded = after.operations[-1]
        self.assertIs(recorded.kind, kit.EditKind.BOUNDARY)
        self.assertEqual(recorded.actor_origin, ActorOrigin.AGENT)
        self.assertEqual(recorded.label, "before refactor")
        self.assertEqual(recorded.provenance["label"], "before refactor")

        raw = self.raw_connect()
        audit = raw.execute(
            "SELECT detail_json FROM audit WHERE event = 'commit:boundary' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(audit)
        detail = json.loads(audit[0])
        self.assertEqual(detail["boundary_labels"], ["before refactor"])

    def test_unlabelled_boundary_still_works(self) -> None:
        result = self.commit(self.store, [EditOperation(kind=EditKind.BOUNDARY)], "txn-plain")
        self.assertEqual(result.kind, "boundary")
        self.assertIsNone(self.store.snapshot().operations[-1].label)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
