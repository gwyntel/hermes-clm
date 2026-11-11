"""T44-T47 - proposal substitution and TOCTOU.

Invariants: I25 (immutable first-class proposal), I26 (accept binds id + sha + base
revision and re-validates in the write txn), I27 (apply is derived over rows, current
session, non-pinned), I28 (strict closed schema, unknown fields rejected).
"""

from __future__ import annotations

import json

import pytest

from _support import contract as C
from _support.assertions import expect_refusal, msg

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


def _store_with_rows(home, sid):
    store = C.for_session(home, sid)
    C.ingest(store, [msg("assistant", "proposal target A"), msg("assistant", "proposal target B")])
    C.commit(store, txn_id="seed", expected_revision=C.snap_revision(C.snapshot(store)))
    return store, C.snap_revision(C.snapshot(store)), list(C.snap_spans(C.snapshot(store)))


def _proposal(store, span, revision, text="aux rewrite"):
    return C.create_proposal(
        store,
        [C.make_op("rewrite", [span], replacement=text, actor_origin="aux")],
        base_revision=revision,
    )


@pytest.mark.t("T44")
def test_proposal_byte_change_after_display_is_rejected(home, sid):
    store, revision, spans = _store_with_rows(home, sid)
    proposal = _proposal(store, spans[0], revision)
    pid = C._res_get(proposal, "proposal_id")
    phash = C._res_get(proposal, "proposal_sha256")
    assert pid and phash, f"proposal lacks id/hash: {proposal!r}"

    tampered = "0" * len(str(phash))
    if tampered == phash:
        tampered = "f" * len(str(phash))
    failure = expect_refusal(
        lambda: C.accept_proposal(
            store,
            proposal_id=pid,
            proposal_sha256=tampered,
            base_revision=revision,
            txn_id="t44-bad",
        )
    )
    assert failure, "accept did not bind proposal_sha256 (I26)"
    assert C.snap_revision(C.snapshot(store)) == revision, "rejected accept changed state"

    ok = C.accept_proposal(
        store, proposal_id=pid, proposal_sha256=phash, base_revision=revision, txn_id="t44-good"
    )
    assert C.res_ok(ok), C.res_errors(ok)
    assert C.snap_revision(C.snapshot(store)) == revision, (
        "accept_proposal committed (it must stage only)"
    )
    C.close(store)


@pytest.mark.t("T45")
def test_proposal_with_stale_base_revision_is_rejected(home, sid):
    store, revision, spans = _store_with_rows(home, sid)
    proposal = _proposal(store, spans[0], revision)
    pid = C._res_get(proposal, "proposal_id")
    phash = C._res_get(proposal, "proposal_sha256")

    assert C.res_ok(
        C.stage(
            store, [C.make_op("drop", [spans[1]])], expected_revision=revision, txn_id="t45-move"
        )
    )
    assert C.res_ok(C.commit(store, txn_id="t45-move", expected_revision=revision))
    head = C.snap_revision(C.snapshot(store))

    failure = expect_refusal(
        lambda: C.accept_proposal(
            store,
            proposal_id=pid,
            proposal_sha256=phash,
            base_revision=revision,
            txn_id="t45-stale",
        )
    )
    assert failure, "stale base_revision accepted: TOCTOU on the proposal span set (I26)"
    assert C.snap_revision(C.snapshot(store)) == head
    C.close(store)


@pytest.mark.t("T46")
def test_proposal_cannot_reference_foreign_or_pinned_spans(home, sid, other_home):
    store, revision, spans = _store_with_rows(home, sid)
    foreign = C.for_session(other_home, "other-sess")
    C.ingest(foreign, [msg("assistant", "foreign row")])
    C.commit(foreign, txn_id="f1", expected_revision=C.snap_revision(C.snapshot(foreign)))
    foreign_span = next(iter(C.snap_spans(C.snapshot(foreign))))
    C.close(foreign)

    def _refused_at_create_or_accept(span_id, base_revision, txn):
        """I27: a foreign/pinned span must be rejected at creation or at acceptance."""
        try:
            proposal = _proposal(store, span_id, base_revision)
        except Exception as exc:  # noqa: BLE001
            return f"create: {type(exc).__name__}: {exc}"
        pid = C._res_get(proposal, "proposal_id")
        phash = C._res_get(proposal, "proposal_sha256")
        try:
            accepted = C.accept_proposal(
                store,
                proposal_id=pid,
                proposal_sha256=phash,
                base_revision=base_revision,
                txn_id=txn,
            )
        except Exception as exc:  # noqa: BLE001
            return f"accept: {type(exc).__name__}: {exc}"
        if not C.res_ok(accepted):
            return "; ".join(C.res_errors(accepted))
        raise AssertionError(f"proposal over span {span_id!r} was accepted into the store (I27)")

    failure = _refused_at_create_or_accept(foreign_span, revision, "t46-foreign")
    assert failure, "proposal referenced a span in another session (I27)"

    assert C.res_ok(
        C.stage(store, [C.make_op("pin", [spans[0]])], expected_revision=revision, txn_id="t46-pin")
    )
    assert C.res_ok(C.commit(store, txn_id="t46-pin", expected_revision=revision))
    head = C.snap_revision(C.snapshot(store))
    pin_failure = _refused_at_create_or_accept(spans[0], head, "t46-pinned")
    assert pin_failure, "proposal targeted a pinned span (I27)"
    C.close(store)


@pytest.mark.t("T47")
def test_unknown_proposal_fields_and_op_kinds_are_rejected(home, sid):
    store, revision, spans = _store_with_rows(home, sid)
    baseline = _proposal(store, spans[0], revision)
    assert baseline is not None

    def _refused(op_factory):
        try:
            ops = op_factory()
        except Exception as exc:  # noqa: BLE001 - refusing at construction is fine
            return f"construct: {type(exc).__name__}: {exc}"
        try:
            proposal = C.create_proposal(store, ops, base_revision=revision)
        except Exception as exc:  # noqa: BLE001
            return f"create: {type(exc).__name__}: {exc}"
        pid = C._res_get(proposal, "proposal_id")
        phash = C._res_get(proposal, "proposal_sha256")
        try:
            accepted = C.accept_proposal(
                store,
                proposal_id=pid,
                proposal_sha256=phash,
                base_revision=revision,
                txn_id="t47",
            )
        except Exception as exc:  # noqa: BLE001
            return f"accept: {type(exc).__name__}: {exc}"
        if not C.res_ok(accepted):
            return "; ".join(C.res_errors(accepted))
        raise AssertionError("a proposal with an unknown kind/field was accepted (I28)")

    assert _refused(lambda: [C.make_op("archive_everything", [spans[0]])]), (
        "unknown operation kind was accepted (F1/I28)"
    )
    assert _refused(lambda: [{"kind": "drop", "span_ids": [spans[0]], "unknown_field": 1}]), (
        "unknown proposal field was accepted, not rejected (I28)"
    )
    assert C.snap_revision(C.snapshot(store)) == revision
    C.close(store)
