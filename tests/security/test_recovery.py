"""T22-T24/T28-T33 - abandoned txns, derived index, crashes, corruption degrade paths.

Invariants: I8 (staged rows never apply retroactively), I30 (active.json is a cache),
I31 (no auto-repair), I32 (payload tombstone), I33 (structural corruption => no projection),
I34/I35 (one txn per commit; index written after commit).
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import time

import pytest

from _support import contract as C
from _support.assertions import expect_refusal, msg, run_child, subprocess_env

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


def _seed(home, sid, texts=("row-one", "row-two")):
    store = C.for_session(home, sid)
    C.ingest(store, [msg("assistant", t) for t in texts])
    C.commit(store, txn_id="seed", expected_revision=C.snap_revision(C.snapshot(store)))
    return store


@pytest.mark.t("T22")
def test_txn_staged_by_a_crashed_process_is_never_applied(home, sid):
    store = _seed(home, sid)
    revision = C.snap_revision(C.snapshot(store))
    span = list(C.snap_spans(C.snapshot(store)))[0]
    C.close(store)

    child = f"""
import json, os, sys
from _support import contract as C
store = C.for_session({str(home)!r}, {sid!r})
snap = C.snapshot(store)
staged = C.stage(store, [C.make_op("drop", [{span!r}])], expected_revision=C.snap_revision(snap), txn_id="orphan-txn")
print(json.dumps({{"staged": C.res_ok(staged), "revision": C.snap_revision(snap)}}))
sys.stdout.flush()
os._exit(9)  # crash with a staged transaction, never committed
"""
    payload = run_child(child)
    assert payload["_rc"] == 9
    assert payload.get("staged") is True, (
        f"the child could not stage the transaction, so T22 is vacuous: {payload}"
    )

    store = C.for_session(home, sid)
    try:
        after = C.snap_revision(C.snapshot(store))
        result = C.commit(store, txn_id="orphan-txn", expected_revision=after)
        assert not C.res_ok(result), (
            "a staged txn from a crashed process applied retroactively (I8)"
        )
        assert C.snap_revision(C.snapshot(store)) == after
        assert span in C.snap_spans(C.snapshot(store))
    finally:
        C.close(store)


@pytest.mark.t("T23")
def test_stale_active_json_is_discarded_not_replayed(home, sid):
    store = _seed(home, sid, ("resurrect-me", "keep-me"))
    revision = C.snap_revision(C.snapshot(store))
    index = C.active_json(home, sid)
    stale = index.read_bytes() if index.exists() else b""

    spans = list(C.snap_spans(C.snapshot(store)))
    C.stage(store, [C.make_op("drop", [spans[0]])], expected_revision=revision, txn_id="t23-drop")
    assert C.res_ok(C.commit(store, txn_id="t23-drop", expected_revision=revision))
    head = C.snap_revision(C.snapshot(store))
    C.close(store)

    if stale:
        index.write_bytes(stale)

    store = C.for_session(home, sid)
    try:
        assert C.snap_revision(C.snapshot(store)) == head, (
            "stale active.json overrode the DB (F2/I30)"
        )
        out = C.project(store, [msg("assistant", "request")])
        blob = json.dumps(out.messages, default=str) if out.messages else ""
        assert "resurrect-me" not in blob, "dropped content resurrected from a stale index (I30)"
    finally:
        C.close(store)


@pytest.mark.t("T24")
def test_foreign_active_json_is_discarded(home, sid, other_home):
    store = _seed(home, sid, ("own-content",))
    head = C.snap_revision(C.snapshot(store))
    C.close(store)
    foreign = C.for_session(other_home, "foreign-sess")
    C.ingest(foreign, [msg("assistant", "FOREIGN-SECRET-3b7f")])
    C.commit(foreign, txn_id="f1", expected_revision=C.snap_revision(C.snapshot(foreign)))
    C.close(foreign)

    source = C.active_json(other_home, "foreign-sess")
    if not source.exists():
        pytest.skip("foreign session has no active.json to copy (index format may be absent)")
    C.active_json(home, sid).write_bytes(source.read_bytes())

    store = C.for_session(home, sid)
    try:
        assert C.snap_revision(C.snapshot(store)) == head
        out = C.project(store, [msg("assistant", "request")])
        assert "FOREIGN-SECRET-3b7f" not in out.text, "foreign db_id accepted (I30)"
    finally:
        C.close(store)


@pytest.mark.t("T28")
def test_sigkill_during_commit_leaves_a_consistent_head(home, sid):
    _seed(home, sid)
    db = C.db_path(home, sid)
    baseline = C.quick_check(db)
    assert baseline == ["ok"], baseline

    code = f"""
import os, time
from _support import contract as C
store = C.for_session({str(home)!r}, {sid!r})
i = 0
while True:
    i += 1
    snap = C.snapshot(store)
    spans = list(C.snap_spans(snap))
    if not spans:
        break
    rev = C.snap_revision(snap)
    C.stage(store, [C.make_op("rewrite", [spans[0]], replacement=f"churn-{{i}}")], expected_revision=rev, txn_id=f"churn-{{i}}")
    C.commit(store, txn_id=f"churn-{{i}}", expected_revision=rev)
    time.sleep(0.004)
