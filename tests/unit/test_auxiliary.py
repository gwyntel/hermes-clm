"""Auxiliary compression: task slot, bounded timeout, proposal immutability, no auto-apply."""

from __future__ import annotations

import json
import sys
import types

import pytest
from clm_fakes import (
    FakeAuxResponse,
    FakeService,
    RecordingAuxClient,
    aux_json,
    store_proposal_digest,
)

from hermes_clm import auxiliary as auxiliary_module
from hermes_clm.auxiliary import (
    AUX_TASK,
    AuxCompressor,
    AuxLimits,
    bound_timeout,
    default_aux_call,
    extract_response_text,
)
from hermes_clm.tools import ToolHandlers

REWRITE_REPLY = {"rewrites": [{"span_id": "m-0001", "text": "short output"}]}


@pytest.fixture()
def service() -> FakeService:
    service = FakeService(revision=3)
    service.add_span("m-0001", "long tool output " * 20)
    return service


# --------------------------------------------------------------------------- #
# Host call contract
# --------------------------------------------------------------------------- #


def test_default_aux_call_uses_compression_task_and_no_route_overrides(monkeypatch):
    recorded: dict = {}

    def fake_call_llm(**kwargs):
        recorded.update(kwargs)
        return FakeAuxResponse("{}")

    module = types.ModuleType("agent.auxiliary_client")
    module.call_llm = fake_call_llm  # type: ignore[attr-defined]
    package = types.ModuleType("agent")
    package.auxiliary_client = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)

    out = default_aux_call(
        messages=[{"role": "user", "content": "hi"}], timeout=12.0, max_tokens=64
    )
    assert out is not None
    assert recorded["task"] == "compression"
    assert recorded["timeout"] == 12.0
    assert recorded["max_tokens"] == 64
    assert set(recorded) == {"task", "messages", "max_tokens", "timeout"}
    for forbidden in ("provider", "model", "base_url", "api_key", "fallback", "extra_body"):
        assert forbidden not in recorded


def test_compressor_calls_host_with_bounded_timeout(service, monkeypatch):
    assert AUX_TASK == "compression"
    captured: dict = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return aux_json(REWRITE_REPLY)

    module = types.ModuleType("agent.auxiliary_client")
    module.call_llm = fake_call_llm  # type: ignore[attr-defined]
    package = types.ModuleType("agent")
    package.auxiliary_client = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)

    compressor = AuxCompressor(timeout_seconds=10_000)
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["ok"] is True
    assert captured["task"] == "compression"
    assert captured["timeout"] == 120.0, "configured timeout must be clamped"
    assert "model" not in captured and "provider" not in captured


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 45.0),
        ("nope", 45.0),
        (True, 45.0),
        (0, 1.0),
        (-5, 1.0),
        (10, 10.0),
        (500, 120.0),
        ("12.5", 12.5),
        (float("nan"), 45.0),
    ],
)
def test_bound_timeout(value, expected):
    assert bound_timeout(value) == expected


# --------------------------------------------------------------------------- #
# Proposal path
# --------------------------------------------------------------------------- #


