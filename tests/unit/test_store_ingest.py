"""Ingestion: redaction at rest, UID/content dedupe, aliases, protections."""

from __future__ import annotations

import copy
import unittest

import store_testkit as kit

from hermes_clm.errors import ValidationError
from hermes_clm.store import StoreConfig


class IngestBasicsTests(kit.StoreTestCase):
    def test_archives_every_uid_message_once(self) -> None:
        store = self.open_store()
        result = store.ingest(kit.sample_messages())
        self.assertTrue(result.ok)
        self.assertEqual(result.archived, 6)
        self.assertEqual(result.duplicates, 0)
        self.assertEqual(result.spans_total, 6)
        self.assertEqual(result.revision, 1, "ingest must not advance the edit revision")

    def test_reingest_is_idempotent(self) -> None:
        store = self.open_store()
        first = store.ingest(kit.sample_messages())
        second = store.ingest(kit.sample_messages())
        self.assertEqual(second.archived, 0)
        self.assertEqual(second.duplicates, first.archived)
        self.assertEqual(len(store.snapshot().spans), 6)

    def test_input_is_never_mutated_or_aliased(self) -> None:
        store = self.open_store()
        messages = kit.sample_messages()
        original = copy.deepcopy(messages)
        store.ingest(messages)
        self.assertEqual(messages, original, "ingest must not mutate caller messages")
        messages[0]["content"] = "mutated after ingest"
        snapshot = store.snapshot()
        system_span = next(s for s in snapshot.spans.values() if s.uid == "u-sys")
        self.assertEqual(system_span.message["content"], "You are a careful coding agent.")

    def test_ephemeral_messages_are_skipped(self) -> None:
        store = self.open_store()
        result = store.ingest(
            [
                {"role": "assistant", "content": "no uid here"},
                {"message_uid": "", "role": "assistant", "content": "empty uid"},
                {"message_uid": "u-1", "role": "assistant", "content": "kept"},
            ]
        )
        self.assertEqual(result.ephemeral_skipped, 2)
        self.assertEqual(result.archived, 1)

    def test_bad_input_shape_is_rejected(self) -> None:
        store = self.open_store()
        for bad in (
            "not-a-list",
            {"role": "user"},
            42,
            [{"role": "user", "content": "x"}][0:0] + ["nope"],
        ):
            with self.assertRaises(ValidationError):
                store.ingest(bad)  # type: ignore[arg-type]

    def test_sequence_numbers_follow_history_order(self) -> None:
        store = self.open_store()
        self.ingest(store)
        spans = sorted(store.snapshot().spans.values(), key=lambda s: s.seq)
        self.assertEqual([s.uid for s in spans], ["u-sys", "u-q1", "u-a1", "u-t1", "u-a2", "u-q2"])


