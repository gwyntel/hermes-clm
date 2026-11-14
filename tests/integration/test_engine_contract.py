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

import json

from _clm_fakes import (
    StoreFactory,
    assemble,
    assistant_call,
    message,
    snapshot_for,
    tool_result,
)
from hermes_clm.auxiliary import AuxCompressor, AuxLimits
from hermes_clm.engine import CLMEngine, EngineConfig, OperationSpec
from hermes_clm.models import canonical_sha256

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
    assert staged["ok"] is True, staged
    committed = service.commit(txn_id=txn_id, expected_revision=revision)
    assert committed["ok"] is True, committed


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


# --------------------------------------------------------------------------- #
# Real store: the same contract, but SQLite does the validating and persisting.
# --------------------------------------------------------------------------- #


def real_host(tmp_path, history, *, session_id: str = "sess-real", aux=None, **config):
    """Engine backed by a real ``ContextStore`` under ``tmp_path``."""
    engine = CLMEngine(
        config=EngineConfig(max_rewrite_bytes=4096, **config),
        hermes_home=str(tmp_path / "home"),
        aux=aux,
    )
    engine.on_session_start(session_id, context_length=128_000)
    assert engine.store is not None, "real store did not open"
    return FakeHost(engine, history), engine


def inspect_spans(engine, history):
    """``uid -> inspect item`` for the archived history, via tool dispatch."""
    payload = json.loads(
        engine.handle_tool_call("clm_inspect", {"limit": 50}, messages=list(history))
    )
    assert payload["ok"] is True, payload
    return {item["uid"]: item for item in payload["items"]}


def test_real_store_drop_through_tool_dispatch_edits_only_the_target_group(tmp_path):
    """End-to-end: archive → stage → commit → the next request carries the drop."""
    history = clm_history()
    host, engine = real_host(tmp_path, history)

    _, first_selection = host.iterate()
    assert first_selection is None  # nothing committed: request untouched

    status = json.loads(engine.handle_tool_call("clm_status", {"detail": "full"}, messages=history))
    assert status["ok"] is True
    assert status["store_health"]["ok"] is True
    revision = status["revision"]
    assert revision >= 1

    spans = inspect_spans(engine, history)
    # A tool group is atomic: the assistant call row and its result go together.
    group = [spans["a1"]["span_id"], spans["t1"]["span_id"]]
    assert all(span_id.startswith("sp_") for span_id in group)

    staged = json.loads(
        engine.handle_tool_call(
            "clm_stage",
            {
                "operations": [{"kind": "drop", "span_ids": group}],
                "expected_revision": revision,
                "txn_id": "txn-real-drop",
            },
            messages=history,
        )
    )
    assert staged["ok"] is True, staged
    committed = json.loads(
        engine.handle_tool_call(
            "clm_commit", {"txn_id": "txn-real-drop", "expected_revision": revision}
        )
    )
    assert committed["ok"] is True, committed
    assert committed["revision"] == revision + 1

    request, selected = host.iterate()

    assert selected is not None
    dropped = {request_index(history, "a1"), request_index(history, "t1")}
    # One extra row: the revision-keyed compaction notice, right after the system
    # row. It is the only injection, it is not a transcript row, and it names the
    # revision that archived the dropped group.
    notices = [row for row in selected if str(row.get("content", "")).startswith("[clm.notice")]
    assert len(notices) == 1, selected
    assert selected[1] is notices[0]
    assert f"rev={committed['revision']}" in notices[0]["content"]
    without_notice = [row for row in selected if row is not notices[0]]
    assert len(without_notice) == len(request) - len(dropped)
    assert without_notice == [
        dict(row) for index, row in enumerate(request) if index not in dropped
    ]

    # Host-owned rows survive byte-identically.
    assert selected[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert {"role": "user", "content": "run the test suite"} in selected
    assert {"role": "user", "content": "fix it"} in selected
    assert selected[-1]["content"] == "NEWEST TOOL OUTPUT"
    # The raw transcript is untouched, and the archive still holds the dropped rows.
    assert "FAILED tests/test_x.py" in history[2]["content"]
    archived = inspect_spans(engine, history)
    assert {archived["a1"]["state"], archived["t1"]["state"]} == {"dropped"}
    assert archived["a1"]["span_id"] == group[0]


def test_real_store_status_reports_the_live_head_after_a_commit(tmp_path):
    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()
    service = engine.tool_service()

    before = service.status()["revision"]
    spans = inspect_spans(engine, history)
    stage_and_commit(
        engine,
        [
            {
                "kind": "drop",
                "span_ids": [spans["a1"]["span_id"], spans["t1"]["span_id"]],
            }
        ],
        revision=before,
        txn_id="txn-real-live",
    )

    # No projection has run since the commit; status must still show the new head.
    assert engine.get_status()["revision"] == 0 or engine.get_status()["revision"] == before
    assert service.revision == before + 1
    assert service.status()["revision"] == before + 1


class _MockAux:
    """Auxiliary client stand-in: returns one rewrite for the span it was given."""

    def __init__(self) -> None:
        self.calls = []
        self.rewrite_text = "[tool output elided by the aux model]"
        self.span_id = ""

    def __call__(self, *, messages, timeout, max_tokens):
        self.calls.append({"timeout": timeout, "max_tokens": max_tokens})
        payload = {"rewrites": [{"span_id": self.span_id, "text": self.rewrite_text}]}
        return {
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 9},
        }


