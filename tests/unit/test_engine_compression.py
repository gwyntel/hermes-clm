"""Compression modes: identity, manual force, overflow with archive-backed offload."""

from __future__ import annotations

import json
import sys

import pytest

from _clm_fakes import (
    FakeStoreResult,
    StoreFactory,
    assemble,
    assistant_call,
    message,
    snapshot_for,
    tool_result,
)
from hermes_clm import budget as _budget
from hermes_clm.budget import estimate_messages_tokens
from hermes_clm.engine import CLMEngine, EngineConfig, OperationSpec


def build_engine(snapshot, *, supports_commit: bool = True, context_length: int = 0, **config):
    factory = StoreFactory(snapshot, supports_commit=supports_commit)
    engine = CLMEngine(
        config=EngineConfig(**config), store_factory=factory, hermes_home="/tmp/hermes-home"
    )
    engine.on_session_start("sess-1", hermes_home="/tmp/hermes-home", context_length=context_length)
    return engine, factory


def big_history():
    """Two tool groups, the older one large enough to be worth offloading."""
    return [
        message("user", "list everything", "u1"),
        assistant_call("a1", "call_1"),
        tool_result("t1", "call_1", "HUGE OUTPUT LINE\n" * 400),
        message("assistant", "done listing", "a2"),
        message("user", "now the tests", "u2"),
        assistant_call("a3", "call_2"),
        tool_result("t2", "call_2", "NEWEST OUTPUT"),
    ]


# --------------------------------------------------------------------- identity


def test_routine_compress_never_touches_the_store_or_the_transcript():
    history = big_history()
    engine, factory = build_engine(
        snapshot_for(history, [OperationSpec(kind="drop", span_ids=("a1", "t1"))])
    )
    result = engine.compress(history, current_tokens=10_000)
    assert result == history
    assert factory.store.ingest_calls == []
    assert factory.store.commits == []
    assert engine.compression_count == 0


# ----------------------------------------------------------------------- manual


def test_manual_compress_offloads_the_oldest_group_when_the_projection_saves_nothing():
    """``/compact`` must reduce something, not report a no-op success.

    Regression: ``compress(force=True)`` used to replay the committed projection
    only, so a session with a huge stale tool result answered the manual command
    with "No changes from compression" while the store held nothing to replay.
    """

    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))

    result = engine.compress(history, force=True)

    assert estimate_messages_tokens(result) < estimate_messages_tokens(history)
    assert "[clm.offloaded" in result[2]["content"]  # oldest group stubbed
    assert result[6]["content"] == "NEWEST OUTPUT"  # newest tool result protected
    assert result[0]["content"] == "list everything"  # user row untouched
    assert engine.compression_count == 1
    assert engine._last_compress_aborted is False
    assert getattr(engine, "_last_summary_error", None) is None
    assert len(factory.store.commits) == 1
    staged_ops, expected_revision, txn_id, actor_origin = factory.store.staged[0]
    assert expected_revision == 1
    assert txn_id.startswith("manual-")
    # The human typed /compact: the operator is the actor, not the model.
    assert actor_origin == "operator"
    assert [op.kind for op in staged_ops] == ["offload"]
    assert set(staged_ops[0].span_ids) == {"a1", "t1"}


def test_manual_compress_progresses_across_calls_and_then_reports_exhaustion():
    """Each ``/compact`` sheds the next oldest group; an exhausted one is honest."""

    history = [
        message("user", "list everything", "u1"),
        assistant_call("a1", "call_1"),
        tool_result("t1", "call_1", "HUGE OUTPUT LINE\n" * 400),
        message("assistant", "done listing", "a2"),
        message("user", "now the tests", "u2"),
        assistant_call("a3", "call_2"),
        tool_result("t2", "call_2", "MEDIUM OUTPUT LINE\n" * 100),
        message("assistant", "tests pass", "a4"),
        message("user", "and the build", "u3"),
        assistant_call("a5", "call_3"),
        tool_result("t3", "call_3", "NEWEST OUTPUT"),
    ]
    engine, factory = build_engine(snapshot_for(history, [], revision=1))

    first = engine.compress(history, force=True)
    assert "[clm.offloaded" in first[2]["content"]
    assert first[6]["content"].startswith("MEDIUM")
    second = engine.compress(list(first), force=True)
    assert "[clm.offloaded" in second[6]["content"]  # the next oldest group went
    assert estimate_messages_tokens(second) < estimate_messages_tokens(first)
    assert engine.compression_count == 2, "both calls did real work"

    # Nothing eligible remains (the newest tool result is protected): identity, an
    # explicit reason, and no new revision.
    commits_before = len(factory.store.commits)
    revision_before = factory.store._snapshot.revision
    third = engine.compress(list(second), force=True)
    assert third == list(second)
    assert engine.compression_count == 2
    assert engine._last_compress_aborted is True
    assert "manual_no_offloadable_groups" in (engine._last_summary_error or "")
    assert len(factory.store.commits) == commits_before
    assert factory.store._snapshot.revision == revision_before


