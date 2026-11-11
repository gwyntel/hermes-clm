"""T20/T21/T25/T26/T27 - CAS, replay, triggers, corruption localisation, chain limits.

Invariants: I7 (compare-and-swap in SQL), I8 (consumed txn ids), I10 (triggers, not code),
I11 (per-record checksums localise; unanchored chain cannot detect a consistent rewrite).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from _support import contract as C
from _support.assertions import expect_refusal, msg

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


def _store_with_rows(home, sid, texts=("alpha", "beta", "gamma")):
    store = C.for_session(home, sid)
    C.ingest(store, [msg("assistant", t) for t in texts])
    C.commit(store, txn_id="seed", expected_revision=C.snap_revision(C.snapshot(store)))
    return store


@pytest.mark.t("T20")
def test_two_staged_txns_from_the_same_revision_exactly_one_wins(home, sid):
    store = _store_with_rows(home, sid)
    revision = C.snap_revision(C.snapshot(store))
    spans = list(C.snap_spans(C.snapshot(store)))

    assert C.res_ok(
        C.stage(store, [C.make_op("drop", [spans[0]])], expected_revision=revision, txn_id="cas-a")
    )
    assert C.res_ok(
        C.stage(store, [C.make_op("drop", [spans[1]])], expected_revision=revision, txn_id="cas-b")
    )

    first = C.commit(store, txn_id="cas-a", expected_revision=revision)
    assert C.res_ok(first), C.res_errors(first)
    second = C.commit(store, txn_id="cas-b", expected_revision=revision)
    assert not C.res_ok(second), "second commit from a stale expected_revision must be rejected (I7)"

    snap = C.snapshot(store)
    assert C.snap_revision(snap) == revision + 1
    assert spans[0] not in C.snap_spans(snap)
    assert spans[1] in C.snap_spans(snap), "stale commit applied (last-writer-wins)"
    C.close(store)


@pytest.mark.t("T21")
def test_replayed_transaction_returns_original_outcome_without_a_second_write(home, sid):
    store = _store_with_rows(home, sid)
    revision = C.snap_revision(C.snapshot(store))
    spans = list(C.snap_spans(C.snapshot(store)))
    assert C.res_ok(
        C.stage(store, [C.make_op("drop", [spans[0]])], expected_revision=revision, txn_id="replay-a")
    )
    first = C.commit(store, txn_id="replay-a", expected_revision=revision)
    assert C.res_ok(first)
    head = C.snap_revision(C.snapshot(store))

    replay = C.commit(store, txn_id="replay-a", expected_revision=revision)
    assert C.res_ok(replay), "a replay must return the original outcome, not an error (I8)"
    assert C.snap_revision(C.snapshot(store)) == head, "replay applied a second write (I8)"
    assert C.res_revision(replay) in (head, C.res_revision(first))
    C.close(store)


@pytest.mark.t("T25")
def test_direct_sql_cannot_update_or_delete_pinned_rows(home, sid):
    store = _store_with_rows(home, sid)
    spans = list(C.snap_spans(C.snapshot(store)))
    revision = C.snap_revision(C.snapshot(store))
    assert C.res_ok(C.stage(store, [C.make_op("pin", [spans[0]])], expected_revision=revision, txn_id="trg-pin"))
    assert C.res_ok(C.commit(store, txn_id="trg-pin", expected_revision=revision))
    db = C.db_path(home, sid)
    C.close(store)

    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    try:
        targets = [(t, c) for t, c in C.find_payload_column(conn)]
        assert targets, "no payload column found to attack"
        hits = 0
        for table, column in targets:
            for rowid in [r[0] for r in conn.execute(f'SELECT rowid FROM "{table}" LIMIT 4')]:
                for statement in (
                    f'UPDATE "{table}" SET "{column}" = "{column}" WHERE rowid = ?',
                    f'DELETE FROM "{table}" WHERE rowid = ?',
                ):
                    try:
                        conn.execute(statement, (rowid,))
                    except sqlite3.IntegrityError:
                        hits += 1
                    except sqlite3.OperationalError as exc:
                        hits += 1
                        assert "trigger" in str(exc).lower() or "abort" in str(exc).lower()
        assert hits, (
            "no trigger blocked a direct UPDATE/DELETE on a protected/pinned row (I10): "
            "immutability is enforced in code, not schema"
        )
    finally:
        conn.close()


@pytest.mark.t("T26")
def test_single_byte_payload_flip_tombstones_only_that_span(home, sid):
    store = _store_with_rows(home, sid, texts=("KEEP-ME-11", "FLIP-ME-22", "KEEP-ME-33"))
    spans = list(C.snap_spans(C.snapshot(store)))
    assert len(spans) == 3
    db = C.db_path(home, sid)
    tail = store
    before = C.project(tail, [msg("assistant", "request")])

    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    flipped = False
    try:
        for table, column in C.find_payload_column(conn):
            rows = conn.execute(f'SELECT rowid, "{column}" FROM "{table}"').fetchall()
            for rowid, value in rows:
                if value is None:
                    continue
                if isinstance(value, bytes):
                    data = bytearray(value)
                    data[-1] ^= 0x01
                    new = bytes(data)
                else:
                    text = str(value)
                    if "FLIP-ME-22" not in text:
                        continue
                    new = text.replace("FLIP-ME-22", "FLIP-ME-23")
                conn.execute(f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?', (new, rowid))
                flipped = True
                break
            if flipped:
                break
    finally:
        conn.close()
    assert flipped, "could not locate and flip a payload row"

    after = C.project(tail, [msg("assistant", "request")])
    if after.declined:
        return  # refusing to project is fail-closed
    text = after.text
    assert "KEEP-ME-11" in text or "KEEP-ME-33" in text, (
        "a single corrupt payload must not take out the whole projection (I11/I32)"
    )
    gaps = [m for m in after.messages if C.is_gap_marker(C.content_of(m))]
    assert gaps, "corrupt payload must render an explicit gap marker, never silent omission (I32)"
    assert "FLIP-ME-22" not in text, "corrupt span projected from unverified bytes"
    health = C.status(store)
    blob = json.dumps(health, default=str).lower()
    assert any(word in blob for word in ("chain", "integrity", "checksum", "corrupt")), (
        "status() exposes no integrity signal for a detected corruption (I11): "
        f"keys={sorted(health) if isinstance(health, dict) else health!r}"
    )
    C.close(store)


@pytest.mark.t("T27")
def test_consistent_chain_rewrite_is_undetected_without_an_anchor(home, sid):
    """Documented limitation (review section 5): the unanchored chain is a corruption
    detector. If an implementation anchors the head, this test tells the maintainer to
    update docs/security-testing.md rather than pretending the limit disappeared."""
    store = _store_with_rows(home, sid, texts=("ORIGINAL-A", "ORIGINAL-B"))
    db = C.db_path(home, sid)
    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    rewritten = False
    try:
        for table, column in C.find_payload_column(conn):
            rows = conn.execute(f'SELECT rowid, "{column}" FROM "{table}"').fetchall()
            for rowid, value in rows:
                text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
                if "ORIGINAL-A" not in text:
                    continue
                conn.execute(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                    (text.replace("ORIGINAL-A", "REWRITTEN-A"), rowid),
                )
                rewritten = True
                # keep any per-record checksum consistent: this is the A2 simulation
                cols = C.columns(conn, table)
                for candidate in cols:
                    if any(tag in candidate.lower() for tag in ("sha", "checksum", "digest", "hash")):
                        try:
                            new_text = text.replace("ORIGINAL-A", "REWRITTEN-A")
                            conn.execute(
                                f'UPDATE "{table}" SET "{candidate}" = ? WHERE rowid = ?',
                                (hashlib.sha256(new_text.encode()).hexdigest(), rowid),
                            )
                        except sqlite3.Error:
                            pass
                break
            if rewritten:
                break
    finally:
        conn.close()
    if not rewritten:
        pytest.skip("could not locate a payload row to rewrite (schema drift)")

    out = C.project(store, [msg("assistant", "request")])
    if out.declined:
        pytest.skip("projection declined after tamper; the limitation is then unobservable here")
    detected = "REWRITTEN-A" not in out.text
    if detected:
        pytest.skip(
            "consistent rewrite WAS detected: an anchor exists, so the documented "
            "limitation in docs/security-testing.md T27 is now stale - update the doc"
        )
    C.close(store)
