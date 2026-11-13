"""Dual enablement against a *real* store: one tool surface, one writer.

A profile can enable this package twice: once as the selected context engine
(directory loader or the plugin manager's engine slot) and once as a general
plugin. Before the deferral rule, the general-plugin lane built its own
``CLMEngine`` and registered the twelve ``clm_*`` names; those names won the
lookup, and each handler bound an engine that could not open the session store
— SQLite's sibling ``flock`` makes exactly one writer — so every call answered
``no_session`` while the prompt told the model the tools were callable.

These tests do not fake the store: real ``CLMEngine`` instances open real
``ContextStore`` handles under a throwaway home, so the exclusive writer is the
thing under test. No model, provider or network call is involved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hermes_clm.engine import CLMEngine  # noqa: E402
from hermes_clm.guidance import SYSTEM_PROMPT_SECTION_ID  # noqa: E402
from hermes_clm.plugin import ENGINE_NAME, register  # noqa: E402
from hermes_clm.tools import TOOLSET  # noqa: E402

SESSION = "sess-dual-lane-0001"


class EngineLaneCtx:
    """Hermes' ``_EngineCollector``: engine + ``/clm``, no tools, no sections."""

    def __init__(self) -> None:
        self.engine = None
        self.commands: list[str] = []

    def register_context_engine(self, engine) -> None:
        self.engine = engine

    def register_command(self, name, handler, description="", args_hint="", **kwargs) -> None:
        self.commands.append(name)


class GeneralLaneCtx:
    """The general plugin lane: tools, ``/clm`` and prompt sections."""

    register_context_engine = None  # type: ignore[assignment]

    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.commands: list[str] = []
        self.sections: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_command(self, name, handler, description="", args_hint="", **kwargs) -> None:
        self.commands.append(name)

    def register_system_prompt_section(
        self, id, content, *, position="after_memory", max_chars=4000
    ):
        self.sections.append({"id": id, "content": content})
        return f"handle:{id}"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "hermes-home"
    h.mkdir(mode=0o700)
    return h


def _engine(home, session_id: str = SESSION) -> CLMEngine:
    return CLMEngine(
        config={"clm.hermes_home": str(home)},
        hermes_home=str(home),
        session_id=session_id,
    )


def _write_through_the_selected_engine(engine: CLMEngine) -> None:
    """Prove the store is writable: stage one boundary marker, then commit."""

    service = engine.service_for_session(SESSION)
    assert service is not None, "the selected engine must own the writer"
    revision = service.revision
    staged = service.stage(
        [
            {
                "kind": "boundary",
                "span_ids": [],
                "replacement": "dual-lane probe",
                "actor_origin": "system",
            }
        ],
        expected_revision=revision,
        txn_id="txn-dual-lane-probe",
        actor_origin="system",
    )
    assert staged["ok"] is True, staged
    committed = service.commit(txn_id="txn-dual-lane-probe", expected_revision=revision)
    assert committed["ok"] is True, committed


def test_dual_lane_defers_the_plugin_tools_to_the_selected_engine(home):
    """The selected engine owns the names; the plugin lane builds no engine."""

    selected = _engine(home)
    engine_ctx = EngineLaneCtx()
    register(engine_ctx, engine=selected, config={}, engine_selected=True)
    assert engine_ctx.engine is selected and engine_ctx.commands == ["clm"]

    built: list[object] = []

    def factory(config):
        built.append(config)
        return _engine(home)

    plugin_ctx = GeneralLaneCtx()
    runtime = register(
        plugin_ctx,
        config={"clm.hermes_home": str(home)},
        engine_factory=factory,
        engine_selected=True,
    )

    assert built == [], "no second CLMEngine (and so no second store) is constructed"
    assert plugin_ctx.tools == [], "no clm_* name is registered a second time"
    assert plugin_ctx.commands == [], "/clm stays with the selected engine"
    assert runtime.deferred_to_engine is True and runtime.engine is None
    assert [section["id"] for section in plugin_ctx.sections] == [SYSTEM_PROMPT_SECTION_ID]

    # The selected engine's surface is what the model calls, and it works.
    names = [schema["name"] for schema in selected.get_tool_schemas()]
    assert len(names) == 12 and len(set(names)) == 12, names
    assert "clm_status" in names and "clm_accept_proposal" in names
    _write_through_the_selected_engine(selected)
    payload = json.loads(
        selected.handle_tool_call("clm_status", {"detail": "full"}, session_id=SESSION)
    )
    assert payload["ok"] is True, payload
    assert payload["engine"] == ENGINE_NAME
    assert payload["store"] == "attached"


def test_a_plugin_lane_engine_would_have_answered_no_session(home):
    """The hazard the deferral removes: a second engine cannot serve the session.

    This is the pre-fix shape, kept as a live regression guard: two engines over
    one session directory, only the first holds ``store.lock``. The second
    swallows ``StoreBusyError`` on lazy attach, so its tools report
    ``no_session`` — exactly the dead-but-advertised surface.
    """

    first = _engine(home)
    assert first.service_for_session(SESSION) is not None

    second = _engine(home)
    assert second.service_for_session(SESSION) is None
    assert "StoreBusyError" in str(second.get_status()["last_error"]), (
        "the second writer must fail on the flock, not on something else"
    )

    payload = json.loads(second.handle_tool_call("clm_status", {}, session_id=SESSION))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "no_session"

    # And the first engine is unaffected: the deferral costs nothing.
    payload = json.loads(first.handle_tool_call("clm_status", {}, session_id=SESSION))
    assert payload["ok"] is True, payload


def test_plugins_only_fallback_still_registers_a_working_tool_surface(home):
    """No engine selected: this lane owns the tools, and they can open a store."""

    ctx = GeneralLaneCtx()
    runtime = register(ctx, config={"clm.hermes_home": str(home)}, engine_selected=False)

    assert len(ctx.tools) == 12
    assert {tool["toolset"] for tool in ctx.tools} == {TOOLSET}
    assert [command for command in ctx.commands] == ["clm"]
    assert runtime.deferred_to_engine is False

    status = next(tool for tool in ctx.tools if tool["name"] == "clm_status")
    payload = json.loads(status["handler"]({"detail": "full"}, session_id=SESSION))
    assert payload["ok"] is True, payload
    assert payload["store"] == "attached"
