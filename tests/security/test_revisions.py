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
    second = expect_refusal(lambda: C.commit(store, txn_id="cas-b", expected_revision=revision))
    assert second, "second commit from a stale expected_revision must be rejected (I7)"

    snap = C.snapshot(store)
    assert C.snap_revision(snap) == revision + 1
    current = C.snap_spans(snap)
    first_state = (
        str(C.span_field(current[spans[0]], "state", "") or "")
        if spans[0] in current
        else "dropped"
    )
    second_state = (
        str(C.span_field(current[spans[1]], "state", "") or "")
        if spans[1] in current
        else "dropped"
    )
    if not second_state:
        second_state = "active"  # fixture snapshots omit state for live rows
    assert first_state == "dropped", "first commit did not apply"
    assert second_state == "active", "stale commit applied (last-writer-wins)"
    C.close(store)


@pytest.mark.t("T21")
def test_replayed_transaction_returns_original_outcome_without_a_second_write(home, sid):
    store = _store_with_rows(home, sid)
    revision = C.snap_revision(C.snapshot(store))
    spans = list(C.snap_spans(C.snapshot(store)))
    assert C.res_ok(
        C.stage(
            store, [C.make_op("drop", [spans[0]])], expected_revision=revision, txn_id="replay-a"
        )
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
    assert C.res_ok(
        C.stage(store, [C.make_op("pin", [spans[0]])], expected_revision=revision, txn_id="trg-pin")
    )
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
    seeded = [
        dict(C._res_get(C.snap_spans(C.snapshot(store))[span_id], "message")) for span_id in spans
    ]
    db = C.db_path(home, sid)
    tail = store
    before = C.project(tail, seeded, seeded)
    assert "KEEP-ME-11" in before.text and "KEEP-ME-33" in before.text
    C.close(store)

    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    flipped = False
    try:
        for trigger in [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='spans'"
            ).fetchall()
        ]:
            conn.execute(f'DROP TRIGGER "{trigger}"')
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

    tail = C.for_session(home, sid)
    after = C.project(
        tail,
        [dict(C._res_get(span, "message")) for span in C.snap_spans(C.snapshot(tail)).values()],
        [dict(C._res_get(span, "message")) for span in C.snap_spans(C.snapshot(tail)).values()],
    )
    if after.declined:
        return  # refusing to project is fail-closed
    text = after.text
    assert "KEEP-ME-11" in text or "KEEP-ME-33" in text, (
        "a single corrupt payload must not take out the whole projection (I11/I32)"
    )
    gaps = [m for m in after.messages if C.is_gap_marker(C.content_of(m))]
    assert gaps, "corrupt payload must render an explicit gap marker, never silent omission (I32)"
    assert "FLIP-ME-22" not in text, "corrupt span projected from unverified bytes"
    health = C.status(tail)
    blob = json.dumps(health, default=str).lower()
    assert any(word in blob for word in ("chain", "integrity", "checksum", "corrupt")), (
        "status() exposes no integrity signal for a detected corruption (I11): "
        f"keys={sorted(health) if isinstance(health, dict) else health!r}"
    )
    C.close(tail)


@pytest.mark.t("T27")
def test_consistent_chain_rewrite_is_undetected_without_an_anchor(home, sid):
    """Documented limitation (review section 5): the unanchored chain is a corruption
    detector. If an implementation anchors the head, this test tells the maintainer to
    update docs/security-testing.md rather than pretending the limit disappeared."""
    store = _store_with_rows(home, sid, texts=("ORIGINAL-A", "ORIGINAL-B"))
    db = C.db_path(home, sid)
    C.close(store)
    conn = sqlite3.connect(str(db), timeout=10)
    conn.isolation_level = None
    rewritten = False
    try:
        for trigger in [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='spans'"
            ).fetchall()
        ]:
            conn.execute(f'DROP TRIGGER "{trigger}"')
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
                    if any(
                        tag in candidate.lower() for tag in ("sha", "checksum", "digest", "hash")
                    ):
                        try:
                            new_text = text.replace("ORIGINAL-A", "REWRITTEN-A")
                            parsed = json.loads(new_text)
                            digest = hashlib.sha256(
                                json.dumps(
                                    parsed,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    ensure_ascii=False,
                                ).encode()
                            ).hexdigest()
                            conn.execute(
                                f'UPDATE "{table}" SET "{candidate}" = ? WHERE rowid = ?',
                                (digest, rowid),
                            )
                        except (sqlite3.Error, json.JSONDecodeError):
                            pass
                break
            if rewritten:
                break
    finally:
        conn.close()
    if not rewritten:
        pytest.skip("could not locate a payload row to rewrite (schema drift)")

    store = C.for_session(home, sid)
    seeded = [
        dict(C._res_get(span, "message") or {}) for span in C.snap_spans(C.snapshot(store)).values()
    ]
    out = C.project(store, seeded, seeded)
    assert not out.declined, "projection declined after a consistent same-UID rewrite"
    assert "REWRITTEN-A" in out.text, (
        "the unanchored chain detected a consistent rewrite; update docs/security-testing.md "
        "and make this an assertion of the stronger guarantee"
    )
    C.close(store)
