"""Ambiguous identity aborts the projection instead of guessing a row."""

from __future__ import annotations

from _clm_fakes import FakeOperation, assemble, message, snapshot_for

from hermes_clm.projection import project


def _ambiguous_history():
    return [
        message("user", "start", "u0"),
        message("assistant", "ok", "a1"),
        message("assistant", "ok", "a2"),
        message("user", "next", "u1"),
    ]


def test_an_op_on_an_ambiguous_row_aborts_the_whole_projection():
    history = _ambiguous_history()
    # One of the two identical rows is missing from the request.
    request = assemble([history[0], history[1], history[3]])
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert result.messages == ()
    assert any(item["code"] == "ambiguous_identity" for item in result.conflicts)


def test_an_op_on_the_other_ambiguous_candidate_also_aborts():
    history = _ambiguous_history()
    request = assemble([history[0], history[1], history[3]])
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a2",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "ambiguous_identity" for item in result.conflicts)


def test_unrelated_edits_still_project_when_a_different_row_is_ambiguous():
    history = _ambiguous_history() + [message("user", "third", "u2"), message("assistant", "fourth", "a3")]
    # One duplicate row is missing (a1/a2 ambiguous), but the target is elsewhere.
    request = assemble([history[0], history[1], history[3], history[4], history[5]])
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a3",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert [row["content"] for row in result.messages] == ["SYSTEM PROMPT", "start", "ok", "next", "third"]


def test_alignment_diagnostics_expose_the_ambiguous_uids():
    history = _ambiguous_history()
    request = assemble([history[0], history[1], history[3]])
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert sorted(result.alignment["ambiguous_uids"]) == ["a1", "a2"]


def test_identical_duplicates_shown_in_full_are_editable_by_position():
    history = _ambiguous_history()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a2",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    assert [row["content"] for row in result.messages] == [
        "SYSTEM PROMPT",
        "start",
        "ok",
        "next",
    ]
    assert result.applied[0]["uid"] == "a2"
