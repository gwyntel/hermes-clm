"""The model-facing system-prompt section: content, gating and host budget.

These tests pin three separate things, because they fail independently:

* the **text** — static, inside the host's character ceiling, and accurate to
  the twelve tools this package advertises (guidance for tools that do not
  exist is worse than no guidance);
* the **gate** — guidance is registered only where the ``clm_*`` tools are
  callable, so a profile that installs the plugin but selects another engine
  receives nothing;
* the **failure posture** — a host without the section API, or one that
  rejects the registration, still gets a working engine and tool surface, with
  the failure recorded instead of silently claimed.
"""

from __future__ import annotations

import logging
import re

import pytest

from hermes_clm import guidance
from hermes_clm import plugin as plugin_module
from hermes_clm.plugin import (
    ENGINE_NAME,
    PluginRuntime,
    clm_engine_selected,
    prompt_section_enabled,
    read_context_engine,
    register,
)
from hermes_clm.tools import TOOLSET

SECTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
RESERVED_MARKERS = ("<!-- hermes-plugin-sections:start -->", "<!-- hermes-plugin-sections:end -->")


class FakeEngine:
    """Engine stand-in; the section tests never touch session state."""

    name = ENGINE_NAME

    def service_for_session(self, session_id: str | None = None):  # pragma: no cover - unused
        return None


class HypotheticalEngineAndSectionCtx:
    """*Hypothetical* host: engine registration *and* the section API.

    Hermes' engine lane is ``plugins/context_engine._EngineCollector``, a
    ``NoopPluginContext`` subclass that exposes ``register_context_engine`` and
    ``register_command`` only — no ``register_system_prompt_section``, no
    ``register_tool``. This shape is therefore **unreachable in production**;
    it exists only to pin ``register()``'s gate (an engine-lane ctx that *did*
    have the API would get guidance only when it is the selected engine). The
    real engine-only path is covered by ``EngineOnlyCtx`` below, and the real
    dual-enablement path by the host probe in
    ``tests/integration/test_prompt_section.py``.
    """

    def __init__(self) -> None:
        self.engines: list = []
        self.commands: list[str] = []
        self.sections: list[dict] = []

    def register_context_engine(self, engine) -> None:
        self.engines.append(engine)

    def register_command(self, name, handler, description="", args_hint="", **kwargs) -> None:
        self.commands.append(name)

    def register_system_prompt_section(
        self, id, content, *, position="after_memory", max_chars=4000
    ):
        self.sections.append(
            {"id": id, "content": content, "position": position, "max_chars": max_chars}
        )
        return f"handle:{id}"


class EngineOnlyCtx:
    """Hermes' ``_EngineCollector``: engine + command, no tools, no sections."""

    def __init__(self) -> None:
        self.engine = None
        self.commands: list[str] = []

    def register_context_engine(self, engine) -> None:
        self.engine = engine

    def register_command(self, name, handler, description="", args_hint="", **kwargs) -> None:
        self.commands.append(name)


class FallbackCtx:
    """No engine registration (older host): tools are registered by the plugin."""

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
        self.sections.append(
            {"id": id, "content": content, "position": position, "max_chars": max_chars}
        )
        return f"handle:{id}"


class RejectingSectionCtx(HypotheticalEngineAndSectionCtx):
    def __init__(self, message: str = "section id already registered by plugin 'other'") -> None:
        super().__init__()
        self._message = message

    def register_system_prompt_section(self, *args, **kwargs):
        raise ValueError(self._message)

    def register_tool(self, **kwargs) -> None:  # pragma: no cover - engine lane only
        raise AssertionError("engine lane must not call register_tool")


# --------------------------------------------------------------------------- #
# Text contract
# --------------------------------------------------------------------------- #


def test_section_identity_matches_the_host_contract():
    assert SECTION_ID_RE.fullmatch(guidance.SYSTEM_PROMPT_SECTION_ID)
    assert guidance.SYSTEM_PROMPT_SECTION_ID == "hermes-clm.usage"
    assert guidance.SYSTEM_PROMPT_SECTION_POSITION == "after_memory"
    assert 0 < guidance.SYSTEM_PROMPT_SECTION_MAX_CHARS <= 4_000


def test_section_text_fits_the_declared_budget_with_headroom():
    text = guidance.SYSTEM_PROMPT_SECTION_TEXT
    assert isinstance(text, str)
    assert text == text.strip(), "the host strips rendered text; leading/trailing space would drift"
    assert len(text) <= guidance.SYSTEM_PROMPT_SECTION_MAX_CHARS
    # Headroom matters: the host also renders a heading and a length frame, and
    # a section that grows past max_chars is skipped outright.
    assert len(text) <= guidance.SYSTEM_PROMPT_SECTION_MAX_CHARS - 500
    for marker in RESERVED_MARKERS:
        assert marker not in text, "a reserved persistence marker makes the host skip the section"


