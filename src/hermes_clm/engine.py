"""Hermes context engine: durable CLM projection over raw conversation history.

The engine is the only Hermes-facing module in this package. It:

* implements the host ``ContextEngine`` contract (``select_context``,
  ``compress``, lifecycle, model switch, token accounting);
* keeps the durable store optional and lazy - ``import hermes_clm.engine``
  works with no Hermes checkout, and no store is opened until the host names a
  session;
* refuses to guess: any store, projection or configuration problem degrades to
  "leave the request byte-identical" (``select_context`` returns ``None``)
  instead of sending a partial edit.

Nothing here edits the Hermes checkout, its config, or its database.
"""

from __future__ import annotations

import copy
import functools
import importlib
import json
import logging
import os
import threading
import uuid
import weakref
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import budget as _budget
from . import notice as _notice
from ._version import __version__ as _ENGINE_VERSION
from .auxiliary import (
    AuxCompressor,
    AuxLimits,
    bound_timeout,
)
from .budget import TokenCalibration, token_vocabulary
from .errors import CLMError
from .models import MAX_REWRITE_BYTES
from .projection import ProjectionResult, project
from .tools import ToolHandlers, ToolLimits, build_tool_specs

#: Fallback logger: an aborted compaction must leave a trace even when the host
#: constructs the engine without one (a silent refusal is undiagnosable).
_LOG = logging.getLogger(__name__)

#: Default reserve kept free for the next completion.
DEFAULT_RESERVE_TOKENS = _budget.DEFAULT_RESERVE_TOKENS
#: Default pressure thresholds (fractions of the raw context length).
DEFAULT_PRESSURE_THRESHOLDS: Tuple[float, ...] = _budget.DEFAULT_PRESSURE_THRESHOLDS
#: Fields a proposal operation contributes to its aux digest.
_PROPOSAL_OPERATION_FIELDS = ("kind", "span_ids", "replacement")

#: How many recent projection/usage receipts the engine keeps in memory for status.
RECEIPT_LIMIT = 20

#: Default fraction of the projection a manual ``/compact`` tries to shed when the
#: window is not under pressure. Deterministic, archive-backed, and capped by the
#: offloadable tool groups that actually exist.
DEFAULT_MANUAL_TARGET_RATIO = 0.2

#: Store error codes that mean "this engine has no usable store right now" rather
#: than "the store refused this edit".
_STORE_ATTACH_CODES = frozenset(
    {
        "invalid_home",
        "no_session",
        "store_not_found",
        "store_busy",
        "store_closed",
        "unsafe_path",
        "symlink_refused",
        "sqlite_open_failed",
        "schema_missing",
        "schema_incomplete",
    }
)

#: Live engines by the session they serve. A surface that can only name a session —
#: the host's slash-command dispatcher passes ``session_id``/``session_key`` and
#: never the agent — resolves its engine here. Weak values: an engine the host has
#: dropped must never be resurrected by a late command.
_ENGINE_BINDINGS: "weakref.WeakValueDictionary[str, Any]" = weakref.WeakValueDictionary()
_ENGINE_BINDINGS_LOCK = threading.Lock()


def engine_for_session(session_id: Optional[str]) -> Optional["CLMEngine"]:
    """The live engine bound to *session_id*, or ``None`` when none is bound.

    Only an engine that adopted the session registers here, so this never returns
    the plugin system's shared prototype (which is deep-copied per agent) or an
    instance an earlier agent left behind: the engine that owns the store answers.
    A ``None``/blank ``session_id`` is never resolved to "whichever engine is
    bound" — that would hand one session's archive to a caller who named none.
    """

    if not session_id:
        return None
    with _ENGINE_BINDINGS_LOCK:
        return _ENGINE_BINDINGS.get(str(session_id))


class _StandaloneEngineBase:
    """Attribute/behaviour surface used when Hermes is not importable.

    Mirrors the defaults of ``agent.context_engine.ContextEngine`` so this
    package can be imported and tested without a Hermes checkout. When Hermes
    *is* present the real ABC is the base class and these defaults come from it.
    """

    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6
    emit_automatic_compaction_status: bool = False

    def select_context(self, request_messages, **kwargs: Any):
        """No-op: leave the request unchanged."""
        return None

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Never request compression."""
        return False

    def should_compress_info(
        self, prompt_tokens: Optional[int] = None
    ) -> Tuple[bool, Optional[str]]:
        """Never request compression."""
        return False, None

    def should_compress_preflight(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        """Never request preflight compression."""
        return False

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        """Never defer preflight."""
        return False

    def prune_tool_results_only(self, messages, current_tokens: Optional[int] = None):
        """Default no-op pruning."""
        return messages, 0

    def has_content_to_compress(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        """Host preflight for ``/compress``: there is always a projection to apply."""
        return True

    def on_turn_complete(self, messages, usage=None, **kwargs: Any) -> None:
        """Observe a finished turn (accounting rides on usage updates)."""

    def get_automatic_compaction_status_message(
        self, *, phase: str, default_message: str, **context: Any
    ):
        """This engine never compacts automatically, so it never reports doing so."""
        return default_message if self.emit_automatic_compaction_status else None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """No tools by default."""
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Reject unknown tools with a JSON error string."""
        message = f"Unknown context engine tool: {name}"
        return json.dumps(
            {
                "ok": False,
                "error": message,
                "errors": [{"code": "unknown_tool", "message": message}],
            }
        )

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """No-op."""

    def on_session_end(self, session_id: str, messages: Sequence[Mapping[str, Any]]) -> None:
        """No-op."""

    def on_session_reset(self) -> None:
        """Reset per-session counters (host parity)."""
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    def clone_for_agent(self):
        """Fresh instance sharing only the durable backend (overridden here)."""
        return copy.deepcopy(self)

    def get_status(self) -> Dict[str, Any]:
        """Standard host status fields."""
        last_prompt = max(self.last_prompt_tokens, 0)
        return {
            "last_prompt_tokens": last_prompt,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                min(100, last_prompt / self.context_length * 100) if self.context_length else 0
            ),
            "compression_count": self.compression_count,
        }


def _load_engine_base() -> type:
    """The host ``ContextEngine`` when importable, else the standalone stand-in.

    Guarded and cheap: importing this package never requires Hermes.
    """
    try:  # pragma: no cover - depends on host presence
        from agent.context_engine import ContextEngine

        return ContextEngine
    except Exception:
        return _StandaloneEngineBase


_EngineBase: Any = _load_engine_base()


def _config_value(mapping: Mapping[str, Any], key: str, default: Any) -> Any:
    """Read ``clm.<key>`` (preferred) or a bare ``<key>`` from a config mapping."""
    for candidate in (f"clm.{key}", key):
        if candidate in mapping:
            return mapping[candidate]
    return default


@dataclass(frozen=True)
class EngineConfig:
    """``clm.*`` configuration. No model, provider, base-URL or key fields exist here."""

    reserve_tokens: int = DEFAULT_RESERVE_TOKENS
    pressure_thresholds: Tuple[float, ...] = DEFAULT_PRESSURE_THRESHOLDS
    aux_enabled: bool = True
    aux_timeout_seconds: float = 45.0
    archive_redact: bool = True
    retention_sessions: int = 32
    max_retrieve_bytes: int = 32_768
    max_rewrite_bytes: int = MAX_REWRITE_BYTES
    max_aux_spans: int = 8
    manual_target_ratio: float = DEFAULT_MANUAL_TARGET_RATIO
    hermes_home: Optional[str] = None

    @classmethod
    def from_mapping(cls, mapping: Optional[Mapping[str, Any]] = None) -> "EngineConfig":
        """Build a config from a profile config mapping (``clm.*`` keys).

        Provider/model/credential keys are rejected outright: this engine has no
        business selecting a model, and a typo must not silently become one.
        """
        source = dict(mapping or {})
        forbidden = [
            key
            for key in source
            if key.split(".")[-1]
            in {"model", "provider", "base_url", "api_key", "fallback", "fallback_chain", "models"}
        ]
        if forbidden:
            raise ValueError(f"clm config accepts no model/provider keys: {sorted(forbidden)}")
        thresholds = _config_value(source, "pressure_thresholds", DEFAULT_PRESSURE_THRESHOLDS)
        if isinstance(thresholds, (int, float)):
            thresholds = (float(thresholds),)
        return cls(
            reserve_tokens=int(_config_value(source, "reserve_tokens", DEFAULT_RESERVE_TOKENS)),
            pressure_thresholds=tuple(float(value) for value in thresholds),
            aux_enabled=bool(_config_value(source, "aux_enabled", True)),
            aux_timeout_seconds=float(_config_value(source, "aux_timeout_seconds", 45.0)),
            archive_redact=bool(_config_value(source, "archive_redact", True)),
            retention_sessions=int(_config_value(source, "retention_sessions", 32)),
            max_retrieve_bytes=int(_config_value(source, "max_retrieve_bytes", 32_768)),
            max_rewrite_bytes=int(_config_value(source, "max_rewrite_bytes", MAX_REWRITE_BYTES)),
            max_aux_spans=int(_config_value(source, "max_aux_spans", 8)),
            manual_target_ratio=float(
                _config_value(source, "manual_target_ratio", DEFAULT_MANUAL_TARGET_RATIO)
            ),
            hermes_home=_config_value(source, "hermes_home", None),
        )

    @classmethod
    def coerce(cls, config: EngineConfig | Mapping[str, Any] | None) -> EngineConfig:
        """Accept an ``EngineConfig``, a flat ``clm.*`` mapping, or ``None``.

        The plugin hands the engine the flat ``clm.*`` dict it read from the
        profile config, so a mapping is a first-class input rather than a
        programming error.
        """
        if config is None:
            return cls()
        if isinstance(config, cls):
            return config
        if isinstance(config, Mapping):
            return cls.from_mapping(config)
        raise TypeError(
            f"config must be EngineConfig, mapping or None; got {type(config).__name__}"
        )

    def to_store_config(self) -> dict[str, Any]:
        """Store limits derived from this config (``store.StoreConfig`` fields)."""
        return {
            "max_retrieve_bytes": self.max_retrieve_bytes,
            "max_rewrite_bytes": self.max_rewrite_bytes,
            "max_aux_spans": self.max_aux_spans,
            "archive_redact": self.archive_redact,
            "retention_sessions": self.retention_sessions,
        }

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable config (no credentials can appear here)."""
        return {
            "reserve_tokens": self.reserve_tokens,
            "pressure_thresholds": list(self.pressure_thresholds),
            "aux_enabled": self.aux_enabled,
            "aux_timeout_seconds": self.aux_timeout_seconds,
            "archive_redact": self.archive_redact,
            "retention_sessions": self.retention_sessions,
            "max_retrieve_bytes": self.max_retrieve_bytes,
            "max_rewrite_bytes": self.max_rewrite_bytes,
            "max_aux_spans": self.max_aux_spans,
            "manual_target_ratio": self.manual_target_ratio,
        }


@dataclass(frozen=True)
class OperationSpec:
    """Structural stand-in for the store's ``models.EditOperation``.

    Used when the store package is absent (independent branches) or when its
    constructor does not accept these exact fields. Field names follow
    ``docs/integration-contract.md``.
    """

    kind: str
    span_ids: Tuple[str, ...]
    replacement: Optional[str] = None
    expected_revision: int = 0
    txn_id: str = ""
    actor_origin: str = "agent"
    provenance: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable operation."""
        payload: Dict[str, Any] = {
            "kind": self.kind,
            "span_ids": list(self.span_ids),
            "actor_origin": self.actor_origin,
        }
        if self.replacement is not None:
            payload["replacement"] = self.replacement
        if self.expected_revision:
            payload["expected_revision"] = self.expected_revision
        if self.txn_id:
            payload["txn_id"] = self.txn_id
        if self.provenance:
            payload["provenance"] = dict(self.provenance)
        return payload


