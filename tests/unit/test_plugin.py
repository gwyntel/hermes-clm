"""Plugin registration: engine + tools + operator command, strict schemas, session scoping."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import pytest
from clm_fakes import FakeService

from hermes_clm import plugin as plugin_module
from hermes_clm.auxiliary import AuxCompressor
from hermes_clm.guidance import (
    SYSTEM_PROMPT_SECTION_ID,
    SYSTEM_PROMPT_SECTION_MAX_CHARS,
    SYSTEM_PROMPT_SECTION_POSITION,
    SYSTEM_PROMPT_SECTION_TEXT,
)
from hermes_clm.plugin import (
    ENGINE_NAME,
    PLUGIN_NAME,
    PluginRuntime,
    build_aux,
    register,
)
from hermes_clm.tools import TOOLSET

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_YAML = REPO_ROOT / "plugin.yaml"

FORBIDDEN_SCHEMA_KEYS = {
    "provider",
    "model",
    "base_url",
    "api_key",
    "fallback",
    "route",
    "api_mode",
}


class FakeEngine:
    """Minimal engine stand-in: one store per session, keyed like the real engine."""

    name = ENGINE_NAME
    #: Set by tests that stand this fake in for the engine bound to a session.
    session_id: str | None = None

    def __init__(self) -> None:
        self.sessions: dict[str | None, FakeService] = {}
        self.requested: list[str | None] = []

    def service_for_session(self, session_id: str | None = None) -> FakeService | None:
        self.requested.append(session_id)
        return self.sessions.get(session_id)


class FakeCtx:
    """Context-engine lane: Hermes' ``_EngineCollector`` has no ``register_tool``."""

    def __init__(self) -> None:
        self.engines: list = []
        self.commands: list[dict] = []

    def register_context_engine(self, engine) -> None:
        self.engines.append(engine)

    def register_command(self, name, handler, description="", args_hint="", **kwargs) -> None:
        self.commands.append(
            {"name": name, "handler": handler, "description": description, "args_hint": args_hint}
        )


class FallbackCtx(FakeCtx):
    """General-plugin lane: tools exist, no context-engine registration."""

    #: Hermes' general plugin context has no engine registration; mirror that.
    register_context_engine = None  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.tools: list[dict] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


class GeneralLaneCtx(FallbackCtx):
    """The full general-plugin surface: tools, commands and prompt sections.

    This is the lane a ``plugins.enabled`` entry gets on a host build that also
    has the system-prompt-section API (no engine registration here, which is
    what makes the fallback lane own the tools).
    """

    def __init__(self) -> None:
        super().__init__()
        self.sections: list[dict] = []

    def register_system_prompt_section(
        self, id, content, *, position="after_memory", max_chars=4000
    ):
        self.sections.append(
            {"id": id, "content": content, "position": position, "max_chars": max_chars}
        )
        return f"handle:{id}"


def registered_ctx(**kwargs):
    ctx = FakeCtx()
    engine = kwargs.pop("engine", None) or FakeEngine()
    # Engine-lane tests pin the section gate explicitly so they never read the
    # operator's live profile config.
    kwargs.setdefault("engine_selected", False)
    runtime = register(ctx, engine=engine, config=kwargs.pop("config", {}), **kwargs)
    return ctx, runtime, engine


def fallback_ctx(**kwargs):
    ctx = FallbackCtx()
    engine = kwargs.pop("engine", None) or FakeEngine()
    # ``False`` is the plugins-only fallback: no engine is selected, so this
    # lane owns the tools. Deferral under selection is tested explicitly.
    kwargs.setdefault("engine_selected", False)
    runtime = register(ctx, engine=engine, config=kwargs.pop("config", {}), **kwargs)
    return ctx, runtime, engine


def load(raw):
    assert isinstance(raw, str)
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# Registration surface
# --------------------------------------------------------------------------- #


def test_plugin_identity_matches_manifest():
    assert PLUGIN_NAME == "hermes-clm"
    assert ENGINE_NAME == "hermes-clm", "context.engine must name the registered engine"
    text = PLUGIN_YAML.read_text(encoding="utf-8")
    assert re.search(r"^name:\s*hermes-clm$", text, re.MULTILINE)
    assert re.search(r"^author:\s*\S+", text, re.MULTILINE)
    assert re.search(r"^license:\s*LicenseRef-MutuaL-1\.2$", text, re.MULTILINE)
    assert "manifest_version: 2" in text


