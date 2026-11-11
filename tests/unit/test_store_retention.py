"""Bounded retention purge: never current/pinned without an explicit request."""

from __future__ import annotations

import os
import unittest

import store_testkit as kit

from hermes_clm.errors import ValidationError


class RetentionTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.old = "session-old"
        self.pinned = "session-pinned"
        self.new = "session-new"
        self._make_session(self.old, tag="OLD", mtime_offset=-300)
        self._make_session(self.pinned, tag="PINNED", mtime_offset=-200, pin=True)
        self._make_session(self.new, tag="NEW", mtime_offset=-100)

    def _make_session(
        self, session_id: str, *, tag: str, mtime_offset: int, pin: bool = False
    ) -> None:
        store = kit.ContextStore.for_session(self.home, session_id)
        try:
            store.ingest(
                [
                    {
                        "message_uid": f"u-{tag}",
                        "role": "assistant",
                        "content": f"payload {tag}",
                    }
                ]
            )
            if pin:
                span_id = next(iter(store.snapshot().spans))
                revision = store.snapshot().revision
                store.stage(
                    [kit.EditOperation(kind=kit.EditKind.PIN, span_ids=(span_id,))],
                    expected_revision=revision,
                    txn_id="txn-pin",
                    actor_origin="agent",
                )
                store.commit(txn_id="txn-pin", expected_revision=revision)
        finally:
            store.close()
        db = kit.session_dir(self.home, session_id) / kit.DB_FILENAME
        stamp = db.stat().st_mtime + mtime_offset
        os.utime(db, (stamp, stamp))

    def _remaining(self) -> set[str]:
        return {
            item["session_id"]
            for item in kit.ContextStore.list_sessions(self.home)
            if item["session_id"] is not None
        }

    def test_list_sessions_exposes_pinned_and_revision_metadata(self) -> None:
        sessions = {item["session_id"]: item for item in kit.ContextStore.list_sessions(self.home)}
        self.assertEqual(set(sessions), {self.old, self.pinned, self.new})
        for item in sessions.values():
            self.assertTrue(item["ok"])
            self.assertGreaterEqual(item["revision"], 1)
            self.assertIsNotNone(item["store_key"])
        self.assertEqual(sessions[self.pinned]["pinned_count"], 1)
        self.assertEqual(sessions[self.old]["pinned_count"], 0)

    def test_default_purge_keeps_newest_and_never_purges_pinned(self) -> None:
        report = kit.ContextStore.purge_expired_sessions(self.home, keep=1)
        self.assertTrue(report["ok"])
        self.assertEqual(report["purged_count"], 1)
        self.assertEqual(report["purged"][0]["session_id"], self.old)
        self.assertEqual(self._remaining(), {self.pinned, self.new})
        self.assertIn(
            {
                "store_key": kit.session_key(self.pinned),
                "session_id": self.pinned,
                "reason": "pinned",
            },
            report["skipped"],
        )

    def test_current_session_is_never_purged_without_explicit_request(self) -> None:
        report = kit.ContextStore.purge_expired_sessions(
            self.home, keep=0, current_session_id=self.old, include_pinned=True
        )
        self.assertEqual(self._remaining(), {self.old})
        self.assertIn(
            {"store_key": kit.session_key(self.old), "session_id": self.old, "reason": "current"},
            report["skipped"],
        )

    def test_include_current_allows_purging_the_current_session(self) -> None:
        kit.ContextStore.purge_expired_sessions(
            self.home,
            keep=0,
            current_session_id=self.old,
            include_current=True,
            include_pinned=True,
        )
        self.assertEqual(self._remaining(), set())

    def test_include_pinned_is_explicit(self) -> None:
        report = kit.ContextStore.purge_expired_sessions(self.home, keep=1, include_pinned=True)
        self.assertEqual(self._remaining(), {self.new})
        self.assertEqual({item["session_id"] for item in report["purged"]}, {self.old, self.pinned})

    def test_dry_run_reports_without_deleting(self) -> None:
        report = kit.ContextStore.purge_expired_sessions(
            self.home, keep=0, include_pinned=True, dry_run=True
        )
        self.assertEqual(report["purged_count"], 3)
        self.assertEqual(self._remaining(), {self.old, self.pinned, self.new})

    def test_max_purge_bounds_a_sweep(self) -> None:
        report = kit.ContextStore.purge_expired_sessions(
            self.home, keep=0, include_pinned=True, max_purge=1
        )
        self.assertEqual(report["purged_count"], 1)
        self.assertTrue(report["limit_hit"])
        self.assertEqual(len(self._remaining()), 2)
        self.assertTrue(any(item["reason"] == "purge_limit" for item in report["skipped"]))

    def test_unreadable_store_is_listed_but_never_purged(self) -> None:
        broken_dir = kit.session_dir(self.home, "session-broken")
        broken_dir.mkdir(parents=True, exist_ok=True)
        (broken_dir / kit.DB_FILENAME).write_text("not a database", encoding="utf-8")
        sessions = {item["session_id"]: item for item in kit.ContextStore.list_sessions(self.home)}
        self.assertFalse(sessions[None]["ok"])
        report = kit.ContextStore.purge_expired_sessions(
            self.home, keep=0, include_pinned=True, include_current=True
        )
        self.assertTrue(broken_dir.exists(), "unverifiable store must not be deleted")
        self.assertTrue(any(item["reason"] == "unreadable" for item in report["skipped"]))

    def test_invalid_retention_values_are_rejected(self) -> None:
        for kwargs in ({"keep": -1}, {"keep": True}, {"max_purge": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError) as ctx:
                    kit.ContextStore.purge_expired_sessions(self.home, **kwargs)
                self.assertEqual(ctx.exception.code, "invalid_retention")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