@dataclass(eq=False)
class _SnapshotView:
    """Minimal structural view of a store snapshot (used for local-only plans)."""

    revision: int = 0
    operations: Tuple[Any, ...] = ()
    spans: Mapping[str, Any] = field(default_factory=dict)
    pins: Tuple[str, ...] = ()
    head_hash: str = ""


def default_store_factory(
    hermes_home: str | None,
    session_id: str,
    *,
    read_only: bool = False,
    config: Any | None = None,
):
    """Open a session store lazily.

    Imported inside the call so this package imports without the store package
    present. The store is never opened read-write by a projection.

    ``config`` may be an :class:`EngineConfig`, a ``clm.*`` mapping or a store
    config object; engine-derived limits (redaction, retention, retrieve/rewrite
    bounds) are applied so the store and the tool surface agree on the same
    numbers. Custom test factories keep the plain three-argument protocol.
    """
    module = importlib.import_module(".store", __package__)  # lazy: separate delivery unit
    store_config = _store_config(module, config)

    return module.ContextStore.for_session(
        hermes_home or "", session_id, read_only=read_only, config=store_config
    )


def _store_config(module: Any, config: Any | None) -> Any:
    """Coerce engine config into a ``store.StoreConfig`` (``None`` keeps defaults)."""
    if config is None:
        return None
    if isinstance(config, EngineConfig):
        payload: Mapping[str, Any] | None = config.to_store_config()
    elif isinstance(config, Mapping):
        payload = config
    elif hasattr(config, "to_store_config"):  # pragma: no cover - defensive duck typing
        payload = config.to_store_config()
    else:
        return config  # already a StoreConfig instance
    if payload is None:  # pragma: no cover - defensive
        return None
    known = set(getattr(module.StoreConfig, "__dataclass_fields__", {}))
    return module.StoreConfig(
        **{key: value for key, value in dict(payload).items() if key in known}
    )


def _result_error_code(result: Any) -> Optional[str]:
    """The store's own error code from a stage/commit result, when it reports one."""

    if not isinstance(result, Mapping):
        code = getattr(result, "code", None)
        return str(code) if code else None
    errors = result.get("errors") or result.get("conflicts") or ()
    if isinstance(errors, Mapping):
        errors = [errors]
    for item in errors:
        if isinstance(item, Mapping):
            code = item.get("code")
            if code:
                return str(code)
    code = result.get("code") or result.get("error_code")
    return str(code) if code else None


def _result_ok(result: Any, default: bool = True) -> bool:
    """Read ``ok`` from a store result mapping or object."""
    if result is None:
        return default
    if isinstance(result, Mapping):
        return bool(result.get("ok", default))
    return bool(getattr(result, "ok", default))


def _span_state(span: Any) -> str:
    """A snapshot span's state as a lowercase string (``"active"`` when unstated)."""

    state = getattr(span, "state", None)
    if state is None:
        return "active"
    value = getattr(state, "value", state)
    text = str(value or "").strip().lower()
    return text or "active"


def _settled_control_spans(snapshot: Any) -> set:
    """Span ids whose last committed content operation is ``offload``/``drop``.

    A store reports each span's state, but the operation log is the authority the
    engine can replay for itself — and the only thing that works when a store (or a
    test double) reports spans without state. Re-targeting an already-offloaded
    group would be inert in the store while still costing a revision, so the manual
    and overflow planners skip those groups instead.
    """

    settle: Dict[str, str] = {}
    for operation in getattr(snapshot, "operations", ()) or ():
        kind = (
            operation.get("kind")
            if isinstance(operation, Mapping)
            else getattr(operation, "kind", None)
        )
        kind = str(getattr(kind, "value", kind) or "").strip().lower()
        span_ids = (
            operation.get("span_ids")
            if isinstance(operation, Mapping)
            else getattr(operation, "span_ids", ())
        ) or ()
        if not isinstance(span_ids, (list, tuple)):
            span_ids = (span_ids,)
        for span_id in span_ids:
            if isinstance(span_id, str) and span_id:
                settle[span_id] = kind
    return {span_id for span_id, kind in settle.items() if kind in {"offload", "drop"}}


def _pin_identifiers(pins: Any) -> set:
    """Span ids and uids named by a snapshot's ``pins``.

    Accepts the store's ``Mapping[span_id, PinRecord]``, a sequence of
    ``PinRecord`` objects (use ``span_id``, never ``repr()``), a sequence of
    mapping records, or plain id strings.
    """
    if pins is None:
        return set()
    if isinstance(pins, Mapping):
        identifiers = {str(key) for key in pins}
        for value in pins.values():
            span_id = (
                value.get("span_id")
                if isinstance(value, Mapping)
                else getattr(value, "span_id", None)
            )
            if isinstance(span_id, str) and span_id:
                identifiers.add(span_id)
        return identifiers
    if isinstance(pins, str | bytes):
        return {pins.decode() if isinstance(pins, bytes) else pins}
    identifiers = set()
    for item in pins:
        if isinstance(item, str):
            identifiers.add(item)
            continue
        span_id = (
            item.get("span_id") if isinstance(item, Mapping) else getattr(item, "span_id", None)
        )
        if isinstance(span_id, str) and span_id:
            identifiers.add(span_id)
    return identifiers


def _payload(result: Any, *, ok: bool = True) -> dict[str, Any]:
    """Normalise a store result (mapping, dataclass, ``to_dict``) into a dict."""
    if result is None:
        return {"ok": ok, "errors": [], "receipts": []}
    if isinstance(result, Mapping):
        payload = dict(result)
    elif hasattr(result, "to_dict"):
        payload = dict(result.to_dict())
    elif hasattr(result, "as_dict"):  # pragma: no cover - defensive
        payload = dict(result.as_dict())
    else:  # pragma: no cover - defensive
        payload = dict(vars(result))
    payload.setdefault("ok", ok)
    if not isinstance(payload.get("errors"), list):
        payload["errors"] = []
    payload.setdefault("receipts", [])
    return payload


def _decode_tool_arguments(raw: str | bytes) -> Any:
    """Decode a JSON-string argument payload; a blank string means no arguments."""

    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if not text.strip():
        return {}
    return json.loads(text)


def _invalid_arguments(name: str, detail: str) -> str:
    """Structured refusal for a tool call whose arguments are not an object."""

    message = f"Invalid arguments for {name}: {detail}"
    return json.dumps(
        {
            "ok": False,
            "error": message,
            "errors": [{"code": "invalid_arguments", "message": message}],
        }
    )


