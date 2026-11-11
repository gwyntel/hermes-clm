"""Multimodal and unknown content parts are refused, never flattened."""

from __future__ import annotations

import copy

from _clm_fakes import FakeOperation, assemble, coding_turns, message, snapshot_for

from hermes_clm.projection import project

IMAGE_PARTS = [
    {"type": "text", "text": "what is in this screenshot?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]


def _history_with_image_result(prefix: str = ""):
    """A turn whose tool result is an image, plus a later turn so it is not the newest."""
    tag = (lambda uid: f"{prefix}{uid}")
    return [
        message("user", "look at this", tag("u1")),
        message("assistant", "reading", tag("a1")),
        message("user", "second request", tag("u2")),
        {
            "role": "assistant",
            "content": "here",
            "message_uid": tag("a2"),
            "tool_calls": [{"id": f"{prefix}call_1", "type": "function", "function": {"name": "shot", "arguments": "{}"}}],
        },
        {"role": "tool", "content": copy.deepcopy(IMAGE_PARTS), "message_uid": tag("t1"), "tool_call_id": f"{prefix}call_1"},
        message("user", "thanks", tag("u3")),
        message("assistant", "anytime", tag("a3")),
    ]


def test_offload_of_a_multimodal_tool_result_is_refused():
    history = _history_with_image_result()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="offload", span_ids=("t1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert result.messages == ()
    # Multimodal content is protected at the row level, so the refusal arrives as
    # protected_row; non_text_content is the deeper defensive check.
    assert any(item["code"] in {"non_text_content", "protected_row"} for item in result.conflicts)


def test_offload_of_the_newest_multimodal_result_is_refused_as_protected():
    history = _history_with_image_result()[:5]
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="offload", span_ids=("t1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] in {"non_text_content", "protected_row"} for item in result.conflicts)


def test_rewrite_of_a_multimodal_assistant_row_is_refused():
    history = _history_with_image_result()
    history[1]["content"] = copy.deepcopy(IMAGE_PARTS)
    request = assemble(history)
    snapshot = snapshot_for(
        history, [FakeOperation(kind="rewrite", span_ids=("a1",), replacement="a picture")]
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] in {"non_text_content", "protected_row"} for item in result.conflicts)


def test_multimodal_rows_pass_through_byte_identically_when_untargeted():
    history = _history_with_image_result()
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("a1",))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    image_row = next(row for row in result.messages if row["role"] == "tool")
    assert image_row["content"] == IMAGE_PARTS
    assert image_row["content"] is not IMAGE_PARTS


def test_a_text_parts_list_is_still_not_rewritable():
    history = [
        message("user", "go", "u1"),
        message("assistant", [{"type": "text", "text": "structured"}], "a1"),
    ]
    request = assemble(history)
    snapshot = snapshot_for(
        history, [FakeOperation(kind="rewrite", span_ids=("a1",), replacement="flattened")]
    )

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert not result.ok
    assert any(item["code"] == "non_text_content" for item in result.conflicts)


def test_offloading_an_earlier_text_group_leaves_a_later_image_untouched():
    history = coding_turns() + _history_with_image_result("x")
    request = assemble(history)
    snapshot = snapshot_for(history, [FakeOperation(kind="offload", span_ids=("a1", "t1"))])

    result = project(request, conversation_messages=history, snapshot=snapshot)

    assert result.ok
    image_row = next(row for row in result.messages if isinstance(row.get("content"), list))
    assert image_row["content"] == IMAGE_PARTS
    assert "[clm.offloaded" in result.messages[3]["content"]
