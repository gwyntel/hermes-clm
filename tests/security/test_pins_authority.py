"""T16-T19 - pin authority. Agent pin/commit/revert are REQUIRED and permitted;
what is forbidden is authority elevation, unpin, pin removal, and partial application.

Invariants: I21 (agent pin only; no unpin verb), I22 (content-addressed pin ids),
I23 (a commit intersecting a pin is refused whole), I24 (pins are append-only records),
I9 (revert never truncates).
"""

from __future__ import annotations

import inspect
import json

import pytest

from _support import contract as C
from _support.assertions import expect_refusal, msg

C.need(C.STORE_MOD, C.MODELS_MOD)

pytestmark = pytest.mark.security


def _ingested(store, base_messages):
    C.ingest(store, base_messages)
    revision = C.snap_revision(C.snapshot(store))
    return revision, list(C.snap_spans(C.snapshot(store)))


@pytest.mark.t("T16")
def test_agent_pin_is_allowed_but_confers_no_authority(store, base_messages):
    revision, spans = _ingested(store, base_messages)
    target = spans[-1]
    origin_before = C.span_field(C.snapshot(store).spans[target], "origin")

    staged = C.stage(
        store, [C.make_op("pin", [target], actor_origin="model")],
        expected_revision=revision, txn_id="txn-pin-agent", actor_origin="model",
    )
    assert C.res_ok(staged), C.res_errors(staged)
    committed = C.commit(store, txn_id="txn-pin-agent", expected_revision=revision)
    assert C.res_ok(committed), C.res_errors(committed)

    snap = C.snapshot(store)
    pins = C.snap_pins(snap)
    assert pins, "agent pin must be permitted (plan D; ADR 0001 decision 5)"

    records = pins.values() if isinstance(pins, dict) else pins
    for record in records:
        actor = str(C._res_get(record, "actor_origin", "origin", "actor", default="") or "").lower()
        assert actor not in ("operator", "system", "host"), (
            f"agent pin recorded with privileged actor {actor!r} (I21)"
        )

    origin_after = C.span_field(snap.spans[target], "origin")
    assert origin_after == origin_before, (
        f"pinning changed span origin {origin_before!r} -> {origin_after!r} (no elevation)"
    )


@pytest.mark.t("T16")
def test_agent_commit_and_revert_are_permitted(store, base_messages):
    revision, spans = _ingested(store, base_messages)
    target = spans[-1]
    staged = C.stage(
        store, [C.make_op("drop", [target])], expected_revision=revision, txn_id="txn-drop-1"
    )
    assert C.res_ok(staged), C.res_errors(staged)
    committed = C.commit(store, txn_id="txn-drop-1", expected_revision=revision)
    assert C.res_ok(committed), C.res_errors(committed)
    after_drop = C.snap_revision(C.snapshot(store))
    assert after_drop == revision + 1

    reverted = C.revert(
        store, target_revision=revision, expected_revision=after_drop, txn_id="txn-revert-1"
    )
    assert C.res_ok(reverted), C.res_errors(reverted)
    snap = C.snapshot(store)
    assert C.snap_revision(snap) == after_drop + 1, "revert must be a new revision (I9)"
    assert target in C.snap_spans(snap), "revert must restore without truncating"
    kinds = [str(C._res_get(op, "kind", "op_kind", default="")) for op in C.snap_ops(snap)]
    assert kinds.count("drop") == 1, "revert truncated history instead of appending (I9)"


@pytest.mark.t("T16")
def test_unpin_is_not_an_operation_kind(store):
    kinds = {name.lower() for name in dir(C.module(C.MODELS_MOD))}
    _ = kinds
    failure = expect_refusal(
        lambda: C.make_op("unpin", ["span-x"])
    )
    assert failure, "unknown op kind 'unpin' must raise, not coerce (I21/F1)"


@pytest.mark.t("T16")
def test_no_agent_tool_can_unpin_or_move_a_pin():
    module = C.need(C.TOOLS_MOD)
    forbidden = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        if not callable(obj) or inspect.isclass(obj):
            continue
        lowered = name.lower()
        if "unpin" in lowered or ("pin" in lowered and ("remove" in lowered or "delete" in lowered or "move" in lowered)):
            forbidden.append(name)
    assert not forbidden, f"agent tool surface exposes pin mutation: {forbidden} (I21)"


