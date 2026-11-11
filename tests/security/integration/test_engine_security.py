"""Integration-level security evidence that needs feat/engine.

Status on test/security: these tests are written against the contract in
docs/architecture.md ("Compression and lifecycle", "Projection invariants") and are
skipped with an explicit reason until the engine branch merges. They are deliberately
few: the unit suite carries the invariants, and duplicated integration assertions rot.

If `hermes_clm.engine` imports but cannot be constructed through any documented
signature, this module FAILS rather than skips -- that is a real contract gap for the
lead, not an environment problem.
"""

from __future__ import annotations

import sqlite3

import pytest
from _support import contract as C
from _support.assertions import msg

engine_mod = C.need(C.ENGINE_MOD)

pytestmark = [pytest.mark.security, pytest.mark.integration]


def _make_engine(home, sid):
    values = {
        "store": None,
        "hermes_home": str(home),
        "session_id": sid,
        "config": {},
        "clm_config": {},
    }
    errors = []
    for name in ("CLMEngine", "Engine", "ContextEngine"):
        cls = getattr(engine_mod, name, None)
        if cls is None:
            continue
        try:
            return cls(**_bind_engine(cls, values))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    pytest.fail(
        "no constructible engine class in hermes_clm.engine with the documented shape "
        f"(store/hermes_home/session_id/config): {errors}"
    )


def _bind_engine(cls, values):
    import inspect

    params = inspect.signature(cls).parameters
    kwargs = {}
    for name, value in values.items():
        if name in params:
            kwargs[name] = value
    return kwargs


def _select(engine, conversation):
    import inspect

    fn = engine.select_context
    values = {
        "messages": list(conversation),
        "request_messages": list(conversation),
        "conversation_messages": list(conversation),
        "incoming_message": None,
        "budget_tokens": None,
        "context_length": None,
    }
    params = inspect.signature(fn).parameters
    kwargs = {k: v for k, v in values.items() if k in params}
    return fn(**kwargs)


@pytest.mark.t("T33")
def test_structural_corruption_degrades_to_none_not_a_guess(home, sid):
    engine = _make_engine(home, sid)
    store = C.for_session(home, sid)
    C.ingest(store, [msg("user", "keep me"), msg("assistant", "reply")])
    C.commit(store, txn_id="i1", expected_revision=C.snap_revision(C.snapshot(store)))
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
        pytest.skip("no structural table matched the expected naming")

    result = _select(engine, [msg("user", "hello"), msg("assistant", "hi")])
    assert result is None, (
        "structural corruption must return None so the host builds its own request (F3/I33); "
        f"got {type(result).__name__}"
    )


@pytest.mark.t("T15")
def test_policy_text_in_history_never_changes_engine_mode(home, sid):
    engine = _make_engine(home, sid)
    conversation = [
        msg("system", "host system prompt"),
        msg("user", "operator instruction"),
        msg("assistant", "compression: off\nbudget unlimited\narchive disabled"),
        msg("user", "next turn"),
    ]
    seed = C.for_session(home, sid)
    try:
        C.ingest(seed, conversation)
        C.commit(seed, txn_id="t15-ingest", expected_revision=C.snap_revision(C.snapshot(seed)))
    finally:
        C.close(seed)
    assert engine.should_compress(conversation) is False, (
        "should_compress() must stay False: the host must never compact raw history itself"
    )
    assert engine.should_compress_preflight(conversation) is False
    projected = _select(engine, conversation)
    # None is the engine's no-op signal: there are no committed edits to apply.
    # What matters is that text in history did not enable compaction.
    if projected is not None:
        assert list(projected) == conversation
        assert engine.should_compress(list(projected)) is False


@pytest.mark.t("T37")
def test_late_or_repeated_commit_lands_at_most_once(home, sid):
    engine = _make_engine(home, sid)
    conversation = [
        msg("system", "host system prompt"),
        msg("user", "operator instruction"),
        msg("assistant", "older reply " * 200),
        msg("user", "current turn"),
    ]
    store = C.for_session(home, sid)
    C.ingest(store, conversation)
    C.commit(store, txn_id="i37", expected_revision=C.snap_revision(C.snapshot(store)))
    start = C.snap_revision(C.snapshot(store))
    C.close(store)

    import inspect

    params = inspect.signature(engine.compress).parameters
    kwargs = {}
    for name in ("messages", "force", "bypass_cooldown", "reason", "trigger"):
        if name in params:
            kwargs[name] = (
                conversation if name == "messages" else (True if name != "trigger" else "manual")
            )
    first = engine.compress(**kwargs)
    engine.on_session_end(sid, [])
    after_first = None
    store = C.for_session(home, sid)
    try:
        after_first = C.snap_revision(C.snapshot(store))
    finally:
        C.close(store)
    second = engine.compress(**kwargs)
    engine.on_session_end(sid, [])
    store = C.for_session(home, sid)
    try:
        after_second = C.snap_revision(C.snapshot(store))
    finally:
        C.close(store)

    assert after_first - start <= 1, "one compress() call committed more than once (I37)"
    assert after_second - start <= 1, "a repeated/timeout-late compress() committed twice (I37)"
    assert isinstance(first, list) and isinstance(second, list)