def test_manual_compress_is_honest_when_nothing_is_offloadable():
    """No tool groups, no committed edits: say so instead of "no changes" success."""

    history = [
        message("user", "hello", "u1"),
        message("assistant", "hi", "a1"),
        message("user", "still there?", "u2"),
        message("assistant", "yes", "a2"),
    ]
    engine, factory = build_engine(snapshot_for(history, [], revision=1))

    result = engine.compress(history, force=True)

    assert result == history
    assert engine.compression_count == 0
    assert engine._last_compress_aborted is True
    assert "manual_no_offloadable_groups" in (engine._last_summary_error or "")
    assert factory.store.commits == []


def test_manual_compress_never_reports_a_local_only_offload_as_saved():
    """A store without a commit surface cannot persist the stubs: refuse instead.

    The next ``select_context`` would ingest the stubbed rows and re-project from a
    store that never recorded the offload, restoring the full content — so a
    local-only "reduction" would be undone behind the user's back.
    """

    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1), supports_commit=False)

    result = engine.compress(history, force=True)

    assert result == history
    assert engine.compression_count == 0
    assert engine._last_compress_aborted is True
    assert "manual_commit_refused" in (engine._last_summary_error or "")


def test_manual_compress_applies_the_committed_projection_only():
    history = big_history()
    engine, factory = build_engine(
        snapshot_for(history, [OperationSpec(kind="offload", span_ids=("a1", "t1"))])
    )
    result = engine.compress(history, force=True)
    assert len(result) == len(history)
    assert "[clm.offloaded" in result[2]["content"]
    assert result[4]["content"] == "now the tests"  # user row untouched
    assert estimate_messages_tokens(result) < estimate_messages_tokens(history)
    assert engine.compression_count == 1
    assert factory.store.commits == []  # manual applies, does not invent new ops


def test_manual_compress_aborts_honestly_on_a_refused_projection():
    history = big_history()
    engine, factory = build_engine(
        snapshot_for(history, [OperationSpec(kind="drop", span_ids=("a3",))])
    )
    result = engine.compress(history, force=True)
    assert result == history
    assert engine._last_compress_aborted is True
    assert "partial_tool_group" in engine._last_summary_error
    assert engine.compression_count == 0


def test_manual_compress_does_not_commit_a_plan_that_predicts_no_saving(monkeypatch):
    """Commit-before-saving: a zero-saving plan must be refused *before* the write.

    The order matters. The engine plans, checks the prediction, then commits once;
    committing first and explaining afterwards would leave the archive changed while
    the operator was told nothing happened. ``plan_offload``'s candidates are what
    the prediction sums, so a candidate that frees nothing must not reach the store.
    """

    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))

    zero = _budget.OffloadCandidate(
        order=0, group_id="g1", uids=("a1", "t1"), tokens=0, span_ids=("a1", "t1")
    )
    monkeypatch.setattr(_budget, "plan_offload", lambda candidates, need_tokens=0: [zero])

    result = engine.compress(history, force=True)

    assert result == history
    assert engine._last_compress_aborted is True
    assert "manual_no_predicted_saving" in (engine._last_summary_error or "")
    assert factory.store.commits == [], "a zero-saving plan must not be committed"
    assert factory.store.staged == []
    assert engine.compression_count == 0