def test_engine_lane_registers_engine_and_command_only():
    ctx, runtime, engine = registered_ctx()
    assert ctx.engines == [engine]
    assert not hasattr(ctx, "tools"), "engine tools come from get_tool_schemas(), not register_tool"
    assert [command["name"] for command in ctx.commands] == ["clm"]
    assert runtime.engine is engine


def test_fallback_lane_registers_the_same_tools_without_an_engine():
    ctx, runtime, engine = fallback_ctx()
    assert ctx.engines == []
    assert len(ctx.tools) == 12
    assert all(tool["toolset"] == TOOLSET for tool in ctx.tools)
    assert {tool["name"] for tool in ctx.tools} == set(_clm_tool_names())
    assert [command["name"] for command in ctx.commands] == ["clm"]
    assert runtime.engine is engine


# --------------------------------------------------------------------------- #
# Dual enablement: plugin enabled *and* this engine selected
# --------------------------------------------------------------------------- #


def test_dual_enablement_defers_tools_command_and_engine_construction():
    """The selected engine owns the names and the writer; this lane defers.

    A general-plugin lane that registered the twelve tools would win the tool
    lookup while its handlers bound a second ``CLMEngine`` whose store cannot
    open (the selected engine holds the exclusive writer), so every call would
    answer ``no_session``. It registers the usage section and nothing else.
    """

    ctx = GeneralLaneCtx()
    built: list[dict] = []

    def factory(config):
        built.append(dict(config))
        return FakeEngine()

    runtime = register(ctx, config={}, engine_factory=factory, engine_selected=True)

    assert built == [], "a second engine instance must not be constructed"
    assert ctx.tools == [], "the selected engine injects these names itself"
    assert ctx.commands == [], "/clm belongs to the selected engine's registration"
    assert runtime.engine is None
    assert runtime.deferred_to_engine is True
    assert runtime.handlers is not None, "the runtime object stays inert, not absent"


def test_dual_enablement_still_registers_the_usage_section(monkeypatch):
    """Tools remain callable (from the engine), so guidance must still ship."""

    monkeypatch.setattr(
        plugin_module,
        "_profile_home",
        lambda profile: None if profile is None else "/profiles/" + profile,
    )
    monkeypatch.setattr(
        plugin_module,
        "_read_profile_config",
        lambda home=None: {"context": {"engine": ENGINE_NAME}},
    )

    ctx = GeneralLaneCtx()
    runtime = register(ctx, config={}, engine_selected=True)

    assert [section["id"] for section in ctx.sections] == [SYSTEM_PROMPT_SECTION_ID]
    section = ctx.sections[0]
    content = section["content"]
    rendered = content({"profile_name": "clmtest"}) if callable(content) else content
    assert rendered == SYSTEM_PROMPT_SECTION_TEXT
    assert section["position"] == SYSTEM_PROMPT_SECTION_POSITION
    assert section["max_chars"] == SYSTEM_PROMPT_SECTION_MAX_CHARS
    assert runtime.prompt_section == f"handle:{SYSTEM_PROMPT_SECTION_ID}"


def test_no_tool_name_the_engine_owns_is_registered_in_the_dual_lane():
    ctx = GeneralLaneCtx()
    register(ctx, config={}, engine_selected=True)
    registered = {tool["name"] for tool in ctx.tools}
    assert registered == set(), registered & set(_clm_tool_names())


def test_dual_enablement_reads_the_selection_from_the_profile(monkeypatch):
    """Production passes only ``ctx``; the gate must read ``context.engine``."""

    monkeypatch.setattr(
        plugin_module,
        "_read_profile_config",
        lambda home=None: {"context": {"engine": ENGINE_NAME}},
    )
    ctx = GeneralLaneCtx()
    runtime = register(ctx, config={})
    assert runtime.deferred_to_engine is True
    assert ctx.tools == [] and ctx.commands == []
    assert len(ctx.sections) == 1


def test_dual_enablement_logs_why_the_tools_are_absent(caplog):
    """Silence would look like a broken install; the deferral is an INFO fact."""

    caplog.set_level(logging.INFO, logger="hermes_clm.plugin")
    register(GeneralLaneCtx(), config={}, engine_selected=True)
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "hermes_clm.plugin" and record.levelno == logging.INFO
    ]
    assert len(messages) == 1, messages
    assert ENGINE_NAME in messages[0]
    assert "skipped" in messages[0]
    assert "section" in messages[0]


