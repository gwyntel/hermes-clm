"""Hermes plugin wiring for CLM.

``register(ctx)`` does exactly what the *lane* it is loaded through supports:

1. reads the ``clm.*`` profile config section (no production config edits),
2. builds the context engine and — when the context supports it — registers it,
3. registers the ``clm_*`` tools **only** in the general-plugin fallback lane
   (a ctx with ``register_tool`` but without ``register_context_engine``); a
   context engine advertises the same tools through ``get_tool_schemas()``, and
   registering them twice is how duplicate tool names happen,
4. registers the operator-only ``/clm`` slash command when the context can.

Hermes is imported lazily and only inside the functions that need it, so this
module imports cleanly in a bare environment (tests use fakes for ``ctx``).

The engine owns session lifecycle and exposes the scoped service used by tools:

    engine.service_for_session(session_id: str | None) -> service | None

``engine.tool_service()`` is accepted as a fallback for host engines that bind
one service per engine instance. That is the only coupling between this layer
and the engine: no store handle, no paths, no provider or model configuration
crosses it. A session resolver must resolve the *calling* session's store,
including for subagents, whose stores are separate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .auxiliary import AuxCompressor, AuxLimits, bound_timeout
from .commands import COMMAND_NAME
from .commands import handle as handle_command
from .tools import TOOLSET, ToolHandlers, ToolLimits, build_tool_specs

__all__ = [
    "ENGINE_NAME",
    "PLUGIN_NAME",
    "PluginRuntime",
    "build_aux",
    "build_engine",
    "build_tool_handlers",
    "read_clm_config",
    "register",
]

PLUGIN_NAME = "hermes-clm"
#: Must equal the engine's ``name`` and the operator's ``context.engine`` value.
ENGINE_NAME = "hermes-clm"
CONFIG_SECTION = "clm"

DEFAULT_AUX_TIMEOUT_SECONDS = 45.0
DEFAULT_AUX_ENABLED = True


def read_clm_config() -> dict[str, Any]:
    """Read the ``clm.*`` section of the active profile config.

    Returns a flat mapping such as ``{"clm.aux_enabled": True}``. A missing or
    unreadable config yields defaults: the plugin never writes config and never
    falls back to environment secrets.
    """

    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return {}
    if not isinstance(config, Mapping):
        return {}
    section = config.get(CONFIG_SECTION)
    if not isinstance(section, Mapping):
        return {}
    return {f"{CONFIG_SECTION}.{key}": value for key, value in section.items()}


def _config_get(config: Mapping[str, Any], key: str, default: Any) -> Any:
    full = f"{CONFIG_SECTION}.{key}"
    if full in config:
        return config[full]
    return config.get(key, default)


def _config_bool(config: Mapping[str, Any], key: str, default: bool) -> bool:
    value = _config_get(config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def build_aux(
    config: Mapping[str, Any] | None = None, *, call: Callable[..., Any] | None = None
) -> AuxCompressor:
    """Build the auxiliary compressor from ``clm.*`` settings."""

    config = config or {}
    limits = ToolLimits.from_config(config)
    aux_limits = AuxLimits(
        max_spans=limits.max_aux_spans,
        max_rewrite_bytes=limits.max_rewrite_bytes,
        max_instruction_bytes=limits.max_instruction_bytes,
    )
    timeout = bound_timeout(_config_get(config, "aux_timeout_seconds", DEFAULT_AUX_TIMEOUT_SECONDS))
    return AuxCompressor(
        call,
        timeout_seconds=timeout,
        limits=aux_limits,
        enabled=_config_bool(config, "aux_enabled", DEFAULT_AUX_ENABLED),
    )


def build_engine(config: Mapping[str, Any] | None = None) -> Any:
    """Instantiate the CLM context engine from the flat ``clm.*`` mapping.

    ``CLMEngine`` accepts a mapping (``EngineConfig.from_mapping``); the plugin
    therefore never translates keys. Engines built before that coercion existed
    read ``config.hermes_home`` off the raw mapping and raise inside ``__init__``,
    so for those — and only those — rebuild through the engine's own
    ``EngineConfig.from_mapping``. Transitional shim: delete it once every
    supported engine coerces mappings itself.
    """

    from . import engine as engine_module

    cfg = dict(config or {})
    constructor = engine_module.CLMEngine

    def engine_config(mapping: Mapping[str, Any]) -> Any:
        from_mapping = getattr(getattr(engine_module, "EngineConfig", None), "from_mapping", None)
        return from_mapping(mapping) if callable(from_mapping) else None

    try:
        # The engine accepts a mapping by contract (`EngineConfig.from_mapping`);
        # its constructor annotation still says EngineConfig until the engine fix.
        engine = constructor(config=cfg)
    except AttributeError as exc:
        adapter = engine_config(cfg) if cfg and "hermes_home" in str(exc) else None
        if adapter is None:
            raise
        return constructor(config=adapter)

    if cfg and isinstance(getattr(engine, "config", None), Mapping):
        adapter = engine_config(cfg)
        if adapter is not None:
            return constructor(config=adapter)
    return engine


def build_tool_handlers(
    resolve_service: Callable[[str | None], Any],
    *,
    config: Mapping[str, Any] | None = None,
    aux: AuxCompressor | None = None,
) -> ToolHandlers:
    """Handlers for one plugin registration."""

    config = config or {}
    return ToolHandlers(
        resolve_service,
        limits=ToolLimits.from_config(config),
        aux=aux,
    )


class PluginRuntime:
    """State shared by the registered tools and the operator command."""

    def __init__(
        self,
        engine: Any,
        *,
        config: Mapping[str, Any] | None = None,
        aux: AuxCompressor | None = None,
    ) -> None:
        self.engine = engine
        self.config = dict(config or {})
        self.aux = aux
        self.handlers = build_tool_handlers(self.service, config=self.config, aux=aux)

    # -- session → service -------------------------------------------------- #

    def service(self, session_id: str | None) -> Any:
        """Resolve the calling session's scoped service; ``None`` when unavailable."""

        engine = self.engine
        resolver = getattr(engine, "service_for_session", None)
        if callable(resolver):
            try:
                return resolver(session_id)
            except Exception:
                return None
        # Host engines that own exactly one session expose tool_service() instead.
        factory = getattr(engine, "tool_service", None)
        if callable(factory):
            try:
                return factory()
            except Exception:
                return None
        return None

    # -- operator command --------------------------------------------------- #

    def command(self, raw_args: str = "", **kwargs: Any) -> str:
        """Handler for ``/clm``; receives ``session_id`` from the host dispatcher."""

        session_id = kwargs.get("session_id")
        return handle_command(raw_args, resolve_service=self.service, session_id=session_id)