def test_manual_compress_is_honest_when_the_store_accepted_but_nothing_shrank():
    """A commit that the projection cannot show is an anomaly, not a success.

    Once the transaction is durable the archive *has* changed, so reporting
    "nothing happened" would be a lie, and reporting a compaction would be a
    different one. The engine returns the transcript unchanged, keeps the success
    counter still, and says which revision accepted what — visibly, in the error
    slot and the receipt, not only in a debug log.
    """

    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))
    if not hasattr(factory.store, "commit") or factory.store.commit is None:
        pytest.skip("this fake has no commit surface")

    def commit_but_forget(*, txn_id, expected_revision):
        # The store accepts the transaction (and bumps its head) but the operation
        # never becomes part of the projection: exactly the mismatch a real store
        # exhibits when a span id no longer resolves.
        factory.store.commits.append(
            {"txn_id": txn_id, "expected_revision": expected_revision, "lost": True}
        )
        return FakeStoreResult(ok=True, revision=int(expected_revision) + 1)

    factory.store.commit = commit_but_forget

    result = engine.compress(history, force=True)

    assert result == history, "no saving to report, so the transcript is unchanged"
    assert engine.compression_count == 0, "a non-shrinking projection is not a compaction"
    assert factory.store.commits, "the engine did commit what it planned"
    assert "manual_committed_without_saving" in (engine._last_summary_error or "")
    assert engine._last_compress_aborted is False, "the archive changed: not a refusal"
    status = engine.get_status()
    assert (
        status["last_compress_error"]
        and "manual_committed_without_saving" in status["last_compress_error"]
    )
    anomaly = [r for r in status["receipts"] if r.get("anomaly")]
    assert anomaly and anomaly[-1]["anomaly"] == "manual_committed_without_saving"
    assert anomaly[-1]["durable"] is True
    assert anomaly[-1]["offloaded"], "the receipt names the groups that were committed"


# --------------------------------------------------------------------- overflow


def _overflow_engine(snapshot, history, *, supports_commit: bool = True, reserve: int = 100):
    tokens = estimate_messages_tokens(history)
    # Deliberately tight: the usable limit sits below the current context.
    context_length = int(tokens * 0.6) + reserve
    engine, factory = build_engine(
        snapshot,
        supports_commit=supports_commit,
        context_length=context_length,
        reserve_tokens=reserve,
    )
    return engine, factory, tokens


def test_overflow_without_pressure_returns_the_projection_unchanged():
    history = big_history()
    engine, factory = build_engine(
        snapshot_for(history, [OperationSpec(kind="drop", span_ids=("a1", "t1"))]),
        context_length=10_000_000,
    )
    result = engine.compress(history, bypass_cooldown=True)
    # The committed whole-group drop applies; no overflow work is added.
    assert [row["message_uid"] for row in result] == ["u1", "a2", "u2", "a3", "t2"]
    assert result[3]["content"] == ""  # assistant call row survives with its structure
    assert result[4]["content"] == "NEWEST OUTPUT"
    assert factory.store.commits == []


def test_overflow_offloads_the_oldest_unpinned_tool_group_and_commits():
    history = big_history()
    engine, factory, before = _overflow_engine(snapshot_for(history, [], revision=1), history)
    engine.last_prompt_tokens = 0

    result = engine.compress(history, bypass_cooldown=True)

    after = estimate_messages_tokens(result)
    assert after < before
    assert "[clm.offloaded" in result[2]["content"]  # oldest group stubbed
    assert result[6]["content"] == "NEWEST OUTPUT"  # newest tool output protected
    assert result[0]["content"] == "list everything"  # user row untouched
    assert len(factory.store.staged) == 1
    assert len(factory.store.commits) == 1
    staged_ops, expected_revision, txn_id, actor_origin = factory.store.staged[0]
    assert expected_revision == 1
    # Overflow is engine policy, so the actor must be a valid store actor.
    assert actor_origin == "system"
    assert txn_id.startswith("overflow-")
    assert [op.kind for op in staged_ops] == ["offload"]
    assert set(staged_ops[0].span_ids) == {"a1", "t1"}
    assert factory.store.commits[0] == {"txn_id": txn_id, "expected_revision": 1}
    assert engine.compression_count == 1


def test_overflow_uses_the_edit_operation_shape_the_store_accepts():
    """A real ``EditOperation`` must be built, not the structural fallback."""
    history = big_history()
    engine, factory, _ = _overflow_engine(snapshot_for(history, [], revision=1), history)

    engine.compress(history, bypass_cooldown=True)

    staged_ops, _, _, actor_origin = factory.store.staged[0]
    for operation in staged_ops:
        # The fallback spec would carry no provenance and would be rejected by
        # the store's coerce_operations(); the real type is importable here.
        assert operation.__class__.__name__ == "EditOperation"
        assert str(operation.actor_origin) == actor_origin == "system"


