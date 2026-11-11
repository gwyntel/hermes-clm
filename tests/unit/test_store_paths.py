"""Derived path safety, permissions and session isolation."""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path

import store_testkit as kit

from hermes_clm.errors import NotFoundError, PathSafetyError, ValidationError
from hermes_clm.store import (
    ContextStore,
    DB_FILENAME,
    INDEX_FILENAME,
    LOCK_FILENAME,
    session_dir,
    session_key,
    sessions_root,
)


class SessionKeyTests(unittest.TestCase):
    def test_key_is_stable_and_filesystem_safe(self) -> None:
        key = session_key("session/with:weird chars")
        self.assertEqual(key, session_key("session/with:weird chars"))
        self.assertNotIn("/", key)
        self.assertNotIn(":", key)
        self.assertNotIn(" ", key)

    def test_traversal_attempts_cannot_escape(self) -> None:
        for hostile in ("../../etc/passwd", "..", ".", "/etc/shadow", "a/../../../b"):
            key = session_key(hostile)
            self.assertNotIn("..", key)
            self.assertFalse(key.startswith("."))
            path = Path("/tmp/home/clm/sessions") / key
            self.assertNotIn("..", str(path.relative_to("/tmp/home/clm/sessions")))

    def test_distinct_sessions_never_collide(self) -> None:
        pairs = [
            ("session-1", "session_1"),
            ("session 1", "session~1"),
            ("A" * 256, "A" * 250 + "B" * 6),
            ("sess/1", "sess\\1"),
        ]
        keys = [session_key(a) for pair in pairs for a in pair]
        self.assertEqual(len(keys), len(set(keys)))

    def test_length_bound_and_empty_rejected(self) -> None:
        self.assertLessEqual(len(session_key("x" * 256)), 256)
        with self.assertRaises(ValidationError):
            session_key("")
        with self.assertRaises(ValidationError):
            session_key("bad\x00id")
        with self.assertRaises(ValidationError):
            session_key("x" * 500)

    def test_non_string_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            session_key(1234)  # type: ignore[arg-type]


class DirectoryLayoutTests(kit.StoreTestCase):
    def test_layout_and_permissions(self) -> None:
        store = self.open_store()
        store.ingest([])
        directory = session_dir(self.home, self.session_id)
        self.assertEqual(store.directory, directory)
        self.assertEqual(sessions_root(self.home), self.home / "clm" / "sessions")
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(directory.parent).st_mode), 0o700)
        for name in (DB_FILENAME, LOCK_FILENAME, INDEX_FILENAME):
            path = directory / name
            self.assertTrue(path.exists(), f"missing {name}")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, f"{name} permissions")

    def test_existing_loose_permissions_are_tightened(self) -> None:
        directory = session_dir(self.home, self.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o755)
        os.chmod(directory.parent, 0o755)
        ContextStore.for_session(self.home, self.session_id).close()
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(directory.parent).st_mode), 0o700)

    def test_symlinked_session_dir_is_refused(self) -> None:
        root = sessions_root(self.home)
        root.mkdir(parents=True, exist_ok=True)
        target = Path(self.tmp) / "elsewhere"
        target.mkdir()
        link = root / session_key(self.session_id)
        os.symlink(target, link)
        with self.assertRaises(PathSafetyError) as ctx:
            ContextStore.for_session(self.home, self.session_id)
        self.assertEqual(ctx.exception.code, "symlink_refused")

    def test_symlinked_db_file_is_refused(self) -> None:
        store = self.open_store()
        store.ingest([])
        store.close()
        directory = session_dir(self.home, self.session_id)
        real = directory / "real.db"
        os.replace(directory / DB_FILENAME, real)
        os.symlink(real, directory / DB_FILENAME)
        with self.assertRaises(PathSafetyError):
            ContextStore.for_session(self.home, self.session_id)
        with self.assertRaises(PathSafetyError):
            ContextStore.for_session(self.home, self.session_id, read_only=True)

    def test_sessions_are_isolated_on_disk(self) -> None:
        first = self.open_store("session-a")
        second = self.open_store("session-b")
        ids_a = self.ingest(first)
        ids_b = self.ingest(second, [{"message_uid": "other", "role": "assistant", "content": "b"}])
        self.assertNotEqual(first.directory, second.directory)
        self.assertNotIn(ids_a["u-a2"], ids_b.values())
        # a span id from session a must not resolve in session b
        with self.assertRaises(NotFoundError):
            second.retrieve(ids_a["u-a2"])

    def test_read_only_missing_store_does_not_create_directories(self) -> None:
        with self.assertRaises(NotFoundError):
            self.open_store("never-created", read_only=True)
        self.assertFalse(session_dir(self.home, "never-created").exists())

    def test_home_with_trailing_separator_is_normalised(self) -> None:
        store = ContextStore.for_session(str(self.home) + os.sep, self.session_id)
        self.addCleanup(store.close)
        self.assertEqual(store.directory.name, session_key(self.session_id))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
