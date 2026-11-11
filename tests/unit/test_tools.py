"""Tool handler contract: strict JSON in/out, closed operation kinds, staging only."""

from __future__ import annotations

import json

import pytest
from clm_fakes import FakeService

from hermes_clm.tools import (
    AGENT_ORIGIN,
    STAGE_KINDS,
    TOOLSET,
    ToolHandlers,
    ToolLimits,
    build_tool_specs,
)

EXPECTED_TOOLS = {
    "clm_status",
    "clm_inspect",
    "clm_stage",
    "clm_commit",
    "clm_discard",
    "clm_revert",
    "clm_search",
    "clm_retrieve",
    "clm_pin",
    "clm_boundary",
    "clm_propose_compression",
    "clm_accept_proposal",
}

FORBIDDEN_KEYS = {"provider", "model", "base_url", "api_key", "api_key_env", "fallback", "route"}


@pytest.fixture()
def service() -> FakeService:
    service = FakeService()
    service.add_span("m-0001", "tool output that is long enough to rewrite")
    return service


@pytest.fixture()
def handlers(service: FakeService) -> ToolHandlers:
    return ToolHandlers(lambda session_id: service, limits=ToolLimits())


def load(raw: str) -> dict:
    assert isinstance(raw, str), "handlers must return JSON strings"
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def test_toolset_name():
    assert TOOLSET == "clm"


def test_specs_cover_the_documented_surface(handlers: ToolHandlers):
    specs = build_tool_specs(handlers)
    assert {spec.name for spec in specs} == EXPECTED_TOOLS
    assert len(specs) == len(EXPECTED_TOOLS)