@pytest.mark.t("T17")
def test_commit_that_would_drop_a_pinned_span_is_refused_whole(store, base_messages):
    revision, spans = _ingested(store, base_messages)
    pinned_span = spans[-1]
    other_span = spans[0]
    assert C.res_ok(
        C.stage(store, [C.make_op("pin", [pinned_span])], expected_revision=revision, txn_id="t17-pin")
    )
    assert C.res_ok(C.commit(store, txn_id="t17-pin", expected_revision=revision))
    pinned_rev = C.snap_revision(C.snapshot(store))
    pin_ids = [str(k) for k in (C.snap_pins(C.snapshot(store)) or {})]

    staged = C.stage(
        store,
        [C.make_op("drop", [pinned_span, other_span])],
        expected_revision=pinned_rev,
        txn_id="t17-drop",
    )
    if not C.res_ok(staged):
        errors = " ".join(C.res_errors(staged)).lower()
        assert "pin" in errors, f"refusal did not name the pin: {C.res_errors(staged)}"
        assert C.snap_revision(C.snapshot(store)) == pinned_rev
        return

    committed = C.commit(store, txn_id="t17-drop", expected_revision=pinned_rev)
    assert not C.res_ok(committed), "commit intersecting a pin must be refused (I23)"
    errors = " ".join(C.res_errors(committed)).lower()
    assert "pin" in errors, f"refusal must name the pin that blocked it: {C.res_errors(committed)}"
    snap = C.snapshot(store)
    assert C.snap_revision(snap) == pinned_rev, "refused commit changed the revision"
    assert pinned_span in C.snap_spans(snap), "pinned span vanished on a refused commit"
    assert other_span in C.snap_spans(snap), "refused commit partially applied (drop of unpinned row)"
    assert any(pid for pid in pin_ids)


@pytest.mark.t("T18")
def test_pin_ids_are_content_addressed_and_not_reused(store):
    text = "identical content pinned twice"
    C.ingest(store, [msg("assistant", text), msg("assistant", text)])
    revision = C.snap_revision(C.snapshot(store))
    spans = list(C.snap_spans(C.snapshot(store)))
    assert len(spans) >= 2, "fixture needs two rows with identical content"
    first, second = spans[0], spans[1]

    assert C.res_ok(C.stage(store, [C.make_op("pin", [first])], expected_revision=revision, txn_id="t18-a"))
    assert C.res_ok(C.commit(store, txn_id="t18-a", expected_revision=revision))
    rev2 = C.snap_revision(C.snapshot(store))
    assert C.res_ok(C.stage(store, [C.make_op("pin", [second])], expected_revision=rev2, txn_id="t18-b"))
    assert C.res_ok(C.commit(store, txn_id="t18-b", expected_revision=rev2))

    snap = C.snapshot(store)
    pins = C.snap_pins(snap)
    ids = list(pins.keys()) if isinstance(pins, dict) else [C._res_get(p, "pin_id") for p in pins]
    assert len(ids) >= 2, "second pin did not create a new record (I24)"
    assert len(set(ids)) == len(ids), f"pin id reused for different revision: {ids}"


@pytest.mark.t("T19")
def test_revert_across_a_pin_is_refused_and_archive_keeps_both_sides(store, base_messages):
    revision, spans = _ingested(store, base_messages)
    pinned_span = spans[-1]
    assert C.res_ok(C.stage(store, [C.make_op("pin", [pinned_span])], expected_revision=revision, txn_id="t19-pin"))
    assert C.res_ok(C.commit(store, txn_id="t19-pin", expected_revision=revision))
    pinned_rev = C.snap_revision(C.snapshot(store))

    other = spans[0]
    assert C.res_ok(C.stage(store, [C.make_op("drop", [other])], expected_revision=pinned_rev, txn_id="t19-drop"))
    assert C.res_ok(C.commit(store, txn_id="t19-drop", expected_revision=pinned_rev))
    head = C.snap_revision(C.snapshot(store))

    failure = expect_refusal(
        lambda: C.revert(store, target_revision=revision - 1 if revision else 0, expected_revision=head, txn_id="t19-revert")
    )
    assert failure, "revert across a pinned span must be refused (I23)"
    assert "pin" in failure.lower(), f"refusal did not name the pin: {failure}"
    snap = C.snapshot(store)
    assert C.snap_revision(snap) == head, "refused revert changed the head"
    assert pinned_span in C.snap_spans(snap)