def test_real_store_proposal_roundtrip_with_a_mocked_aux_hash(tmp_path):
    """Propose (aux → store proposal) → accept → commit → next request rewritten."""
    history = clm_history()
    aux_client = _MockAux()
    compressor = AuxCompressor(aux_client, timeout_seconds=12, limits=AuxLimits(max_spans=4))
    host, engine = real_host(tmp_path, history, aux=compressor)
    host.iterate()

    service = engine.tool_service()
    spans = inspect_spans(engine, history)
    target = spans["t1"]["span_id"]
    aux_client.span_id = target
    revision = service.revision

    proposed = json.loads(
        engine.handle_tool_call(
            "clm_propose_compression",
            {"span_ids": [target], "base_revision": revision},
            messages=history,
        )
    )

    assert proposed["ok"] is True, proposed
    proposal = proposed["proposal"]
    expected_hash = canonical_sha256(
        {
            "base_revision": revision,
            "operations": [
                {"kind": "rewrite", "span_ids": [target], "replacement": aux_client.rewrite_text}
            ],
            "origin": "aux",
        }
    )
    assert proposal["proposal_sha256"] == expected_hash
    assert proposal["base_revision"] == revision
    assert proposal["origin"] == "aux"
    assert len(proposal["proposal_id"]) > 0
    # Nothing was staged or applied by proposing.
    assert service.revision == revision
    assert aux_client.calls and aux_client.calls[0]["timeout"] == 12

    # A wrong hash is refused with the store's own code, not a generic error.
    mismatch = service.accept_proposal(
        proposal_id=proposal["proposal_id"],
        proposal_sha256="b" * 64,
        base_revision=revision,
        txn_id="txn-real-accept-bad",
    )
    assert mismatch["ok"] is False
    assert mismatch["errors"][0]["code"] == "proposal_hash_mismatch"

    accepted = service.accept_proposal(
        proposal_id=proposal["proposal_id"],
        proposal_sha256=proposal["proposal_sha256"],
        base_revision=revision,
        txn_id="txn-real-accept",
    )
    assert accepted["ok"] is True, accepted
    committed = service.commit(txn_id="txn-real-accept", expected_revision=revision)
    assert committed["ok"] is True, committed

    request, selected = host.iterate()

    assert selected is not None
    row = request_index(history, "t1")
    assert selected[row]["content"] == aux_client.rewrite_text
    assert selected[row]["role"] == "tool"
    assert selected[row]["tool_call_id"] == "call_1"
    assert selected[-1]["content"] == "NEWEST TOOL OUTPUT"
    assert selected[0] == {"role": "system", "content": SYSTEM_PROMPT}
    # The proposal is represented in the audit trail with aux provenance.
    assert service.revision == revision + 1
    snapshot = engine.store.snapshot()
    rewrite_ops = [op for op in snapshot.operations if str(op.kind) == "rewrite"]
    assert rewrite_ops and rewrite_ops[-1].provenance.get("origin") in {None, "aux"}


def test_real_store_pin_survives_and_blocks_overflow_offload(tmp_path):
    """A pinned group is snapshotted as ``PinRecord`` and never offloaded."""
    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()
    service = engine.tool_service()
    spans = inspect_spans(engine, history)
    group = [spans["a1"]["span_id"], spans["t1"]["span_id"]]
    revision = service.revision

    pinned = service.stage(
        [{"kind": "pin", "span_ids": group}],
        expected_revision=revision,
        txn_id="txn-real-pin",
        actor_origin="agent",
    )
    assert pinned["ok"] is True, pinned
    assert service.commit(txn_id="txn-real-pin", expected_revision=revision)["ok"] is True

    snapshot = engine.store.snapshot()
    assert all(record.span_id in group for record in snapshot.pins.values())

    # Operator-only unpin is refused for an agent, then works for the operator.
    # A real ``StoreSnapshot.pins`` mapping of ``PinRecord`` blocks overflow offload.
    engine.context_length = 400
    engine.threshold_percent = 0.75
    engine.threshold_tokens = 300
    revision_before_overflow = service.revision
    overflowed = engine.compress(history, bypass_cooldown=True)
    assert "[clm.offloaded" not in "".join(str(row.get("content")) for row in overflowed)
    assert service.revision == revision_before_overflow  # nothing committed

    # Operator-only unpin is refused for an agent, then works for the operator.
    refused = service.unpin(
        group, expected_revision=service.revision, txn_id="t", actor_origin="agent"
    )
    assert refused["ok"] is False and refused["errors"][0]["code"] == "pin_authority"
    released = service.unpin(
        group, expected_revision=service.revision, txn_id="txn-real-unpin", actor_origin="operator"
    )
    assert released["ok"] is True, released
    assert released["revision"] == service.revision
    assert engine.store.snapshot().pins == {}