def test_schemas_are_closed_objects(handlers: ToolHandlers):
    for spec in build_tool_specs(handlers):
        parameters = spec.schema["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False, spec.name
        assert spec.schema["name"] == spec.name
        assert spec.schema["description"].strip()


def test_schemas_expose_no_provider_or_model_configuration(handlers: ToolHandlers):
    for spec in build_tool_specs(handlers):
        blob = json.dumps(spec.schema)
        for forbidden in FORBIDDEN_KEYS:
            assert f'"{forbidden}"' not in blob, f"{spec.name} exposes {forbidden}"
        for prop in spec.schema["parameters"].get("properties", {}):
            assert prop not in {"session_id", "session", "path", "db_path", "store_path"}


def test_every_handler_returns_a_json_string(handlers: ToolHandlers):
    args_by_tool = {
        "clm_status": {},
        "clm_inspect": {},
        "clm_stage": {"operations": [{"kind": "drop", "span_ids": ["m-0001"]}], "expected_revision": 1},
        "clm_commit": {"txn_id": "t-1", "expected_revision": 1},
        "clm_discard": {"txn_id": "t-1"},
        "clm_revert": {"target_revision": 1, "expected_revision": 1},
        "clm_search": {"query": "output"},
        "clm_retrieve": {"span_id": "m-0001"},
        "clm_pin": {"span_ids": ["m-0001"], "expected_revision": 1},
        "clm_boundary": {"expected_revision": 1},
        "clm_propose_compression": {"span_ids": ["m-0001"]},
        "clm_accept_proposal": {
            "proposal_id": "prop-1",
            "proposal_sha256": "a" * 64,
            "base_revision": 1,
        },
    }
    by_name = {spec.name: spec for spec in build_tool_specs(handlers)}
    for name, args in args_by_tool.items():
        payload = load(by_name[name].handler(args))
        assert "ok" in payload, name


# --------------------------------------------------------------------------- #
# Strict arguments
# --------------------------------------------------------------------------- #


def test_unknown_field_is_rejected(handlers: ToolHandlers):
    payload = load(handlers.inspect({"limit": 5, "session_id": "other-session"}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "unknown_field"


def test_missing_required_field_is_rejected(handlers: ToolHandlers):
    payload = load(handlers.stage({"operations": [{"kind": "drop", "span_ids": ["m-0001"]}]}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "missing_field"


def test_non_object_arguments_are_rejected(handlers: ToolHandlers):
    payload = load(handlers.inspect(["not", "an", "object"]))  # type: ignore[arg-type]
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_arguments"


def test_negative_revision_is_rejected(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {"operations": [{"kind": "drop", "span_ids": ["m-0001"]}], "expected_revision": -1}
        )
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"


def test_bool_revision_is_not_an_int(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {"operations": [{"kind": "drop", "span_ids": ["m-0001"]}], "expected_revision": True}
        )
    )
    assert payload["ok"] is False


def test_limit_above_maximum_is_rejected(handlers: ToolHandlers):
    payload = load(handlers.inspect({"limit": 10_000}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "limit_exceeded"


# --------------------------------------------------------------------------- #
# Operation validation
# --------------------------------------------------------------------------- #


def test_stage_accepts_closed_kinds(handlers: ToolHandlers, service: FakeService):
    assert STAGE_KINDS == {"keep", "drop", "rewrite", "offload", "restore"}
    payload = load(
        handlers.stage(
            {
                "operations": [
                    {"kind": "drop", "span_ids": ["m-0001"]},
                    {"kind": "rewrite", "span_ids": ["m-0002"], "replacement": "shorter"},
                ],
                "expected_revision": 3,
                "txn_id": "txn-1",
            }
        )
    )
    assert payload["ok"] is True
    operations = service.args_for("stage")[0]
    assert operations == [
        {"kind": "drop", "span_ids": ["m-0001"]},
        {"kind": "rewrite", "span_ids": ["m-0002"], "replacement": "shorter"},
    ]
    assert service.kwargs_for("stage")["actor_origin"] == AGENT_ORIGIN


def test_pin_kind_must_use_the_pin_tool(handlers: ToolHandlers, service: FakeService):
    payload = load(
        handlers.stage({"operations": [{"kind": "pin", "span_ids": ["m-0001"]}], "expected_revision": 1})
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "wrong_tool"
    assert "clm_pin" in payload["hint"]
    assert "stage" not in service.names()


def test_boundary_kind_must_use_the_boundary_tool(handlers: ToolHandlers):
    payload = load(
        handlers.stage({"operations": [{"kind": "boundary", "span_ids": []}], "expected_revision": 1})
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "wrong_tool"


def test_unknown_kind_is_rejected(handlers: ToolHandlers):
    payload = load(
        handlers.stage({"operations": [{"kind": "summarize", "span_ids": ["m-0001"]}], "expected_revision": 1})
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"


def test_rewrite_requires_replacement(handlers: ToolHandlers):
    payload = load(
        handlers.stage({"operations": [{"kind": "rewrite", "span_ids": ["m-0001"]}], "expected_revision": 1})
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "missing_field"


def test_replacement_is_forbidden_for_drop(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {
                "operations": [{"kind": "drop", "span_ids": ["m-0001"], "replacement": "x"}],
                "expected_revision": 1,
            }
        )
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"


def test_unknown_operation_field_is_rejected(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {
                "operations": [
                    {"kind": "drop", "span_ids": ["m-0001"], "provider": "openrouter"}
                ],
                "expected_revision": 1,
            }
        )
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "unknown_field"


def test_overlapping_operations_are_rejected(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {
                "operations": [
                    {"kind": "drop", "span_ids": ["m-0001"]},
                    {"kind": "keep", "span_ids": ["m-0001"]},
                ],
                "expected_revision": 1,
            }
        )
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "conflicting_operations"


def test_oversized_replacement_is_rejected(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {
                "operations": [
                    {"kind": "rewrite", "span_ids": ["m-0001"], "replacement": "x" * 9000}
                ],
                "expected_revision": 1,
            }
        )
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "limit_exceeded"


def test_malformed_span_id_is_rejected(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {"operations": [{"kind": "drop", "span_ids": ["../../etc/passwd"]}], "expected_revision": 1}
        )
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"


def test_duplicate_span_ids_in_one_operation_are_rejected(handlers: ToolHandlers):
    payload = load(
        handlers.stage(
            {"operations": [{"kind": "drop", "span_ids": ["m-0001", "m-0001"]}], "expected_revision": 1}
        )
    )
    assert payload["ok"] is False


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


def test_stage_generates_a_txn_id_when_absent(handlers: ToolHandlers, service: FakeService):
    payload = load(
        handlers.stage({"operations": [{"kind": "drop", "span_ids": ["m-0001"]}], "expected_revision": 1})
    )
    assert payload["ok"] is True
    assert payload["txn_id"].startswith("tool-")
    assert payload["staged"] is True
    assert service.kwargs_for("stage")["txn_id"] == payload["txn_id"]


def test_stage_echoes_caller_txn_id(handlers: ToolHandlers, service: FakeService):
    payload = load(
        handlers.stage(
            {
                "operations": [{"kind": "drop", "span_ids": ["m-0001"]}],
                "expected_revision": 1,
                "txn_id": "agent-42",
            }
        )
    )
    assert payload["txn_id"] == "agent-42"
    assert service.kwargs_for("stage")["txn_id"] == "agent-42"


def test_malformed_txn_id_is_rejected(handlers: ToolHandlers):
    payload = load(handlers.commit({"txn_id": "bad id!", "expected_revision": 1}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"


def test_service_rejection_carries_a_plain_error_string(handlers: ToolHandlers, service: FakeService):
    service.stage_result = {
        "ok": False,
        "revision": 9,
        "errors": [{"code": "stale_revision", "message": "expected revision 9"}],
    }
    payload = load(
        handlers.stage({"operations": [{"kind": "drop", "span_ids": ["m-0001"]}], "expected_revision": 1})
    )
    assert payload["error"] == "expected revision 9"
    assert payload["errors"][0]["code"] == "stale_revision"


def test_stage_surfaces_a_stale_revision_rejection(handlers: ToolHandlers, service: FakeService):
    service.stage_result = {
        "ok": False,
        "revision": 9,
        "errors": [{"code": "stale_revision", "message": "expected 9"}],
        "receipts": [],
    }
    payload = load(
        handlers.stage({"operations": [{"kind": "drop", "span_ids": ["m-0001"]}], "expected_revision": 1})
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "stale_revision"
    assert "staged" not in payload
    assert "commit" not in service.names()


def test_commit_passes_through(handlers: ToolHandlers, service: FakeService):
    payload = load(handlers.commit({"txn_id": "txn-1", "expected_revision": 2}))
    assert payload["ok"] is True
    assert payload["revision"] == 3
    assert service.kwargs_for("commit") == {"txn_id": "txn-1", "expected_revision": 2}


def test_discard_returns_ok(handlers: ToolHandlers, service: FakeService):
    payload = load(handlers.discard({"txn_id": "txn-1"}))
    assert payload["ok"] is True
    assert payload["discarded"] is True
    assert service.kwargs_for("discard") == {"txn_id": "txn-1"}


def test_revert_records_a_new_revision_not_a_truncation(handlers: ToolHandlers, service: FakeService):
    payload = load(handlers.revert({"target_revision": 1, "expected_revision": 4}))
    assert payload["ok"] is True
    assert payload["revision"] == 5
    kwargs = service.kwargs_for("revert")
    assert kwargs["target_revision"] == 1
    assert kwargs["actor_origin"] == AGENT_ORIGIN
    assert "audit" in payload["note"]


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def test_inspect_passes_bounded_limit(handlers: ToolHandlers, service: FakeService):
    payload = load(handlers.inspect({"limit": 7, "cursor": "c1"}))
    assert payload["ok"] is True
    assert service.kwargs_for("inspect") == {"cursor": "c1", "limit": 7}


def test_search_requires_query(handlers: ToolHandlers):
    payload = load(handlers.search({}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "missing_field"


def test_search_passes_query_and_limit(handlers: ToolHandlers, service: FakeService):
    payload = load(handlers.search({"query": "traceback", "limit": 5}))
    assert payload["ok"] is True
    assert service.args_for("search") == ("traceback",)
    assert service.kwargs_for("search")["limit"] == 5


def test_retrieve_rejects_arbitrary_paths(handlers: ToolHandlers):
    payload = load(handlers.retrieve({"span_id": "../session.sqlite3"}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"


def test_retrieve_returns_archive_record(handlers: ToolHandlers):
    payload = load(handlers.retrieve({"span_id": "m-0001"}))
    assert payload["ok"] is True
    assert payload["span_id"] == "m-0001"


# --------------------------------------------------------------------------- #
# Pin / boundary / proposals
# --------------------------------------------------------------------------- #


def test_pin_stages_a_pin_operation_as_the_agent(handlers: ToolHandlers, service: FakeService):
    payload = load(handlers.pin({"span_ids": ["m-0001", "m-0002"], "expected_revision": 2}))
    assert payload["ok"] is True
    assert service.args_for("stage")[0] == [{"kind": "pin", "span_ids": ["m-0001", "m-0002"]}]
    assert service.kwargs_for("stage")["actor_origin"] == AGENT_ORIGIN
    assert "commit" not in service.names(), "pin only stages; the agent must commit explicitly"
    assert "unpin" not in service.names()
    assert "operator" in payload["note"]


def test_boundary_stages_a_marker_with_label(handlers: ToolHandlers, service: FakeService):
    payload = load(handlers.boundary({"label": "before refactor", "expected_revision": 2}))
    assert payload["ok"] is True
    assert service.args_for("stage")[0] == [
        {"kind": "boundary", "span_ids": [], "replacement": "before refactor"}
    ]


def test_boundary_label_is_bounded(handlers: ToolHandlers):
    payload = load(handlers.boundary({"label": "x" * 500, "expected_revision": 1}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "limit_exceeded"


def test_propose_without_aux_client_is_a_clear_error(handlers: ToolHandlers):
    payload = load(handlers.propose_compression({"span_ids": ["m-0001"]}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "aux_unavailable"


def test_accept_proposal_requires_a_full_hash(handlers: ToolHandlers):
    payload = load(
        handlers.accept_proposal({"proposal_id": "p1", "proposal_sha256": "abc", "base_revision": 1})
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"


def test_accept_proposal_stages_only():
    service = FakeService(revision=4)
    service.proposals.append(
        {"proposal_id": "prop-1", "proposal_sha256": "b" * 64, "base_revision": 4, "origin": "aux"}
    )
    handlers = ToolHandlers(lambda session_id: service)
    payload = load(
        handlers.accept_proposal(
            {
                "proposal_id": "prop-1",
                "proposal_sha256": "b" * 64,
                "base_revision": 4,
                "txn_id": "txn-9",
            }
        )
    )
    assert payload["ok"] is True
    assert payload["staged"] is True
    kwargs = service.kwargs_for("accept_proposal")
    assert kwargs == {
        "proposal_id": "prop-1",
        "proposal_sha256": "b" * 64,
        "base_revision": 4,
        "txn_id": "txn-9",
    }
    assert "commit" not in service.names()
    assert "stage" not in service.names()


# --------------------------------------------------------------------------- #
# Failure containment
# --------------------------------------------------------------------------- #


def test_no_session_is_an_error_not_a_cross_session_read():
    handlers = ToolHandlers(lambda session_id: None)
    payload = load(handlers.inspect({}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "no_session"


def test_session_comes_from_the_dispatch_kwarg_only():
    seen: list[str | None] = []

    def resolve(session_id: str | None):
        seen.append(session_id)
        return FakeService()

    handlers = ToolHandlers(resolve)
    load(handlers.inspect({}, session_id="sess-2"))
    assert seen == ["sess-2"]


def test_service_exception_becomes_a_bounded_error(handlers: ToolHandlers, service: FakeService):
    service.raise_on.add("inspect")
    payload = load(handlers.inspect({}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "internal_error"
    assert "/private/path" not in payload["error"]


def test_status_reports_aux_metrics_when_configured():
    from hermes_clm.auxiliary import AuxCompressor

    aux = AuxCompressor(call=lambda **_: None, enabled=False)
    handlers = ToolHandlers(lambda _: FakeService(), aux=aux)
    payload = load(handlers.status({}))
    assert payload["aux"]["task"] == "compression"
    assert payload["aux"]["enabled"] is False


def test_limits_from_config():
    limits = ToolLimits.from_config({"clm.max_rewrite_bytes": 1234, "clm.max_aux_spans": "5"})
    assert limits.max_rewrite_bytes == 1234
    assert limits.max_aux_spans == 5
    assert ToolLimits.from_config({"clm.max_rewrite_bytes": "nope"}) == ToolLimits()
