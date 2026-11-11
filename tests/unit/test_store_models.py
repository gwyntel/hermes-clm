"""Immutable value types: canonical hashing, strict parsing, deep-copy safety."""

from __future__ import annotations

import copy
import json
import unittest
from types import MappingProxyType

import store_testkit as kit

from hermes_clm.errors import ValidationError
from hermes_clm.models import (
    ActorOrigin,
    ArchivedSpan,
    CommitResult,
    EditKind,
    EditOperation,
    Proposal,
    Receipt,
    SpanOrigin,
    SpanState,
    StoreSnapshot,
    canonical_json,
    canonical_sha256,
    offload_stub,
    origin_for_role,
)


class JsonSurfaceTests(kit.StoreTestCase):
    """Results must survive json.dumps: tools serialise them straight to a client."""

    def test_snapshot_and_results_are_json_serializable(self) -> None:
        store = self.open_store()
        ingest = store.ingest(kit.sample_messages())
        snapshot = store.snapshot()
        target = kit.StoreTestCase.span_ids(store)["u-a2"]
        op = kit.EditOperation(kind=kit.EditKind.DROP, span_ids=(target,), actor_origin="agent")
        staged = store.stage(
            [op], expected_revision=snapshot.revision, txn_id="txn-json", actor_origin="agent"
        )
        commit = store.commit(txn_id="txn-json", expected_revision=snapshot.revision)

        for payload in (
            ingest.to_dict(),
            snapshot.to_dict(),
            snapshot.to_dict(include_messages=True),
            staged.to_dict(),
            commit.to_dict(),
            store.status(),
            store.inspect(),
            store.search("refund"),
            store.retrieve(target),
        ):
            json.dumps(payload)  # no default= hook: everything must be native

    def test_message_is_a_read_only_proxy(self) -> None:
        store = self.open_store()
        store.ingest(kit.sample_messages())
        span = next(iter(store.snapshot().spans.values()))
        self.assertIsInstance(span.message, MappingProxyType)
        with self.assertRaises(TypeError):
            span.message["content"] = "mutated"  # type: ignore[index]


class CanonicalHashTests(unittest.TestCase):
    def test_key_order_does_not_change_hash(self) -> None:
        self.assertEqual(canonical_sha256({"a": 1, "b": 2}), canonical_sha256({"b": 2, "a": 1}))

    def test_json_roundtrip_is_stable(self) -> None:
        payload = {"z": [1, 2, {"y": None}], "a": "üñí"}
        encoded = canonical_json(payload)
        self.assertEqual(encoded, canonical_json(dict(reversed(list(payload.items())))))

    def test_differs_for_different_values(self) -> None:
        self.assertNotEqual(canonical_sha256({"a": 1}), canonical_sha256({"a": 2}))


class EditOperationTests(unittest.TestCase):
    def test_closed_kind_set(self) -> None:
        self.assertEqual(
            {k.value for k in EditKind},
            {"keep", "drop", "rewrite", "pin", "offload", "restore", "boundary"},
        )
        with self.assertRaises(ValidationError):
            EditOperation(kind="summarize", span_ids=("sp_1",))

    def test_rewrite_requires_text(self) -> None:
        with self.assertRaises(ValidationError):
            EditOperation(kind=EditKind.REWRITE, span_ids=("sp_1",), replacement=None)
        with self.assertRaises(ValidationError):
            EditOperation(kind=EditKind.REWRITE, span_ids=("sp_1",), replacement=["a", "b"])

    def test_rewrite_rejects_multimodal_replacement(self) -> None:
        with self.assertRaises(ValidationError):
            EditOperation(
                kind=EditKind.REWRITE, span_ids=("sp_1",), replacement={"type": "image_url"}
            )

    def test_non_rewrite_rejects_replacement(self) -> None:
        with self.assertRaises(ValidationError):
            EditOperation(kind=EditKind.DROP, span_ids=("sp_1",), replacement="x")

    def test_span_ids_required_except_boundary(self) -> None:
        with self.assertRaises(ValidationError):
            EditOperation(kind=EditKind.DROP, span_ids=())
        with self.assertRaises(ValidationError):
            EditOperation(kind=EditKind.BOUNDARY, span_ids=("sp_1",))
        self.assertEqual(EditOperation(kind=EditKind.BOUNDARY).span_ids, ())

    def test_unknown_fields_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            EditOperation.from_mapping(
                {"kind": "drop", "span_ids": ["sp_1"], "session_id": "other"}
            )
        self.assertEqual(ctx.exception.code, "unknown_field")

    def test_provenance_is_read_only(self) -> None:
        op = EditOperation(
            kind=EditKind.REWRITE, span_ids=("sp_1",), replacement="x", provenance={"origin": "aux"}
        )
        self.assertIsInstance(op.provenance, MappingProxyType)
        with self.assertRaises(TypeError):
            op.provenance["origin"] = "operator"  # type: ignore[index]

    def test_hash_is_stable_and_field_sensitive(self) -> None:
        a = EditOperation(kind=EditKind.DROP, span_ids=("sp_1",))
        b = EditOperation(kind=EditKind.DROP, span_ids=("sp_1",))
        c = EditOperation(kind=EditKind.OFFLOAD, span_ids=("sp_1",))
        self.assertEqual(a.sha256, b.sha256)
        self.assertNotEqual(a.sha256, c.sha256)

    def test_actor_origin_coerced_from_string(self) -> None:
        op = EditOperation.from_mapping(
            {"kind": "drop", "span_ids": ["sp_1"], "actor_origin": "operator"}
        )
        self.assertIs(op.actor_origin, ActorOrigin.OPERATOR)