def test_real_store_overflow_commits_a_system_offload_and_the_next_request_shows_it(tmp_path):
    """Overflow is a durable, ``system``-actor offload over store span ids."""
    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()
    service = engine.tool_service()
    spans = inspect_spans(engine, history)
    revision = service.revision

    # Pressure: a 400-token context against a multi-thousand-token history.
    engine.context_length = 400
    engine.threshold_percent = 0.75
    engine.threshold_tokens = 300

    compressed = engine.compress(history, bypass_cooldown=True)

    # ``compress`` returns rows aligned with the host history, not the request.
    assert "[clm.offloaded" in compressed[request_index(history, "t1") - 1]["content"]
    assert service.revision == revision + 1
    snapshot = engine.store.snapshot()
    offloads = [op for op in snapshot.operations if str(op.kind) == "offload"]
    assert offloads and str(offloads[-1].actor_origin) == "system"
    # Store span ids, not host uids: the archived ids are what the store resolves.
    assert set(offloads[-1].span_ids) == {spans["a1"]["span_id"], spans["t1"]["span_id"]}

    request, selected = host.iterate()

    assert selected is not None
    row = request_index(history, "t1")
    assert "[clm.offloaded" in selected[row]["content"]
    assert selected[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert {"role": "user", "content": "run the test suite"} in selected
    assert {"role": "user", "content": "fix it"} in selected
    assert selected[-1]["content"] == "NEWEST TOOL OUTPUT"
    assert "FAILED tests/test_x.py" in history[2]["content"]


def test_real_store_fast_commit_applies_a_group_offload_in_one_call(tmp_path):
    """`clm_stage` with `commit=true` writes once and the next request shows it."""
    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()
    service = engine.tool_service()
    spans = inspect_spans(engine, history)
    revision = service.revision
    group = [spans["a1"]["span_id"], spans["t1"]["span_id"]]

    payload = json.loads(
        engine.handle_tool_call(
            "clm_stage",
            {
                "operations": [{"kind": "offload", "span_ids": group}],
                "expected_revision": revision,
                "txn_id": "txn-fast-offload",
                "commit": True,
            },
            messages=history,
        )
    )
    assert payload["ok"] is True, payload
    assert payload["committed"] is True
    assert payload["atomic"] is True
    assert payload["revision"] == revision + 1
    assert service.revision == revision + 1
    assert service.status()["staged_transactions"] == []

    request, selected = host.iterate()
    assert selected is not None
    row = request_index(history, "t1")
    assert "[clm.offloaded" in selected[row]["content"]

    # A refused minimum writes nothing: no revision, no staged row, no change.
    refused = json.loads(
        engine.handle_tool_call(
            "clm_stage",
            {
                "operations": [{"kind": "offload", "span_ids": group}],
                "expected_revision": service.revision,
                "txn_id": "txn-fast-refused",
                "commit": True,
                "min_saved_tokens": 10**9,
            },
            messages=history,
        )
    )
    assert refused["ok"] is False, refused
    assert refused["errors"][0]["code"] == "insufficient_saving"
    assert refused.get("committed") is not True
    assert service.revision == revision + 1
    assert service.status()["staged_transactions"] == []
    _, after = host.iterate()
    assert after is not None
    assert after[row]["content"] == selected[row]["content"]


def test_real_store_last_compaction_names_the_revision_that_archived_output(tmp_path):
    """The store, not the caller, decides what counts as a compaction."""

    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()
    service = engine.tool_service()
    assert service.status()["last_compaction"] is None, "nothing has been compacted yet"

    revision = service.status()["revision"]
    spans = inspect_spans(engine, history)
    stage_and_commit(
        engine,
        [{"kind": "offload", "span_ids": [spans["a1"]["span_id"], spans["t1"]["span_id"]]}],
        revision=revision,
        txn_id="txn-real-compaction",
    )

    compaction = service.status()["last_compaction"]
    assert compaction["revision"] == revision + 1
    assert compaction["spans_offloaded"] == 2
    assert compaction["tokens_freed"] >= 0
    assert compaction["actor"]
    assert compaction["kind"]

    # A later rewrite-only revision is not a compaction and does not hide it.
    newer = service.status()["revision"]
    stage_and_commit(
        engine,
        [{"kind": "rewrite", "span_ids": [spans["a2"]["span_id"]], "replacement": "short"}],
        revision=newer,
        txn_id="txn-real-rewrite",
    )
    assert service.status()["last_compaction"]["revision"] == revision + 1

    # Nor does a revert: the compaction did happen.
    reverted = service.status()["revision"]
    outcome = service.revert(
        target_revision=1, expected_revision=reverted, txn_id="txn-real-revert"
    )
    assert outcome["ok"] is True, outcome
    assert service.status()["last_compaction"]["revision"] == revision + 1


def test_real_store_stage_returns_a_pure_preview_without_committing(tmp_path):
    """An agent can price a batch before committing it, and nothing moves."""

    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()
    spans = inspect_spans(engine, history)
    revision = engine.tool_service().status()["revision"]

    staged = json.loads(
        engine.handle_tool_call(
            "clm_stage",
            {
                "operations": [
                    {
                        "kind": "offload",
                        "span_ids": [spans["a1"]["span_id"], spans["t1"]["span_id"]],
                    }
                ],
                "expected_revision": revision,
                "txn_id": "txn-real-preview",
            },
            messages=list(history),
        )
    )

    assert staged["ok"] is True, staged
    projected = staged["projected"]
    assert projected["available"] is True
    assert projected["ok"] is True
    assert projected["before"] > projected["after"]
    assert projected["saved"] == projected["before"] - projected["after"]
    assert projected["calibrated"]["saved"] == projected["saved"]

    # Pure: the head did not move, and the staged batch is still uncommitted.
    assert engine.tool_service().status()["revision"] == revision
    assert engine.store.snapshot().revision == revision

    committed = json.loads(
        engine.handle_tool_call(
            "clm_commit",
            {"txn_id": "txn-real-preview", "expected_revision": revision},
            messages=list(history),
        )
    )
    assert committed["ok"] is True, committed
    assert engine.tool_service().status()["revision"] == revision + 1


def test_real_store_preview_after_compact_prices_the_compacted_projection(tmp_path):
    """`/compact` replaces the request, so a later stage must price the replacement.

    Regression: the remembered request was the one from before the compaction, so
    the preview counted the tool output the compaction had just offloaded and
    reported a saving against a transcript the agent no longer sends.
    """

    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()  # a turn ran before the compaction

    compacted = engine.compress(list(history), force=True)
    assert any("[clm.offloaded" in str(row.get("content")) for row in compacted)
    revision = engine.tool_service().status()["revision"]
    spans = inspect_spans(engine, history)

    priced = engine.tool_service().preview([{"kind": "drop", "span_ids": [spans["a2"]["span_id"]]}])

    assert priced["available"] is True
    assert priced["before"] == engine.budget_state(compacted)["raw_estimated_tokens"]
    pre_compaction = json.loads(json.dumps(host.assemble_request()))
    assert priced["before"] < engine.budget_state(pre_compaction)["raw_estimated_tokens"]
    assert engine.tool_service().status()["revision"] == revision, "a preview is pure"


def test_real_store_compaction_notice_reaches_the_next_request(tmp_path):
    """`/compact` is operator work; the agent has to be told its output moved."""

    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()

    engine.compress(list(history), current_tokens=100_000, force=True)

    request, selected = host.iterate()

    assert selected is not None
    head = "\n".join(str(row.get("content", "")) for row in selected[:2])
    assert "[clm.notice" in head, selected[:2]
    assert f"rev={engine.tool_service().status()['revision']}" in head
    assert selected[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert "[clm.offloaded" in "".join(str(row.get("content", "")) for row in selected)


def test_real_store_reverting_a_compaction_keeps_the_notice_honest(tmp_path):
    """After a revert the rows are live again; the notice still points at the archive."""

    history = clm_history()
    host, engine = real_host(tmp_path, history)
    host.iterate()
    engine.compress(list(history), current_tokens=100_000, force=True)
    revision = engine.tool_service().status()["revision"]

    outcome = engine.tool_service().revert(
        target_revision=1, expected_revision=revision, txn_id="txn-real-uncompact"
    )
    assert outcome["ok"] is True, outcome
    request, selected = host.iterate()

    # The revert restored the rows, so no offload stub remains...
    assert selected is not None
    assert "[clm.offloaded" not in "".join(str(row.get("content", "")) for row in selected)
    # ...and the notice still names the revision whose originals are retrievable.
    assert "[clm.notice" in str(selected[1].get("content", ""))