def test_overflow_treats_pin_records_and_pin_mappings_as_pins():
    """``StoreSnapshot.pins`` is ``Mapping[span_id, PinRecord]``; both pin shapes block offload."""
    from hermes_clm.models import ActorOrigin, PinRecord

    history = big_history()
    as_sequence = snapshot_for(history, [], revision=1)
    as_sequence.pins = (  # type: ignore[assignment]
        PinRecord(span_id="a1", actor_origin=ActorOrigin.OPERATOR, revision=1),
        PinRecord(span_id="t1", actor_origin=ActorOrigin.OPERATOR, revision=1),
    )
    as_mapping = snapshot_for(history, [], revision=1)
    as_mapping.pins = {  # type: ignore[assignment]
        "a1": PinRecord(span_id="a1", actor_origin=ActorOrigin.OPERATOR, revision=1),
        "t1": PinRecord(span_id="t1", actor_origin=ActorOrigin.OPERATOR, revision=1),
    }

    for snapshot in (as_sequence, as_mapping):
        engine, factory, before = _overflow_engine(snapshot, history)
        result = engine.compress(history, bypass_cooldown=True)
        assert "[clm.offloaded" not in "".join(str(row.get("content")) for row in result)
        assert factory.store.commits == []
        assert estimate_messages_tokens(result) == before


def test_overflow_never_offloads_pinned_groups():
    history = big_history()
    engine, factory, before = _overflow_engine(
        snapshot_for(history, [], revision=1, pins=("a1", "t1")), history
    )

    result = engine.compress(history, bypass_cooldown=True)

    assert "[clm.offloaded" not in "".join(str(row.get("content")) for row in result)
    assert factory.store.commits == []
    # A shortfall is reported, never papered over by dropping user content.
    assert estimate_messages_tokens(result) == before


def test_overflow_never_offloads_system_or_user_rows():
    history = big_history() + [message("user", "x" * 20_000, "u3")]
    engine, factory, before = _overflow_engine(snapshot_for(history, [], revision=1), history)

    result = engine.compress(history, bypass_cooldown=True)

    uids = [row.get("message_uid") for row in result]
    assert "u1" in uids and "u2" in uids and "u3" in uids
    assert all(row["content"] != "" for row in result if row["role"] == "user")


def test_overflow_works_without_a_committing_store():
    history = big_history()
    engine, factory, before = _overflow_engine(
        snapshot_for(history, [], revision=1), history, supports_commit=False
    )

    result = engine.compress(history, bypass_cooldown=True)

    assert estimate_messages_tokens(result) < before
    assert "[clm.offloaded" in result[2]["content"]
    assert factory.store.commits == []
    receipts = [item for item in engine._last_receipts if item.get("stage") == "overflow"]
    assert receipts and receipts[-1]["durable"] is False


def test_overflow_aborts_on_a_refused_projection():
    history = big_history()
    engine, factory, before = _overflow_engine(
        snapshot_for(history, [OperationSpec(kind="drop", span_ids=("a3",))], revision=1), history
    )

    result = engine.compress(history, bypass_cooldown=True)

    assert result == history
    assert engine._last_compress_aborted is True
    assert factory.store.commits == []


def test_overflow_with_unknown_context_length_does_nothing():
    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1), context_length=0)

    result = engine.compress(history, bypass_cooldown=True)

    assert result == history
    assert factory.store.commits == []


# -------------------------------------------------------------- no hidden egress


def test_compression_paths_never_import_hermes_or_call_a_model():
    history = big_history()
    engine, factory, _ = _overflow_engine(snapshot_for(history, [], revision=1), history)
    engine.compress(history, force=True)
    engine.compress(history, bypass_cooldown=True)
    assert "agent" not in sys.modules

    config = json.loads(json.dumps(engine.get_status()["config"]))
    assert not any(key in config for key in ("model", "provider", "base_url", "api_key"))


# ------------------------------------------------- calibration and offload order


def calibration_history():
    """Four equal tool groups; the fourth is the newest, protected result."""

    rows = []
    for index in range(4):
        rows.append(message("user", f"step {index}", f"u{index}"))
        rows.append(assistant_call(f"a{index}", f"call_{index}"))
        rows.append(tool_result(f"t{index}", f"call_{index}", "OUTPUT LINE\n" * 300))
    return rows