class ArchivedSpanTests(unittest.TestCase):
    def _span(self, **overrides) -> ArchivedSpan:
        payload = dict(
            span_id="sp_1",
            uid="u-1",
            message={"role": "assistant", "content": "hello"},
            origin=SpanOrigin.ASSISTANT,
            protected=False,
            content_sha256=canonical_sha256({"role": "assistant", "content": "hello"}),
        )
        payload.update(overrides)
        return ArchivedSpan(**payload)

    def test_message_is_deep_copied_and_immutable(self) -> None:
        source = {"role": "assistant", "content": "hello"}
        span = ArchivedSpan(
            span_id="sp_1",
            uid="u-1",
            message=source,
            origin=SpanOrigin.ASSISTANT,
            protected=False,
            content_sha256="x",
        )
        source["content"] = "mutated after the fact"
        self.assertEqual(span.message["content"], "hello")
        with self.assertRaises(TypeError):
            span.message["content"] = "nope"  # type: ignore[index]

    def test_deepcopy_is_safe(self) -> None:
        span = self._span()
        clone = copy.deepcopy(span)
        self.assertIs(clone, span)
        snapshot = StoreSnapshot(
            session_id="s",
            db_id="d",
            revision=1,
            head_hash="h",
            spans={"sp_1": span},
            pins={},
        )
        self.assertIs(copy.deepcopy(snapshot), snapshot)

    def test_effective_message_replaces_content_only(self) -> None:
        span = self._span(rewrite_text="rewritten")
        message = span.effective_message()
        self.assertEqual(message["content"], "rewritten")
        self.assertEqual(message["role"], "assistant")

    def test_effective_message_offloaded_uses_stub(self) -> None:
        span = self._span(state=SpanState.OFFLOADED)
        message = span.effective_message()
        self.assertEqual(message["content"], offload_stub("sp_1", span.content_sha256))
        self.assertIn("sp_1", message["content"])
        self.assertNotIn("hello", message["content"])

    def test_stub_has_no_imperative_text(self) -> None:
        stub = offload_stub("sp_9", "a" * 64)
        for word in ("ignore", "must", "you should", "instruction"):
            self.assertNotIn(word, stub.lower())


class SnapshotTests(unittest.TestCase):
    def test_alias_resolution_follows_chain(self) -> None:
        span = ArchivedSpan(
            span_id="sp_3",
            uid="u-1",
            message={"role": "assistant", "content": "v3"},
            origin=SpanOrigin.ASSISTANT,
            protected=False,
            content_sha256="h",
        )
        snapshot = StoreSnapshot(
            session_id="s",
            db_id="d",
            revision=2,
            head_hash="h",
            spans={"sp_3": span},
            aliases={"sp_1": "sp_2", "sp_2": "sp_3"},
        )
        self.assertEqual(snapshot.resolve("sp_1"), "sp_3")
        self.assertEqual(snapshot.span("sp_1").span_id, "sp_3")  # type: ignore[union-attr]
        self.assertEqual(snapshot.resolve("sp_unknown"), "sp_unknown")

    def test_active_spans_are_ordered_by_seq(self) -> None:
        def span(span_id: str, seq: int, state: SpanState) -> ArchivedSpan:
            return ArchivedSpan(
                span_id=span_id,
                uid=span_id,
                message={"role": "assistant", "content": span_id},
                origin=SpanOrigin.ASSISTANT,
                protected=False,
                content_sha256="h",
                seq=seq,
                state=state,
            )

        snapshot = StoreSnapshot(
            session_id="s",
            db_id="d",
            revision=1,
            head_hash="h",
            spans={
                "b": span("b", 2, SpanState.ACTIVE),
                "a": span("a", 1, SpanState.DROPPED),
                "c": span("c", 3, SpanState.ACTIVE),
            },
        )
        self.assertEqual([s.span_id for s in snapshot.active_spans()], ["b", "c"])

    def test_to_dict_is_json_serializable(self) -> None:
        snapshot = StoreSnapshot(session_id="s", db_id="d", revision=1, head_hash="h")
        kit.json.dumps(snapshot.to_dict())


class ResultTypeTests(unittest.TestCase):
    def test_commit_result_roundtrip(self) -> None:
        result = CommitResult(
            ok=True,
            revision=4,
            applied=(Receipt(span_id="sp_1", kind="drop", effect="applied"),),
            inert=(Receipt(span_id="sp_2", kind="drop", effect="inert", reason="not_active"),),
            txn_id="txn-9",
            head_hash="abc",
            kind="edit",
            base_revision=3,
        )
        payload = result.to_dict()
        self.assertEqual(payload["revision"], 4)
        self.assertEqual(payload["applied"][0]["effect"], "applied")
        self.assertEqual(payload["inert"][0]["reason"], "not_active")
        kit.json.dumps(payload)

    def test_proposal_origin_is_aux_only(self) -> None:
        with self.assertRaises(ValidationError):
            Proposal(
                proposal_id="p",
                proposal_sha256="0" * 64,
                base_revision=1,
                operations=(),
                origin="agent",
            )

    def test_role_origin_mapping(self) -> None:
        self.assertIs(origin_for_role("user"), SpanOrigin.USER)
        self.assertIs(origin_for_role(" tool "), SpanOrigin.TOOL)
        self.assertIs(origin_for_role("hacker"), SpanOrigin.UNKNOWN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