def test_successful_proposal_is_immutable_and_not_applied(service):
    client = RecordingAuxClient(aux_json(REWRITE_REPLY))
    compressor = AuxCompressor(client, timeout_seconds=15)
    payload = compressor.propose(service, span_ids=["m-0001"])

    assert payload["ok"] is True
    assert payload["staged"] is False and payload["applied"] is False
    assert service.names() == ["status", "retrieve", "create_proposal"]
    assert "stage" not in service.names() and "commit" not in service.names()

    proposal = payload["proposal"]
    assert proposal["origin"] == "aux"
    assert proposal["base_revision"] == 3
    assert proposal["immutable"] is True
    assert proposal["operations"] == [
        {"kind": "rewrite", "span_ids": ["m-0001"], "replacement": "short output"}
    ]
    assert proposal["proposal_sha256"] == store_proposal_digest(
        proposal["operations"], base_revision=3
    )
    assert "NOT applied" in payload["review"]
    assert payload["aux"]["task"] == "compression"
    assert payload["aux"]["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}


def test_returned_proposal_snapshot_cannot_mutate_the_stored_proposal(service):
    compressor = AuxCompressor(RecordingAuxClient(aux_json(REWRITE_REPLY)))
    payload = compressor.propose(service, span_ids=["m-0001"])
    stored = service.proposals[0]
    before = json.dumps(stored, sort_keys=True)

    payload["proposal"]["operations"][0]["replacement"] = "tampered"
    payload["proposal"]["base_revision"] = 99
    assert json.dumps(stored, sort_keys=True) == before
    assert payload["proposal"]["proposal_sha256"] == store_proposal_digest(
        stored["operations"], base_revision=3
    )


def test_second_proposal_gets_a_distinct_id(service):
    compressor = AuxCompressor(RecordingAuxClient(aux_json(REWRITE_REPLY)))
    first = compressor.propose(service, span_ids=["m-0001"])
    second = compressor.propose(service, span_ids=["m-0001"])
    assert first["proposal"]["proposal_id"] != second["proposal"]["proposal_id"]


def test_stale_base_revision_is_rejected_without_an_aux_call(service):
    client = RecordingAuxClient(aux_json(REWRITE_REPLY))
    compressor = AuxCompressor(client)
    payload = compressor.propose(service, span_ids=["m-0001"], base_revision=2)
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "stale_base_revision"
    assert client.calls == []
    assert "create_proposal" not in service.names()


def test_aux_disabled_makes_no_call(service):
    client = RecordingAuxClient(aux_json(REWRITE_REPLY))
    compressor = AuxCompressor(client, enabled=False)
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["errors"][0]["code"] == "aux_disabled"
    assert client.calls == []
    assert service.names() == []


# --------------------------------------------------------------------------- #
# Failure containment
# --------------------------------------------------------------------------- #


def test_none_response_is_an_error(service):
    compressor = AuxCompressor(lambda **_: None)
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "aux_empty_response"
    assert "create_proposal" not in service.names()


def test_empty_text_response_is_an_error(service):
    compressor = AuxCompressor(lambda **_: FakeAuxResponse("   "))
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["errors"][0]["code"] == "aux_empty_response"


def test_exception_is_contained_and_does_not_leak_route_detail(service):
    client = RecordingAuxClient(
        raises=RuntimeError("401 from https://gateway.example/v1 model=x key=sk-live-1")
    )
    compressor = AuxCompressor(client)
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "aux_error"
    assert "gateway.example" not in json.dumps(payload)
    assert "sk-live" not in json.dumps(payload)
    assert "create_proposal" not in service.names()


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("not json at all", "aux_invalid_output"),
        ('{"rewrites": "nope"}', "aux_invalid_output"),
        ('{"rewrites": [], "notes": "hi"}', "aux_invalid_output"),
        ('{"rewrites": [{"span_id": "m-9999", "text": "x"}]}', "aux_unknown_span"),
        ('{"rewrites": [{"span_id": "m-0001", "text": ""}]}', "aux_invalid_output"),
        ('{"rewrites": [{"span_id": "m-0001", "text": "a", "extra": 1}]}', "aux_invalid_output"),
        (
            '{"rewrites": [{"span_id": "m-0001", "text": "a"}, {"span_id": "m-0001", "text": "b"}]}',
            "aux_invalid_output",
        ),
    ],
)
def test_bad_aux_output_is_rejected(service, content, code):
    compressor = AuxCompressor(lambda **_: FakeAuxResponse(content))
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == code
    assert "create_proposal" not in service.names()


def test_oversized_aux_rewrite_is_rejected(service):
    reply = json.dumps({"rewrites": [{"span_id": "m-0001", "text": "x" * 200}]})
    compressor = AuxCompressor(
        lambda **_: FakeAuxResponse(reply), limits=AuxLimits(max_rewrite_bytes=100)
    )
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["errors"][0]["code"] == "aux_invalid_output"
    assert "create_proposal" not in service.names()


def test_no_reduction_is_reported(service):
    compressor = AuxCompressor(lambda **_: FakeAuxResponse('{"rewrites": []}'))
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["errors"][0]["code"] == "no_reduction"


def test_store_hash_is_returned_verbatim(service):
    """The store owns proposal identity; aux must not substitute its own digest."""

    service.proposal_result = {
        "proposal_id": "prop-x",
        "proposal_sha256": "f" * 64,
        "base_revision": 3,
    }
    compressor = AuxCompressor(RecordingAuxClient(aux_json(REWRITE_REPLY)))
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["ok"] is True
    assert payload["proposal"]["proposal_sha256"] == "f" * 64
    assert "stage" not in service.names() and "commit" not in service.names()


def test_store_proposal_without_hash_fails_closed(service):
    service.proposal_result = {"proposal_id": "prop-y", "base_revision": 3}
    compressor = AuxCompressor(RecordingAuxClient(aux_json(REWRITE_REPLY)))
    payload = compressor.propose(service, span_ids=["m-0001"])
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "proposal_hash_missing"
    assert "stage" not in service.names() and "commit" not in service.names()


def test_aux_compatibility_digest_matches_the_store():
    operations = [{"kind": "rewrite", "span_ids": ["m-0001"], "replacement": "short"}]
    assert auxiliary_module.canonical_proposal_sha256(
        operations, base_revision=4
    ) == auxiliary_module.canonical_sha256(
        {"base_revision": 4, "operations": operations, "origin": "aux"}
    )


# --------------------------------------------------------------------------- #
# Span selection rules
# --------------------------------------------------------------------------- #


def test_protected_span_is_refused(service):
    service.add_span("m-0002", "system text", protected=True)
    client = RecordingAuxClient(aux_json(REWRITE_REPLY))
    payload = AuxCompressor(client).propose(service, span_ids=["m-0002"])
    assert payload["errors"][0]["code"] == "protected_span"
    assert client.calls == []


