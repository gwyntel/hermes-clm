"""Identity, protection and UID/subsequence alignment."""

from __future__ import annotations

from _clm_fakes import assemble, assistant_call, message, tool_result
from hermes_clm import identity
from hermes_clm.identity import (
    absorbed_uids,
    align,
    group_covered,
    is_editable_content,
    is_multimodal_content,
    is_protected_message,
    message_uid,
    row_signature,
    tool_groups,
)


def test_message_uid_requires_a_non_empty_string():
    assert message_uid({"message_uid": "abc"}) == "abc"
    assert message_uid({"message_uid": ""}) is None
    assert message_uid({"message_uid": 7}) is None
    assert message_uid({}) is None


def test_absorbed_aliases_are_deduplicated_and_ordered():
    row = {"message_uid": "a", "_absorbed_message_uids": ["b", "c", "b", "", 3]}
    assert absorbed_uids(row) == ("b", "c")
    assert identity.row_span_ids(row) == ("a", "b", "c")


def test_multimodal_detection_is_structural_only():
    assert is_multimodal_content([{"type": "image_url", "image_url": {"url": "x"}}])
    assert is_multimodal_content([{"type": "text", "text": "hi"}, {"type": "video"}])
    assert not is_multimodal_content([{"type": "text", "text": "hi"}])
    assert not is_multimodal_content("plain")
    assert is_editable_content("plain")
    assert is_editable_content(None)
    assert not is_editable_content([{"type": "text", "text": "hi"}])


def test_protected_rows_are_role_pin_position_and_shape_based():
    assert is_protected_message(message("system", "s"))
    assert is_protected_message(message("developer", "d"))
    assert is_protected_message(message("user", "u"))
    assert is_protected_message(message("assistant", "a"), pinned=True)
    assert is_protected_message(message("tool", "t"), newest_tool_result=True)
    assert is_protected_message(message("assistant", [{"type": "image_url"}]))
    assert not is_protected_message(message("assistant", "plain"))
    assert not is_protected_message(message("tool", "t"))


def test_alignment_matches_text_rows_in_order():
    history = [message("user", "hi", "u1"), message("assistant", "hello", "a1")]
    request = assemble(history)
    alignment = align(request, history)
    assert alignment.uid_for(1) == "u1"
    assert alignment.uid_for(2) == "a1"
    assert alignment.uid_for(0) is None  # system prompt: host-owned, unmatched
    assert alignment.unmatched_request_indices == (0,)
    assert not alignment.ambiguous_uids


def test_alignment_matches_tool_structure_even_when_content_was_rewritten():
    history = [
        assistant_call("a1", "call_1", "call_2"),
        tool_result("t1", "call_1", "big output"),
        tool_result("t2", "call_2", "other"),
    ]
    request = assemble(history)
    request[2]["content"] = "REWRITTEN SIDECAR BYTES"
    alignment = align(request, history)
    assert [alignment.uid_for(i) for i in range(1, 4)] == ["a1", "t1", "t2"]


def test_alignment_is_a_subsequence_when_history_has_rows_the_request_dropped():
    history = [
        message("user", "u1", "u1"),
        message("assistant", "a1", "a1"),
        message("user", "u2", "u2"),
        message("assistant", "a2", "a2"),
    ]
    request = assemble([history[0], history[3]])
    alignment = align(request, history)
    assert alignment.uid_for(1) == "u1"
    assert alignment.uid_for(2) == "a2"


def test_alignment_requires_uid_on_the_history_row():
    history = [message("assistant", "same")]
    request = assemble([message("assistant", "same", "a1")])
    alignment = align(request, history)
    assert alignment.uid_for(1) is None


def test_alignment_never_matches_across_roles():
    history = [message("assistant", "text", "a1")]
    request = assemble([message("user", "text", "u1")])
    alignment = align(request, history)
    assert alignment.uid_for(1) is None


