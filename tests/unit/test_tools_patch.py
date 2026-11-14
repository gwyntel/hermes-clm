from __future__ import annotations

import json

import pytest
from clm_fakes import FakeService

from hermes_clm import models
from hermes_clm.errors import ValidationError
from hermes_clm.tools import (
    PATCH_KIND,
    STAGE_KINDS,
    STAGE_TOOL_KINDS,
    ToolHandlers,
    ToolLimits,
    build_tool_specs,
)

SPAN = "sp_" + "a" * 22
OTHER = "sp_" + "b" * 22


def handlers(service=None, **limits):
    service = service if service is not None else FakeService()
    return ToolHandlers(lambda _session_id: service, limits=ToolLimits(**limits))


def load(raw):
    return json.loads(raw)


def patch(**over):
    entry = {"kind": "patch", "span_ids": [SPAN], "old": "failing", "new": "passing"}
    entry.update(over)
    return entry


def stage(service, *entries, **extra):
    args = {"operations": list(entries), "expected_revision": 1}
    args.update(extra)
    return load(handlers(service).stage(args))


def refusing(service, entry, code):
    payload = stage(service, entry)
    assert payload["ok"] is False, payload
    assert payload["errors"][0]["code"] == code, payload
    # Resolution may read, but nothing may be staged, committed or written.
    assert not {"stage", "stage_commit", "commit"} & set(service.names()), payload
    return payload


def test_patch_is_a_tool_kind_only():
    assert PATCH_KIND == "patch"
    assert PATCH_KIND in STAGE_TOOL_KINDS
    assert PATCH_KIND not in STAGE_KINDS
    assert PATCH_KIND not in {str(kind.value) for kind in models.EditKind}
    with pytest.raises(ValidationError) as ctx:
        models.EditOperation.from_mapping({"kind": PATCH_KIND, "span_ids": [SPAN]})
    assert ctx.value.code == "invalid_kind"
    # The tool-only keys are invisible to the persisted model, too.
    with pytest.raises(ValidationError) as extra:
        models.EditOperation.from_mapping(patch())
    assert extra.value.code == "unknown_field"
    specs = {spec.name: spec for spec in build_tool_specs(handlers())}
    kinds = specs["clm_stage"].schema["parameters"]["properties"]["operations"]["items"][
        "properties"
    ]["kind"]["enum"]
    assert PATCH_KIND in kinds


def test_unique_replacement_is_staged_as_a_whole_span_rewrite():
    service = FakeService()
    service.add_span(SPAN, "STATUS: failing\nrest")
    payload = stage(service, patch())
    assert payload["ok"] is True, payload
    operations = service.args_for("stage")[0]
    assert operations == [
        {
            "kind": "rewrite",
            "span_ids": [SPAN],
            "replacement": "STATUS: passing\nrest",
            "provenance": {"via": PATCH_KIND},
        }
    ]
    expanded = "STATUS: passing\nrest"
    assert payload["patched"] == [
        {
            "span_id": SPAN,
            "kind": PATCH_KIND,
            "bytes": len(expanded.encode("utf-8")),
            "sha256": models.sha256_hex(expanded),
            "old_bytes": len("failing".encode("utf-8")),
            "new_bytes": len("passing".encode("utf-8")),
        }
    ]
    # The expanded span is withheld; only a digest receipt comes back.
    assert payload["staged_operations"] == [
        {
            "kind": "rewrite",
            "span_ids": [SPAN],
            "via": PATCH_KIND,
            "bytes": len(expanded.encode("utf-8")),
            "sha256": models.sha256_hex(expanded),
        }
    ]
    assert "STATUS: passing" not in json.dumps(payload)


def test_empty_new_deletes_the_match():
    service = FakeService()
    service.add_span(SPAN, "keep\nDROP ME\nkeep")
    assert stage(service, patch(old="\nDROP ME", new=""))["ok"] is True
    assert service.args_for("stage")[0][0]["replacement"] == "keep\nkeep"


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        (patch(old=""), "invalid_field"),
        (patch(old="   "), "invalid_field"),
        ({"kind": "patch", "span_ids": [SPAN], "new": "x"}, "missing_field"),
        ({"kind": "patch", "span_ids": [SPAN], "old": "x"}, "missing_field"),
        (patch(replacement="whole"), "invalid_field"),
        ({"kind": "rewrite", "span_ids": [SPAN], "replacement": "x", "old": "a"}, "invalid_field"),
        ({"kind": "drop", "span_ids": [SPAN], "new": "a"}, "invalid_field"),
        (patch(span_ids=[SPAN, OTHER]), "invalid_field"),
        (patch(extra=1), "unknown_field"),
    ],
)
def test_patch_shape_validation_writes_nothing(entry, code):
    service = FakeService()
    service.add_span(SPAN, "STATUS: failing")
    service.add_span(OTHER, "STATUS: failing")
    refusing(service, entry, code)


