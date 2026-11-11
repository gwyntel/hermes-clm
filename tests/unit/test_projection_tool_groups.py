"""Tool-group atomicity and protected-row behaviour."""

from __future__ import annotations

from _clm_fakes import (
    FakeOperation,
    assemble,
    assistant_call,
    coding_turns,
    message,
    snapshot_for,
    tool_result,
)
from hermes_clm.projection import project


def test_whole_group_drop_removes_call_and_results_together():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(
        history,
        [FakeOperation(kind="drop", span_ids=("a1", "t1"))],
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    contents = [row["content"] for row in result.messages]
    assert "list the files" in contents
    assert all(row["role"] != "tool" or row["tool_call_id"] != "call_1" for row in result.messages)
    remaining_calls = [
        call["id"] for row in result.messages for call in (row.get("tool_calls") or [])
    ]
    assert remaining_calls == ["call_2"]


def test_partial_group_drop_is_refused_whole():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert result.messages == ()
    assert any(item["code"] == "partial_tool_group" for item in result.conflicts)


def test_dropping_only_the_result_of_a_group_is_refused():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("t1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "partial_tool_group" for item in result.conflicts)


def test_group_with_a_member_the_request_does_not_show_is_refused():
    history = coding_turns()
    # The host dropped the tool result from the request (e.g. an earlier prune),
    # so coverage of a full-group drop cannot be verified here.
    request = assemble([row for row in history if row["message_uid"] != "t1"])
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1", "t1"))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "partial_tool_group" for item in result.conflicts)
    assert any(item["reason"] == "not_in_request" for item in result.inert)


def test_rewrite_inside_a_group_keeps_call_and_result_pairing():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(
        history,
        [FakeOperation(kind="rewrite", span_ids=("a1",), replacement="ran ls")],
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assistant_row = result.messages[2]
    result_row = result.messages[3]
    assert assistant_row["content"] == "ran ls"
    assert assistant_row["tool_calls"][0]["id"] == "call_1"
    assert result_row["tool_call_id"] == "call_1"


def test_newest_tool_result_is_protected_from_offload():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="offload", span_ids=("t2",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "protected_row" for item in result.conflicts)


def test_newest_tool_result_is_protected_from_drop():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a3", "t2"))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "protected_row" for item in result.conflicts)


def test_older_tool_group_may_be_dropped():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1", "t1"))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert [row.get("tool_call_id") for row in result.messages if row["role"] == "tool"] == [
        "call_2"
    ]


def test_second_group_of_a_three_group_history_stays_editable():
    history = coding_turns()
    extra = [
        message("assistant", "tests pass", "a4"),
        message("user", "one more thing", "u3"),
        assistant_call("a5", "call_3"),
        tool_result("t3", "call_3", "LATEST"),
    ]
    history = history + extra
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="offload", span_ids=("a3", "t2"))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    tools = {
        row["tool_call_id"]: row["content"] for row in result.messages if row["role"] == "tool"
    }
    assert "[clm.offloaded" not in tools["call_1"]  # untouched group stays verbatim
    assert "[clm.offloaded" in tools["call_2"]  # both members of the targeted group stubbed
    assert tools["call_3"] == "LATEST"  # newest tool output is host-owned


def test_protected_system_and_user_rows_survive_identical_bytes():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1", "t1"))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert result.messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert [row for row in result.messages if row["role"] == "user"] == [
        {"role": "user", "content": "list the files"},
        {"role": "user", "content": "now run the tests"},
    ]


def test_an_attempt_to_drop_a_user_row_is_refused():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("u1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "protected_row" for item in result.conflicts)


def test_an_operation_naming_an_unarchived_span_is_inert():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(
        history, [FakeOperation(kind="rewrite", span_ids=("system",), replacement="new system")]
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    # The host's system prompt is never archived, so the op cannot name it.
    assert result.ok
    assert not result.changed
    assert result.inert[0]["reason"] == "unknown_span"
    assert result.messages[0]["content"] == "SYSTEM PROMPT"


def test_an_archived_protected_span_is_refused_even_if_the_role_looks_editable():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(
        history,
        [FakeOperation(kind="offload", span_ids=("a1",))],
        protected=("a1",),
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "protected_span" for item in result.conflicts)