def test_section_is_static_and_carries_no_placeholders():
    text = guidance.SYSTEM_PROMPT_SECTION_TEXT
    assert "{" not in text and "}" not in text, (
        "no format placeholders: nothing may be interpolated"
    )
    assert "$" not in text
    for forbidden in ("http://", "https://", "/Users/", "~/.hermes", "sk-"):
        assert forbidden not in text


def test_guidance_names_every_tool_the_plugin_advertises():
    text = guidance.SYSTEM_PROMPT_SECTION_TEXT
    for tool in (
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
    ):
        assert tool in text, f"guidance never mentions {tool}"


def test_guidance_states_the_operating_rules_the_tools_enforce():
    text = guidance.SYSTEM_PROMPT_SECTION_TEXT.lower()
    # Stable IDs and revision CAS.
    assert "span id" in text and "expected_revision" in text
    # Preservation of intent and exact constraints.
    assert "exact constraint" in text and "open questions" in text
    # Review before applying; the archive is lookup, not authority.
    assert "commit" in text and "archive" in text and "not authority" in text
    # Cache-minimal editing.
    assert "prefix cache" in text and "batch" in text
    # Pins, boundaries, revert, and operator-only authority.
    assert "pin" in text and "boundary" in text and "revert" in text
    assert "operator" in text and "cannot unpin" in text
    # Aux proposals are untrusted and apply only after explicit acceptance.
    assert "untrusted" in text and "proposal" in text


# --------------------------------------------------------------------------- #
# Selection gate
# --------------------------------------------------------------------------- #


def test_selection_reads_the_host_context_engine_key(monkeypatch):
    monkeypatch.setattr(
        plugin_module, "_read_profile_config", lambda: {"context": {"engine": ENGINE_NAME}}
    )
    assert read_context_engine() == ENGINE_NAME
    assert clm_engine_selected() is True


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"context": {}},
        {"context": {"engine": "compressor"}},
        {"context": {"engine": "hermes-lcm"}},
        {"context": {"engine": ""}},
        {"context": {"engine": None}},
        {"context": {"engine": 7}},
        {"context": "hermes-clm"},
    ],
)
def test_only_an_exact_engine_selection_counts(monkeypatch, profile):
    monkeypatch.setattr(plugin_module, "_read_profile_config", lambda: profile)
    assert clm_engine_selected() is False
    assert read_context_engine() != ENGINE_NAME


def test_engine_selection_is_not_normalised(monkeypatch):
    """The host passes the string verbatim; whitespace would miss the engine."""

    monkeypatch.setattr(
        plugin_module, "_read_profile_config", lambda: {"context": {"engine": " hermes-clm "}}
    )
    assert clm_engine_selected() is False


def test_prompt_section_enabled_follows_the_tool_surface():
    # Engine lane: guidance only when the engine is the selected one.
    assert prompt_section_enabled(engine_lane=True, selected=True) is True
    assert prompt_section_enabled(engine_lane=True, selected=False) is False
    # Fallback lane: the plugin registered the tools itself.
    assert prompt_section_enabled(engine_lane=False, selected=None) is True


def test_unreadable_config_withholds_guidance(monkeypatch):
    monkeypatch.setattr(plugin_module, "_read_profile_config", lambda: {})
    ctx = HypotheticalEngineAndSectionCtx()
    register(ctx, engine=FakeEngine(), config={})
    assert ctx.engines, "the engine must still register"
    assert ctx.sections == [], "no guidance without a positive context.engine selection"


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_selected_engine_lane_registers_the_section_once():
    ctx = HypotheticalEngineAndSectionCtx()
    runtime = register(ctx, engine=FakeEngine(), config={}, engine_selected=True)
    assert len(ctx.sections) == 1
    section = ctx.sections[0]
    assert section["id"] == guidance.SYSTEM_PROMPT_SECTION_ID
    assert section["position"] == "after_memory"
    assert section["max_chars"] == guidance.SYSTEM_PROMPT_SECTION_MAX_CHARS
    assert section["content"] is guidance.SYSTEM_PROMPT_SECTION_TEXT, "static text, not a callable"
    assert runtime.prompt_section == "handle:hermes-clm.usage"
    assert runtime.prompt_section_error is None
    assert [command for command in ctx.commands] == ["clm"]


def test_unselected_engine_lane_registers_nothing_for_the_model():
    ctx = HypotheticalEngineAndSectionCtx()
    runtime = register(ctx, engine=FakeEngine(), config={}, engine_selected=False)
    assert ctx.engines, "the engine registration is unchanged"
    assert ctx.sections == [], "an unselected engine must not inject instructions"
    assert runtime.prompt_section is None and runtime.prompt_section_error is None


def test_engine_only_host_without_the_section_api_still_loads():
    """The real engine lane: ``_EngineCollector`` has no section API at all."""

    ctx = EngineOnlyCtx()
    assert not hasattr(ctx, "register_system_prompt_section"), (
        "this fake must mirror the real collector: else the gate is untested"
    )
    runtime = register(ctx, engine=FakeEngine(), config={}, engine_selected=True)
    assert ctx.engine is not None and ctx.commands == ["clm"]
    assert runtime.prompt_section is None, "no API, no fake claim of a registered section"
    assert runtime.prompt_section_error is None, "a missing API is a limitation, not an error"


