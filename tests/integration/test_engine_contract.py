"""Engine/host contract against a fake Hermes host.

The host shape under test is the real one (``agent/turn_request_assembly.py``):
``select_context(request_messages, conversation_messages=..., incoming_message=...,
budget_tokens=...)`` runs before every provider request and returns either a
replacement request for *that call only* or ``None`` to leave it untouched.

The headline acceptance: an edit committed mid-turn, after the newest tool
result already exists, changes the **next** assembled request - with protected
rows surviving byte-identically and the stored transcript untouched.
"""

from __future__ import annotations

from _clm_fakes import (
    StoreFactory,
    assemble,
    assistant_call,
    message,
    snapshot_for,
    tool_result,
)
from hermes_clm.engine import CLMEngine, EngineConfig, OperationSpec

SYSTEM_PROMPT = "You are Hermes. Follow the operator's instructions."


def clm_history():
    """Prior turn with a large tool result, then a turn in flight.

    ``t1`` is an *older* tool result (editable); ``t2`` is the newest tool result
    of the live turn and therefore host-owned.
    """
    return [
        message("user", "run the test suite", "u1"),
        assistant_call("a1", "call_1"),
        tool_result("t1", "call_1", "FAILED tests/test_x.py::test_y - AssertionError\n" * 60),
        message("assistant", "One test fails on the assertion.", "a2"),
        message("user", "fix it", "u2"),
        assistant_call("a3", "call_2"),
        tool_result("t2", "call_2", "NEWEST TOOL OUTPUT"),
    ]


def request_index(history, uid: int | str) -> int:
    """Request index of a history row (system prompt occupies request index 0)."""
    for position, row in enumerate(history):
        if row.get("message_uid") == uid:
            return position + 1
    raise AssertionError(f"no history row with uid {uid}")


class FakeHost:
    """Minimal host: assembles a request, runs the engine hook, keeps history raw."""

    def __init__(self, engine: CLMEngine, history) -> None:
        self.engine = engine
        self.history = history

    def assemble_request(self, *, injections=None, prefill=()) -> list:
        return assemble(self.history, system=SYSTEM_PROMPT, prefill=prefill, injections=injections)

    def iterate(self, *, injections=None, prefill=(), budget_tokens: int = 128_000):
        """One provider-bound iteration: assemble, then let the engine select context."""
        request = self.assemble_request(injections=injections, prefill=prefill)
        incoming = next((row for row in reversed(request) if row["role"] == "user"), None)
        selected = self.engine.select_context(
            request,
            conversation_messages=list(self.history),
            incoming_message=incoming,
            budget_tokens=budget_tokens,
        )
        return request, selected


def build_host(history):
    snapshot = snapshot_for(history, [], revision=1)
    factory = StoreFactory(snapshot)
    engine = CLMEngine(
        config=EngineConfig(max_rewrite_bytes=4096),
        store_factory=factory,
        hermes_home="/tmp/hermes-home",
    )
    engine.on_session_start("sess-live", hermes_home="/tmp/hermes-home", context_length=128_000)
    return FakeHost(engine, history), factory


def stage_and_commit(
    engine: CLMEngine, operations, *, revision: int, txn_id: str = "txn-1"
) -> None:
    service = engine.tool_service()
    staged = service.stage(
        operations, expected_revision=revision, txn_id=txn_id, actor_origin="agent"
    )
    assert staged.ok
    committed = service.commit(txn_id=txn_id, expected_revision=revision)
    assert committed.ok


def test_an_edit_committed_after_a_tool_result_changes_the_next_request():
    history = clm_history()
    host, factory = build_host(history)
    tool_row = request_index(history, "t1")

    first_request, first_selection = host.iterate()
    assert first_selection is None  # nothing committed yet: byte-identical request

    # The agent decides mid-turn, after the newest tool result arrived, to compress
    # an older tool output it already received.
    stage_and_commit(
        host.engine,
        [OperationSpec(kind="offload", span_ids=("t1",), actor_origin="agent")],
        revision=1,
    )

    second_request, second_selection = host.iterate()

    assert second_selection is not None
    assert "FAILED tests/test_x.py" not in second_selection[tool_row]["content"]
    assert "[clm.offloaded" in second_selection[tool_row]["content"]
    # Structure and identity are preserved; only content changed.
    assert second_selection[tool_row]["role"] == "tool"
    assert second_selection[tool_row]["tool_call_id"] == "call_1"
    assert second_selection[tool_row - 1]["tool_calls"][0]["id"] == "call_1"
    assert len(second_selection) == len(second_request)
    # The stale first request and the raw transcript are untouched.
    assert "FAILED tests/test_x.py" in first_request[tool_row]["content"]
    assert "FAILED tests/test_x.py" in history[2]["content"]
    assert factory.store.commits[0]["expected_revision"] == 1


