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
import importlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import budget as _budget
from .budget import TokenCalibration
from .projection import DEFAULT_MAX_REWRITE_BYTES, ProjectionResult, project

#: Default reserve kept free for the next completion.
DEFAULT_RESERVE_TOKENS = _budget.DEFAULT_RESERVE_TOKENS
#: Default pressure thresholds (fractions of the raw context length).
DEFAULT_PRESSURE_THRESHOLDS: Tuple[float, ...] = _budget.DEFAULT_PRESSURE_THRESHOLDS
#: How many recent projection/usage receipts the engine keeps in memory for status.
RECEIPT_LIMIT = 20


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

    def should_compress_info(self, prompt_tokens: Optional[int] = None) -> Tuple[bool, Optional[str]]:
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

    def get_automatic_compaction_status_message(self, *, phase: str, default_message: str, **context: Any):
        """This engine never compacts automatically, so it never reports doing so."""
        return default_message if self.emit_automatic_compaction_status else None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """No tools by default."""
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Reject unknown tools with a JSON error string."""
        return json.dumps({"error": f"Unknown context engine tool: {name}"})

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


_EngineBase = _load_engine_base()


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
    max_rewrite_bytes: int = DEFAULT_MAX_REWRITE_BYTES
    max_aux_spans: int = 8
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
            max_rewrite_bytes=int(_config_value(source, "max_rewrite_bytes", DEFAULT_MAX_REWRITE_BYTES)),
            max_aux_spans=int(_config_value(source, "max_aux_spans", 8)),
            hermes_home=_config_value(source, "hermes_home", None),
        )

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
    actor_origin: str = "engine"
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


def default_store_factory(hermes_home: Optional[str], session_id: str, *, read_only: bool = False):
    """Open a session store lazily.

    Imported inside the call so this package imports without the store package
    present. The store is never opened read-write by a projection.
    """
    module = importlib.import_module(".store", __package__)  # lazy: separate delivery unit

    return module.ContextStore.for_session(hermes_home or "", session_id, read_only=read_only)


def _result_ok(result: Any, default: bool = True) -> bool:
    """Read ``ok`` from a store result mapping or object."""
    if result is None:
        return default
    if isinstance(result, Mapping):
        return bool(result.get("ok", default))
    return bool(getattr(result, "ok", default))


class ToolService:
    """Scoped store access for tool handlers.

    Tool handlers never see ``session_id``, paths, provider or model settings;
    they call these methods, which are bound to this engine's single session.
    """

    def __init__(self, engine: "CLMEngine") -> None:
        self._engine = engine

    def _require_store(self) -> Any:
        store = self._engine.store
        if store is None:
            raise RuntimeError("clm store is not attached for this session")
        return store

    def status(self) -> Dict[str, Any]:
        """Engine status including revision and budget."""
        return self._engine.get_status()

    def inspect(self, *, cursor: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        """Bounded active-context view for this session only."""
        return self._require_store().inspect(cursor=cursor, limit=limit)

    def stage(
        self,
        operations: Sequence[Any],
        *,
        expected_revision: int,
        txn_id: str,
        actor_origin: str = "agent",
    ) -> Any:
        """Stage operations against the current revision."""
        return self._require_store().stage(
            operations,
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=actor_origin,
        )

    def commit(self, *, txn_id: str, expected_revision: int) -> Any:
        """Commit a staged transaction (idempotent replay by ``txn_id``)."""
        return self._require_store().commit(txn_id=txn_id, expected_revision=expected_revision)

    def discard(self, *, txn_id: str) -> None:
        """Drop a staged transaction without committing."""
        self._require_store().discard(txn_id=txn_id)

    def revert(
        self, *, target_revision: int, expected_revision: int, txn_id: str, actor_origin: str = "operator"
    ) -> Any:
        """Create a new head revision restoring an earlier projection."""
        return self._require_store().revert(
            target_revision=target_revision,
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=actor_origin,
        )

    def search(self, query: str, *, cursor: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        """Bounded archive search, current session only."""
        return self._require_store().search(query, cursor=cursor, limit=limit)

    def retrieve(self, span_id: str) -> Dict[str, Any]:
        """Exact archived detail for one span, current session only."""
        return self._require_store().retrieve(span_id)


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
        config: Optional[EngineConfig] = None,
        store_factory: Optional[Any] = None,
        hermes_home: Optional[str] = None,
        session_id: Optional[str] = None,
        logger: Optional[Any] = None,
    ) -> None:
        self.config = config or EngineConfig()
        self._store_factory = store_factory or default_store_factory
        self._hermes_home = hermes_home or self.config.hermes_home
        self._session_id = session_id
        self._store: Any = None
        self._logger = logger
        self.calibration = TokenCalibration()
        # Per-model threshold overrides; the host assigns this after construction.
        self.model_thresholds: Dict[str, float] = {}
        self._config_threshold_percent = float(getattr(self, "threshold_percent", 0.75) or 0.75)
        self._base_threshold_percent = self._config_threshold_percent
        self._last_error: Optional[str] = None
        self._last_projection: Optional[ProjectionResult] = None
        self._last_receipts: List[Dict[str, Any]] = []
        self._last_estimate = 0
        self._usage_receipts: List[Dict[str, Any]] = []
        self.last_cache_read_tokens = 0
        self.last_cache_write_tokens = 0
        self.last_aux_metrics: Dict[str, Any] = {}

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
        logger = self._logger
        if logger is not None:  # pragma: no cover - host logging only
            try:
                logger.warning("hermes-clm: %s", self._last_error)
            except Exception:
                pass

    def _ensure_store(self) -> Any:
        """Open this session's store once; failures degrade to detached mode."""
        if self._store is not None:
            return self._store
        if not self._session_id:
            return None
        try:
            self._store = self._store_factory(self._hermes_home, self._session_id)
        except Exception as exc:
            self._record_error(exc)
            return None
        return self._store

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Lazily attach the store for ``session_id`` (host calls this once per session)."""
        self._session_id = session_id or self._session_id
        hermes_home = kwargs.get("hermes_home")
        if hermes_home:
            self._hermes_home = str(hermes_home)
        context_length = kwargs.get("context_length")
        if context_length:
            self.context_length = int(context_length)
            self.threshold_tokens = int(self.context_length * self.threshold_percent)
        self._ensure_store()

    def on_session_end(self, session_id: str, messages: Sequence[Mapping[str, Any]]) -> None:
        """Flush the final transcript, then release the store handle."""
        try:
            if messages:
                self._ingest(messages)
        except Exception as exc:  # pragma: no cover - best effort at a boundary
            self._record_error(exc)
        self._close_store()

    def on_session_reset(self) -> None:
        """Reset counters and reopen the store lazily for the same session id."""
        super().on_session_reset()
        self._last_estimate = 0
        self._last_projection = None
        self._last_receipts.clear()
        self._close_store()

    def _close_store(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception as exc:  # pragma: no cover - close is best effort
                self._record_error(exc)

    def clone_for_agent(self) -> "CLMEngine":
        """A fresh engine for another agent: no shared store, same durable home."""
        clone = type(self)(
            config=self.config,
            store_factory=self._store_factory,
            hermes_home=self._hermes_home,
            session_id=None,
            logger=self._logger,
        )
        clone.context_length = self.context_length
        clone.threshold_percent = self.threshold_percent
        clone.threshold_tokens = self.threshold_tokens
        clone.last_prompt_tokens = self.last_prompt_tokens
        clone.last_completion_tokens = self.last_completion_tokens
        clone.last_total_tokens = self.last_total_tokens
        clone.last_cache_read_tokens = self.last_cache_read_tokens
        clone.last_cache_write_tokens = self.last_cache_write_tokens
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
        """
        try:
            if not request_messages or not all(isinstance(row, Mapping) for row in request_messages):
                return None
            if budget_tokens and not self.context_length:
                self.context_length = int(budget_tokens)
            history = list(conversation_messages or ())
            if not history:
                # No durable identity => nothing is editable; do not even open the store.
                return None
            if not self._ingest(history):
                return None
            snapshot = self._snapshot()
            if snapshot is None:
                return None
            if int(getattr(snapshot, "revision", 0) or 0) <= 0 and not tuple(
                getattr(snapshot, "operations", ()) or ()
            ):
                self._last_estimate = _budget.estimate_messages_tokens(request_messages)
                return None
            result = self._project(
                request_messages, history, snapshot=snapshot, incoming_message=incoming_message
            )
            self._last_projection = result
            self._last_estimate = result.after_tokens or result.before_tokens
            self._receipt(result.to_dict())
            if not result.ok:
                self._last_error = "projection_refused"
                return None
            if not result.changed:
                return None
            return [dict(message) for message in result.messages]
        except Exception as exc:
            self._record_error(exc)
            return None

    # ------------------------------------------------------------------ compression

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Never: CLM edits are agent decisions, not a threshold reaction."""
        return False

    def should_compress_info(self, prompt_tokens: Optional[int] = None) -> Tuple[bool, Optional[str]]:
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

        Manual: return the current committed projection (report no change when it
        saves nothing). Overflow: archive, then commit an archive-backed offload
        of the oldest unpinned tool groups until the reserve fits - never a
        user/system/pinned row.
        """
        self._last_compress_aborted = False
        if not force and not bypass_cooldown:
            return self._identity_copy(messages)
        if not messages:
            return []

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
            if result.after_tokens < result.before_tokens:
                self.compression_count += 1
                return [dict(message) for message in result.messages]
            return self._identity_copy(messages)
        except Exception as exc:
            self._record_error(exc)
            return self._identity_copy(messages)

    def _abort_compress(self, reason: str, messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        self._last_compress_aborted = True
        self._last_summary_error = reason
        self._record_error(RuntimeError(reason))
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
                {"stage": "overflow", "needed": state.overflow_tokens, "offloaded": [], "budget": state.to_dict()}
            )
            return projected

        txn_id = "overflow-" + uuid.uuid4().hex
        operations = [
            self._make_operation(
                kind="offload",
                span_ids=tuple(candidate.uids),
                expected_revision=int(getattr(snapshot, "revision", 0) or 0),
                txn_id=txn_id,
                actor_origin="engine:overflow",
            )
            for candidate in plan
        ]
        durable = self._commit_operations(
            snapshot, operations, txn_id=txn_id, actor_origin="engine:overflow"
        )
        view = snapshot if durable else self._local_view(snapshot, operations, txn_id)
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
        pins = getattr(snapshot, "pins", None)
        pin_ids: set = set()
        if isinstance(pins, Mapping):
            pin_ids = {str(key) for key in pins}
        elif isinstance(pins, (list, tuple, set, frozenset)):
            pin_ids = {str(item) for item in pins}
        by_uid: Dict[str, Any] = {}
        for key, span in (spans or {}).items():
            uid = getattr(span, "uid", None) or (key if isinstance(key, str) else None)
            if uid:
                by_uid.setdefault(uid, span)
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
                or role_of(getattr(span, "message", {}) or {}) in ("system", "developer", "user")
                or not is_editable_content((getattr(span, "message", {}) or {}).get("content"))
                for span in span_list
            )
            clipped = any(
                uid == newest_tool_uid
                or is_protected_message(uid_rows.get(uid, {}), pinned=uid in pin_ids, newest_tool_result=uid == newest_tool_uid)
                for uid in members
            )
            pinned = any(
                uid in pin_ids
                or getattr(by_uid.get(uid), "span_id", None) in pin_ids
                for uid in members
            )
            tokens = sum(_budget.estimate_message_tokens(uid_rows.get(uid, {})) for uid in members)
            candidates.append(
                _budget.OffloadCandidate(
                    order=order,
                    group_id=group_id,
                    uids=tuple(members),
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
                return False
            committed = commit(txn_id=txn_id, expected_revision=revision)
            return _result_ok(committed, default=False)
        except Exception as exc:
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

    def budget_state(self, messages: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
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
        status.update(
            {
                "engine": self.name,
                "session_id": self._session_id,
                "revision": int(getattr(self._last_projection, "revision", 0) or 0),
                "store": "attached" if self._store is not None else "detached",
                "budget": self.budget_state(),
                "calibration": self.calibration.to_dict(),
                "cache_read_tokens": self.last_cache_read_tokens,
                "cache_write_tokens": self.last_cache_write_tokens,
                "compression_count": self.compression_count,
                "last_error": self._last_error,
                "projection": self._last_projection.to_dict() if self._last_projection else None,
                "receipts": list(self._last_receipts),
                "config": self.config.to_dict(),
            }
        )
        return status

    # ------------------------------------------------------------------ tools

    def tool_service(self) -> ToolService:
        """Scoped store access for the tool handlers."""
        return ToolService(self)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Delegate tool schemas to the tools module when it is installed."""
        try:
            _tools = importlib.import_module(".tools", __package__)

            provider = getattr(_tools, "get_tool_schemas", None)
            if callable(provider):
                return [dict(schema) for schema in (provider(self) or [])]
        except Exception as exc:
            self._record_error(exc)
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Delegate a tool call to the tools module; always returns JSON text."""
        try:
            _tools = importlib.import_module(".tools", __package__)

            handler = getattr(_tools, "handle_tool_call", None)
            if callable(handler):
                return str(handler(self, name, args, **kwargs))
        except Exception as exc:
            self._record_error(exc)
            return json.dumps({"error": f"clm tool dispatch failed: {type(exc).__name__}"})
        return json.dumps({"error": f"Unknown context engine tool: {name}"})