def test_multimodal_span_is_never_flattened(service):
    service.spans["m-0003"] = {
        "ok": True,
        "span_id": "m-0003",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ],
        },
        "protected": False,
    }
    payload = AuxCompressor(lambda **_: None).propose(service, span_ids=["m-0003"])
    assert payload["errors"][0]["code"] == "unsupported_content"


def test_unknown_span_is_an_error(service):
    payload = AuxCompressor(lambda **_: None).propose(service, span_ids=["m-9999"])
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "span_not_found"


def test_too_many_spans_are_rejected_before_any_retrieve(service):
    compressor = AuxCompressor(lambda **_: None, limits=AuxLimits(max_spans=2))
    payload = compressor.propose(service, span_ids=["a", "b", "c"])
    assert payload["errors"][0]["code"] == "limit_exceeded"
    assert service.names() == []


# --------------------------------------------------------------------------- #
# Prompt and metrics
# --------------------------------------------------------------------------- #


def test_prompt_is_original_wording_and_carries_no_upstream_markers(service):
    client = RecordingAuxClient(aux_json(REWRITE_REPLY))
    compressor = AuxCompressor(client)
    compressor.propose(service, span_ids=["m-0001"], instruction="focus on the failing test")
    messages = client.calls[0]["messages"]
    blob = json.dumps(messages)
    for marker in ("[[CTX_TURN]]", "LIVE_CTX", "LIVE_CTX_MAIN", "context-language-models"):
        assert marker not in blob
    assert "focus on the failing test" in blob
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "SPAN m-0001" in messages[1]["content"]


def test_propose_refuses_a_span_over_the_prompt_cap_before_calling_aux(service):
    """A span the prompt cannot carry whole must never be rewritten from a prefix."""

    service.add_span("m-0004", "y" * 20_000)
    client = RecordingAuxClient(aux_json({"rewrites": [{"span_id": "m-0004", "text": "short"}]}))
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=100))

    result = compressor.propose(service, span_ids=["m-0004"])

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "span_too_large"
    assert "m-0004" in result["errors"][0]["message"]
    assert client.calls == [], "an over-cap span must be refused before any aux call"
    assert compressor.metrics()["calls"] == 0
    assert compressor.metrics()["last_error_code"] == "span_too_large"


def test_propose_refuses_the_batch_when_one_span_is_over_the_cap(service):
    service.add_span("m-0004", "y" * 20_000)
    client = RecordingAuxClient(aux_json(REWRITE_REPLY))
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=100))

    result = compressor.propose(service, span_ids=["m-0001", "m-0004"])

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "span_too_large"
    assert client.calls == []


def test_prompt_carries_a_span_at_the_cap_without_truncating_it(service):
    text = "z" * 100
    service.add_span("m-0005", text)
    client = RecordingAuxClient(aux_json({"rewrites": [{"span_id": "m-0005", "text": "short"}]}))
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=100))

    result = compressor.propose(service, span_ids=["m-0005"])

    assert result["ok"] is True
    content = client.calls[0]["messages"][1]["content"]
    assert text in content
    assert "truncated" not in content


def test_metrics_count_calls_and_failures(service):
    compressor = AuxCompressor(lambda **_: None, enabled=False)
    compressor.propose(service, span_ids=["m-0001"])
    metrics = compressor.metrics()
    assert metrics["enabled"] is False
    assert metrics["task"] == AUX_TASK
    assert metrics["calls"] == 0
    assert metrics["last_error_code"] == "aux_disabled"

    failing = AuxCompressor(RecordingAuxClient(raises=TimeoutError()))
    failing.propose(service, span_ids=["m-0001"])
    metrics = failing.metrics()
    assert metrics["calls"] == 1
    assert metrics["failures"] == 1
    assert metrics["last_error_code"] == "aux_error"
    assert metrics["timeout_seconds"] == 45.0


# --------------------------------------------------------------------------- #
# Tool integration
# --------------------------------------------------------------------------- #


def test_propose_tool_returns_a_proposal_and_never_stages(service):
    client = RecordingAuxClient(aux_json(REWRITE_REPLY))
    handlers = ToolHandlers(lambda _: service, aux=AuxCompressor(client))
    raw = handlers.propose_compression({"span_ids": ["m-0001"]})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["proposal"]["proposal_id"]
    assert "stage" not in service.names() and "commit" not in service.names()


def test_extract_response_text_accepts_dicts_and_strings():
    assert extract_response_text("plain") == "plain"
    assert extract_response_text({"choices": [{"message": {"content": "a"}}]}) == "a"
    assert extract_response_text({"choices": [{"text": "b"}]}) == "b"
    assert extract_response_text({"message": {"content": "c"}}) == "c"
    assert extract_response_text(None) is None


def test_auxiliary_module_imports_without_hermes():
    assert "hermes_clm.auxiliary" in sys.modules