def test_system_user_and_injected_rows_survive_byte_identically():
    history = clm_history()
    host, _ = build_host(history)
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)

    request, selected = host.iterate(injections={0: "\n\n[MEMORY] operator prefers flock"})

    assert selected is not None
    assert selected[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert selected[1]["content"] == request[1]["content"]
    assert "[MEMORY] operator prefers flock" in selected[1]["content"]
    assert selected[-3]["content"] == "fix it"
    assert selected[-1]["content"] == "NEWEST TOOL OUTPUT"


def test_an_uncommitted_stage_does_not_change_the_request():
    history = clm_history()
    host, _ = build_host(history)
    service = host.engine.tool_service()
    service.stage(
        [OperationSpec(kind="drop", span_ids=("a1", "t1"))],
        expected_revision=1,
        txn_id="txn-dry",
        actor_origin="agent",
    )

    _, selected = host.iterate()

    assert selected is None


def test_discard_removes_a_staged_transaction():
    history = clm_history()
    host, factory = build_host(history)
    service = host.engine.tool_service()
    service.stage(
        [OperationSpec(kind="drop", span_ids=("a1", "t1"))],
        expected_revision=1,
        txn_id="txn-discard",
        actor_origin="agent",
    )
    service.discard(txn_id="txn-discard")

    _, selected = host.iterate()

    assert selected is None
    assert factory.store.discarded == ["txn-discard"]
    assert factory.store.commits == []


def test_a_refused_edit_leaves_the_request_byte_identical():
    history = clm_history()
    host, _ = build_host(history)
    stage_and_commit(host.engine, [OperationSpec(kind="drop", span_ids=("a1",))], revision=1)

    request, selected = host.iterate()

    assert selected is None
    assert host.engine.get_status()["last_error"] == "projection_refused"
    assert host.engine.get_status()["projection"]["ok"] is False


def test_protected_newest_tool_output_is_never_edited():
    history = clm_history()
    host, _ = build_host(history)
    newest = request_index(history, "t2")
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)

    _, selected = host.iterate()

    assert selected is not None
    assert selected[newest]["content"] == "NEWEST TOOL OUTPUT"


def test_repeated_iterations_of_one_revision_are_byte_stable():
    history = clm_history()
    host, _ = build_host(history)
    tool_row = request_index(history, "t1")
    stage_and_commit(
        host.engine,
        [OperationSpec(kind="rewrite", span_ids=("t1",), replacement="[tool output elided]")],
        revision=1,
    )

    _, first = host.iterate()
    _, second = host.iterate()

    assert first == second
    assert first[tool_row]["content"] == "[tool output elided]"


def test_returned_rows_carry_no_persistence_fields():
    history = clm_history()
    host, _ = build_host(history)
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)

    _, selected = host.iterate()

    assert selected is not None
    for row in selected:
        assert "message_uid" not in row
        assert "_row_id" not in row


def test_caller_mutation_of_the_selection_cannot_reach_the_store_or_later_requests():
    history = clm_history()
    host, _ = build_host(history)
    tool_row = request_index(history, "t1")
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)

    _, selected = host.iterate()
    selected[0]["content"] = "TAMPERED"
    selected[tool_row]["content"] = "TAMPERED"

    _, again = host.iterate()
    assert again[0]["content"] == SYSTEM_PROMPT
    assert "[clm.offloaded" in again[tool_row]["content"]
    assert "FAILED tests/test_x.py" in history[2]["content"]


def test_prefill_and_ephemeral_rows_pass_through():
    history = clm_history()
    host, _ = build_host(history)
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)

    request, selected = host.iterate(
        prefill=[{"role": "assistant", "content": "Sure, here is the plan:"}]
    )

    assert selected is not None
    assert selected[1] == {"role": "assistant", "content": "Sure, here is the plan:"}
    assert selected[2:] == [dict(row) for row in request[2:]] or "[clm.offloaded" in str(
        selected[request_index(history, "t1") + 1]["content"]
    )


def test_a_broken_snapshot_fails_open_without_touching_the_request():
    history = clm_history()
    host, factory = build_host(history)
    factory.store.snapshot = lambda: None  # type: ignore[method-assign]

    request, selected = host.iterate()

    assert selected is None
    assert host.engine.get_status()["last_error"] == "snapshot_unavailable"


def test_revision_and_budget_are_visible_to_the_agent_through_status():
    history = clm_history()
    host, _ = build_host(history)
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)
    host.iterate(budget_tokens=64_000)

    status = host.engine.get_status()
    assert status["revision"] == 2  # the fake store bumps the head revision on commit
    assert status["budget"]["context_length"] == 128_000
    assert status["budget"]["tier"] in {"normal", "elevated", "high", "critical"}
    assert status["projection"]["applied"]


def test_no_store_write_happens_during_the_projection_itself():
    history = clm_history()
    host, factory = build_host(history)
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)
    commits_before = len(factory.store.commits)

    host.iterate()
    host.iterate()

    assert len(factory.store.commits) == commits_before
    # Ingest is write-through and idempotent by contract; only that repeats.
    assert len(factory.store.ingest_calls) >= 2


def test_a_second_agent_clone_gets_its_own_engine_and_store():
    history = clm_history()
    host, factory = build_host(history)
    stage_and_commit(host.engine, [OperationSpec(kind="offload", span_ids=("t1",))], revision=1)
    host.iterate()

    child = host.engine.clone_for_agent()
    # No shared SQLite handle, no inherited in-memory projection state.
    assert child is not host.engine
    assert child.store is None
    assert child._last_projection is None
    assert child.session_id is None

    child.on_session_start("sess-child")
    assert child.store is not host.engine.store
    assert child.session_id == "sess-child"
    assert [entry["session_id"] for entry in factory.opened] == ["sess-live", "sess-child"]
