"""Projection purity: deep copies both ways, no writes, no host mutation."""

from __future__ import annotations

import copy

from _clm_fakes import FakeOperation, assemble, coding_turns, message, snapshot_for
from hermes_clm.projection import project


def test_returned_rows_are_independent_of_the_request():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(
        history, [FakeOperation(kind="rewrite", span_ids=("a1",), replacement="short")]
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    # Mutating the result must not reach the caller's request.
    result.messages[2]["tool_calls"][0]["function"]["arguments"] = '{"mutated": true}'
    result.messages[2]["content"] = "mutated"
    assert request[2]["content"] == ""
    assert request[2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_untouched_rows_are_deep_copies_not_shared_references():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1", "t1"))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    survivors = [request[0], request[1], request[4], request[5], request[6], request[7]]
    for produced, original in zip(result.messages, survivors, strict=True):
        assert produced == original
        assert produced is not original


def test_caller_mutation_cannot_affect_a_later_projection():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1", "t1"))])

    first = project(request, conversation_messages=history, snapshot=snapshot)
    first.messages[0]["content"] = "TAMPERED"
    second = project(request, conversation_messages=history, snapshot=snapshot)

    assert second.messages[0]["content"] == "SYSTEM PROMPT"


def test_projection_never_writes_to_the_snapshot_or_its_spans():
    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(
        history, [FakeOperation(kind="rewrite", span_ids=("a1",), replacement="short")]
    )
    before = copy.deepcopy(snapshot)

    project(request, conversation_messages=history, snapshot=snapshot)

    assert snapshot.revision == before.revision
    assert snapshot.operations == before.operations
    assert snapshot.pins == before.pins
    for key, span in snapshot.spans.items():
        assert span.message == before.spans[key].message
        assert span.content_sha256 == before.spans[key].content_sha256


def test_projection_reads_no_clock_and_no_environment(monkeypatch):
    import time as _time

    history = coding_turns()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1", "t1"))])

    def _boom(*args, **kwargs):
        raise AssertionError("projection must not read the clock")

    monkeypatch.setattr(_time, "time", _boom)
    monkeypatch.setattr(_time, "monotonic", _boom)

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok and result.changed


def test_projection_needs_no_store_writes_and_no_aux_calls():
    """The snapshot is read-only duck-typed state; nothing write-like is available."""

    class ReadOnlySnapshot:
        revision = 2
        operations = (FakeOperation(kind="drop", span_ids=("a2",)),)
        pins = ()
        head_hash = "h"

        @property
        def spans(self):
            return snapshot_for([message("user", "q", "u1"), message("assistant", "a", "a2")]).spans

    history = [message("user", "q", "u1"), message("assistant", "a", "a2")]
    request = assemble(history)

    result = project(request, conversation_messages=history, snapshot=ReadOnlySnapshot())

    assert result.ok
    assert [row["content"] for row in result.messages] == ["SYSTEM PROMPT", "q"]


def test_structured_content_survives_deep_copy():
    parts = [{"type": "text", "text": "hello"}]
    history = [
        message("user", "q", "u1"),
        message("assistant", parts, "a1"),
        message("user", "q2", "u2"),
    ]
    request = assemble(history)
    snapshot = snapshot_for(history, [], revision=1)

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.messages[2]["content"] == parts
    assert result.messages[2]["content"] is not parts
