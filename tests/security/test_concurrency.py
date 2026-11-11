"""T34/T35/T36/T38 - single writer, subagent isolation, lock contention, pin race.

Invariants: I38 (WAL, busy_timeout, BEGIN IMMEDIATE), I39 (single writer per session file),
I40 (own directory per session/subagent; no lineage guessing), I23 (pin re-checked inside
the committing transaction).
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from _support import contract as C
from _support.assertions import assert_strings_absent, expect_refusal, msg, run_child

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


def _seed(store, texts=("one", "two")):
    C.ingest(store, [msg("assistant", t) for t in texts])
    C.commit(store, txn_id="seed", expected_revision=C.snap_revision(C.snapshot(store)))
    return C.snap_revision(C.snapshot(store)), list(C.snap_spans(C.snapshot(store)))


@pytest.mark.t("T34")
def test_second_opener_is_read_only_and_its_commits_are_rejected(home, sid):
    writer = C.for_session(home, sid)
    revision, spans = _seed(writer)

    code = f"""
import json
from _support import contract as C
result = {{"opened": False}}
try:
    store = C.for_session({str(home)!r}, {sid!r})
    result["opened"] = True
    snap = C.snapshot(store)
    rev = C.snap_revision(snap)
    spans = list(C.snap_spans(snap))
    result["snapshot_revision"] = rev
    try:
        staged = C.stage(store, [C.make_op("drop", [spans[0]])], expected_revision=rev, txn_id="second-writer")
        result["stage_ok"] = C.res_ok(staged)
        result["stage_errors"] = C.res_errors(staged)
        if result["stage_ok"]:
            committed = C.commit(store, txn_id="second-writer", expected_revision=rev)
            result["commit_ok"] = C.res_ok(committed)
            result["commit_errors"] = C.res_errors(committed)
    finally:
        C.close(store)
except Exception as exc:
    result["exception"] = f"{{type(exc).__name__}}: {{exc}}"
print(json.dumps(result))
"""
    payload = run_child(code)
    if not payload.get("opened"):
        # refusing the second opener outright satisfies I39 as well
        assert payload.get("exception"), payload
        assert C.snap_revision(C.snapshot(writer)) == revision
        C.close(writer)
        return
    assert payload.get("commit_ok") is not True, (
        f"a second opener committed to a session owned by another process (I39): {payload}"
    )
    assert payload.get("stage_ok") is not True or payload.get("commit_ok") is False

    snap = C.snapshot(writer)
    assert C.snap_revision(snap) == revision, "second writer moved the revision (I39)"
    assert spans[0] in C.snap_spans(snap)
    C.close(writer)


@pytest.mark.t("T35")
def test_sibling_and_parent_sessions_are_unreachable(home, sid, other_home):
    parent = C.for_session(home, "parent-sess")
    C.ingest(parent, [msg("user", "PARENT-SECRET-c41d")])
    C.commit(parent, txn_id="p1", expected_revision=C.snap_revision(C.snapshot(parent)))
    parent_span = next(iter(C.snap_spans(C.snapshot(parent))))

    child = C.for_session(home, "child-sess")
    try:
        C.ingest(child, [msg("user", "child content")])
        C.commit(child, txn_id="c1", expected_revision=C.snap_revision(C.snapshot(child)))
        assert expect_refusal(lambda: C.retrieve(child, parent_span))
        assert_strings_absent(C.inspect_(child), ["PARENT-SECRET-c41d"], where="child inspect")
        assert_strings_absent(
            C.search(child, "PARENT-SECRET"), ["PARENT-SECRET-c41d"], where="child search"
        )
        assert_strings_absent(C.status(child), ["PARENT-SECRET-c41d"], where="child status")
        # the child cannot ask for the parent by session id either
        assert expect_refusal(lambda: C.retrieve(child, "parent-sess"))
        assert parent_span not in C.snap_spans(C.snapshot(child))
    finally:
        C.close(child)
        C.close(parent)

    # nothing the child wrote appeared in the parent
    parent = C.for_session(home, "parent-sess")
    try:
        assert_strings_absent(C.inspect_(parent), ["child content"], where="parent inspect")
    finally:
        C.close(parent)


@pytest.mark.t("T36")
def test_long_write_transaction_elsewhere_yields_a_bounded_refusal_not_corruption(home, sid):
    store = C.for_session(home, sid)
    revision, spans = _seed(store)
    db = C.db_path(home, sid)

    assert C.res_ok(
        C.stage(store, [C.make_op("drop", [spans[0]])], expected_revision=revision, txn_id="t36")
    )

    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        conn.execute("SELECT COUNT(*) FROM sqlite_master")
        try:
            result = C.commit(store, txn_id="t36", expected_revision=revision)
            ok = C.res_ok(result)
            errors = " ".join(C.res_errors(result))
        except Exception as exc:  # noqa: BLE001
            ok = False
            errors = f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - started
    finally:
        conn.execute("ROLLBACK")
        conn.close()

    assert elapsed < 60, f"commit blocked for {elapsed:.1f}s: no bounded busy_timeout (I38)"
    if ok:
        assert C.snap_revision(C.snapshot(store)) == revision + 1
    else:
        assert C.snap_revision(C.snapshot(store)) == revision, "refused commit changed state"
        assert errors, "a refusal must carry a retryable error"
        assert spans[0] in C.snap_spans(C.snapshot(store))
    assert C.quick_check(db) == ["ok"]
    C.close(store)


@pytest.mark.t("T38")
def test_pin_created_between_stage_and_commit_blocks_the_commit(home, sid):
    store = C.for_session(home, sid)
    revision, spans = _seed(store, ("target-a", "target-b"))
    target = spans[0]

    assert C.res_ok(
        C.stage(store, [C.make_op("drop", [target])], expected_revision=revision, txn_id="t38-drop")
    )
    # pin lands after the drop was staged, before it is committed
    assert C.res_ok(
        C.stage(store, [C.make_op("pin", [target])], expected_revision=revision, txn_id="t38-pin")
    )
    assert C.res_ok(C.commit(store, txn_id="t38-pin", expected_revision=revision))
    pinned_rev = C.snap_revision(C.snapshot(store))

    result = C.commit(store, txn_id="t38-drop", expected_revision=revision)
    assert not C.res_ok(result), (
        "commit staged before pin creation was applied: pin not re-checked inside the "
        "write transaction (I23 TOCTOU)"
    )
    snap = C.snapshot(store)
    assert C.snap_revision(snap) == pinned_rev
    assert target in C.snap_spans(snap), "pinned row dropped by a stale staged commit"
    C.close(store)
