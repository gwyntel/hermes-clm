"""Derived index (``active.json``): non-authoritative, self-healing."""

from __future__ import annotations

import json
import unittest

import store_testkit as kit

from hermes_clm.store import INDEX_FILENAME


class DerivedIndexTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)
        self.index_path = self.store.directory / INDEX_FILENAME

    def _index(self) -> dict:
        return json.loads(self.index_path.read_text())

    def test_index_shape(self) -> None:
        payload = self._index()
        self.assertEqual(payload["format"], 1)
        self.assertEqual(payload["session_id"], self.session_id)
        self.assertEqual(payload["db_id"], self.store.db_id)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(len(payload["head_hash"]), 64)
        self.assertEqual(set(payload["row_hashes"]), {"spans", "operations", "revisions"})
        self.assertEqual(payload["rows"]["spans"], 6)
        self.assertFalse(payload.get("authoritative", False))

    def test_index_tracks_revision_after_commit(self) -> None:
        result = self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        payload = self._index()
        self.assertEqual(payload["revision"], result.revision)
        self.assertEqual(payload["head_hash"], result.head_hash)
        self.assertEqual(payload["generated_at"], self.store.status()["index"]["generated_at"])

    def test_tampered_index_is_not_trusted(self) -> None:
        payload = self._index()
        payload["revision"] = 999
        payload["head_hash"] = "f" * 64
        self.index_path.write_text(kit.canonical_json(payload))
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.revision, 1, "SQLite stays authoritative")
        self.assertNotEqual(snapshot.head_hash, "f" * 64)
        self.assertEqual(self.store.index_status()["state"], "rebuilt")
        self.assertEqual(self._index()["revision"], 1, "stale index is rewritten from SQLite")

    def test_corrupt_index_json_is_rebuilt(self) -> None:
        self.index_path.write_text("{not json")
        status = self.store.index_status()
        self.assertEqual(status["state"], "rebuilt")
        self.assertEqual(self._index()["revision"], 1)

    def test_missing_index_is_rebuilt(self) -> None:
        self.index_path.unlink()
        self.assertEqual(self.store.index_status()["state"], "rebuilt")
        self.assertTrue(self.index_path.exists())

    def test_index_of_other_session_is_rejected(self) -> None:
        payload = self._index()
        payload["session_id"] = "someone-else"
        self.index_path.write_text(kit.canonical_json(payload))
        status = self.store.index_status()
        self.assertEqual(status["state"], "rebuilt")
        self.assertEqual(self._index()["session_id"], self.session_id)

    def test_index_write_failure_never_breaks_commit(self) -> None:
        # a directory where the index file belongs makes the atomic replace fail
        self.index_path.unlink()
        self.index_path.mkdir()
        result = self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")
        self.assertTrue(result.ok)
        self.assertEqual(self.store.snapshot().revision, result.revision)
        self.assertEqual(self.store.index_state, "failed")

    def test_read_only_handle_does_not_write_index(self) -> None:
        reader = self.open_store(read_only=True)
        self.index_path.unlink()
        self.assertEqual(reader.index_status()["state"], "rebuilt")
        self.assertFalse(self.index_path.exists(), "read-only handles never write")

    def test_index_contains_no_message_payloads(self) -> None:
        raw = self.index_path.read_text()
        self.assertNotIn("The file looks fine", raw)
        self.assertNotIn("sk-abcdefghij", raw)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
