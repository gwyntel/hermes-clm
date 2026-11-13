"""End-to-end flow through the registered tool surface.

Uses plugin registration (fake engine, fake store) plus the engine tool seam
(``build_engine_handlers``) so the path is the same one Hermes takes: the engine
advertises ``get_tool_schemas()`` and dispatches named calls into a handler bound
to its own session service. Proves the auxiliary proposal cannot apply itself and
that a stale proposal is refused.
"""

from __future__ import annotations

import json

from clm_fakes import FakeService, RecordingAuxClient, aux_json
from test_plugin import FakeCtx, FakeEngine

from hermes_clm import tools as tools_module
from hermes_clm.auxiliary import AuxCompressor
from hermes_clm.plugin import register

REPLY = {"rewrites": [{"span_id": "m-0001", "text": "tests pass: 12 passed"}]}


def build(revision: int = 2):
    ctx = FakeCtx()
    engine = FakeEngine()
    # The fake stands in for the engine the host owns for this session; ``/clm``
    # resolves the engine bound to the calling session, not the handler's closure.
    engine.session_id = "sess-1"
    service = FakeService(revision=revision)
    service.add_span("m-0001", "verbose pytest output " * 40)
    engine.sessions["sess-1"] = service
    client = RecordingAuxClient(aux_json(REPLY))
    register(ctx, engine=engine, config={"clm.aux_enabled": True})
    handlers = tools_module.build_engine_handlers(
        engine, aux=AuxCompressor(client, timeout_seconds=20)
    )
    specs = tools_module.build_tool_specs(handlers)
    tools = {spec.name: spec.handler for spec in specs}
    return ctx, engine, service, client, tools


def call(tools, name, args=None, session_id="sess-1"):
    payload = tools[name](args or {}, session_id=session_id)
    assert isinstance(payload, str)
    return json.loads(payload)


def test_inspect_then_stage_then_commit_changes_the_next_revision():
    _, _, service, _, tools = build(revision=2)
    inspected = call(tools, "clm_inspect")
    assert inspected["revision"] == 2
    assert [span["span_id"] for span in inspected["spans"]] == ["m-0001"]

    staged = call(
        tools,
        "clm_stage",
        {
            "operations": [{"kind": "drop", "span_ids": ["m-0001"]}],
            "expected_revision": 2,
            "txn_id": "t1",
        },
    )
    assert staged["ok"] is True and staged["staged"] is True
    assert service.revision == 2, "staging must not move the revision"

    committed = call(tools, "clm_commit", {"txn_id": "t1", "expected_revision": 2})
    assert committed["ok"] is True
    assert committed["revision"] == 3
    assert service.revision == 3


def test_stage_forwards_the_callers_expected_revision_for_store_cas():
    # The store owns the compare-and-swap; the tool must not guess or clamp the
    # revision, or a stale caller would silently ride a newer head.
    _, _, service, _, tools = build(revision=5)
    call(
        tools,
        "clm_stage",
        {"operations": [{"kind": "drop", "span_ids": ["m-0001"]}], "expected_revision": 1},
        session_id="sess-1",
    )
    assert service.kwargs_for("stage")["expected_revision"] == 1
    assert service.revision == 5


def test_proposal_requires_accept_then_commit():
    _, _, service, client, tools = build(revision=2)
    proposal_payload = call(tools, "clm_propose_compression", {"span_ids": ["m-0001"]})
    assert proposal_payload["ok"] is True
    assert proposal_payload["staged"] is False
    assert service.names() == ["status", "retrieve", "create_proposal"]
    assert service.revision == 2
    assert client.calls and client.calls[0]["timeout"] == 20.0

    proposal = proposal_payload["proposal"]
    accepted = call(
        tools,
        "clm_accept_proposal",
        {
            "proposal_id": proposal["proposal_id"],
            "proposal_sha256": proposal["proposal_sha256"],
            "base_revision": proposal["base_revision"],
            "txn_id": "txn-aux",
        },
    )
    assert accepted["ok"] is True and accepted["staged"] is True
    assert service.revision == 2, "acceptance stages; it must not apply"
    assert "commit" not in service.names()

    committed = call(tools, "clm_commit", {"txn_id": "txn-aux", "expected_revision": 2})
    assert committed["ok"] is True and committed["revision"] == 3


def test_stale_proposal_is_refused_after_another_commit():
    _, _, service, _, tools = build(revision=2)
    proposal = call(tools, "clm_propose_compression", {"span_ids": ["m-0001"]})["proposal"]

    # Another actor commits, moving the head past the proposal's base revision.
    service.revision = 4
    stale = call(
        tools,
        "clm_accept_proposal",
        {
            "proposal_id": proposal["proposal_id"],
            "proposal_sha256": proposal["proposal_sha256"],
            "base_revision": proposal["base_revision"],
        },
    )
    assert stale["ok"] is False
    assert stale["errors"][0]["code"] == "stale_revision"
    assert "commit" not in service.names()


def test_wrong_proposal_hash_is_refused():
    _, _, service, _, tools = build(revision=2)
    proposal = call(tools, "clm_propose_compression", {"span_ids": ["m-0001"]})["proposal"]
    rejected = call(
        tools,
        "clm_accept_proposal",
        {
            "proposal_id": proposal["proposal_id"],
            "proposal_sha256": "0" * 64,
            "base_revision": proposal["base_revision"],
        },
    )
    assert rejected["ok"] is False
    assert "commit" not in service.names()


def test_agent_cannot_unpin_and_operator_command_can():
    ctx, _, service, _, tools = build(revision=3)
    assert not any(name.startswith("clm_unpin") for name in tools)

    command = ctx.commands[0]["handler"]
    text = command("unpin m-0001", session_id="sess-1")
    assert "unpinned m-0001" in text
    assert service.kwargs_for("unpin")["actor_origin"] == "operator"


def test_revert_creates_a_new_revision():
    _, _, service, _, tools = build(revision=6)
    payload = call(tools, "clm_revert", {"target_revision": 2, "expected_revision": 6})
    assert payload["ok"] is True
    assert payload["revision"] == 7
    assert service.revision == 7
