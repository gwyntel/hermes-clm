"""Session layout and status/health hash-chain signal."""

from __future__ import annotations

import re
import unittest

import store_testkit as kit

from hermes_clm.store import SESSIONS_DIRNAME, STORE_DIRNAME, session_dir, session_key


class SessionLayoutTests(kit.StoreTestCase):
    def test_directory_is_home_clm_sessions_slug_digest(self) -> None:
        store = self.open_store()
        expected = self.home / STORE_DIRNAME / SESSIONS_DIRNAME / session_key(self.session_id)
        self.assertEqual(store.directory, expected)
        self.assertEqual(session_dir(self.home, self.session_id), expected)
        self.assertEqual(expected.parent, self.home / "clm" / "sessions")
        self.assertRegex(expected.name, re.compile(r"^[A-Za-z0-9._-]+-[0-9a-f]{16}$"))
        self.assertTrue((expected / kit.DB_FILENAME).is_file())

    def test_key_slug_is_readable_and_digest_is_over_raw_id(self) -> None:
        key = session_key("Project/Alpha Session")
        slug, digest = key.rsplit("-", 1)
        self.assertEqual(slug, "Project-Alpha-Session")
        self.assertEqual(digest, kit.session_key("Project/Alpha Session").rsplit("-", 1)[1])
        self.assertNotEqual(digest, session_key("Project Alpha Session").rsplit("-", 1)[1])


class StatusHealthChainTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)
        # a committed revision so the chain has a non-genesis link
        self.commit(self.store, [self.drop([self.ids["u-a2"]])], "txn-1")

    def test_status_health_reports_head_hash_chain(self) -> None:
        status = self.store.status()
        self.assertTrue(status["health"]["ok"])
        chain = status["health"]["hash_chain"]
        self.assertTrue(chain["ok"])
        self.assertEqual(chain["checked"], "head")
        self.assertEqual(chain["revision"], status["revision"])
        self.assertEqual(chain["head_hash"], status["head_hash"])

    def test_verify_chain_reports_revision_and_archive_chains(self) -> None:
        report = self.store.verify_chain()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["revision"], self.store.snapshot().revision)
        self.assertGreaterEqual(report["revisions_checked"], 2)
        self.assertGreaterEqual(report["archive_entries"], 1)
        self.assertEqual(report["problems"], [])

    def test_tampered_head_hash_is_exposed_in_status(self) -> None:
        raw = self.raw_connect()
        raw.execute("DROP TRIGGER trg_revisions_no_update")
        raw.execute("UPDATE revisions SET head_hash = ? WHERE revision = 2", ("f" * 64,))
        status = self.store.status()
        self.assertFalse(status["health"]["ok"])
        self.assertFalse(status["health"]["hash_chain"]["ok"])
        self.assertEqual(status["health"]["error"]["code"], "revision_hash_mismatch")
        chain = self.store.verify_chain()
        self.assertFalse(chain["ok"])
        self.assertIn("revision_hash_mismatch", {item["code"] for item in chain["problems"]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