def test_equal_duplicates_in_order_are_unique_by_position():
    history = [
        message("user", "start", "u0"),
        message("assistant", "ok", "a1"),
        message("assistant", "ok", "a2"),
        message("user", "next", "u1"),
    ]
    request = assemble(history)
    alignment = align(request, history)
    # Both identical rows are present, so order alone pins each to one durable
    # row: monotonicity forbids swapping them.
    assert alignment.ambiguous_uids == frozenset()
    assert [alignment.uid_for(i) for i in range(1, 5)] == ["u0", "a1", "a2", "u1"]


def test_missing_duplicate_makes_the_surviving_row_ambiguous():
    history = [
        message("user", "start", "u0"),
        message("assistant", "ok", "a1"),
        message("assistant", "ok", "a2"),
        message("user", "next", "u1"),
    ]
    # The host dropped one of the two identical rows; bytes cannot say which.
    request = assemble([history[0], history[1], history[3]])
    alignment = align(request, history)
    assert alignment.ambiguous_uids == frozenset({"a1", "a2"})
    assert alignment.ambiguous_request_indices == frozenset({2})


def test_a_duplicate_outside_the_possible_window_is_not_ambiguous():
    history = [
        message("user", "start", "u0"),
        message("assistant", "ok", "a1"),
        message("user", "middle", "u1"),
        message("assistant", "ok", "a2"),
    ]
    request = assemble(history)
    alignment = align(request, history)
    assert alignment.ambiguous_uids == frozenset()
    assert alignment.present_uids == frozenset({"u0", "a1", "u1", "a2"})


def test_injected_user_content_keeps_its_row_unmatched_not_misaligned():
    history = [message("user", "question", "u1"), message("assistant", "answer", "a1")]
    request = assemble(history, injections={0: "\n\n[MEMORY: pinned facts]"})
    alignment = align(request, history)
    # The rewritten user row no longer matches its archived bytes, but the
    # assistant row must still align and the user row must never be guessed.
    assert alignment.uid_for(1) is None
    assert alignment.uid_for(2) == "a1"


def test_tool_groups_pair_calls_with_contiguous_results():
    history = [
        message("user", "go", "u1"),
        assistant_call("a1", "call_1"),
        tool_result("t1", "call_1", "one"),
        assistant_call("a2", "call_2"),
        tool_result("t2", "call_2", "two"),
        message("assistant", "done", "a3"),
    ]
    groups = tool_groups(history)
    assert groups.members_of("a1") == ("a1", "t1")
    assert groups.members_of("t1") == ("a1", "t1")
    assert groups.group_of("a3") is None
    assert sorted(groups.members) == ["group:a1", "group:a2"]


def test_tool_group_stops_at_a_result_for_another_call():
    history = [
        assistant_call("a1", "call_1"),
        tool_result("t1", "call_OTHER", "unrelated"),
    ]
    groups = tool_groups(history)
    assert groups.members_of("a1") == ("a1",)


def test_group_covered_requires_all_or_nothing():
    assert group_covered(("a", "b"), ["a", "b"])
    assert group_covered(("a", "b"), [])
    assert not group_covered(("a", "b"), ["a"])


def test_alignment_of_a_long_session_stays_fast():
    """Two thousand rows must align without a quadratic blow-up."""
    import time

    history = []
    for index in range(2000):
        history.append(
            message("user" if index % 2 == 0 else "assistant", f"row {index}", f"m{index}")
        )
    request = assemble(history)

    started = time.perf_counter()
    alignment = align(request, history)
    elapsed = time.perf_counter() - started

    assert len(alignment.pairs) == 2000
    assert alignment.uid_for(1) == "m0"
    assert alignment.uid_for(2000) == "m1999"
    assert elapsed < 2.0, f"alignment took {elapsed:.2f}s"


def test_row_signature_is_none_without_a_role():
    assert row_signature({"content": "x"}) is None
    assert row_signature({"role": "assistant", "content": "x"}) == (
        "text",
        "assistant",
        identity.content_sha256("x"),
    )
