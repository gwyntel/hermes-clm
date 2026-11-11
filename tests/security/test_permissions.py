"""T41-T43 - file modes, tightening on open, hardlink refusal.

Invariants: I43 (0700 dirs, 0600 files, tighten on open, regular file + nlink check).
The parent directory is the real boundary; these modes are defence in depth for copies,
backups, and anything that later moves (review S1).
"""

from __future__ import annotations

import os
import stat

import pytest

from _support import contract as C
from _support.assertions import expect_refusal, msg

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


@pytest.fixture
def umask_0022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _populated(home, sid):
    store = C.for_session(home, sid)
    C.ingest(store, [msg("user", "perm probe")])
    C.commit(store, txn_id="perm", expected_revision=C.snap_revision(C.snapshot(store)))
    C.close(store)


@pytest.mark.t("T41")
def test_fresh_store_ignores_a_permissive_umask(home, sid, umask_0022):
    _populated(home, sid)
    root = home / "clm"
    assert root.is_dir(), "store layout drifted from I1 (clm/sessions/<sid>/)"
    for path in [root, *[p for p in root.rglob("*")]]:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if stat.S_ISDIR(os.stat(path).st_mode):
            assert mode == 0o700, f"directory {path} has mode {oct(mode)} under umask 0022 (I43)"
        else:
            assert mode == 0o600, f"file {path} has mode {oct(mode)} under umask 0022 (I43)"


@pytest.mark.t("T42")
def test_loose_existing_modes_are_tightened_or_refused(home, sid):
    _populated(home, sid)
    root = home / "clm"
    session = C.session_dir(home, sid)
    os.chmod(root, 0o755)
    os.chmod(session, 0o755)
    for path in session.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o644)

    try:
        store = C.for_session(home, sid)
    except Exception:
        return  # refused: acceptable, must be documented
    try:
        C.project(store, [msg("user", "x")])
    finally:
        C.close(store)

    loose = []
    for path in [root, *root.rglob("*")]:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        expected = 0o700 if stat.S_ISDIR(os.stat(path).st_mode) else 0o600
        if mode != expected:
            loose.append((str(path), oct(mode)))
    assert not loose, (
        "store opened with looser modes and did not tighten them; I43 requires tighten "
        f"on open or refuse. Still loose: {loose}"
    )


@pytest.mark.t("T43")
def test_hardlinked_database_is_refused(home, sid, tmp_path):
    _populated(home, sid)
    db = C.db_path(home, sid)
    link = tmp_path / "hardlink.db"
    os.link(db, link)
    assert os.stat(db).st_nlink > 1

    failure = expect_refusal(lambda: C.for_session(home, sid))
    assert failure, f"hardlinked database accepted (I43): nlink={os.stat(db).st_nlink}"