def test_fallback_lane_registers_tools_and_guidance():
    ctx = FallbackCtx()
    runtime = register(ctx, engine=FakeEngine(), config={}, engine_selected=False)
    assert len(ctx.tools) == 12 and all(tool["toolset"] == TOOLSET for tool in ctx.tools)
    assert len(ctx.sections) == 1, "the fallback lane registered the tools, so guidance travels"
    assert runtime.prompt_section == "handle:hermes-clm.usage"


def test_host_rejection_is_recorded_and_the_plugin_still_works():
    ctx = RejectingSectionCtx()
    runtime = register(ctx, engine=FakeEngine(), config={}, engine_selected=True)
    assert ctx.engines, "engine registration happened before the section"
    assert ctx.commands == ["clm"]
    assert runtime.prompt_section is None
    assert runtime.prompt_section_error is not None
    assert "ValueError" in runtime.prompt_section_error


def test_host_rejection_is_logged_without_echoing_host_text(caplog):
    """The entry-point shim discards ``PluginRuntime``; production needs a log.

    Host exception text is untrusted (it can name other plugins or paths), so the
    warning may only carry the section id and the exception type.
    """

    caplog.set_level(logging.WARNING, logger="hermes_clm.plugin")
    ctx = RejectingSectionCtx(
        "denied at /Users/gwyn/.hermes/profiles/claire/api_key=sk-live-abc123"
    )
    runtime = register(ctx, engine=FakeEngine(), config={}, engine_selected=True)

    assert runtime.prompt_section_error is not None
    records = [record for record in caplog.records if record.name == "hermes_clm.plugin"]
    assert len(records) == 1, "exactly one rejection warning per registration"
    record = records[0]
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert guidance.SYSTEM_PROMPT_SECTION_ID in message
    assert "ValueError" in message
    assert "load" in message, "the log must say the engine and tools still work"
    for leaked in ("/Users/", "sk-live", "api_key", "denied at"):
        assert leaked not in message
        assert leaked not in caplog.text


def test_successful_registration_logs_no_warning(caplog):
    caplog.set_level(logging.WARNING, logger="hermes_clm.plugin")
    register(
        HypotheticalEngineAndSectionCtx(), engine=FakeEngine(), config={}, engine_selected=True
    )
    assert [record for record in caplog.records if record.name == "hermes_clm.plugin"] == []


def test_guidance_only_names_advertised_tools_and_real_parameters():
    """Guard against guidance for a tool, or an argument, that does not exist."""

    from hermes_clm.tools import ToolHandlers, build_tool_specs

    specs = build_tool_specs(ToolHandlers(lambda session_id=None: None))
    names = {spec.name for spec in specs}
    text = guidance.SYSTEM_PROMPT_SECTION_TEXT
    mentioned = set(re.findall(r"clm_[a-z_]+", text))
    assert mentioned <= names, f"guidance invents tools: {sorted(mentioned - names)}"
    parameters: set[str] = set()
    for spec in specs:
        parameters.update(spec.schema["parameters"].get("properties", {}))
    snake_case = {token for token in re.findall(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b", text)}
    invented = {token for token in snake_case if not token.startswith("clm_")} - parameters
    assert not invented, f"guidance names parameters no schema has: {sorted(invented)}"


def test_guidance_states_the_auxiliary_span_size_limit():
    """Aux refuses spans over 4000 chars: the model must be told, not surprised."""

    text = guidance.SYSTEM_PROMPT_SECTION_TEXT
    assert "4000" in text
    assert "refused" in text
    assert "clm_stage" in text, "the guidance must name the tool that can do it instead"
    assert len(text) <= guidance.SYSTEM_PROMPT_SECTION_MAX_CHARS - 500


def test_guidance_does_not_overclaim_boundary_or_auxiliary_scope():
    """Regression guards for two claims the handlers do not support."""

    text = guidance.SYSTEM_PROMPT_SECTION_TEXT
    # A boundary is an audit marker; it does not confine later edits.
    assert "does not restrict later edits" in text
    assert "stay inside one task" not in text
    # propose_compression sends span text, not span IDs alone.
    assert "raw text of only the spans you select" in text
    assert "sends only the span IDs" not in text


def test_section_text_is_never_rebuilt_from_session_data():
    """The host freezes rendered bytes; a callable would re-run plugin code."""

    ctx = HypotheticalEngineAndSectionCtx()
    register(ctx, engine=FakeEngine(), config={}, engine_selected=True)
    content = ctx.sections[0]["content"]
    assert not callable(content)
    assert content == plugin_module.SYSTEM_PROMPT_SECTION_TEXT


def test_runtime_defaults_are_inert():
    runtime = PluginRuntime(FakeEngine(), config={})
    assert runtime.prompt_section is None
    assert runtime.prompt_section_error is None
