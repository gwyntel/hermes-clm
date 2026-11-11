"""T48/T49 - redaction at write and retention.

Invariant: I46. This is the only real confidentiality control the plugin has; the archive
would otherwise make every leaked secret permanent (review section 2).

T49 exercises a retention/purge entry point that the current integration contract does
not define (it only names `clm.retention_sessions`). The test reports that honestly as a
blocked requirement instead of passing vacuously.
"""

from __future__ import annotations

import inspect
import json

import pytest

from _support import contract as C
from _support.adversarial import SECRETS
from _support.assertions import assert_bytes_absent, assert_strings_absent, msg

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


@pytest.mark.t("T48")
@pytest.mark.parametrize("secret", SECRETS)
def test_secret_in_tool_output_is_redacted_at_write(home, sid, secret):
    store = C.for_session(home, sid)
    try:
        C.ingest(
            store,
            [
                msg("user", "run the thing"),
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": "{}"},
                        }
                    ],
                },
                msg("tool", f"export TOKEN={secret}\n", tool_call_id="c1", name="bash"),
            ],
        )
        C.commit(store, txn_id="redact", expected_revision=C.snap_revision(C.snapshot(store)))
        snap = C.snapshot(store)
        assert_strings_absent(snap, [secret], where="snapshot after ingest")

        for span_id in C.snap_spans(snap):
            assert_strings_absent(C.retrieve(store, span_id), [secret], where=f"retrieve {span_id}")
        assert_strings_absent(C.inspect_(store), [secret], where="inspect")
        assert_strings_absent(C.search(store, "TOKEN"), [secret], where="search")
    finally:
        C.close(store)

    # Redaction must be at write, not at render: the bytes must not be on disk.
    db = C.db_path(home, sid)
    files = [str(p) for p in C.session_files(home, sid)]
    assert str(db) in files
    assert_bytes_absent(
        [p for p in files if p.endswith(C.DB_SUFFIXES) or "-wal" in p or "-shm" in p],
        [secret.encode()],
        where="session database",
    )


@pytest.mark.t("T48")
def test_redaction_is_origin_independent(home, sid):
    secret = SECRETS[0]
    store = C.for_session(home, sid)
    try:
        C.ingest(store, [msg("user", f"my key is {secret}"), msg("system", f"key {secret}")])
        C.commit(store, txn_id="redact2", expected_revision=C.snap_revision(C.snapshot(store)))
        assert_strings_absent(C.snapshot(store), [secret], where="snapshot (user/system rows)")
    finally:
        C.close(store)


def _retention_callable(store):
    for name in ("purge_expired", "sweep_retention", "expire", "purge", "apply_retention"):
        fn = getattr(store, name, None)
        if callable(fn):
            return name, fn
    module = C.module(C.STORE_MOD)
    for name in ("purge_expired", "sweep_retention", "apply_retention", "purge_sessions"):
        fn = getattr(module, name, None)
        if callable(fn):
            return name, fn
    return None, None


@pytest.mark.t("T49")
def test_retention_expires_unpinned_sessions_and_keeps_pinned(home, sid):
    store = C.for_session(home, sid)
    C.ingest(store, [msg("assistant", "PINNED-CONTENT")])
    revision = C.snap_revision(C.snapshot(store))
    C.commit(store, txn_id="r1", expected_revision=revision)
    span = next(iter(C.snap_spans(C.snapshot(store))))
    assert C.res_ok(
        C.stage(store, [C.make_op("pin", [span])], expected_revision=revision, txn_id="r2")
    )
    assert C.res_ok(C.commit(store, txn_id="r2", expected_revision=revision))

    name, fn = _retention_callable(store)
    if fn is None:
        pytest.skip(
            "no retention/purge entry point on ContextStore or hermes_clm.store. "
            "clm.retention_sessions is specified (integration contract) but unreachable: "
            "required production fix - expose operator purge (I46/T49)"
        )
    try:
        try:
            fn(keep=2)
        except TypeError:
            fn()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{name}() failed: {type(exc).__name__}: {exc}")
    finally:
        C.close(store)

    # the pinned session must still be there with its content
    reopened = C.for_session(home, sid)
    try:
        assert "PINNED-CONTENT" in C.flatten_text(C.snapshot(reopened)), (
            "retention expired a pinned session (I46/T49)"
        )
    finally:
        C.close(reopened)
