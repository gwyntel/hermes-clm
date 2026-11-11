"""Plugin registration: engine + tools + operator command, strict schemas, session scoping."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from clm_fakes import FakeService

from hermes_clm import plugin as plugin_module
from hermes_clm.auxiliary import AuxCompressor
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


def registered_ctx(**kwargs):
    ctx = FakeCtx()
    engine = kwargs.pop("engine", None) or FakeEngine()
    runtime = register(ctx, engine=engine, config=kwargs.pop("config", {}), **kwargs)
    return ctx, runtime, engine


def fallback_ctx(**kwargs):
    ctx = FallbackCtx()
    engine = kwargs.pop("engine", None) or FakeEngine()
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
    assert {tool["name"] for tool in ctx.tools} == set(_manifest_tools())
    assert [command["name"] for command in ctx.commands] == ["clm"]
    assert runtime.engine is engine


def _manifest_tools() -> list[str]:
    """Read ``provides_tools`` without a YAML dependency."""

    lines = PLUGIN_YAML.read_text(encoding="utf-8").splitlines()
    tools: list[str] = []
    in_block = False
    for line in lines:
        if line.startswith("provides_tools:"):
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("- "):
                break
            tools.append(stripped[2:].strip())
    return tools


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
    assert {schema["name"] for schema in schemas} == set(_manifest_tools())

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
    engine.sessions["sess-a"] = FakeService(revision=5)
    command = ctx.commands[0]["handler"]
    text = command("status", session_id="sess-a")
    assert "CLM" in text and "revision 5" in text


def test_command_handler_works_without_a_session():
    ctx, _, _ = registered_ctx()
    text = ctx.commands[0]["handler"]("status", session_id=None)
    assert "no CLM store" in text


def test_command_help_lists_operator_actions():
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
    payload = load(status_tool["handler"]({}, session_id=None))
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


def test_manifest_tool_list_matches_registration():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(PLUGIN_YAML.read_text(encoding="utf-8"))
    ctx, _, _ = fallback_ctx()
    assert set(data["provides_tools"]) == {tool["name"] for tool in ctx.tools}
    assert all(key.startswith("clm.") for key in data["config_keys"])
    assert data["kind"] == "standalone"


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
