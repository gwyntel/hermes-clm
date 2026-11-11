"""Fuzz: random operation payloads are refused or applied without coercion.

Property: `stage`/`commit` never turn an unknown kind into a known one, never move the
revision by more than one per commit, and never leave the store unreadable (F1, I6, I7).
"""

from __future__ import annotations

import os
import random

import pytest

from _support import contract as C
from _support.assertions import msg

C.need(C.STORE_MOD)

pytestmark = [pytest.mark.fuzz, pytest.mark.security]

KIND_ALPHABET = list(C.CLOSED_OP_KINDS) + [
    "unpin",
    "archive_all",
    "",
    "DROP",
    "delete",
    "compact",
    "restore_all",
    "keep_all",
]


def _iterations(default: int = 60) -> int:
    return int(os.environ.get("CLM_FUZZ_ITERATIONS", default))


@pytest.mark.t("T47")
def test_random_operation_kinds_are_never_coerced(store, home, sid):
    C.ingest(store, [msg("assistant", f"row {i}") for i in range(4)])
    C.commit(store, txn_id="fuzz-seed", expected_revision=C.snap_revision(C.snapshot(store)))
    rng = random.Random(0x0BEE)
    spans = list(C.snap_spans(C.snapshot(store)))

    for step in range(_iterations()):
        revision = C.snap_revision(C.snapshot(store))
        kind = rng.choice(KIND_ALPHABET)
        target = rng.choice(spans)
        txn = f"fuzz-{step}"
        try:
            op = C.make_op(kind, [target], replacement="fuzz text")
        except Exception:
            continue  # rejecting an unknown kind at construction is correct
        staged = C.stage(store, [op], expected_revision=revision, txn_id=txn)
        if not C.res_ok(staged):
            continue
        applied = C.commit(store, txn_id=txn, expected_revision=revision)
        new_revision = C.snap_revision(C.snapshot(store))
        if kind not in C.CLOSED_OP_KINDS:
            assert not C.res_ok(applied), f"unknown kind {kind!r} was applied (F1)"
            assert new_revision == revision, f"unknown kind {kind!r} moved the revision"
            continue
        assert new_revision in (revision, revision + 1), (
            f"commit moved revision {revision} -> {new_revision} (I6)"
        )
        assert C.res_ok(applied) or new_revision == revision, (
            "a failed commit must not move the revision (I7)"
        )

    assert C.quick_check(C.db_path(home, sid)) == ["ok"], "fuzz left the database unreadable"
    assert C.snap_spans(C.snapshot(store)) is not None