def test_overflow_planning_prices_candidates_in_the_calibrated_vocabulary():
    """Over-offload counterexample: a real-vs-estimate ratio of 2.

    ``need_tokens`` is calibrated, so candidates must be too. Priced raw, each
    group looks half its real cost and the planner offloads a third group it
    does not need (and, at a ratio below 1, too few to restore the reserve).
    """

    history = calibration_history()
    raw_total = estimate_messages_tokens(history)
    reserve = 200
    # ratio 2.0 => calibrated total 7488, each group 1860; a 2500-token deficit
    # is covered by two groups in calibrated tokens, by three in raw ones.
    engine, factory = build_engine(
        snapshot_for(history, [], revision=1),
        context_length=raw_total * 2 - 2500 + reserve,
        reserve_tokens=reserve,
    )
    engine.calibration = _budget.TokenCalibration(2.0)

    result = engine.compress(history, bypass_cooldown=True)

    receipt = [r for r in engine.get_status()["receipts"] if r.get("stage") == "overflow"][-1]
    assert receipt["needed"] == 2500
    assert len(receipt["offloaded"]) == 2, receipt["offloaded"]
    assert factory.store.commits, "the two-group plan was committed"
    assert estimate_messages_tokens(result) < raw_total
    assert engine.budget_state()["fits"] is True, "the committed offload restores the reserve"


def order_history():
    """Group ids that sort in the *opposite* order to the transcript rows."""

    rows = []
    for uid, tag in (("z-old", "OLD"), ("m-mid", "MID"), ("a-new", "NEW")):
        rows.append(message("user", f"step {uid}", f"u-{uid}"))
        rows.append(assistant_call(uid, f"call_{uid}"))
        rows.append(tool_result(f"t-{uid}", f"call_{uid}", f"{tag} OUTPUT\n" * 300))
    return rows


def test_offload_plans_the_oldest_group_first_even_when_ids_sort_backwards():
    """``sorted(group_id)`` offloads ``group:a-new`` before ``group:z-old``."""

    history = order_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))

    candidates = engine._offload_candidates(history, factory.store.snapshot())
    assert [candidate.group_id for candidate in candidates] == [
        "group:z-old",
        "group:m-mid",
        "group:a-new",
    ], "plan order must be history row order, not lexicographic group id order"

    result = engine.compress([dict(row) for row in history], force=True)

    receipt = [r for r in engine.get_status()["receipts"] if r.get("stage") == "manual"][-1]
    assert receipt["offloaded"] == ["group:z-old"]
    joined = "\n".join(str(row.get("content") or "") for row in result)
    assert "OLD OUTPUT" not in joined, "the oldest group is the one that was offloaded"
    assert "MID OUTPUT" in joined and "NEW OUTPUT" in joined


def test_manual_plan_is_priced_in_the_same_vocabulary_as_the_target():
    history = calibration_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))
    engine.calibration = _budget.TokenCalibration(2.0)

    result = engine.compress(history, force=True)

    receipt = [r for r in engine.get_status()["receipts"] if r.get("stage") == "manual"][-1]
    assert receipt["calibration_ratio"] == 2.0
    assert receipt["needed"] > 0
    assert receipt["predicted_saving"] >= receipt["needed"], (
        "the plan must cover the calibrated target it was asked for"
    )
    assert estimate_messages_tokens(result) < estimate_messages_tokens(history)


def test_default_status_summary_reports_the_compaction_diagnostic():
    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1), manual_target_ratio=0)

    engine.compress(history, force=True)
    payload = json.loads(engine.handle_tool_call("clm_status", {}))

    assert payload["detail"] == "summary"
    assert payload["last_compress_aborted"] is True
    assert "manual_no_offloadable_groups" in (payload["last_compress_error"] or "")
    assert payload["store"] == "attached"
    assert isinstance(payload["engine_version"], str) and payload["engine_version"]
    assert payload["store_error"] is None
    assert payload["last_lineage_error"] is None
    for expensive in ("receipts", "projection", "budget", "config"):
        assert expensive not in payload


def test_stage_preview_after_compact_prices_the_compacted_projection():
    """The pre-compaction request is stale: pricing against it counts archived text."""

    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))
    request = assemble(history)
    assert engine.select_context(request, conversation_messages=history) is None

    compacted = engine.compress([dict(row) for row in history], force=True)
    assert any("[clm.offloaded" in str(row.get("content")) for row in compacted)

    priced = engine.tool_service().preview([{"kind": "drop", "span_ids": ["a2"]}])

    assert priced["available"] is True
    assert priced["before"] == engine.budget_state(compacted)["raw_estimated_tokens"]
    assert priced["before"] < engine.budget_state(request)["raw_estimated_tokens"], (
        "a stale request would price the text the compaction already offloaded"
    )


def test_stage_preview_after_compact_needs_no_prior_turn():
    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))

    compacted = engine.compress(history, force=True)
    priced = engine.tool_service().preview([{"kind": "drop", "span_ids": ["a2"]}])

    assert priced["available"] is True, priced
    assert priced["before"] == engine.budget_state(compacted)["raw_estimated_tokens"]