def register(
    ctx: Any,
    *,
    config: Mapping[str, Any] | None = None,
    engine: Any = None,
    engine_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    aux: AuxCompressor | None = None,
    aux_call: Callable[..., Any] | None = None,
) -> PluginRuntime:
    """Register the engine and/or tools and the operator command with ``ctx``.

    Two host lanes exist and they must not both advertise the same tools:

    * **context-engine lane** — ``ctx`` has ``register_context_engine`` (Hermes'
      ``_EngineCollector``). We hand over the engine and the ``/clm`` command.
      Tools come from ``engine.get_tool_schemas()``, which Hermes injects into
      ``agent.tools``; calling ``register_tool`` here would either raise (the
      collector is not a general plugin context) or duplicate every name.
    * **general-plugin fallback** — no engine registration exists, so the same
      schemas are registered through ``ctx.register_tool``.

    In the engine lane the tools exist only while the engine is selected
    (``context.engine: hermes-clm``); enabling the plugin without selecting the
    engine intentionally exposes nothing.

    Injection points exist for tests and for integration: ``engine`` (a ready
    engine), ``engine_factory``, ``aux`` (a compressor) and ``aux_call`` (the
    auxiliary client). Production passes only ``ctx``.
    """

    cfg = dict(config) if config is not None else read_clm_config()
    compressor = aux if aux is not None else build_aux(cfg, call=aux_call)
    clm_engine = engine if engine is not None else (engine_factory or build_engine)(cfg)

    runtime = PluginRuntime(clm_engine, config=cfg, aux=compressor)

    register_engine = getattr(ctx, "register_context_engine", None)
    engine_lane = callable(register_engine)
    if engine_lane and register_engine is not None:
        register_engine(clm_engine)

    register_tool = getattr(ctx, "register_tool", None)
    if not engine_lane and callable(register_tool):
        for spec in build_tool_specs(runtime.handlers):
            register_tool(
                name=spec.name,
                toolset=TOOLSET,
                schema=spec.schema,
                handler=spec.handler,
                description=spec.description,
            )

    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command(
            COMMAND_NAME,
            runtime.command,
            description="Context management: status, operator edit, unpin",
            args_hint="status|edit|unpin",
        )
    return runtime
