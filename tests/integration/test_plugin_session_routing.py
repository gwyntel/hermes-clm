"""Which engine answers a tool call or ``/clm`` in a multiplexed host.

Two lanes can load this package in one process:

* the context-engine loader, whose engine the host wires into the agent — that
  engine owns the store and is the one ``/compact`` uses. The host registers a
  **prototype** and deep-copies it per agent (``_select_context_engine`` →
  ``clone_for_agent``), so an object the plugin kept a reference to is not an
  agent's engine;
* the general plugin system, which may build its *own* engine when the profile
  does not select this one. That lane is isolated: nothing else in the process
  serves those sessions.

The rules pinned here:

* tools and ``/clm`` resolve the engine **bound to the calling session**, never
  the registered prototype (a prototype that lazily attached would open a second
  writer for a session the resident engine already owns);
* no session id is a refusal, not "whichever engine this handler holds";
* a session no engine serves is a refusal, and nothing opens a store;
* the fallback lane serves only its own isolated instance and refuses other ids;
* the deferred lane (engine selected *and* plugin enabled) registers nothing.
"""

from __future__ import annotations

from typing import Callable, cast

import pytest

from hermes_clm.engine import CLMEngine
from hermes_clm.plugin import register

pytestmark = pytest.mark.integration

LIVE = "sess-live"


class _Ctx:
    """The host surface ``register`` may find: engine slot, tools, command, section."""

    def __init__(self, *, engine_lane: bool = True, tools: bool = True) -> None:
        self.engines: list[object] = []
        self.commands: list[dict] = []
        self.tools: dict[str, object] = {}
        self.sections: list[tuple[str, object]] = []
        if engine_lane:
            self.register_context_engine = self.engines.append
        if tools:
            self.register_tool = self._register_tool
        self.register_command = self._register_command
        self.register_system_prompt_section = self._register_section

    def _register_tool(self, *, name, toolset, schema, handler, description):
        self.tools[name] = handler

    def _register_command(self, name, handler, *, description="", args_hint=""):
        if any(entry["name"] == name for entry in self.commands):
            # The host's registry is process-wide and first registration wins;
            # a second agent loading the plugin gets no second entry.
            return
        self.commands.append({"name": name, "handler": handler})

    def _register_section(self, section_id, content, **kwargs):
        self.sections.append((section_id, content))
        return object()

    @property
    def handler(self):
        return self.commands[0]["handler"]


def _engine(home, session_id: str | None = None) -> CLMEngine:
    engine = CLMEngine(config={"clm.hermes_home": str(home)})
    if session_id is not None:
        engine.on_session_start(session_id, hermes_home=str(home))
    return engine


def _seed(home, session_id: str, text: str, *, spans: int = 1) -> CLMEngine:
    engine = _engine(home, session_id)
    engine.store.ingest(
        [
            {"role": "assistant", "content": f"{text} #{i}", "message_uid": f"uid-{text}-{i}"}
            for i in range(spans)
        ]
    )
    return engine


# --------------------------------------------------------------------------- #
# The prototype must stay inert
# --------------------------------------------------------------------------- #


def test_the_registered_prototype_never_answers_for_a_session(home):
    """The handler resolves the resident engine; the prototype stays unbound."""

    resident = _seed(home, LIVE, "live archive")
    prototype = _engine(home)
    ctx = _Ctx()
    runtime = register(
        ctx, engine=prototype, engine_selected=True, config={"clm.hermes_home": str(home)}
    )

    text = ctx.handler("status", session_id=LIVE)

    assert "revision" in text
    assert prototype.store is None, "the prototype opened a store for another engine's session"
    assert runtime.engine is prototype
    assert resident.store is not None


def test_the_prototype_cannot_steal_a_live_writer(home):
    """Even with the resident's store closed, a bare command touches no archive."""

    resident = _seed(home, LIVE, "writer owned archive")
    prototype = _engine(home)
    ctx = _Ctx()
    register(ctx, engine=prototype, engine_selected=True, config={"clm.hermes_home": str(home)})

    ctx.handler("status", session_id="sess-never-seen")

    assert prototype.store is None
    assert prototype.session_id is None
    assert resident.store.status()["ok"] is True, "the resident store was disturbed"


def test_two_agents_in_one_process_answer_for_their_own_session(home):
    """The failure this replaces: the second agent's ``/clm`` showed the first's store."""

    _seed(home, "sess-first", "first archive only")
    _seed(home, "sess-second", "second archive only", spans=3)
    ctx = _Ctx()
    register(ctx, engine=_engine(home), engine_selected=True, config={"clm.hermes_home": str(home)})
    # A second agent loads the plugin; the host keeps the first registry entry.

    text_first = ctx.handler("status", session_id="sess-first")
    text_second = ctx.handler("status", session_id="sess-second")

    assert "spans: 1 active" in text_first
    assert "spans: 3 active" in text_second
    # Each answer describes its own archive: never the other agent's.
    assert "spans: 3 active" not in text_first
    assert "spans: 1 active" not in text_second


