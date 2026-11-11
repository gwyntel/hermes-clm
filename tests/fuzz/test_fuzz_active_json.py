"""Fuzz: arbitrary derived-index content is discarded, never trusted.

Property (I30/F2): for any bytes in active.json, opening the session either refuses or
lands on the database head revision and projects the same message list as a clean open.
"""

from __future__ import annotations

import json
import os
import random

import pytest
from _support import contract as C
from _support.assertions import msg

C.need(C.STORE_MOD)

pytestmark = [pytest.mark.fuzz, pytest.mark.security]

PIECES = [
    b"",
    b"null",
    b"[]",
    b"{}",
    b'{"revision": 999999}',
    b'{"revision": -1}',
    b'{"revision": "3"}',
    b'{"db_id": "other"}',
    b'{"session_id": "parent-sess"}',
    b'{"spans": {"x": {"content": "INJECTED-ROW"}}}',
    b"\x00" * 32,
    b'{"revision": NaN}',
]


def _iterations(default: int = 6) -> int:
    return int(os.environ.get("CLM_FUZZ_ITERATIONS", default))


@pytest.mark.t("T30")
def test_random_derived_index_content_never_changes_the_projection(home, sid):
    store = C.for_session(home, sid)
    C.ingest(store, [msg("assistant", "BASELINE-ROW"), msg("user", "operator row")])
    C.commit(store, txn_id="fz-seed", expected_revision=C.snap_revision(C.snapshot(store)))
    head = C.snap_revision(C.snapshot(store))
    baseline = json.dumps(
        C.project(store, [msg("assistant", "req")]).messages, default=str, sort_keys=True
    )
    C.close(store)

    index = C.active_json(home, sid)
    if not index.parent.is_dir():
        pytest.skip("this store format has no active.json index")

    rng = random.Random(0xD15C)
    for _ in range(_iterations()):
        blob = rng.choice(PIECES)
        if rng.random() < 0.3:
            blob = blob + bytes(rng.randint(0, 255) for _ in range(rng.randint(1, 16)))
        index.write_bytes(blob)
        try:
            reopened = C.for_session(home, sid)
        except Exception:
            continue  # refusing to open on a corrupt index is acceptable
        try:
            assert C.snap_revision(C.snapshot(reopened)) == head, (
                f"derived index {blob!r} overrode the DB head (I30)"
            )
            current = json.dumps(
                C.project(reopened, [msg("assistant", "req")]).messages,
                default=str,
                sort_keys=True,
            )
            assert current == baseline, f"derived index {blob!r} changed the projection"
        finally:
            C.close(reopened)