class RedactionAtRestTests(kit.StoreTestCase):
    def test_secrets_are_redacted_before_archival(self) -> None:
        store = self.open_store()
        result = store.ingest(kit.sample_messages(secret="sk-abcdefghijklmnopqrstuvwx"))
        self.assertEqual(result.redactions, 1)
        raw = self.raw_connect()
        payloads = [row[0] for row in raw.execute("SELECT payload_json FROM archive")]
        blob = "".join(payloads)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", blob)
        self.assertIn("[REDACTED:openai_key]", blob)

    def test_retrieve_returns_redacted_text_not_the_secret(self) -> None:
        store = self.open_store()
        ids = self.ingest(store)
        retrieved = store.retrieve(ids["u-q1"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", retrieved["message"]["content"])
        self.assertEqual(
            retrieved["content_sha256"], store.snapshot().span(ids["u-q1"]).content_sha256
        )

    def test_opt_out_is_explicit_and_recorded(self) -> None:
        store = self.open_store(config=StoreConfig(archive_redact=False))
        result = store.ingest(kit.sample_messages(secret="sk-abcdefghijklmnopqrstuvwx"))
        self.assertEqual(result.redactions, 0)
        raw = self.raw_connect()
        row = raw.execute("SELECT redacted FROM archive LIMIT 1").fetchone()
        self.assertEqual(row[0], 0, "opt-out archives must be marked unredacted")
        blob = "".join(r[0] for r in raw.execute("SELECT payload_json FROM archive"))
        self.assertIn("sk-abcdefghijklmnopqrstuvwx", blob)


class ContentVersionTests(kit.StoreTestCase):
    def test_changed_content_creates_version_and_absorbing_alias(self) -> None:
        store = self.open_store()
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v1"}])
        old_id = self.span_ids(store)["u-1"]
        result = store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v2"}])
        new_id = self.span_ids(store)["u-1"]
        self.assertEqual(result.archived, 1)
        self.assertEqual(result.versions, 1)
        self.assertEqual(result.aliases_absorbed, 1)
        self.assertNotEqual(old_id, new_id)
        snapshot = store.snapshot()
        self.assertEqual(len(snapshot.spans), 1, "only the current version is live")
        self.assertEqual(snapshot.resolve(old_id), new_id)
        self.assertEqual(snapshot.aliases[old_id], new_id)

    def test_alias_chain_resolves_through_three_versions(self) -> None:
        store = self.open_store()
        versions = []
        for index in range(3):
            store.ingest([{"message_uid": "u-1", "role": "assistant", "content": f"v{index}"}])
            versions.append(self.span_ids(store)["u-1"])
        snapshot = store.snapshot()
        self.assertEqual(snapshot.resolve(versions[0]), versions[-1])
        self.assertEqual(snapshot.resolve(versions[1]), versions[-1])
        self.assertEqual(store.retrieve(versions[0])["span_id"], versions[-1])
        self.assertEqual(store.retrieve(versions[0])["message"]["content"], "v2")

    def test_historical_version_payload_stays_archived(self) -> None:
        store = self.open_store()
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v1"}])
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v2"}])
        raw = self.raw_connect()
        contents = [
            row[0] for row in raw.execute("SELECT payload_json FROM archive ORDER BY archive_id")
        ]
        self.assertEqual(len(contents), 2)
        self.assertIn("v1", contents[0])
        self.assertIn("v2", contents[1])

    def test_duplicate_content_after_version_reuses_existing_span(self) -> None:
        store = self.open_store()
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v1"}])
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v2"}])
        result = store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v1"}])
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.archived, 0)
        self.assertEqual(store.retrieve(self.span_ids(store)["u-1"])["message"]["content"], "v1")


class ProtectionTests(kit.StoreTestCase):
    def test_roles_map_to_protection(self) -> None:
        store = self.open_store()
        self.ingest(store)
        snapshot = store.snapshot()
        by_uid = {span.uid: span for span in snapshot.spans.values()}
        for uid in ("u-sys", "u-q1", "u-q2"):
            self.assertTrue(by_uid[uid].protected, uid)
            self.assertFalse(by_uid[uid].editable, uid)
        for uid in ("u-a1", "u-t1", "u-a2"):
            self.assertFalse(by_uid[uid].protected, uid)
            self.assertTrue(by_uid[uid].editable, uid)

    def test_unknown_roles_fail_closed(self) -> None:
        store = self.open_store()
        store.ingest([{"message_uid": "u-x", "role": "wizard", "content": "hi"}])
        span = next(iter(store.snapshot().spans.values()))
        self.assertTrue(span.protected)
        self.assertEqual(span.origin.value, "unknown")

    def test_multimodal_is_protected(self) -> None:
        store = self.open_store()
        store.ingest(
            [
                {
                    "message_uid": "u-img",
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "x"}},
                    ],
                }
            ]
        )
        span = next(iter(store.snapshot().spans.values()))
        self.assertTrue(span.protected)
        with self.assertRaises(Exception) as ctx:
            self.commit(
                store,
                [kit.EditOperation(kind=kit.EditKind.OFFLOAD, span_ids=(span.span_id,))],
                "t1",
            )
        self.assertEqual(getattr(ctx.exception, "code", None), "protected_span")

    def test_host_can_only_escalate_protection(self) -> None:
        store = self.open_store()
        store.ingest(
            [
                {
                    "message_uid": "u-a",
                    "role": "assistant",
                    "content": "keep me",
                    "protected": True,
                },
                {
                    "message_uid": "u-b",
                    "role": "user",
                    "content": "do not unprotect",
                    "protected": False,
                },
            ]
        )
        by_uid = {span.uid: span for span in store.snapshot().spans.values()}
        self.assertTrue(by_uid["u-a"].protected, "host hint may escalate")
        self.assertTrue(by_uid["u-b"].protected, "user rows stay protected regardless of the hint")


class GroupTests(kit.StoreTestCase):
    def test_tool_group_is_derived_from_call_ids(self) -> None:
        store = self.open_store()
        self.ingest(store)
        by_uid = {span.uid: span for span in store.snapshot().spans.values()}
        self.assertIsNotNone(by_uid["u-a1"].group_id)
        self.assertEqual(by_uid["u-a1"].group_id, by_uid["u-t1"].group_id)
        self.assertIsNone(by_uid["u-a2"].group_id)

    def test_unpaired_tool_result_is_not_grouped(self) -> None:
        store = self.open_store()
        store.ingest(
            [{"message_uid": "u-orphan", "role": "tool", "tool_call_id": "nope", "content": "x"}]
        )
        self.assertIsNone(next(iter(store.snapshot().spans.values())).group_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