def test_engine_lane_does_not_duplicate_the_tool_surface(home):
    """Tools come from the selected engine's own schemas, not from this lane."""

    ctx = _Ctx(engine_lane=True, tools=True)
    engine = _engine(home, LIVE)
    register(ctx, engine=engine, engine_selected=True, config={"clm.hermes_home": str(home)})

    assert ctx.engines == [engine]
    schemas = engine.get_tool_schemas()
    names = [spec["name"] for spec in schemas]
    assert len(names) >= 12
    assert len(set(names)) == len(names), "the engine itself must not duplicate a tool name"
    # The engine lane registered no tools of its own: no name can resolve to a
    # handler bound to a different instance.
    assert ctx.tools == {}


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_command_with_no_session_id_refuses(home):
    resident = _seed(home, LIVE, "live archive")
    ctx = _Ctx()
    register(
        ctx, engine=_engine(home, LIVE), engine_selected=True, config={"clm.hermes_home": str(home)}
    )

    text = ctx.handler("status", session_id=None)

    assert "no session id" in text
    assert str(resident.store.status()["revision"]) not in text


def test_a_command_for_an_unserved_session_refuses_and_opens_nothing(home):
    prototype = _engine(home)
    ctx = _Ctx()
    register(ctx, engine=prototype, engine_selected=True, config={"clm.hermes_home": str(home)})

    text = ctx.handler("status", session_id="sess-orphan")

    assert "no hermes-clm engine is bound" in text
    assert prototype.store is None


def test_help_works_without_a_session(home):
    ctx = _Ctx()
    register(ctx, engine=_engine(home), engine_selected=True, config={"clm.hermes_home": str(home)})
    assert "/clm unpin" in ctx.handler("", session_id=None)


# --------------------------------------------------------------------------- #
# The general-plugin fallback lane
# --------------------------------------------------------------------------- #


def test_fallback_lane_serves_its_own_isolated_engine(home):
    ctx = _Ctx(engine_lane=False)
    engine = _engine(home, "sess-plugin-only")
    runtime = register(
        ctx, engine=engine, engine_selected=False, config={"clm.hermes_home": str(home)}
    )

    assert runtime.lane == "fallback"
    assert ctx.engines == []
    assert ctx.tools, "the fallback lane is the tool surface; it must register its tools"
    text = ctx.handler("status", session_id="sess-plugin-only")
    assert "revision" in text
    assert engine.store is not None


def test_fallback_lane_refuses_a_session_it_does_not_serve(home):
    """One isolated runtime, one session: a second id never sees the first archive."""

    ctx = _Ctx(engine_lane=False)
    first = _seed(home, "sess-plugin-one", "plugin archive")
    runtime = register(
        ctx, engine=first, engine_selected=False, config={"clm.hermes_home": str(home)}
    )

    assert runtime.service("sess-plugin-one") is not None
    assert runtime.service("sess-plugin-two") is None

    text = ctx.handler("status", session_id="sess-plugin-two")
    assert "no attached store" in text or "no hermes-clm engine is bound" in text
    assert str(first.store.status()["revision"]) not in text


def test_fallback_lane_tools_scope_to_the_bound_session(home):
    ctx = _Ctx(engine_lane=False)
    engine = _seed(home, "sess-plugin-tools", "tool archive")
    register(ctx, engine=engine, engine_selected=False, config={"clm.hermes_home": str(home)})

    status = cast(Callable[..., str], ctx.tools["clm_status"])
    assert "revision" in status(session_id="sess-plugin-tools")
    # A subagent that inherits the tool surface with another id gets a refusal,
    # not the parent's archive.
    other = status(session_id="sess-subagent")
    assert "revision" not in other or "no " in other.lower()


# --------------------------------------------------------------------------- #
# The deferred lane
# --------------------------------------------------------------------------- #


def test_a_deferred_lane_registers_nothing_but_guidance(home):
    """Engine selected *and* plugin enabled: the selected engine owns everything."""

    ctx = _Ctx(engine_lane=False)
    runtime = register(ctx, engine_selected=True, config={"clm.hermes_home": str(home)})

    assert runtime.deferred_to_engine is True
    assert runtime.engine is None
    assert ctx.engines == []
    assert ctx.tools == {}, "a duplicate tool name would shadow the engine's handler"
    assert ctx.commands == [], "a second /clm cannot be registered, so do not try"
    assert ctx.sections, "the guidance section still explains the tools"
