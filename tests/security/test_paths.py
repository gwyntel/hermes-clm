"""T1/T2/T3/T5/T6 - path traversal, mount identity, store-root pinning, cross-session reach.

Invariants under test: I1 (session id shape), I2 (component-wise resolution, no links),
I3 (db session_id/db_id mount assertion), I4 (store root not relocatable), I40 (no
sibling/parent reach).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _support import contract as C
from _support.adversarial import BAD_SESSION_IDS, GOOD_SESSION_IDS
from _support.assertions import expect_refusal, msg

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


def _populated(home, sid, text="VICTIM-SESSION-CONTENT-8f21"):
    store = C.for_session(home, sid)
    try:
        C.ingest(store, [msg("user", text), msg("assistant", "ack " + text)])
        C.commit(store, txn_id=f"txn-{sid}-1", expected_revision=C.snap_revision(C.snapshot(store)))
    finally:
        C.close(store)
    return text


@pytest.mark.t("T1")
@pytest.mark.parametrize("bad", BAD_SESSION_IDS)
def test_hostile_session_id_never_escapes_and_is_refused_or_hashed(home, bad):
    """Hostile ids are either refused up front or mapped to a confined, hashed key."""
    before = sorted(str(p) for p in home.rglob("*"))
    try:
        store = C.for_session(home, bad)
    except Exception:
        after = sorted(str(p) for p in home.rglob("*"))
        assert after == before, (
            "a rejected session id must be refused before the filesystem is touched; "
            f"created: {sorted(set(after) - set(before))}"
        )
        return
    try:
        directory = Path(store.directory).resolve()
        root = (home / "clm" / "sessions").resolve()
        assert os.path.commonpath([str(directory), str(root)]) == str(root)
        assert Path(bad).name != directory.name or directory.name.startswith("session-")
    finally:
        C.close(store)


@pytest.mark.t("T1")
@pytest.mark.parametrize("good", GOOD_SESSION_IDS)
def test_host_shaped_session_ids_accepted_and_confined(home, good):
    store = C.for_session(home, good)
    try:
        created = Path(store.directory).resolve()
        root = (home / "clm" / "sessions").resolve()
        assert os.path.commonpath([str(created), str(root)]) == str(root)
        assert created.parent == root
        assert created.name.startswith(good[:8])
    finally:
        C.close(store)


@pytest.mark.t("T2")
def test_session_directory_that_is_a_symlink_is_refused(home, sid):
    victim_text = _populated(home, "victim")
    link = C.session_dir(home, sid)
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(C.session_dir(home, "victim"), link)

    failure = expect_refusal(lambda: C.for_session(home, sid))
    assert failure, "a symlinked session directory was accepted (I2)"
    # and never a projection of the foreign session
    try:
        store = C.for_session(home, sid)
    except Exception:
        return
    out = C.project(store, [msg("user", "hello")])
    C.close(store)
    assert victim_text not in out.text, "symlinked session dir leaked foreign rows"


@pytest.mark.t("T3")
def test_database_swapped_for_symlink_to_other_session_is_refused(home, sid):
    victim_text = _populated(home, "victim")
    store = C.for_session(home, sid)
    C.ingest(store, [msg("user", "own content")])
    C.close(store)

    db = C.db_path(home, sid)
    for sibling in db.parent.glob(db.name + "*"):
        sibling.unlink()
    os.symlink(C.db_path(home, "victim"), db)

    opened = None
    try:
        opened = C.for_session(home, sid)
    except Exception:
        return  # refused at mount: I3 satisfied
    try:
        out = C.project(opened, [msg("user", "hello")])
    finally:
        C.close(opened)
    assert victim_text not in out.text, (
        "db.session_id/db_id mismatch must be a mount error, not a foreign projection"
    )


@pytest.mark.t("T3")
def test_mount_identity_mismatch_refuses_to_open(home, sid):
    """Correct path, wrong file: the only cheap defence is asserting identity on open."""
    _populated(home, "victim", "VICTIM-COPY-9911")
    store = C.for_session(home, sid)
    C.ingest(store, [msg("user", "own content")])
    C.close(store)

    db = C.db_path(home, sid)
    for sibling in db.parent.glob(db.name + "*"):
        sibling.unlink()
    import shutil

    shutil.copyfile(C.db_path(home, "victim"), db)

    failure = expect_refusal(lambda: C.for_session(home, sid))
    assert failure, "a database copied from another session was accepted as this session (I3)"


@pytest.mark.t("T5")
def test_active_json_cannot_relocate_the_store_root(home, sid, tmp_path):
    _populated(home, sid)
    evil = tmp_path / "evil-root"
    index = C.active_json(home, sid)
    assert index.parent.is_dir(), "store layout drifted from I1 (clm/sessions/<sid>/)"
    index.write_text(
        json.dumps(
            {
                "db_id": "x",
                "session_id": sid,
                "revision": 0,
                "store_root": str(evil),
                "path": str(evil),
                "db_path": str(evil / "store.db"),
                "hermes_home": str(evil),
            }
        )
    )

    store = C.for_session(home, sid)
    try:
        out = C.project(store, [msg("user", "hello")])
        status = C.status(store)
    finally:
        C.close(store)

    assert not evil.exists(), "active.json relocated the store root (I4)"
    assert out.declined or out.messages, "store must stay usable after a hostile index"
    assert str(evil) not in json.dumps(status, default=str)


@pytest.mark.t("T5")
def test_stored_content_cannot_relocate_the_store_root(home, sid, tmp_path):
    evil = tmp_path / "evil-two"
    store = C.for_session(home, sid)
    C.ingest(
        store,
        [
            msg(
                "user",
                "note",
                metadata={"store_root": str(evil), "hermes_home": str(evil)},
            ),
            msg("assistant", json.dumps({"store_root": str(evil)})),
        ],
    )
    C.commit(store, txn_id="txn-t5", expected_revision=C.snap_revision(C.snapshot(store)))
    C.close(store)
    assert not evil.exists()
    reopened = C.for_session(home, sid)
    C.close(reopened)
    assert C.db_path(home, sid).parent == C.session_dir(home, sid)


@pytest.mark.t("T6")
def test_cross_session_retrieve_is_refused(home, sid):
    victim_text = _populated(home, "victim", "VICTIM-SPAN-4c9d")
    victim = C.for_session(home, "victim")
    victim_span = next(iter(C.snap_spans(C.snapshot(victim))))
    C.close(victim)

    probe = C.for_session(home, sid)
    try:
        failure = expect_refusal(lambda: C.retrieve(probe, victim_span))
        assert failure
        assert victim_text not in failure
        for call in (
            lambda: C.inspect_(probe),
            lambda: C.search(probe, "VICTIM-SPAN"),
        ):
            result = call()
            assert victim_text not in C.res_text(result), "cross-session read leaked content"
    finally:
        C.close(probe)


@pytest.mark.t("T6")
def test_read_entry_points_have_no_session_or_path_selector():
    store_type = C.store_cls()
    forbidden = {"session_id", "sid", "path", "hermes_home", "db_path", "session", "root"}
    for name in ("retrieve", "inspect", "search", "status", "snapshot"):
        fn = getattr(store_type, name, None)
        if fn is None:
            continue
        import inspect

        params = set(inspect.signature(fn).parameters)
        assert not (params & forbidden), f"{name} accepts a session/path selector: {params}"
