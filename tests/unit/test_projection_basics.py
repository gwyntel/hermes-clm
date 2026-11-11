"""Projection: protected pass-through, drop/rewrite/offload/restore, purity."""

from __future__ import annotations

import copy
import json

from _clm_fakes import (
    FakeOperation,
    assemble,
    assistant_call,
    coding_turns,
    message,
    snapshot_for,
)

from hermes_clm.projection import offload_stub, project


def history_three():
    return [
        message("user", "first question", "u1"),
        message("assistant", "first answer", "a1"),
        message("user", "second question", "u2"),
        message("assistant", "second answer", "a2"),
    ]


def test_identity_projection_returns_equal_deep_copies():
    history = history_three()
    request = assemble(history)
    snapshot = snapshot_for(history, [], revision=0)

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert not result.changed
    assert list(result.messages) == request
    assert not any(row is original for row, original in zip(result.messages, request, strict=True))


def test_drop_removes_only_the_targeted_assistant_row():
    history = history_three()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert [row["content"] for row in result.messages] == [
        "SYSTEM PROMPT",
        "first question",
        "second question",
        "second answer",
    ]
    assert result.applied[0]["kind"] == "drop"
    assert result.applied[0]["uid"] == "a1"


def test_rewrite_changes_content_and_preserves_structure():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(
        history,
        [FakeOperation(kind="rewrite", span_ids=("t1",), replacement="[summary of output]")],
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    rewritten = result.messages[3]
    assert rewritten["content"] == "[summary of output]"
    assert rewritten["role"] == "tool"
    assert rewritten["tool_call_id"] == "call_1"
    assert result.messages[2]["tool_calls"][0]["id"] == "call_1"
    assert result.after_tokens < result.before_tokens


def test_rewrite_never_touches_role_tool_calls_or_ids():
    history = [message("user", "go", "u1"), assistant_call("a1", "call_1")]
    request = assemble(history)
    snapshot = snapshot_for(
        history, [FakeOperation(kind="rewrite", span_ids=("a1",), replacement="short")]
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    original = request[2]
    updated = result.messages[2]
    assert updated["content"] == "short"
    assert set(updated) == set(original)
    assert updated["tool_calls"] == original["tool_calls"]


def test_offload_stub_names_only_span_and_hash():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="offload", span_ids=("t1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    stub = result.messages[3]["content"]
    span = snapshot.spans["t1"]
    assert stub == offload_stub("t1", span.content_sha256)
    assert "t1" in stub and span.content_sha256 in stub
    assert stub.startswith("[clm.offloaded")
    lowered = stub.lower()
    for imperative in ("please", "you must", "call ", "run ", "use ", "read ", "ignore"):
        assert imperative not in lowered


def test_restore_reinstates_the_archived_content():
    history = [message("user", "go", "u1"), message("assistant", "original text", "a1")]
    request = assemble(history)
    snapshot = snapshot_for(
        history,
        [
            FakeOperation(kind="rewrite", span_ids=("a1",), replacement="offloaded summary"),
            FakeOperation(kind="restore", span_ids=("a1",)),
        ],
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert result.messages[2]["content"] == "original text"
    # Receipts describe the row's final effect: a superseded rewrite is not reported.
    assert [item["kind"] for item in result.applied] == ["restore"]


def test_last_write_wins_for_repeated_operations():
    history = [message("user", "go", "u1"), message("assistant", "original", "a1")]
    request = assemble(history)
    snapshot = snapshot_for(
        history,
        [
            FakeOperation(kind="rewrite", span_ids=("a1",), replacement="first"),
            FakeOperation(kind="rewrite", span_ids=("a1",), replacement="second"),
        ],
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.messages[2]["content"] == "second"


def test_unknown_span_is_inert_not_fatal():
    history = history_three()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("ghost",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert not result.changed
    assert result.inert[0]["reason"] == "unknown_span"
    assert list(result.messages) == request


def test_span_absent_from_the_request_is_inert():
    history = history_three()
    request = assemble([history[0], history[3]])
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert result.inert[0]["reason"] == "not_in_request"


def test_noop_kinds_are_receipts_only():
    history = history_three()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="boundary", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok and not result.changed
    assert result.inert[0]["reason"] == "noop_operation"


def test_unknown_operation_kind_refuses_the_whole_projection():
    history = history_three()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="summarize", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert result.messages == ()
    assert result.conflicts[0]["code"] == "unknown_operation_kind"


def test_revision_and_token_accounting_are_reported():
    history = history_three()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))], revision=7)

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.revision == 7
    assert result.before_tokens > result.after_tokens
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["revision"] == 7 and payload["changed"] is True


def test_stale_api_content_sidecar_is_dropped_from_rewritten_rows():
    history = [message("user", "go", "u1"), message("assistant", "original", "a1")]
    request = assemble(history)
    request[2]["api_content"] = "original"
    snapshot = snapshot_for(
        history, [FakeOperation(kind="rewrite", span_ids=("a1",), replacement="edited")]
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.messages[2]["content"] == "edited"
    assert "api_content" not in result.messages[2]


def test_projection_is_deterministic():
    history = history_three()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))], revision=3)

    first = project(request, conversation_messages=history, snapshot=snapshot)
    second = project(request, conversation_messages=history, snapshot=snapshot)

    assert first.messages == second.messages
    assert first.to_dict() == second.to_dict()


def test_invalid_request_rows_are_refused():
    result = project([{"role": "user"}], conversation_messages=[], snapshot=None)  # type: ignore[list-item]
    assert result.ok  # an empty-history request simply passes through

    result = project([], conversation_messages=[], snapshot=None)
    assert not result.ok
    assert result.conflicts[0]["code"] == "invalid_request"


def test_history_entries_are_not_mutated():
    history = history_three()
    before = copy.deepcopy(history)
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))])

    project(request, conversation_messages=history, snapshot=snapshot)

    assert history == before
