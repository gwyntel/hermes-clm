"""Bounded, session-scoped read surfaces: inspect/search/retrieve."""

from __future__ import annotations

import unittest

import store_testkit as kit

from hermes_clm.errors import CorruptStoreError, NotFoundError, ValidationError
from hermes_clm.store import StoreConfig


class InspectTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def test_inspect_is_bounded_and_paginated(self) -> None:
        first = self.store.inspect(limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = self.store.inspect(limit=2, cursor=first["next_cursor"])
        self.assertEqual(len(second["items"]), 2)
        ids = {item["span_id"] for item in first["items"]} | {
            item["span_id"] for item in second["items"]
        }
        self.assertEqual(len(ids), 4)

    def test_inspect_limit_is_clamped(self) -> None:
        page = self.store.inspect(limit=10_000)
        self.assertEqual(page["limit"], self.store.config.inspect_limit_max)
        self.assertEqual(len(page["items"]), 6)

    def test_inspect_items_expose_state_and_no_payload_leak(self) -> None:
        item = next(i for i in self.store.inspect()["items"] if i["uid"] == "u-q1")
        self.assertTrue(item["protected"])
        self.assertEqual(item["state"], "active")
        self.assertEqual(item["origin"], "user")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", item["preview"])
        self.assertLessEqual(len(item["preview"]), 240)

    def test_inspect_rejects_tampered_cursor(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.inspect(cursor="not-a-cursor")
        with self.assertRaises(ValidationError):
            self.store.inspect(limit=0)

    def test_inspect_never_lists_another_session(self) -> None:
        other = self.open_store("other-session")
        other.ingest([{"message_uid": "u-other", "role": "assistant", "content": "other"}])
        listed = {item["uid"] for item in self.store.inspect()["items"]}
        self.assertNotIn("u-other", listed)


class SearchTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def test_search_finds_current_and_archived_text(self) -> None:
        hits = self.store.search("a.py")["items"]
        self.assertEqual(len(hits), 2)  # the tool call arguments and the tool result
        self.assertTrue(all(len(hit["snippet"]) > 0 for hit in hits))

    def test_search_is_case_insensitive_and_paginated(self) -> None:
        first = self.store.search("A.PY", limit=1)
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        second = self.store.search("a.py", limit=5, cursor=first["next_cursor"])
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["span_id"], second["items"][0]["span_id"])

    def test_search_does_not_return_redacted_secrets(self) -> None:
        self.assertEqual(self.store.search("sk-abcdefghijklmnopqrstuvwx")["items"], [])
        self.assertEqual(len(self.store.search("[REDACTED:openai_key]")["items"]), 1)

    def test_search_finds_rewritten_content(self) -> None:
        self.commit(
            self.store,
            [
                kit.EditOperation(
                    kind=kit.EditKind.REWRITE,
                    span_ids=(self.ids["u-a2"],),
                    replacement="condensed summary marker",
                )
            ],
            "txn-1",
        )
        hits = self.store.search("condensed summary")["items"]
        self.assertEqual([hit["span_id"] for hit in hits], [self.ids["u-a2"]])
        self.assertEqual(hits[0]["state"], "active")

    def test_search_validation(self) -> None:
        for bad in ("", "   ", "x" * 10_000, None):
            with self.assertRaises(ValidationError):
                self.store.search(bad)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            self.store.search("x", cursor="garbage")

    def test_search_limit_is_clamped(self) -> None:
        page = self.store.search("the", limit=10_000)
        self.assertEqual(page["limit"], self.store.config.search_limit_max)


class RetrieveTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def test_retrieve_returns_exact_archived_detail(self) -> None:
        payload = self.store.retrieve(self.ids["u-t1"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"]["content"], "content of a.py")
        self.assertEqual(payload["role"], "tool")
        self.assertEqual(
            payload["content_sha256"], self.store.snapshot().span(self.ids["u-t1"]).content_sha256
        )
        self.assertEqual(payload["session_id"], self.session_id)

    def test_retrieve_rejects_foreign_and_unknown_ids(self) -> None:
        other = self.open_store("other-session")
        other_ids = self.ingest(
            other, [{"message_uid": "u-x", "role": "assistant", "content": "x"}]
        )
        with self.assertRaises(NotFoundError):
            self.store.retrieve(other_ids["u-x"])
        with self.assertRaises(NotFoundError):
            self.store.retrieve("sp_made_up")
        with self.assertRaises(ValidationError):
            self.store.retrieve("")

    def test_retrieve_enforces_byte_budget(self) -> None:
        big = self.open_store("budget-session", config=StoreConfig(max_retrieve_bytes=64))
        big.ingest([{"message_uid": "u-big", "role": "assistant", "content": "y" * 500}])
        span_id = next(iter(big.snapshot().spans))
        payload = big.retrieve(span_id)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["errors"][0]["code"], "retrieve_too_large")
        self.assertNotIn("message", payload)
        self.assertGreater(payload["bytes"], payload["limit"])

    def test_retrieve_detects_payload_corruption(self) -> None:
        raw = self.raw_connect()
        raw.execute("DROP TRIGGER trg_spans_no_update")
        raw.execute(
            "UPDATE spans SET message_json = ? WHERE span_id = ?",
            (kit.canonical_json({"role": "tool", "content": "tampered"}), self.ids["u-t1"]),
        )
        with self.assertRaises(CorruptStoreError) as ctx:
            self.store.retrieve(self.ids["u-t1"])
        self.assertEqual(ctx.exception.code, "payload_hash_mismatch")

    def test_snapshot_hash_verification_can_be_requested(self) -> None:
        store = self.open_store("verify-session", config=StoreConfig(snapshot_verify_hashes=True))
        ids = self.ingest(store)
        store.close()
        raw = self.raw_connect("verify-session")
        raw.execute("DROP TRIGGER trg_spans_no_update")
        raw.execute(
            "UPDATE spans SET message_json = ? WHERE span_id = ?",
            (kit.canonical_json({"role": "assistant", "content": "tampered"}), ids["u-a2"]),
        )
        reopened = self.open_store(
            "verify-session", config=StoreConfig(snapshot_verify_hashes=True)
        )
        snapshot = reopened.snapshot()
        by_uid = {span.uid: span for span in snapshot.spans.values()}
        self.assertIn("unreadable", str(by_uid["u-a2"].message))
        self.assertEqual(by_uid["u-t1"].message.get("content"), "content of a.py")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
