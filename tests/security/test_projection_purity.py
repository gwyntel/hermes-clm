"""T39/T40 - projection is side-effect free and returns deep copies.

Invariant: I42. Alias leaks here are how a host mutation ends up rewriting a stored span
or carrying a row into another session.
"""

from __future__ import annotations

import copy
import json

import pytest

from _support import contract as C
from _support.assertions import disk_signature, msg

C.need(C.STORE_MOD, C.PROJECTION_MOD)

pytestmark = pytest.mark.security


@pytest.fixture
def conversation():
    return [
        {"role": "system", "content": "SYSTEM-PROMPT-BYTES-aa"},
        {"role": "user", "content": "operator instruction bb"},
        {"role": "assistant", "content": "model reply cc"},
        {"role": "tool", "content": "tool output dd", "tool_call_id": "call_x", "name": "bash"},
    ]


@pytest.mark.t("T39")
def test_projection_does_not_touch_the_store(home, sid, conversation):
    store = C.for_session(home, sid)
    try:
        C.ingest(store, conversation)
        C.commit(store, txn_id="p1", expected_revision=C.snap_revision(C.snapshot(store)))
        C.project(store, conversation)  # warm any caches first
        # `-shm` is SQLite's shared-memory wal-index: a *reader* legitimately writes read
        # marks into it. Excluding it keeps the assertion about store state, not about
        # SQLite bookkeeping; the database, index and blobs are still byte-compared.
        ignore = ("-shm",)
        before = {k: v for k, v in disk_signature(home, sid).items() if not k.endswith(ignore)}
        for _ in range(100):
            C.project(store, conversation)
        after = {k: v for k, v in disk_signature(home, sid).items() if not k.endswith(ignore)}
    finally:
        C.close(store)
    assert before == after, (
        "select_context changed store bytes/mtimes: projection must be pure (I42).\n"
        f"changed: {sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}"
    )


@pytest.mark.t("T39")
def test_projection_returns_a_deep_copy_not_internal_state(home, sid, conversation):
    store = C.for_session(home, sid)
    try:
        C.ingest(store, conversation)
        C.commit(store, txn_id="p2", expected_revision=C.snap_revision(C.snapshot(store)))
        first = C.project(store, conversation).messages
        assert first is not None
        baseline = copy.deepcopy(first)

        for message in first:
            message["content"] = "CLOBBERED"
            message["injected"] = True
        second = C.project(store, conversation).messages
        assert second == baseline, "mutating a returned list changed later projections (I42 alias)"

        calls = C.project(store, conversation)
        assert conversation[1]["content"] == "operator instruction bb", (
            "projection mutated the caller's input list (I42)"
        )
        assert calls.messages
    finally:
        C.close(store)


@pytest.mark.t("T40")
def test_host_mutation_of_projection_cannot_reach_the_store(home, sid, conversation):
    store = C.for_session(home, sid)
    try:
        C.ingest(store, conversation)
        C.commit(store, txn_id="p3", expected_revision=C.snap_revision(C.snapshot(store)))
        out = C.project(store, conversation).messages
        for message in out or []:
            message.clear()
        snap = C.snapshot(store)
        blob = C.flatten_text(snap)
        assert "model reply cc" in blob, "store rows were emptied through a projected alias (I42)"
        assert C.project(store, conversation).messages is not None
    finally:
        C.close(store)