def test_plugins_only_fallback_keeps_its_tools_and_command_when_not_selected():
    """The compatibility lane: no engine selection, so this plugin owns it all."""

    ctx = GeneralLaneCtx()
    runtime = register(ctx, config={}, engine_selected=False)

    assert len(ctx.tools) == 12
    assert {tool["name"] for tool in ctx.tools} == set(_clm_tool_names())
    assert [command["name"] for command in ctx.commands] == ["clm"]
    assert len(ctx.sections) == 1
    assert runtime.deferred_to_engine is False
    assert runtime.engine is not None


def test_selecting_another_engine_leaves_the_fallback_lane_intact():
    ctx = GeneralLaneCtx()
    runtime = register(ctx, config={}, engine_selected=False)
    assert len(ctx.tools) == 12 and runtime.deferred_to_engine is False


def test_engine_lane_is_unaffected_by_the_deferral():
    """The lane that hands over the engine still registers it and ``/clm``."""

    class StrictEngineCtx(FakeCtx):
        def register_tool(self, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("engine lane must not register a tool")

    ctx = StrictEngineCtx()
    engine = FakeEngine()
    runtime = register(ctx, engine=engine, config={}, engine_selected=True)
    assert ctx.engines == [engine]
    assert [command["name"] for command in ctx.commands] == ["clm"]
    assert runtime.deferred_to_engine is False and runtime.engine is engine


def _clm_tool_names() -> set[str]:
    """The CLM tool surface, sourced from the engine rather than the manifest."""

    from hermes_clm import tools as tools_module
    from hermes_clm.plugin import build_engine

    return {schema["name"] for schema in tools_module.get_tool_schemas(build_engine({}))}


def test_manifest_does_not_declare_engine_tools_as_provided_by_register():
    """``provides_tools`` is a plugin-lane contract; engine tools are not registered there.

    Hermes documents engine tools as exposed via ``get_tool_schemas()`` with "no
    registry registration needed", and ``hermes plugins doctor`` diffs
    ``provides_tools`` against ``register_tool`` calls only — declaring the engine
    surface here produces twelve structurally guaranteed false warnings.
    """

    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(PLUGIN_YAML.read_text(encoding="utf-8"))
    assert "provides_tools" not in data
    assert all(key.startswith("clm.") for key in data["config_keys"])
    assert data["kind"] == "standalone"


def test_engine_still_advertises_the_full_tool_surface():
    ctx, _, _ = fallback_ctx()
    assert len(ctx.tools) == 12
    assert {tool["name"] for tool in ctx.tools} == _clm_tool_names()


def test_every_registered_tool_has_a_closed_strict_schema():
    ctx, _, _ = fallback_ctx()
    for tool in ctx.tools:
        schema = tool["schema"]
        assert schema["name"] == tool["name"]
        assert schema["parameters"]["additionalProperties"] is False
        blob = json.dumps(schema)
        for forbidden in FORBIDDEN_SCHEMA_KEYS:
            assert f'"{forbidden}"' not in blob, f"{tool['name']} exposes {forbidden}"


def test_no_provider_configuration_is_ever_registered():
    ctx, _, _ = fallback_ctx(config={"clm.aux_enabled": True})
    blob = json.dumps([tool["schema"] for tool in ctx.tools])
    for forbidden in ("openrouter", "anthropic", "openai", "plexus", "gpt-", "claude-"):
        assert forbidden not in blob.lower()


def test_engine_collector_without_register_tool_is_supported():
    """Hermes' ``_EngineCollector`` has no ``register_tool``; registration must not raise."""

    class Collector:
        def __init__(self):
            self.engine = None
            self.commands = []

        def register_context_engine(self, engine):
            self.engine = engine

        def register_command(self, name, handler, description="", args_hint=""):
            self.commands.append(name)

    engine = FakeEngine()
    ctx = Collector()
    runtime = register(ctx, engine=engine, config={})
    assert ctx.engine is engine and ctx.commands == ["clm"]
    assert runtime.handlers is not None


def test_engine_lane_never_calls_register_tool_so_names_cannot_duplicate():
    class StrictCtx(FakeCtx):
        def register_tool(self, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError(f"duplicate tool registration attempted: {kwargs.get('name')}")

    ctx = StrictCtx()
    register(ctx, engine=FakeEngine(), config={})
    register(ctx, engine=FakeEngine(), config={})  # a second load must stay harmless
    assert ctx.engines and len(ctx.commands) == 2


def test_engine_lane_tools_come_from_the_engine_tool_schemas():
    from hermes_clm import tools as tools_module

    class EngineWithService(FakeEngine):
        def tool_service(self):
            return self.sessions.get(self._session)

        _session = "sess-a"

    engine = EngineWithService()
    engine.sessions["sess-a"] = FakeService(revision=3)
    schemas = tools_module.get_tool_schemas(engine)
    assert {schema["name"] for schema in schemas} == set(_clm_tool_names())

    payload = load(tools_module.handle_tool_call(engine, "clm_status", {}, messages=[]))
    assert payload["ok"] is True
    unknown = load(tools_module.handle_tool_call(engine, "clm_nope", {}))
    assert unknown["ok"] is False and unknown["errors"][0]["code"] == "unknown_tool"


# --------------------------------------------------------------------------- #
# Session scoping
# --------------------------------------------------------------------------- #


def test_tools_resolve_the_calling_session_service():
    ctx, _, engine = fallback_ctx()
    engine.sessions["sess-a"] = FakeService(revision=1)
    engine.sessions["sess-b"] = FakeService(revision=2)
    inspect_tool = next(tool for tool in ctx.tools if tool["name"] == "clm_inspect")

    payload_a = load(inspect_tool["handler"]({}, session_id="sess-a"))
    payload_b = load(inspect_tool["handler"]({}, session_id="sess-b"))
    assert payload_a["ok"] is True and payload_b["ok"] is True
    assert engine.requested == ["sess-a", "sess-b"]


def test_missing_session_yields_no_session_error_not_another_store():
    ctx, _, engine = fallback_ctx()
    engine.sessions["sess-a"] = FakeService()
    status_tool = next(tool for tool in ctx.tools if tool["name"] == "clm_status")
    payload = load(status_tool["handler"]({}, session_id="sess-unknown"))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "no_session"


def test_engine_without_service_seam_refuses_instead_of_guessing():
    class Seamless:
        name = ENGINE_NAME

    runtime = PluginRuntime(Seamless(), config={})
    assert runtime.service("sess-a") is None


def test_runtime_accepts_a_per_engine_tool_service():
    class BoundEngine:
        name = ENGINE_NAME

        def __init__(self):
            self.service = FakeService()

        def tool_service(self):
            return self.service

    engine = BoundEngine()
    runtime = PluginRuntime(engine, config={})
    assert runtime.service("anything") is engine.service


# --------------------------------------------------------------------------- #
# Operator command
# --------------------------------------------------------------------------- #


def test_command_handler_reports_status_text():
    ctx, _, engine = registered_ctx()
    # The engine the host owns for this session is the one the command resolves.
    engine.session_id = "sess-a"
    engine.sessions["sess-a"] = FakeService(revision=5)
    command = ctx.commands[0]["handler"]
    text = command("status", session_id="sess-a")
    assert "CLM" in text and "revision 5" in text


def test_command_without_a_session_refuses_instead_of_using_a_bound_engine():
    """No session id means no engine: never "whichever engine this handler holds".

    The handler is one process-wide registry entry, and the runtime it closed over
    may serve an entirely different session (or be the host's unbound prototype).
    Answering from it would show — and edit — another conversation's archive.
    """

    ctx, runtime, engine = registered_ctx()
    engine.sessions["sess-bound"] = FakeService(revision=9)

    text = ctx.commands[0]["handler"]("status", session_id=None)

    assert "no session id" in text
    assert "9" not in text
    assert engine.requested == [], "the bound engine must not be consulted without an id"


def test_command_help_needs_no_session():
    ctx, _, _ = registered_ctx()
    text = ctx.commands[0]["handler"]("", session_id=None)
    assert "/clm unpin" in text and "/clm edit" in text


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_aux_defaults_are_enabled_with_a_bounded_timeout():
    aux = build_aux({})
    assert aux.enabled is True
    assert aux.timeout_seconds == 45.0
    assert aux.metrics()["task"] == "compression"


def test_aux_config_is_read_from_clm_keys_only():
    aux = build_aux(
        {
            "clm.aux_enabled": False,
            "clm.aux_timeout_seconds": 5000,
            "clm.max_aux_spans": 3,
            "clm.max_rewrite_bytes": 999,
        }
    )
    assert aux.enabled is False
    assert aux.timeout_seconds == 120.0
    assert aux.limits.max_spans == 3
    assert aux.limits.max_rewrite_bytes == 999


def test_plugin_register_passes_an_injected_aux_compressor():
    aux = AuxCompressor(lambda **_: None, enabled=False)
    ctx, runtime, _ = fallback_ctx(aux=aux)
    status_tool = next(tool for tool in ctx.tools if tool["name"] == "clm_status")
    runtime.engine.sessions[None] = FakeService()
    payload = load(status_tool["handler"]({"detail": "full"}, session_id=None))
    assert payload["aux"]["enabled"] is False


def test_register_uses_engine_factory_when_no_engine_is_given():
    seen: list[dict] = []

    def factory(config):
        seen.append(dict(config))
        return FakeEngine()

    ctx = FakeCtx()
    register(ctx, config={"clm.aux_enabled": True}, engine_factory=factory)
    assert seen == [{"clm.aux_enabled": True}]
    assert isinstance(ctx.engines[0], FakeEngine)


def test_build_engine_applies_clm_config_through_the_engine(tmp_path):
    """A non-empty clm.* mapping must yield a usable engine, not a raw-dict config."""

    engine = plugin_module.build_engine(
        {"clm.aux_enabled": False, "clm.reserve_tokens": 4096, "clm.hermes_home": str(tmp_path)}
    )
    assert engine.config.aux_enabled is False
    assert engine.config.reserve_tokens == 4096
    assert engine.config.hermes_home == str(tmp_path)
    assert engine.get_status()["engine"] == ENGINE_NAME


def test_build_engine_rejects_provider_configuration():
    with pytest.raises(ValueError):
        plugin_module.build_engine({"clm.model": "gpt-4o"})


def test_build_engine_passes_the_flat_clm_mapping(monkeypatch):
    """The engine seam is ``CLMEngine(config=<flat clm.* mapping>)``, no translation."""

    import hermes_clm.engine as engine_module

    seen: list[dict] = []

    class RecordingEngine:
        def __init__(self, *, config=None):
            seen.append(dict(config or {}))

    monkeypatch.setattr(engine_module, "CLMEngine", RecordingEngine)
    plugin_module.build_engine({"clm.reserve_tokens": 4096, "clm.aux_enabled": False})
    assert seen == [{"clm.reserve_tokens": 4096, "clm.aux_enabled": False}]


# --------------------------------------------------------------------------- #
# Hermes-free import
# --------------------------------------------------------------------------- #


def test_plugin_module_does_not_import_hermes():
    assert plugin_module.__name__ == "hermes_clm.plugin"
    for name in ("hermes_cli", "hermes_cli.config", "run_agent"):
        assert name not in sys.modules or sys.modules[name] is None


# --------------------------------------------------------------------------- #
# Packaging artifacts: manifest, skill frontmatter, entry point
# --------------------------------------------------------------------------- #


def test_manifest_metadata_is_valid():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(PLUGIN_YAML.read_text(encoding="utf-8"))
    assert all(key.startswith("clm.") for key in data["config_keys"])
    assert data["kind"] == "standalone"
    assert data["name"] == "hermes-clm"


SKILL_MD = REPO_ROOT / "skills" / "context-management" / "SKILL.md"


def test_skill_frontmatter_is_valid():
    yaml = pytest.importorskip("yaml")
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == "context-management"
    assert meta["author"] == "gwyntel"
    assert meta["license"] == "MutuaL-1.2"
    assert len(meta["description"]) <= 60, meta["description"]


def test_docs_carry_no_upstream_markers():
    texts = [SKILL_MD.read_text(encoding="utf-8")]
    example = REPO_ROOT / "examples" / "AGENTS.md"
    if example.is_file():
        texts.append(example.read_text(encoding="utf-8"))
    for text in texts:
        for marker in ("[[CTX_TURN]]", "LIVE_CTX_MAIN", "LIVE_CTX.txt", "context-language-models"):
            assert marker not in text


def test_repo_entrypoint_delegates_to_the_plugin_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hermes_clm_entrypoint", REPO_ROOT / "__init__.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.register)
    assert module._import_register() is plugin_module.register


def test_repo_entrypoint_matches_the_context_engine_discovery_heuristic():
    """Hermes only imports a user engine directory when this marker is present."""

    source = (REPO_ROOT / "__init__.py").read_text(encoding="utf-8")[:8192]
    assert "register_context_engine" in source or "ContextEngine" in source


def test_read_clm_config_tolerates_missing_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    assert plugin_module.read_clm_config() == {}