"""
    proc = subprocess.Popen(
        [__import__("sys").executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=subprocess_env(),
    )
    time.sleep(random.uniform(0.05, 0.35))
    proc.kill()
    proc.wait(timeout=30)

    check = C.quick_check(db)
    assert check == ["ok"], f"SIGKILL mid-commit corrupted the database: {check}"
    store = C.for_session(home, sid)
    try:
        snap = C.snapshot(store)
        assert isinstance(C.snap_revision(snap), int) and C.snap_revision(snap) >= 0
        assert C.snap_spans(snap), "all rows lost after an interrupted commit"
        for span in C.snap_spans(snap).values():
            assert C.span_field(span, "content_sha256") or C.span_field(span, "sha256"), (
                "span survived without a content digest (I11)"
            )
        out = C.project(store, [msg("assistant", "request")])
        assert out.declined or out.messages
    finally:
        C.close(store)


@pytest.mark.t("T29")
def test_interrupted_index_write_is_rebuilt_from_the_db(home, sid):
    store = _seed(home, sid)
    index = C.active_json(home, sid)
    before = index.read_bytes() if index.exists() else b""
    revision = C.snap_revision(C.snapshot(store))
    spans = list(C.snap_spans(C.snapshot(store)))
    C.stage(
        store,
        [C.make_op("rewrite", [spans[0]], replacement="post-index-write")],
        expected_revision=revision,
        txn_id="t29",
    )
    assert C.res_ok(C.commit(store, txn_id="t29", expected_revision=revision))
    head = C.snap_revision(C.snapshot(store))
    C.close(store)

    if before:
        index.write_bytes(before)
    out_rebuilt = C.for_session(home, sid)
    try:
        assert C.snap_revision(C.snapshot(out_rebuilt)) == head
    finally:
        C.close(out_rebuilt)


@pytest.mark.t("T30")
@pytest.mark.parametrize("garbage", [b"", b"{", b"\x00\x01\x02", b'{"revision": "nope"}', b"[]"])
def test_garbage_active_json_is_rebuilt(home, sid, garbage):
    store = _seed(home, sid, ("survives-garbage",))
    head = C.snap_revision(C.snapshot(store))
    C.close(store)
    index = C.active_json(home, sid)
    if not index.parent.is_dir():
        pytest.skip("no active.json in this store format")
    index.write_bytes(garbage)

    store = C.for_session(home, sid)
    try:
        assert C.snap_revision(C.snapshot(store)) == head
        out = C.project(store, [msg("assistant", "request")])
        assert out.declined or out.messages, "store unusable after a garbage index (I30)"
        # the archive is authoritative: content must come from the DB, not the index
        assert "survives-garbage" in C.res_text(C.inspect_(store))
    finally:
        C.close(store)


@pytest.mark.t("T31")
def test_failed_quick_check_is_reported_and_never_repaired(home, sid):
    store = _seed(home, sid)
    C.close(store)
    db = C.db_path(home, sid)
    with open(db, "r+b") as fh:
        fh.seek(0)
        fh.write(b"NOT-A-SQLITE-DB\x00\x00")
    before = (db.stat().st_size, C.sha256_bytes(db.read_bytes()))

    failure = None
    try:
        broken = C.for_session(home, sid)
    except Exception as exc:  # noqa: BLE001
        failure = f"{type(exc).__name__}: {exc}"
    else:
        try:
            out = C.project(broken, [msg("assistant", "request")])
            assert out.declined, "structurally corrupt store still produced a projection (F3/I33)"
        finally:
            C.close(broken)

    after = (db.stat().st_size, C.sha256_bytes(db.read_bytes()))
    assert after == before, "store auto-repaired a corrupt database; the bytes are evidence (I31)"


@pytest.mark.t("T32")
def test_one_unreadable_payload_tombstones_and_the_rest_projects(home, sid):
    store = _seed(home, sid, ("PAYLOAD-A", "PAYLOAD-B", "PAYLOAD-C"))
    db = C.db_path(home, sid)
    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    corrupted = False
    try:
        for table, column in C.find_payload_column(conn):
            for rowid, value in conn.execute(f'SELECT rowid, "{column}" FROM "{table}"').fetchall():
                text = (
                    value.decode("utf-8", "replace")
                    if isinstance(value, bytes)
                    else str(value or "")
                )
                if "PAYLOAD-B" not in text:
                    continue
                conn.execute(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?', (text[:20], rowid)
                )
                corrupted = True
                break
            if corrupted:
                break
    finally:
        conn.close()
    if not corrupted:
        pytest.skip("could not locate the PAYLOAD-B row (schema drift)")

    out = C.project(store, [msg("assistant", "request")])
    if out.declined:
        return
    text = out.text
    assert ("PAYLOAD-A" in text) or ("PAYLOAD-C" in text), "one bad payload took out everything"
    assert any(C.is_gap_marker(C.content_of(m)) for m in out.messages), (
        "no explicit gap marker for the truncated payload (I32)"
    )
    C.close(store)


@pytest.mark.t("T33")
def test_unreadable_structure_yields_no_projection(home, sid):
    store = _seed(home, sid, ("structural",))
    db = C.db_path(home, sid)
    C.close(store)

    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    dropped = None
    try:
        for table in C.tables(conn):
            if any(
                tag in table.lower() for tag in ("span", "message", "archive", "payload", "row")
            ):
                conn.execute(f'DROP TABLE "{table}"')
                dropped = table
                break
    finally:
        conn.close()
    if not dropped:
        pytest.skip(f"no structural table matched the expected naming: {dropped}")

    try:
        broken = C.for_session(home, sid)
    except Exception:
        return  # refusing to open is fail-closed
    try:
        reported = False
        try:
            C.snapshot(broken)
        except Exception:
            reported = True
        out = C.project(broken, [msg("assistant", "request")])
        health = C.status(broken)
        blob = json.dumps(health, default=str).lower()
        healthy = not any(word in blob for word in ("corrupt", "error", "fail", "broken", "degrad"))
        assert out.declined or reported or not healthy, (
            "missing structure produced a guessed subset with no error surfaced (F3/I33): "
            f"status={health!r}"
        )
    finally:
        C.close(broken)