def test_resolution_refusals_never_reach_the_store():
    service = FakeService()
    service.add_span("sp_" + "c" * 22, "nothing here", protected=True)
    service.add_span("sp_" + "d" * 22, "STATUS: failing", pinned=True)
    service.add_span("sp_" + "e" * 22, "STATUS: failing", state="dropped")
    service.add_span("sp_" + "f" * 22, [{"type": "text", "text": "failing"}])
    service.add_span("sp_" + "1" * 22, "failing twice: failing")
    service.add_span(SPAN, "STATUS: failing")

    refusing(service, patch(span_ids=["sp_" + "9" * 22]), "span_not_found")
    refusing(service, patch(old="absent"), "patch_not_found")
    refusing(service, patch(span_ids=["sp_" + "1" * 22], old="failing"), "patch_ambiguous")
    refusing(service, patch(old="failing", new="failing"), "patch_noop")
    refusing(service, patch(span_ids=["sp_" + "c" * 22], old="nothing"), "protected_span")
    refusing(service, patch(span_ids=["sp_" + "d" * 22]), "span_pinned")
    refusing(service, patch(span_ids=["sp_" + "e" * 22]), "span_not_active")
    refusing(service, patch(span_ids=["sp_" + "f" * 22]), "patch_unsupported_content")


def test_oversized_expansion_is_refused():
    service = FakeService()
    service.add_span(SPAN, "STATUS: failing")
    payload = load(
        handlers(service, max_rewrite_bytes=8).stage(
            {"operations": [patch()], "expected_revision": 1}
        )
    )
    assert payload["errors"][0]["code"] == "limit_exceeded", payload
    assert service.names() == ["retrieve"], payload


def test_regex_metacharacters_are_literal():
    service = FakeService()
    service.add_span(SPAN, "cost is a.*b exactly")
    assert stage(service, patch(old="a.*b", new="c"))["ok"] is True
    assert service.args_for("stage")[0][0]["replacement"] == "cost is c exactly"


def test_committed_rewrite_text_wins_over_the_original_message():
    service = FakeService()
    service.add_span(SPAN, "STATUS: failing", rewrite_text="STATUS: fixed")
    assert stage(service, patch(old="fixed", new="passing"))["ok"] is True
    assert service.args_for("stage")[0][0]["replacement"] == "STATUS: passing"

    stale = FakeService()
    stale.add_span(SPAN, "STATUS: failing", rewrite_text="STATUS: fixed")
    payload = stage(stale, patch(old="failing", new="passing"))
    assert payload["errors"][0]["code"] == "patch_not_found", payload
    assert stale.names() == ["retrieve"], payload


def test_a_mixed_batch_preserves_order_and_keeps_one_span_per_edit():
    service = FakeService()
    service.add_span(SPAN, "STATUS: failing")
    service.add_span(OTHER, "other text")
    payload = stage(
        service,
        {"kind": "drop", "span_ids": [OTHER]},
        patch(),
        {"kind": "keep", "span_ids": ["sp_" + "7" * 22]},
    )
    assert payload["ok"] is True, payload
    operations = service.args_for("stage")[0]
    assert [op["kind"] for op in operations] == ["drop", "rewrite", "keep"]
    assert operations[1]["replacement"] == "STATUS: passing"


def test_duplicate_spans_still_conflict():
    service = FakeService()
    service.add_span(SPAN, "STATUS: failing")
    payload = stage(service, patch(), {"kind": "drop", "span_ids": [SPAN]})
    assert payload["errors"][0]["code"] == "conflicting_operations"
    assert service.names() == []


def test_commit_true_with_a_patch_refuses_before_retrieve_or_stage():
    service = FakeService()
    service.add_span(SPAN, "STATUS: failing")
    payload = stage(service, patch(), commit=True)
    assert payload["errors"][0]["code"] == "invalid_field", payload
    assert service.names() == [], payload