class ToolService:
    """Scoped store access for tool handlers.

    Tool handlers never see ``session_id``, paths, provider or model settings;
    they call these methods, which are bound to this engine's single session.
    Results are JSON-serializable mappings so the tool layer can pass them
    through without inspecting store dataclasses.
    """

    def __init__(self, engine: "CLMEngine") -> None:
        self._engine = engine

    def _require_store(self) -> Any:
        store = self._engine.store
        if store is None:
            # Keep the store's own code (``invalid_home``, ``store_busy``,
            # ``no_session``) so a tool refusal names the cause, not just "no store".
            error = getattr(self._engine, "_store_error", None)
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else None
            detail = f"{code}: {message}" if code and message else "clm store is not attached"
            raise RuntimeError(detail)
        return store

    def _call_store(self, name: str, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Delegate to one store API, turning typed store refusals into JSON.

        Store errors carry a stable ``code``; converting them here keeps tool
        receipts honest (``stale_revision`` rather than a generic internal error)
        while still never letting an exception out of a handler.
        """
        store = self._require_store()
        handler = getattr(store, name, None)
        if not callable(handler):
            raise RuntimeError(f"clm store does not implement {name}()")
        try:
            return _payload(handler(*args, **kwargs))
        except CLMError as exc:
            return {
                "ok": False,
                "revision": self._live_revision(store),
                "errors": [exc.to_dict()],
                "receipts": [],
            }

    @staticmethod
    def _live_revision(store: Any) -> int:
        """Head revision from the store itself, never a cached projection."""
        try:
            status = store.status()
        except Exception:
            return 0
        if isinstance(status, Mapping):
            revision = status.get("revision")
        else:  # pragma: no cover - defensive
            revision = getattr(status, "revision", None)
        if isinstance(revision, bool) or not isinstance(revision, int):
            return 0
        return revision

    @property
    def revision(self) -> int:
        """Live head revision for this session (``0`` when no store is attached)."""
        store = self._engine.store
        if store is None:
            return 0
        return self._live_revision(store)

    def status(self) -> dict[str, Any]:
        """Engine status, with the revision read from the store's live head."""
        payload = dict(self._engine.get_status())
        store = self._engine.store
        if store is not None:
            payload["revision"] = self._live_revision(store)
            # Refresh (never guess) what the last compaction did: the operator's
            # ``/compact`` writes revisions this engine did not author.
            payload["last_compaction"] = self._engine.compaction_summary()
            try:
                live = store.status()
            except Exception as exc:
                payload["store_health"] = {"ok": False, "error": type(exc).__name__}
            else:
                if isinstance(live, Mapping):
                    health = live.get("health")
                    if health is not None:
                        payload["store_health"] = health
                        payload["health"] = health
                    for key in (
                        "head_hash",
                        "revision_kind",
                        "counts",
                        "est_tokens",
                        "estimator",
                        "spans_archived",
                        "archive_entries",
                        "operations_committed",
                        "staged_transactions",
                        "last_audit",
                        "index",
                        "read_only",
                        "redaction",
                        "limits",
                    ):
                        if key in live:
                            payload[key] = live[key]
        return payload

    def inspect(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Bounded active-context view for this session only.

        Each span keeps the store's raw ``est_tokens`` (``chars/4``) and gains
        ``est_calibrated``: the same estimate after the learned ratio. Sizing an
        edit from the raw figure while the budget uses the calibrated one is the
        mismatch this field exists to end.
        """
        payload = self._call_store("inspect", cursor=cursor, limit=limit)
        items = payload.get("items")
        if isinstance(items, list):
            calibration = self._engine.calibration
            enriched: list[Any] = []
            for item in items:
                if isinstance(item, Mapping):
                    fresh = dict(item)
                    raw = fresh.get("est_tokens")
                    if isinstance(raw, int) and not isinstance(raw, bool):
                        fresh.setdefault("est_calibrated", calibration.adjust(raw))
                    enriched.append(fresh)
                else:
                    enriched.append(item)
            payload["items"] = enriched
        return payload

    def preview(self, operations: Sequence[Any]) -> dict[str, Any]:
        """Pure projection of a staged batch (no write, no revision bump)."""
        return self._engine.preview(operations)

    def stage(
        self,
        operations: Sequence[Any],
        *,
        expected_revision: int,
        txn_id: str,
        actor_origin: str = "agent",
    ) -> dict[str, Any]:
        """Stage operations against the current revision."""
        return self._call_store(
            "stage",
            list(operations),
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=actor_origin,
        )

    def commit(self, *, txn_id: str, expected_revision: int) -> dict[str, Any]:
        """Commit a staged transaction (idempotent replay by ``txn_id``)."""
        return self._call_store("commit", txn_id=txn_id, expected_revision=expected_revision)

    def discard(self, *, txn_id: str) -> None:
        """Drop a staged transaction without committing."""
        self._require_store().discard(txn_id=txn_id)

    def revert(
        self,
        *,
        target_revision: int,
        expected_revision: int,
        txn_id: str,
        actor_origin: str = "operator",
    ) -> dict[str, Any]:
        """Create a new head revision restoring an earlier projection."""
        return self._call_store(
            "revert",
            target_revision=target_revision,
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=actor_origin,
        )

    def search(self, query: str, *, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Bounded archive search, current session only."""
        return self._call_store("search", query, cursor=cursor, limit=limit)

    def retrieve(self, span_id: str) -> dict[str, Any]:
        """Exact archived detail for one span, current session only."""
        return self._call_store("retrieve", span_id)

    # -- proposals --------------------------------------------------------- #

    def create_proposal(
        self, operations: Sequence[Any], base_revision: int | None = None
    ) -> dict[str, Any]:
        """Persist an immutable aux proposal for ``operations`` at ``base_revision``.

        Plain mapping operations are stamped ``actor_origin="aux"``: the store
        refuses operations whose actor disagrees with the calling actor, and an
        auxiliary proposal is by definition an aux edit.

        The store owns proposal identity: the returned ``proposal_sha256`` is the
        store's digest, and ``accept_proposal`` requires exactly that value. The
        auxiliary layer's :func:`hermes_clm.auxiliary.canonical_proposal_sha256`
        is only a compatibility helper for callers that reviewed a proposal
        before the store owned identity — it is not an alternative accepted hash.
        """
        store = self._require_store()
        base = self._live_revision(store) if base_revision is None else int(base_revision)
        create = getattr(store, "create_proposal", None)
        if not callable(create):
            raise RuntimeError("clm store does not implement create_proposal()")
        plain: list[dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, Mapping):
                raise TypeError("proposal operations must be mappings")
            plain.append(
                {key: operation[key] for key in _PROPOSAL_OPERATION_FIELDS if key in operation}
            )
        payload = _payload(create(plain, base_revision=base))
        return payload

    def accept_proposal(
        self,
        *,
        proposal_id: str,
        proposal_sha256: str,
        base_revision: int,
        txn_id: str,
    ) -> dict[str, Any]:
        """Stage an accepted proposal; commit stays a separate step."""
        return self._call_store(
            "accept_proposal",
            proposal_id=proposal_id,
            proposal_sha256=proposal_sha256,
            base_revision=base_revision,
            txn_id=txn_id,
        )

    # -- operator authority ------------------------------------------------ #

    def unpin(
        self,
        span_ids: Sequence[str],
        *,
        expected_revision: int,
        txn_id: str,
        actor_origin: str = "operator",
    ) -> dict[str, Any]:
        """Remove pins. Operator authority only: agents can pin, never unpin.

        A batch goes to the store's atomic unpin (``operator_unpin_many``): one
        transaction, one revision, one ``txn_id``, all-or-nothing. An unknown
        span aborts the whole batch instead of leaving a partial unpin behind.
        """
        if str(actor_origin) != "operator":
            return {
                "ok": False,
                "errors": [
                    {
                        "code": "pin_authority",
                        "message": "only the operator may unpin",
                        "details": {"actor": str(actor_origin)},
                    }
                ],
                "receipts": [],
            }
        store = self._require_store()
        targets = [str(item) for item in span_ids]
        revision = int(expected_revision)

        def _unpin_result(result: dict[str, Any]) -> dict[str, Any]:
            applied = [dict(item) for item in result.get("applied") or ()]
            return {
                "ok": bool(result.get("ok", True)),
                "revision": int(result.get("revision", revision)),
                "applied": applied,
                "inert": [dict(item) for item in result.get("inert") or ()],
                "errors": [dict(item) for item in result.get("errors") or ()],
                "receipts": applied,
            }

        def _unpin_refused(exc: CLMError) -> dict[str, Any]:
            return {
                "ok": False,
                "revision": self._live_revision(store),
                "applied": [],
                "inert": [],
                "errors": [exc.to_dict()],
                "receipts": [],
            }

        # One call, one transaction: the store validates every target before
        # removing any pin, so a refusal cannot leave a partial unpin behind.
        batch = getattr(store, "operator_unpin_many", None)
        if not callable(batch):
            batch = getattr(store, "operator_unpin", None)
        if callable(batch):
            try:
                return _unpin_result(
                    _payload(batch(targets, expected_revision=revision, txn_id=txn_id))
                )
            except CLMError as exc:
                return _unpin_refused(exc)
        legacy = getattr(store, "unpin", None)
        if not callable(legacy):
            raise RuntimeError("clm store does not implement unpin()")
        # Contract-minimal stores only expose ``unpin``; it accepts the batch too,
        # so the caller still gets one revision for one txn id.
        try:
            return _unpin_result(
                _payload(
                    legacy(
                        targets,
                        expected_revision=revision,
                        txn_id=txn_id,
                        actor_origin="operator",
                    )
                )
            )
        except CLMError as exc:
            return _unpin_refused(exc)


class CLMEngine(_EngineBase):
    """Durable CLM projection engine.

    The host holds one shared plugin instance and calls :meth:`clone_for_agent`
    per agent: each clone reopens its own store handle and never shares SQLite
    objects or locks.
    """

    emit_automatic_compaction_status = False

    def __init__(
        self,
        *,
        config: Any | None = None,
        store_factory: Optional[Any] = None,
        hermes_home: Optional[str] = None,
        session_id: Optional[str] = None,
        logger: Optional[Any] = None,
        aux: AuxCompressor | None = None,
        raw_config: Mapping[str, Any] | None = None,
    ) -> None:
        # The plugin hands in the flat ``clm.*`` dict it read from the profile
        # config; keep that mapping so ToolLimits sees every declared key.
        self.config = EngineConfig.coerce(config)
        if raw_config is not None:
            self._raw_config: dict[str, Any] = dict(raw_config)
        elif isinstance(config, Mapping):
            self._raw_config = dict(config)
        else:
            self._raw_config = {f"clm.{key}": value for key, value in self.config.to_dict().items()}
        self._store_factory = store_factory or functools.partial(
            default_store_factory, config=self.config
        )
        self._hermes_home = hermes_home or self.config.hermes_home
        self._session_id = session_id
        self._store: Any = None
        #: Which session/home the attached store belongs to. A host that rotates or
        #: resumes a session id must never keep serving the previous store.
        self._store_session: Optional[str] = None
        self._store_home: Optional[str] = None
        #: Set by ``on_session_reset`` (``/new``, ``/resume``): the host asked for a
        #: fresh conversation, so the next session start must not adopt a rotation
        #: even when its ``old_session_id`` still matches the id this engine served.
        self._reset_pending = False
        #: ``{"code": ..., "message": ...}`` for the last failed attach, so a
        #: caller sees ``invalid_home`` / ``store_busy`` instead of "no store".
        self._store_error: Optional[Dict[str, Any]] = None
        #: Where the attached store's home came from: ``on_session_start``, ``config``
        #: or ``hermes_constants``. ``None`` until a home resolved.
        self._home_source: Optional[str] = None
        #: Memo for a home resolved from a *stable* source, keyed by its inputs.
        #: ``hermes_constants`` is deliberately not memoised: a multiplexed host
        #: rebinds the ambient profile, and a cached home would follow the wrong one.
        self._home_cache: Optional[Tuple[Tuple[Any, Any], Optional[str], str]] = None
        #: Code of the last refused stage/commit (``stale_revision``, ``store_busy``,
        #: ``read_only``…), so an aborted compaction names what refused it.
        self._last_commit_error: Optional[str] = None
        #: Code of the last failed durable-lineage write, so a child that cannot
        #: resolve its parent's archive is diagnosable instead of merely empty.
        self._last_lineage_error: Optional[str] = None
        self._logger = logger if logger is not None else _LOG
        self.calibration = TokenCalibration()
        self._tool_limits = ToolLimits.from_config(self._raw_config)
        # One auxiliary compressor per engine; clones share it so metric state is
        # one view of one aux budget (it only ever runs the compression task).
        self.aux: AuxCompressor = aux if aux is not None else self._build_aux()
        self._tool_handlers = ToolHandlers(
            self.service_for_session, limits=self._tool_limits, aux=self.aux
        )
        self._tool_specs: list[Any] | None = None
        # Per-model threshold overrides; the host assigns this after construction.
        self.model_thresholds: Dict[str, float] = {}
        self._config_threshold_percent = float(getattr(self, "threshold_percent", 0.75) or 0.75)
        self._base_threshold_percent = self._config_threshold_percent
        self._last_error: Optional[str] = None
        self._last_projection: Optional[ProjectionResult] = None
        #: Last compression outcome: cleared at the start of every ``compress()`` and
        #: readable from ``get_status()`` before the first one.
        self._last_compress_aborted = False
        self._last_summary_error: Optional[str] = None
        self._last_receipts: List[Dict[str, Any]] = []
        self._last_estimate = 0
        #: The last request this engine was asked to project, and the history it
        #: was aligned against. Kept (shallow copies of row references, never
        #: mutated) so ``clm_stage`` can price a batch *before* committing it:
        #: the projection is pure, so the preview needs no write.
        self._last_request: Optional[List[Mapping[str, Any]]] = None
        self._last_history: Optional[List[Mapping[str, Any]]] = None
        #: Memo of the store-derived compaction summary, keyed by the head
        #: revision it was read at. Compaction state only changes when a new
        #: revision lands, so this makes the per-turn notice check free.
        self._compaction_cache: Optional[Tuple[int, Optional[Dict[str, Any]]]] = None
        self._usage_receipts: List[Dict[str, Any]] = []
        self.last_cache_read_tokens = 0
        self.last_cache_write_tokens = 0
        self.last_aux_metrics: Dict[str, Any] = {}

    def _build_aux(self) -> AuxCompressor:
        """Aux compressor from ``clm.*``: compression task only, bounded timeout."""
        return AuxCompressor(
            None,
            timeout_seconds=bound_timeout(self.config.aux_timeout_seconds),
            limits=AuxLimits(
                max_spans=self._tool_limits.max_aux_spans,
                max_rewrite_bytes=self._tool_limits.max_rewrite_bytes,
                max_instruction_bytes=self._tool_limits.max_instruction_bytes,
                max_span_chunks=self._tool_limits.max_span_chunks,
                max_aux_calls=self._tool_limits.max_aux_calls,
            ),
            enabled=self.config.aux_enabled,
        )

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        """Short engine identifier used by host logs and config."""
        return "hermes-clm"

    @property
    def store(self) -> Any:
        """The attached session store, or ``None`` before a session starts."""
        return self._store

    @property
    def session_id(self) -> Optional[str]:
        """The host session this engine is attached to."""
        return self._session_id

    # ------------------------------------------------------------------ lifecycle

    def _record_error(self, exc: BaseException) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"
        sink = self._logger
        if sink is not None:
            try:
                sink.warning("hermes-clm: %s", self._last_error)
            except Exception:  # pragma: no cover - a broken host logger
                pass

    def _record_store_error(self, code: str, message: str) -> None:
        """Remember why the store could not be attached, with its typed code."""

        self._store_error = {"code": str(code), "message": " ".join(str(message).split())[:200]}

    def _resolve_home(self) -> Optional[str]:
        """The Hermes home this engine's store belongs to, or ``None``.

        Resolution order: an explicit attach (``on_session_start(hermes_home=…)``),
        then ``clm.hermes_home``, then the host's own ``get_hermes_home()`` — read
        lazily at open time, because a multiplexed process serves several profiles
        and the ambient home at import time is not necessarily this session's.
        The source is kept so a misattributed home stays diagnosable (status
        ``store_home_source``) instead of silently writing into another profile.

        A home that came from an explicit source is memoised on its inputs so the
        per-call ``(session, home)`` key check stays a tuple comparison; the
        ambient source is re-read every time, because that is exactly the value
        that changes when the host routes another profile through this process.
        """

        explicit = (self._hermes_home, self.config.hermes_home)
        cached = self._home_cache
        if cached is not None and cached[0] == explicit:
            self._home_source = cached[2]
            return cached[1]

        for source, candidate in (
            ("on_session_start", self._hermes_home),
            ("config", self.config.hermes_home),
        ):
            if isinstance(candidate, (str, os.PathLike)) and str(candidate).strip():
                self._home_source = source
                self._home_cache = (explicit, str(candidate), source)
                return str(candidate)
        try:  # pragma: no cover - exercised only inside a Hermes runtime
            from hermes_constants import get_hermes_home

            resolved = get_hermes_home()
        except Exception:
            self._home_source = None
            return None
        if not resolved:
            self._home_source = None
            return None
        self._home_source = "hermes_constants"
        return str(resolved)

    def _ensure_store(self) -> Any:
        """Open this session's store once; failures degrade to detached mode.

        The handle is keyed by ``(session_id, home)``: a host that rotates the
        session id after in-place compaction, or rebinds the same id under another
        profile's home, gets the store that key names instead of silently writing
        the new transcript into the previous archive. A failure to attach is
        recorded with the store's own error code, never flattened into "no store".
        """

        if not self._session_id:
            self._record_store_error("no_session", "no host session id has been attached yet")
            return None
        # Resolve exactly once per call: this runs on every ``select_context``, and
        # the ambient source (a multiplexed gateway) has to be re-read each time.
        home = self._resolve_home()
        if self._store is not None:
            if (self._store_session, self._store_home) == (self._session_id, home):
                return self._store
            # The host named a different session or home: this handle belongs to
            # another store, and SQLite's sibling flock allows one writer.
            self._close_store()
        self._store_error = None
        if not home:
            # The real store refuses a blank home (``invalid_home``); an injected
            # factory may not need one, so it still gets the call. Recording the
            # cause first keeps the failure typed either way.
            self._record_store_error(
                "invalid_home",
                "no Hermes home could be resolved for this session; refusing to open a "
                "store beside the process working directory",
            )
        try:
            store = self._store_factory(home, self._session_id)
        except Exception as exc:
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            message = str(getattr(exc, "message", "") or exc) or type(exc).__name__
            self._record_store_error(code, message)
            self._record_error(exc)
            return None
        self._store = store
        self._store_session = self._session_id
        self._store_home = home
        self._store_error = None
        return store

    def service_for_session(self, session_id: str | None = None) -> ToolService | None:
        """The scoped tool service for this engine's session, or ``None``.

        * no argument (or the engine's own session id): attach the store lazily
          and return a service bound to this engine;
        * any other session id: ``None``. An engine never serves a second
          session's store, so a subagent cannot read its parent's archive;
        * an unbound engine adopts the first explicit session id it is asked
          for, which is the host's lazy-init path for tool-first turns.
        """
        if session_id is not None:
            session_id = str(session_id)
            if self._session_id is None:
                if self._store is not None:  # pragma: no cover - defensive
                    return None
                self._session_id = session_id
                self._register_binding()
            elif session_id != self._session_id:
                return None
        if self._ensure_store() is None:
            return None
        return ToolService(self)

    # -- session → engine index --------------------------------------------- #

    def _register_binding(self) -> None:
        """Publish ``session_id → this engine`` for session-only surfaces."""

        session_id = self._session_id
        if not session_id:
            return
        with _ENGINE_BINDINGS_LOCK:
            _ENGINE_BINDINGS[str(session_id)] = self

    def _release_binding(self, session_id: str | None = None) -> None:
        """Drop this engine's binding (only when it is still the owner of the key)."""

        key = session_id or self._session_id
        if not key:
            return
        with _ENGINE_BINDINGS_LOCK:
            if _ENGINE_BINDINGS.get(str(key)) is self:
                del _ENGINE_BINDINGS[str(key)]

    def bind_session_state(
        self, session_db: Any = None, session_id: str | None = None, **kwargs: Any
    ) -> None:
        """Host seam: adopt the session id a cold/managed engine was built for.

        The gateway builds a one-off plugin engine for ``session.mutate(compress)``
        on a cold session and binds it with ``(session_db, session_id)`` before the
        forced ``compress()``; without this the engine has no session and refuses
        with ``no_session``. The host's ``session_db`` is deliberately not stored:
        this package reads and writes its own store only, never the host database.
        An engine already bound to a different session keeps its own binding — a
        bind can never steal another session's store.
        """

        if not session_id:
            return
        session_id = str(session_id)
        if self._session_id in (None, session_id):
            self._session_id = session_id
            self._register_binding()
        self._ensure_store()

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Attach the store for ``session_id``, surviving a compaction rotation.

        The host rotates the session id after an in-place compaction and tells the
        engine through this hook — ``on_session_start(new_id,
        boundary_reason="compression", old_session_id=old_id)`` — *without* an
        ``on_session_end``/``on_session_reset`` in between. Treating that as a new
        conversation would open an empty store for the child id and silently drop
        every committed edit, pin and archived span of the conversation it just
        compacted.

        A rotation is adopted only when **all** of these hold: the boundary is
        exactly ``"compression"``, the previous id is the session this engine is
        currently serving, and the open handle belongs to that same session. The
        store is kept, the new id is bound to it, and the relation is written to
        disk (``record_session_lineage``) so a *cold* engine for the child resolves
        the same archive after this process is gone. Any other id change — ``/new``,
        ``/resume``, ``/branch``, a stale notification naming someone else — is a
        genuinely different conversation and gets its own store, never an alias.
        """

        new_session_id = str(session_id) if session_id else self._session_id
        previous = kwargs.get("old_session_id") or kwargs.get("previous_session_id")
        boundary = kwargs.get("boundary_reason")
        hermes_home = kwargs.get("hermes_home")
        if hermes_home:
            self._hermes_home = str(hermes_home)

        adopted = self._adopts_rotation(new_session_id, previous, boundary)
        if adopted:
            child_id = str(new_session_id)
            self._release_binding(self._session_id)
            self._session_id = child_id
            self._store_session = child_id
            lineage = self._record_lineage(child_id, str(previous))
            self._receipt(
                {
                    "stage": "session_rotation",
                    "boundary": boundary,
                    "from": str(previous),
                    "to": child_id,
                    "store": "adopted",
                    "lineage": lineage,
                }
            )
        else:
            self._release_binding(self._session_id)
            self._session_id = new_session_id
        self._reset_pending = False
        self._register_binding()

        context_length = kwargs.get("context_length")
        if context_length:
            self.context_length = int(context_length)
            self.threshold_tokens = int(self.context_length * self.threshold_percent)
        self._ensure_store()

    def _adopts_rotation(self, new_session_id: Optional[str], previous: Any, boundary: Any) -> bool:
        """Whether a session-start notification is the host's post-compaction rotation.

        The gate is deliberately narrow, because adopting the wrong notification
        hands one conversation another conversation's archive. ``boundary_reason``
        must be exactly ``"compression"`` (the only value
        ``_notify_context_engine_compression_complete`` sends); the previous id must
        be the session this engine serves; and ``from == to`` adopts nothing.

        The open handle is *not* required: a competing process can adopt the
        continuation of a session it has not touched yet this turn, and an attach
        that failed (another writer holds the sibling lock) must still record the
        lineage rather than start a second archive under the child id. So an
        unopened store is attached first, and a failed attach leaves the ids to be
        rotated with a typed ``store_error``.
        """

        if not new_session_id or not previous:
            return False
        if str(boundary or "") != "compression":
            return False
        if str(previous) == str(new_session_id):
            return False
        if self._reset_pending:
            # The host reset this conversation; ids starting over is not a rename.
            return False
        if str(previous) != str(self._session_id):
            # A stale notification naming another session: not ours to adopt.
            return False
        if self._store is None:
            # Serving the parent but nothing attached yet: open it, then decide.
            self._ensure_store()
        if self._store is not None and self._store_session not in (None, self._session_id):
            return False
        return True

    def _record_lineage(self, child_session_id: str, parent_session_id: str) -> str:
        """Persist ``child → archive owner`` durably; returns a short outcome label.

        Called only from the adopted-rotation path. A failed write must not undo
        the in-memory adoption (this engine still serves the right store), so the
        failure is recorded and logged instead of raised — but it stays visible in
        ``/clm status`` as ``last_lineage_error``, because the *cold* child will
        then start with an empty archive.
        """

        home = self._store_home or self._resolve_home()
        if not home:
            self._last_lineage_error = "invalid_home"
            return "skipped:invalid_home"
        try:
            module = importlib.import_module(".store", __package__)  # lazy, like the factory
            result = module.record_session_lineage(
                home,
                child_session_id,
                parent_session_id,
                boundary="compression",
                engine_version=_ENGINE_VERSION,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            self._last_lineage_error = code
            self._record_error(exc)
            return f"failed:{code}"
        self._last_lineage_error = None
        return "written" if result.get("written") else "present"

    def on_session_end(self, session_id: str, messages: Sequence[Mapping[str, Any]]) -> None:
        """Flush the final transcript, then release the store handle."""
        try:
            if messages:
                self._ingest(messages)
        except Exception as exc:  # pragma: no cover - best effort at a boundary
            self._record_error(exc)
        self._release_binding()
        self._close_store()

    def on_session_reset(self) -> None:
        """Reset counters and reopen the store lazily for the same session id.

        The host calls this for ``/new`` and ``/resume``. The id stays — the store
        reopens lazily for it — but the rotation state does not: a compression
        notification that arrives after a reset is a new conversation, not a rename.
        """

        super().on_session_reset()
        self._last_estimate = 0
        self._last_projection = None
        self._last_receipts.clear()
        self._last_request = None
        self._last_history = None
        self._compaction_cache = None
        self._reset_pending = True
        self._release_binding()
        self._close_store()

    def _close_store(self) -> None:
        store, self._store = self._store, None
        self._store_session = None
        self._store_home = None
        self._compaction_cache = None
        if store is not None:
            try:
                store.close()
            except Exception as exc:  # pragma: no cover - close is best effort
                self._record_error(exc)

    def clone_for_agent(self) -> "CLMEngine":
        """A fresh engine for another agent: no shared store, same durable home.

        Nothing here deep-copies: the frozen config, the injected store factory
        and the aux compressor are shared by reference, and the clone starts with
        no store handle at all. The store is reopened for the clone's own session
        on first use, so two agents never share SQLite objects or locks.
        """
        clone = CLMEngine(
            config=self.config,
            store_factory=self._store_factory,
            hermes_home=self._hermes_home,
            session_id=None,
            logger=self._logger,
            aux=self.aux,
            raw_config=self._raw_config,
        )
        clone.context_length = self.context_length
        clone.threshold_percent = self.threshold_percent
        clone.threshold_tokens = self.threshold_tokens
        clone.last_prompt_tokens = self.last_prompt_tokens
        clone.last_completion_tokens = self.last_completion_tokens
        clone.last_total_tokens = self.last_total_tokens
        clone.last_cache_read_tokens = self.last_cache_read_tokens
        clone.last_cache_write_tokens = self.last_cache_write_tokens
        clone.model_thresholds = dict(self.model_thresholds)
        clone.calibration = TokenCalibration(self.calibration.ratio)
        clone.calibration.samples = self.calibration.samples
        return clone

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        """Recompute budget state for a model switch without importing host helpers."""
        self.context_length = int(context_length or 0)
        percent = float(self._config_threshold_percent)
        for pattern, value in sorted(
            (getattr(self, "model_thresholds", {}) or {}).items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if pattern and isinstance(model, str) and pattern in model:
                percent = float(value)
                break
        self._base_threshold_percent = percent
        self.threshold_percent = percent
        self.threshold_tokens = int(self.context_length * percent)

    # ------------------------------------------------------------------ store views

    def _ingest(self, messages: Sequence[Mapping[str, Any]]) -> bool:
        """Write-through archive of the current history (redaction is the store's job)."""
        store = self._ensure_store()
        if store is None:
            return False
        ingest = getattr(store, "ingest", None)
        if not callable(ingest):
            return False
        try:
            ingest(list(messages))
            return True
        except Exception as exc:
            self._record_error(exc)
            return False

    def _snapshot(self) -> Any:
        """The committed snapshot, or ``None`` when the store cannot supply one."""
        store = self._ensure_store()
        if store is None:
            return None
        snapshot_fn = getattr(store, "snapshot", None)
        if not callable(snapshot_fn):
            return None
        try:
            snapshot = snapshot_fn()
        except Exception as exc:
            self._record_error(exc)
            return None
        # Structural store corruption => refuse, never project a guessed subset.
        if snapshot is None or not isinstance(getattr(snapshot, "revision", None), int):
            self._last_error = "snapshot_unavailable"
            return None
        if getattr(snapshot, "spans", None) is None:
            self._last_error = "snapshot_unavailable"
            return None
        return snapshot

    def _project(
        self,
        request_messages: Sequence[Mapping[str, Any]],
        conversation_messages: Optional[Sequence[Mapping[str, Any]]],
        *,
        snapshot: Any,
        incoming_message: Optional[Mapping[str, Any]] = None,
    ) -> ProjectionResult:
        return project(
            request_messages,
            conversation_messages=conversation_messages,
            snapshot=snapshot,
            incoming_message=incoming_message,
            max_rewrite_bytes=self.config.max_rewrite_bytes,
        )

    def _receipt(self, payload: Dict[str, Any]) -> None:
        self._last_receipts.append(payload)
        if len(self._last_receipts) > RECEIPT_LIMIT:
            del self._last_receipts[: len(self._last_receipts) - RECEIPT_LIMIT]

    # ------------------------------------------------------------------ compaction visibility

    def compaction_summary(self, revision: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Store-derived summary of the newest compaction, or ``None``.

        ``revision`` is the head the caller already knows (a snapshot revision
        costs nothing to pass); ``None`` reads the live head. The answer is
        memoised per head revision: compaction state can only change when a new
        revision lands, so the per-turn notice check costs one query per
        revision, not one per turn.

        A store without ``last_compaction()`` (a contract-minimal fake, an older
        delivery) yields ``None`` rather than a raised exception: absent
        visibility must not break ``select_context``.
        """

        store = self._store
        if store is None:
            return None
        head = int(revision) if revision is not None else ToolService._live_revision(store)
        cache = self._compaction_cache
        if cache is not None and cache[0] == head:
            return cache[1]
        summary: Optional[Dict[str, Any]] = None
        reader = getattr(store, "last_compaction", None)
        if callable(reader):
            try:
                raw = reader()
            except Exception as exc:  # degradation, not a request failure
                self._record_error(exc)
                raw = None
            if isinstance(raw, Mapping):
                candidate = dict(raw)
                revision_value = candidate.get("revision")
                if isinstance(revision_value, int) and not isinstance(revision_value, bool):
                    summary = candidate
        self._compaction_cache = (head, summary)
        return summary

    def _remember_request(
        self,
        request: Sequence[Mapping[str, Any]],
        history: Sequence[Mapping[str, Any]],
    ) -> None:
        """Keep the last projected request (by reference) for a stage preview."""
        self._last_request = [row for row in request if isinstance(row, Mapping)]
        self._last_history = [row for row in history if isinstance(row, Mapping)]

    def preview(self, operations: Sequence[Any]) -> Dict[str, Any]:
        """Price a not-yet-committed batch against the last projected request.

        Pure: it reads the store snapshot, appends ``operations`` to a *local*
        view and projects. No stage, no commit, no revision bump. A batch the
        projector would refuse reports ``ok: False`` with its conflicts and
        ``saved: 0`` — the preview is advice, never a decision.

        ``before``/``after``/``saved`` are raw ``chars/4`` estimates (labelled
        ``estimator``); the same three numbers after calibration are under
        ``calibrated`` so a caller does not size an edit against a figure the
        budget would disagree with.
        """

        request = self._last_request
        if not request:
            return {
                "available": False,
                "reason": "no_request_observed",
                "hint": "the preview prices a batch against the last request this session projected",
            }
        snapshot = self._snapshot()
        if snapshot is None:
            return {"available": False, "reason": "store_unavailable"}
        if bool(getattr(snapshot, "operations_truncated", False)):
            return {"available": False, "reason": "operations_truncated"}
        view = self._local_view(snapshot, list(operations), "preview")
        result = self._project(request, self._last_history or request, snapshot=view)
        before = int(result.before_tokens)
        after = int(result.after_tokens) if result.ok else before
        payload: Dict[str, Any] = {
            "available": True,
            "ok": bool(result.ok),
            "before": before,
            "after": after,
            "saved": max(0, before - after),
            "applied": len(result.applied),
            "inert": len(result.inert),
            "conflicts": [dict(item) for item in result.conflicts],
            "estimator": "chars/4",
        }
        calibrated_before = self.calibration.adjust(before)
        calibrated_after = self.calibration.adjust(after)
        payload["calibrated"] = {
            "before": calibrated_before,
            "after": calibrated_after,
            "saved": max(0, calibrated_before - calibrated_after),
        }
        return payload

    # ------------------------------------------------------------------ projection

    def select_context(
        self,
        request_messages: List[Dict[str, Any]],
        *,
        conversation_messages: Optional[List[Dict[str, Any]]] = None,
        incoming_message: Optional[Dict[str, Any]] = None,
        budget_tokens: int = 0,
    ) -> Optional[List[Dict[str, Any]]]:
        """Apply the committed revision to a copy of this request.

        Returns ``None`` (request untouched, byte-identical, cache-safe) when
        nothing is committed, nothing applies, or the projection refuses.

        One exception, and it is revision-keyed rather than per-turn: after a
        compaction the request carries a single notice row naming the revision
        that archived the agent's earlier tool results. Its bytes are a pure
        function of that revision, so it is byte-identical on every turn until
        the next compaction - the prefix cache survives, and a mid-task agent
        can tell "my tool output was offloaded" from "I never ran that tool".
        """
        try:
            if not request_messages or not all(
                isinstance(row, Mapping) for row in request_messages
            ):
                return None
            if budget_tokens and not self.context_length:
                self.context_length = int(budget_tokens)
            history = list(conversation_messages or ())
            if not history:
                # No durable identity => nothing is editable; do not even open the store.
                return None
            # A host that echoes a previous projection back as the request must
            # not accumulate one notice per turn: strip our own rows first.
            request = _notice.split_notice_rows(request_messages)
            if not request:
                return None
            if not self._ingest(history):
                return None
            snapshot = self._snapshot()
            if snapshot is None:
                return None
            if bool(getattr(snapshot, "operations_truncated", False)):
                # The op log is capped and no longer describes the context.
                # Fail closed: leave the request byte-identical.
                self._last_error = "operations_truncated"
                return None
            revision = int(getattr(snapshot, "revision", 0) or 0)
            if revision <= 0 and not tuple(getattr(snapshot, "operations", ()) or ()):
                self._last_estimate = _budget.estimate_messages_tokens(request)
                self._remember_request(request, history)
                return None
            result = self._project(
                request, history, snapshot=snapshot, incoming_message=incoming_message
            )
            self._last_projection = result
            self._last_estimate = result.after_tokens or result.before_tokens
            self._receipt(result.to_dict())
            if not result.ok:
                self._last_error = "projection_refused"
                return None
            self._remember_request(request, history)
            notice = _notice.compaction_notice(self.compaction_summary(revision))
            if result.changed:
                messages = [dict(message) for message in result.messages]
            elif notice is not None:
                # Nothing to project, but the agent still has to learn that its
                # earlier tool output now lives in the archive.
                messages = self._identity_copy(request)
            else:
                return None
            if notice is not None:
                messages = _notice.with_notice(messages, notice)
                self._last_estimate = _budget.estimate_messages_tokens(messages)
            return messages
        except Exception as exc:
            self._record_error(exc)
            return None

    # ------------------------------------------------------------------ compression

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Never: CLM edits are agent decisions, not a threshold reaction."""
        return False

    def should_compress_info(
        self, prompt_tokens: Optional[int] = None
    ) -> Tuple[bool, Optional[str]]:
        """No automatic compression, so no block reason either."""
        return False, None

    def _identity_copy(self, messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        return [copy.deepcopy(dict(message)) for message in messages]

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
        bypass_cooldown: bool = False,
    ) -> List[Dict[str, Any]]:
        """Identity unless a manual ``force`` or provider-proven ``bypass_cooldown``.

        Manual (``force``, i.e. ``/compact``): apply the committed projection, and
        when that saves nothing, commit a **deterministic archive-backed offload**
        of the oldest unpinned tool groups until the manual target is met. No model
        call, no summary, no replacement of CLM state with prose. A manual
        compaction that cannot reduce anything says so (``aborted`` plus a typed
        reason) instead of returning "success" on a no-op.

        Overflow (``bypass_cooldown``): archive, then commit an archive-backed
        offload of the oldest unpinned tool groups until the reserve fits - never a
        user/system/pinned row.
        """

        self._last_compress_aborted = False
        self._last_summary_error = None
        self._last_commit_error = None
        if not force and not bypass_cooldown:
            return self._identity_copy(messages)
        if not messages:
            return []
        if bool(getattr(self._store, "read_only", False)):
            # An inspection/recovery attach has no writer: /compact here is a
            # policy refusal, and saying so is better than a generic commit error.
            return self._abort_compress(
                "store_read_only: this store is attached read-only", messages
            )

        try:
            self._ingest(messages)
            snapshot = self._snapshot()
            if snapshot is None:
                return self._abort_compress("store_unavailable", messages)
            result = self._project(messages, messages, snapshot=snapshot)
            self._last_projection = result
            self._receipt(result.to_dict())
            if not result.ok:
                codes = ",".join(sorted({str(item.get("code")) for item in result.conflicts}))
                return self._abort_compress(f"projection_refused:{codes}", messages)
            if bypass_cooldown:
                return self._overflow(messages, snapshot, result)
            return self._manual(messages, snapshot, result)
        except Exception as exc:
            self._record_error(exc)
            return self._abort_compress(f"compress_failed:{type(exc).__name__}", messages)

    def _manual(
        self,
        messages: Sequence[Mapping[str, Any]],
        snapshot: Any,
        result: ProjectionResult,
    ) -> List[Dict[str, Any]]:
        """Manual compaction: the projection first, then a real offload if needed.

        The host shows this result's ``before``/``after`` token counts, so every
        path here must either prove a saving or report why there is none:

        * committed edits already shrink the projection -> replay them;
        * otherwise plan whole unpinned tool groups (oldest first) to the manual
          target, commit one transaction, and replay the committed head;
        * nothing eligible -> identity plus ``manual_no_offloadable_groups``;
        * the store cannot commit -> identity: a local-only stub would be undone by
          the next ``select_context`` ingest, which is worse than honesty.
        """

        if result.after_tokens < result.before_tokens:
            self.compression_count += 1
            return [dict(message) for message in result.messages]

        need_tokens = self._manual_target_tokens(result.after_tokens)
        candidates = self._offload_candidates(messages, snapshot)
        plan = _budget.plan_offload(candidates, need_tokens=need_tokens)
        if not plan:
            return self._abort_compress("manual_no_offloadable_groups", messages)
        # Commit only against a plan that predicts a saving. Once committed the
        # archive *has* changed, so "nothing happened" is no longer true; the
        # planner's own invariant (every candidate frees tokens) is what makes the
        # prediction safe, and a zero-token plan is refused before the write.
        predicted = sum(int(getattr(candidate, "tokens", 0) or 0) for candidate in plan)
        if predicted <= 0:
            return self._abort_compress("manual_no_predicted_saving", messages)

        txn_id = "manual-" + uuid.uuid4().hex
        expected_revision = int(getattr(snapshot, "revision", 0) or 0)
        operations = [
            self._make_operation(
                kind="offload",
                span_ids=tuple(candidate.edit_span_ids),
                expected_revision=expected_revision,
                txn_id=txn_id,
                # The human typed /compact; the operator is the actor.
                actor_origin="operator",
            )
            for candidate in plan
        ]
        durable = self._commit_operations(
            snapshot, operations, txn_id=txn_id, actor_origin="operator"
        )
        if not durable:
            code = self._last_commit_error or "refused"
            return self._abort_compress(f"manual_commit_refused:{code}", messages)

        committed = self._snapshot()
        if committed is None:
            # The commit is in; only the reload failed. Replay what we can and say so.
            return self._committed_without_saving(
                plan, None, None, messages, "manual_reload_failed"
            )
        replayed = self._project(messages, messages, snapshot=committed)
        if not replayed.ok:
            codes = ",".join(sorted({str(item.get("code")) for item in replayed.conflicts}))
            return self._committed_without_saving(
                plan, committed, replayed, messages, f"manual_projection_refused:{codes}"
            )
        if replayed.after_tokens >= result.before_tokens:
            # The store accepted the transaction but the transcript did not shrink.
            # Never report a compaction that did not happen — and never report a
            # refusal either, because the archive now differs from what the operator
            # was told before /compact.
            return self._committed_without_saving(
                plan, committed, replayed, messages, "manual_committed_without_saving"
            )

        self._last_projection = replayed
        self.compression_count += 1
        self._receipt(
            {
                "stage": "manual",
                "durable": True,
                "revision": int(getattr(committed, "revision", 0) or 0),
                "offloaded": [candidate.group_id for candidate in plan],
                "needed": need_tokens,
                "predicted_saving": predicted,
                "before_tokens": result.before_tokens,
                "after_tokens": replayed.after_tokens,
            }
        )
        return [dict(message) for message in replayed.messages]

    def _committed_without_saving(
        self,
        plan: Sequence[Any],
        committed: Any,
        replayed: Any,
        messages: Sequence[Mapping[str, Any]],
        reason: str,
    ) -> List[Dict[str, Any]]:
        """A manual compaction that committed but could not be shown to save.

        The transaction is durable, so this is neither success nor a no-op: the
        archive was offloaded and the next projection will apply it, while this
        turn's transcript is unchanged. It is reported as a warning with the
        revision and group ids (``/clm status`` shows the same), never as
        ``compression_count`` growth and never as a silent "no changes".
        """

        self._last_summary_error = (
            f"{reason}: store revision {int(getattr(committed, 'revision', 0) or 0)} accepted "
            f"the offload of {len(plan)} group(s) but the projection did not shrink"
        )
        self._record_error(RuntimeError(self._last_summary_error))
        self._receipt(
            {
                "stage": "manual",
                "durable": True,
                "anomaly": reason,
                "revision": int(getattr(committed, "revision", 0) or 0),
                "offloaded": [getattr(candidate, "group_id", "") for candidate in plan],
                "after_tokens": int(getattr(replayed, "after_tokens", 0) or 0),
            }
        )
        if replayed is not None and getattr(replayed, "ok", False):
            self._last_projection = replayed
            return [dict(message) for message in replayed.messages]
        return self._identity_copy(messages)

    def _manual_target_tokens(self, tokens: int) -> int:
        """Tokens a manual compaction should free.

        Under pressure: exactly the overflow (restore the reserve, do not gut the
        window). Otherwise: a deterministic fraction of the projection, so
        ``/compact`` on a healthy session still does something measurable instead of
        reporting a no-op.
        """

        state = _budget.pressure_state(
            estimated_tokens=int(tokens),
            context_length=self.context_length,
            reserve_tokens=self.config.reserve_tokens,
            threshold_percent=self.threshold_percent,
            thresholds=self.config.pressure_thresholds,
            calibration=self.calibration,
            last_prompt_tokens=self.last_prompt_tokens,
        )
        if state.overflow_tokens > 0:
            return state.overflow_tokens
        ratio = float(self.config.manual_target_ratio or 0)
        if ratio <= 0:
            return 0
        return max(1, int(round(int(tokens) * ratio)))

    def _abort_compress(
        self, reason: str, messages: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Refuse the compaction in one line, keeping the store's own error code.

        ``reason`` is a stable CLM-side cause; when the store could not be attached
        the typed code and message are appended, so the operator reads
        ``store_unavailable:invalid_home: no Hermes home …`` instead of a bare
        "no store".
        """

        self._last_compress_aborted = True
        detail = reason
        store_error = self._store_error
        if isinstance(store_error, Mapping) and store_error.get("code"):
            code = str(store_error["code"])
            # "the engine has no usable store" reads differently from "the store
            # refused this edit"; keep them apart so the fix is obvious.
            prefix = "store_unavailable" if code in _STORE_ATTACH_CODES else "store_error"
            detail = f"{prefix}:{code}: {store_error.get('message', '')}".rstrip(": ")
        self._last_summary_error = detail
        self._record_error(RuntimeError(detail))
        return self._identity_copy(messages)

    def _overflow(
        self,
        messages: Sequence[Mapping[str, Any]],
        snapshot: Any,
        result: ProjectionResult,
    ) -> List[Dict[str, Any]]:
        """Deterministic archive-backed offload until the reserve fits."""
        state = _budget.pressure_state(
            estimated_tokens=result.after_tokens,
            context_length=self.context_length,
            reserve_tokens=self.config.reserve_tokens,
            threshold_percent=self.threshold_percent,
            thresholds=self.config.pressure_thresholds,
            calibration=self.calibration,
            last_prompt_tokens=self.last_prompt_tokens,
        )
        projected = [dict(message) for message in result.messages]
        if self.context_length <= 0 or state.fits:
            self._receipt({"stage": "overflow", "needed": 0, "budget": state.to_dict()})
            return projected

        candidates = self._offload_candidates(messages, snapshot)
        plan = _budget.plan_offload(candidates, need_tokens=state.overflow_tokens)
        if not plan:
            self._receipt(
                {
                    "stage": "overflow",
                    "needed": state.overflow_tokens,
                    "offloaded": [],
                    "budget": state.to_dict(),
                }
            )
            return projected

        txn_id = "overflow-" + uuid.uuid4().hex
        operations = [
            self._make_operation(
                kind="offload",
                # Store span ids, not host uids: they differ once an archive row
                # has been superseded, and the store only resolves span ids.
                span_ids=tuple(candidate.edit_span_ids),
                expected_revision=int(getattr(snapshot, "revision", 0) or 0),
                txn_id=txn_id,
                # "system" is a valid actor origin; overflow is engine policy, not
                # an agent decision, and the store rejects unknown actors.
                actor_origin="system",
            )
            for candidate in plan
        ]
        durable = self._commit_operations(
            snapshot, operations, txn_id=txn_id, actor_origin="system"
        )
        if durable:
            # Re-read the committed head: the pre-commit snapshot cannot show it.
            committed = self._snapshot()
            view = (
                committed
                if committed is not None
                else self._local_view(snapshot, operations, txn_id)
            )
        else:
            view = self._local_view(snapshot, operations, txn_id)
        replayed = self._project(messages, messages, snapshot=view)
        if not replayed.ok:
            return projected
        self._last_projection = replayed
        self.compression_count += 1
        final_state = _budget.pressure_state(
            estimated_tokens=replayed.after_tokens,
            context_length=self.context_length,
            reserve_tokens=self.config.reserve_tokens,
            threshold_percent=self.threshold_percent,
            thresholds=self.config.pressure_thresholds,
            calibration=self.calibration,
            last_prompt_tokens=self.last_prompt_tokens,
        )
        self._receipt(
            {
                "stage": "overflow",
                "durable": durable,
                "offloaded": [candidate.group_id for candidate in plan],
                "needed": state.overflow_tokens,
                "before_tokens": result.after_tokens,
                "after_tokens": replayed.after_tokens,
                "budget": final_state.to_dict(),
            }
        )
        return [dict(message) for message in replayed.messages]

    def _local_view(self, snapshot: Any, operations: Sequence[Any], txn_id: str) -> _SnapshotView:
        """Snapshot view with extra (non-durable) operations appended."""
        return _SnapshotView(
            revision=int(getattr(snapshot, "revision", 0) or 0),
            operations=tuple(getattr(snapshot, "operations", ()) or ()) + tuple(operations),
            spans=getattr(snapshot, "spans", {}) or {},
            pins=getattr(snapshot, "pins", ()) or (),
            head_hash=str(getattr(snapshot, "head_hash", "") or ""),
        )

    def _offload_candidates(
        self, messages: Sequence[Mapping[str, Any]], snapshot: Any
    ) -> List[_budget.OffloadCandidate]:
        """Tool-group costs in history order, with protection already applied."""
        from .identity import (
            is_editable_content,
            is_protected_message,
            role_of,
            tool_groups,
        )

        spans = getattr(snapshot, "spans", {}) or {}
        pin_ids = _pin_identifiers(getattr(snapshot, "pins", None))
        settled = _settled_control_spans(snapshot)
        by_uid: Dict[str, Any] = {}
        by_span_id: dict[str, Any] = {}
        for key, span in (spans or {}).items():
            uid = getattr(span, "uid", None) or (key if isinstance(key, str) else None)
            if uid:
                by_uid.setdefault(uid, span)
            span_id = getattr(span, "span_id", None) or (key if isinstance(key, str) else None)
            if span_id:
                by_span_id.setdefault(span_id, span)
        groups = tool_groups(messages)
        rows = list(messages)
        uid_rows: Dict[str, Mapping[str, Any]] = {}
        for row in rows:
            uid = row.get("message_uid") if isinstance(row, Mapping) else None
            if isinstance(uid, str) and uid:
                uid_rows.setdefault(uid, row)
        newest_tool_uid = None
        for row in reversed(rows):
            if role_of(row) == "tool" and isinstance(row.get("message_uid"), str):
                newest_tool_uid = row.get("message_uid")
                break
        candidates: List[_budget.OffloadCandidate] = []
        for order, (group_id, members) in enumerate(sorted(groups.members.items())):
            span_list = [by_uid.get(uid) for uid in members]
            protected = any(
                span is None
                or bool(getattr(span, "protected", False))
                or _span_state(span) != "active"
                or str(getattr(span, "span_id", "") or getattr(span, "uid", "") or "") in settled
                or role_of(getattr(span, "message", {}) or {}) in ("system", "developer", "user")
                or not is_editable_content((getattr(span, "message", {}) or {}).get("content"))
                for span in span_list
            )
            clipped = any(
                uid == newest_tool_uid
                or is_protected_message(
                    uid_rows.get(uid, {}),
                    pinned=uid in pin_ids,
                    newest_tool_result=uid == newest_tool_uid,
                )
                for uid in members
            )
            pinned = any(
                uid in pin_ids
                or getattr(by_uid.get(uid), "span_id", None) in pin_ids
                or getattr(by_uid.get(uid), "uid", None) in pin_ids
                for uid in members
            )
            tokens = sum(_budget.estimate_message_tokens(uid_rows.get(uid, {})) for uid in members)
            candidates.append(
                _budget.OffloadCandidate(
                    order=order,
                    group_id=group_id,
                    uids=tuple(members),
                    # Store-facing ids: the snapshot span id when archived, else
                    # the uid (which the projection also resolves).
                    span_ids=tuple(
                        str(
                            getattr(by_uid.get(uid), "span_id", None)
                            or getattr(by_uid.get(uid), "uid", None)
                            or uid
                        )
                        for uid in members
                    ),
                    tokens=tokens,
                    protected=protected or clipped,
                    pinned=pinned,
                )
            )
        return candidates

    # ------------------------------------------------------------------ store writes

    def _make_operation(self, **kwargs: Any) -> Any:
        """Construct a store ``EditOperation`` when possible, else a structural spec."""
        try:  # pragma: no cover - depends on sibling delivery branch
            module = importlib.import_module(".models", __package__)

            return module.EditOperation(**kwargs)
        except Exception:
            return OperationSpec(**kwargs)

    def _commit_operations(
        self,
        snapshot: Any,
        operations: Sequence[Any],
        *,
        txn_id: str,
        actor_origin: str,
    ) -> bool:
        """Stage and commit through the store contract; ``False`` when unsupported."""
        store = self._ensure_store()
        if store is None:
            return False
        stage = getattr(store, "stage", None)
        commit = getattr(store, "commit", None)
        if not callable(stage) or not callable(commit):
            return False
        revision = int(getattr(snapshot, "revision", 0) or 0)
        try:
            staged = stage(
                list(operations),
                expected_revision=revision,
                txn_id=txn_id,
                actor_origin=actor_origin,
            )
            if not _result_ok(staged, default=False):
                self._last_commit_error = _result_error_code(staged) or "stage_refused"
                return False
            committed = commit(txn_id=txn_id, expected_revision=revision)
            if not _result_ok(committed, default=False):
                self._last_commit_error = _result_error_code(committed) or "commit_refused"
                return False
            self._last_commit_error = None
            return True
        except Exception as exc:
            self._last_commit_error = str(getattr(exc, "code", "") or type(exc).__name__)
            self._record_error(exc)
            return False

    # ------------------------------------------------------------------ accounting

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Track real usage and calibrate the rough estimator against it."""
        if not isinstance(usage, Mapping):
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
        self.last_prompt_tokens = prompt
        self.last_completion_tokens = completion
        self.last_total_tokens = total
        cache_read = int(usage.get("cache_read_tokens") or 0)
        cache_write = int(usage.get("cache_write_tokens") or 0)
        self.last_cache_read_tokens = cache_read
        self.last_cache_write_tokens = cache_write
        if prompt > 0 and self._last_estimate > 0:
            self.calibration.observe(self._last_estimate, prompt)
        self._usage_receipts.append(
            {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "estimated_tokens": self._last_estimate,
            }
        )
        if len(self._usage_receipts) > RECEIPT_LIMIT:
            del self._usage_receipts[: len(self._usage_receipts) - RECEIPT_LIMIT]

    def budget_state(
        self, messages: Optional[Sequence[Mapping[str, Any]]] = None
    ) -> Dict[str, Any]:
        """The budget number used for edit validation, for status/tool receipts."""
        estimated = (
            _budget.estimate_messages_tokens(messages)
            if messages is not None
            else self._last_estimate
        )
        return _budget.pressure_state(
            estimated_tokens=estimated,
            context_length=self.context_length,
            reserve_tokens=self.config.reserve_tokens,
            threshold_percent=self.threshold_percent,
            thresholds=self.config.pressure_thresholds,
            calibration=self.calibration,
            last_prompt_tokens=self.last_prompt_tokens,
        ).to_dict()

    def get_status(self) -> Dict[str, Any]:
        """Host status plus CLM revision, budget, receipts and last error."""
        status = dict(super().get_status())
        budget = self.budget_state()
        cached_compaction = self._compaction_cache
        status.update(
            {
                "engine": self.name,
                # Which build is live in this process: a long-lived gateway keeps the
                # modules it imported, so a fix is only observable after a restart.
                "engine_version": _ENGINE_VERSION,
                "session_id": self._session_id,
                "revision": int(getattr(self._last_projection, "revision", 0) or 0),
                "store": "attached" if self._store is not None else "detached",
                "store_error": dict(self._store_error) if self._store_error else None,
                "store_session": self._store_session,
                # The session id the attached archive is physically filed under; differs
                # from ``session_id`` only for the child of a compression rotation.
                "store_root_session": getattr(self._store, "session_id", None),
                # A label, never the path: status crosses into host messages.
                "store_home_source": self._home_source,
                # The three token numbers, each under its own label. ``budget`` keeps
                # the figures edit validation uses; this vocabulary makes them
                # comparable instead of leaving one ambiguous "tokens" figure.
                "tokens": token_vocabulary(
                    spans=int(budget.get("raw_estimated_tokens", 0) or 0),
                    calibration=self.calibration,
                    actual=self.last_prompt_tokens,
                ),
                # Store-derived; ``ToolService.status`` refreshes it, this read is
                # the cached view so host status calls stay free of store IO.
                "last_compaction": cached_compaction[1] if cached_compaction else None,
                # Why the last /compact produced no reduction (or committed without one).
                "last_compress_aborted": bool(self._last_compress_aborted),
                "last_compress_error": self._last_summary_error,
                # Set when the durable child→parent lineage could not be written: the
                # live engine is fine, a cold child would start empty.
                "last_lineage_error": self._last_lineage_error,
                "budget": budget,
                "calibration": self.calibration.to_dict(),
                "cache_read_tokens": self.last_cache_read_tokens,
                "cache_write_tokens": self.last_cache_write_tokens,
                "compression_count": self.compression_count,
                "last_error": self._last_error,
                "projection": self._last_projection.to_dict() if self._last_projection else None,
                "receipts": list(self._last_receipts),
                "aux": self.aux.metrics(),
                "config": self.config.to_dict(),
            }
        )
        return status

    # ------------------------------------------------------------------ tools

    def tool_service(self) -> ToolService:
        """Scoped store access for the tool handlers."""
        return ToolService(self)

    def tool_specs(self) -> list[Any]:
        """Tool advertisements (``ToolSpec``) for this engine, built once."""
        if self._tool_specs is None:
            self._tool_specs = list(build_tool_specs(self._tool_handlers))
        return list(self._tool_specs)

    def _tool_handlers_by_name(self) -> dict[str, Any]:
        return {spec.name: spec.handler for spec in self.tool_specs()}

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """The ``clm_*`` schemas; empty (never raising) if the tools module fails."""
        try:
            return [dict(spec.schema) for spec in self.tool_specs()]
        except Exception as exc:
            self._record_error(exc)
            return []

    def handle_tool_call(
        self,
        name: str,
        args: Any = None,
        messages: Sequence[Mapping[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        """Dispatch one ``clm_*`` tool call; always returns a JSON string.

        The handler maps to this engine's scoped service, so a call can only see
        the session this engine owns. Extra ``session_id`` from the host is passed
        through unchanged: a non-matching session resolves to ``None`` and gets a
        ``no_session`` error instead of another session's archive.
        """
        try:
            handler = self._tool_handlers_by_name().get(str(name))
            if handler is None:
                message = f"Unknown context engine tool: {name}"
                return json.dumps(
                    {
                        "ok": False,
                        "error": message,
                        "errors": [{"code": "unknown_tool", "message": message}],
                    }
                )
            payload: dict[str, Any]
            if isinstance(args, Mapping):
                payload = dict(args)
            elif isinstance(args, (str, bytes)):
                try:
                    decoded = _decode_tool_arguments(args)
                except (TypeError, ValueError):
                    return _invalid_arguments(name, "arguments are not valid JSON")
                if not isinstance(decoded, Mapping):
                    return _invalid_arguments(name, "arguments did not decode to an object")
                payload = dict(decoded)
            elif args is None:
                payload = {}
            else:
                # A list/scalar payload is a caller bug, not an empty request:
                # refusing keeps a mistyped edit from silently doing nothing.
                return _invalid_arguments(
                    name, f"arguments must be an object, got {type(args).__name__}"
                )
            if messages:
                # A mid-turn tool call must see the rows that already arrived.
                self._ingest(messages)
            call_kwargs: dict[str, Any] = {"session_id": kwargs.pop("session_id", None)}
            if messages is not None:
                call_kwargs["messages"] = messages
            call_kwargs.update(kwargs)
            return str(handler(payload, **call_kwargs))
        except Exception as exc:
            self._record_error(exc)
            return json.dumps(
                {"ok": False, "error": f"clm tool dispatch failed: {type(exc).__name__}"}
            )
