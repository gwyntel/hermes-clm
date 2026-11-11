"""Compression modes: identity, manual force, overflow with archive-backed offload."""

from __future__ import annotations

import json
import sys

from _clm_fakes import (
    StoreFactory,
    assistant_call,
    message,
    snapshot_for,
    tool_result,
)
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


def test_manual_compress_reports_no_change_when_the_projection_saves_nothing():
    history = big_history()
    engine, factory = build_engine(snapshot_for(history, [], revision=1))
    result = engine.compress(history, force=True)
    assert result == history
    assert engine.compression_count == 0
    assert getattr(engine, "_last_compress_aborted", False) is False
    assert factory.store.commits == []


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


def test_manual_compress_reports_no_change_when_no_operations_exist():
    history = big_history()
    engine, _ = build_engine(snapshot_for(history, [], revision=1))
    assert engine.compress(history, force=True) == history
    assert engine.compression_count == 0


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
    assert actor_origin == "engine:overflow"
    assert txn_id.startswith("overflow-")
    assert [op.kind for op in staged_ops] == ["offload"]
    assert set(staged_ops[0].span_ids) == {"a1", "t1"}
    assert factory.store.commits[0] == {"txn_id": txn_id, "expected_revision": 1}
    assert engine.compression_count == 1


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
