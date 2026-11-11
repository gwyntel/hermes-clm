"""Strict JSON tool handlers for Hermes CLM.

Every handler is a pure adapter: it validates its arguments, calls one scoped
service method, and returns a JSON *string*. Nothing here imports Hermes, reads
paths, or knows a provider, model, base URL or API key — the tool schemas carry
no such fields either, so a model cannot route an auxiliary call from a tool
call.

Service seam
------------
Handlers talk to a small scoped service object supplied per session. The store
owns persistence and conflict validation; this module never opens it:

    status() -> mapping
    inspect(*, cursor=None, limit=50) -> mapping
    stage(operations, *, expected_revision, txn_id, actor_origin) -> mapping
    commit(*, txn_id, expected_revision) -> mapping
    discard(*, txn_id) -> None
    revert(*, target_revision, expected_revision, txn_id, actor_origin) -> mapping
    search(query, *, cursor=None, limit=20) -> mapping
    retrieve(span_id) -> mapping
    create_proposal(operations, *, base_revision) -> proposal
    accept_proposal(*, proposal_id, proposal_sha256, base_revision, txn_id) -> mapping
    revision -> int                       (optional; status()["revision"] is authoritative)

Results may be plain mappings, dataclasses, or objects exposing ``to_dict()``;
:func:`_result_mapping` normalises them. Privileged operations (operator unpin)
are *not* exposed as tools — see :mod:`hermes_clm.commands`.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AGENT_ORIGIN",
    "DEDICATED_KINDS",
    "SPAN_ID_PATTERN",
    "STAGE_KINDS",
    "TOOLSET",
    "ToolHandlers",
    "ToolLimits",
    "ToolSpec",
    "build_tool_specs",
]

TOOLSET = "clm"
AGENT_ORIGIN = "agent"

#: Operation kinds the general ``clm_stage`` tool accepts. ``pin`` and
#: ``boundary`` have dedicated tools so their semantics are not blurred into a
#: free-form batch; ``unpin`` is operator-only and never agent-reachable.
STAGE_KINDS = frozenset({"keep", "drop", "rewrite", "offload", "restore"})
DEDICATED_KINDS = {"pin": "clm_pin", "boundary": "clm_boundary"}

#: Public span-ID format: no whitespace, no separators that could be read as a path.
SPAN_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SPAN_ID_RE = re.compile(SPAN_ID_PATTERN)
_TXN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Absolute POSIX and Windows paths are stripped from model-visible error text:
# store internals must never leak filesystem layout through a tool result.
_PATH_RE = re.compile(r"(?<![\w.:/])(?:/[\w.@%+-]+){2,}|[A-Za-z]:\\[^\s\"']+")
_MAX_ERROR_CHARS = 512
_MAX_LABEL_CHARS = 200


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolLimits:
    """Bounds applied by handlers before anything reaches the store."""

    inspect_limit_max: int = 200
    search_limit_max: int = 50
    max_operations: int = 64
    max_span_ids_per_operation: int = 32
    max_rewrite_bytes: int = 8000
    max_aux_spans: int = 12
    max_instruction_bytes: int = 600

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> ToolLimits:
        """Build limits from a flat ``clm.*`` mapping; unknown values keep defaults."""

        defaults = cls()
        if not isinstance(config, Mapping):
            return defaults
        values: dict[str, int] = {}
        for field in ("max_rewrite_bytes", "max_aux_spans", "max_operations"):
            raw = config.get(f"clm.{field}", config.get(field))
            if isinstance(raw, bool):
                continue
            if isinstance(raw, int) and raw > 0:
                values[field] = raw
            elif isinstance(raw, str) and raw.strip().isdigit() and int(raw) > 0:
                values[field] = int(raw)
        return dataclasses.replace(defaults, **values)


@dataclass(frozen=True)
class ToolSpec:
    """One tool advertisement: registry name, schema and JSON-returning handler."""

    name: str
    schema: Mapping[str, Any]
    handler: Callable[..., str]
    description: str = ""


# --------------------------------------------------------------------------- #
# Payloads and validation
# --------------------------------------------------------------------------- #


def _dump(payload: Mapping[str, Any]) -> str:
    """Serialise a handler payload; always a JSON string, never an exception."""

    return json.dumps(payload, ensure_ascii=False, default=str, sort_keys=False)


def _bounded(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sanitise_exception(exc: BaseException) -> str:
    """Describe an exception for the model without filesystem or URL detail."""

    return _bounded(_PATH_RE.sub("<path>", f"{type(exc).__name__}: {exc}"))


def error_payload(
    code: str, message: str, *, hint: str | None = None, **extra: Any
) -> dict[str, Any]:
    """Uniform failure body: ``ok``/``error``/``errors``/``receipts``."""

    payload: dict[str, Any] = {
        "ok": False,
        "error": _bounded(message),
        "errors": [{"code": code, "message": _bounded(message)}],
        "receipts": [],
    }
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


class _Invalid(ValueError):
    """Rejected arguments; carries a machine-readable code and optional hint."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint

    def payload(self) -> dict[str, Any]:
        return error_payload(self.code, str(self), hint=self.hint)


def _guarded(method: Callable[..., Mapping[str, Any]]) -> Callable[..., str]:
    """Convert any raised exception into a JSON error body.

    Tool dispatch already catches exceptions, but a handler must stay within its
    own contract: one JSON string in, one JSON string out.
    """

    @functools.wraps(method)
    def wrapper(self: ToolHandlers, args: Any = None, **kwargs: Any) -> str:
        try:
            if args is None:
                args = {}
            if not isinstance(args, Mapping):
                raise _Invalid(
                    "invalid_arguments",
                    "arguments must be a JSON object",
                )
            payload = method(self, dict(args), **kwargs)
            if not isinstance(payload, Mapping):
                raise _Invalid("internal_error", "handler produced a non-mapping result")
            return _dump(payload)
        except _Invalid as exc:
            return _dump(exc.payload())
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the model
            return _dump(error_payload("internal_error", _sanitise_exception(exc)))

    return wrapper


def _check_keys(
    args: Mapping[str, Any],
    *,
    allowed: Sequence[str],
    required: Sequence[str] = (),
) -> None:
    """Reject unknown fields and missing required fields (strict schemas)."""

    unknown = sorted(set(args) - set(allowed))
    if unknown:
        raise _Invalid(
            "unknown_field",
            f"unsupported field(s): {', '.join(unknown)}",
            hint=f"allowed fields: {', '.join(sorted(allowed)) or '(none)'}",
        )
    missing = sorted(set(required) - set(args))
    if missing:
        raise _Invalid(
            "missing_field",
            f"missing required field(s): {', '.join(missing)}",
        )


def _require_int(args: Mapping[str, Any], key: str) -> int:
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Invalid("invalid_field", f"{key} must be an integer")
    if value < 0:
        raise _Invalid("invalid_field", f"{key} must be >= 0")
    return value


def _optional_int(args: Mapping[str, Any], key: str, default: int, *, maximum: int) -> int:
    if key not in args:
        return default
    value = _require_int(args, key)
    if value < 1:
        raise _Invalid("invalid_field", f"{key} must be >= 1")
    if value > maximum:
        raise _Invalid("limit_exceeded", f"{key} must be <= {maximum}", hint=None)
    return value


def _require_text(
    args: Mapping[str, Any], key: str, *, max_bytes: int, allow_empty: bool = False
) -> str:
    value = args[key]
    if not isinstance(value, str):
        raise _Invalid("invalid_field", f"{key} must be a string")
    if not allow_empty and not value.strip():
        raise _Invalid("invalid_field", f"{key} must not be empty")
    encoded = len(value.encode("utf-8"))
    if encoded > max_bytes:
        raise _Invalid(
            "limit_exceeded",
            f"{key} is {encoded} bytes, limit is {max_bytes}",
        )
    return value


def _optional_text(args: Mapping[str, Any], key: str, *, max_bytes: int) -> str | None:
    if key not in args or args[key] is None:
        return None
    return _require_text(args, key, max_bytes=max_bytes, allow_empty=False)


def _span_id(value: Any, *, field: str = "span_id") -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Invalid("invalid_field", f"{field} must be a non-empty string")
    if not _SPAN_ID_RE.match(value):
        raise _Invalid(
            "invalid_field",
            f"{field} is malformed; expected [A-Za-z0-9._:-] up to 128 chars",
        )
    return value


def _span_ids(
    args: Mapping[str, Any],
    key: str,
    *,
    limits: ToolLimits,
    allow_empty: bool = False,
) -> list[str]:
    value = args[key]
    if not isinstance(value, list):
        raise _Invalid("invalid_field", f"{key} must be a list of span IDs")
    ids = [_span_id(item, field=key) for item in value]
    if not ids and not allow_empty:
        raise _Invalid("invalid_field", f"{key} must not be empty")
    if len(ids) > limits.max_span_ids_per_operation:
        raise _Invalid(
            "limit_exceeded",
            f"{key} has {len(ids)} entries, limit is {limits.max_span_ids_per_operation}",
        )
    if len(set(ids)) != len(ids):
        raise _Invalid("invalid_field", f"{key} contains duplicate span IDs")
    return ids


def _txn_id(args: Mapping[str, Any], *, required: bool) -> str:
    raw = args.get("txn_id")
    if raw is None:
        if required:
            raise _Invalid("missing_field", "missing required field(s): txn_id")
        return f"tool-{uuid.uuid4().hex}"
    if not isinstance(raw, str) or not _TXN_ID_RE.match(raw):
        raise _Invalid(
            "invalid_field",
            "txn_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
        )
    return raw


def _normalise_operations(
    raw: Any, limits: ToolLimits, *, allow_kinds: frozenset[str] = STAGE_KINDS
) -> list[dict[str, Any]]:
    """Validate a batch of edits into store-ready ``EditOperation`` payloads."""

    if not isinstance(raw, list):
        raise _Invalid("invalid_field", "operations must be a list")
    if not raw:
        raise _Invalid("invalid_field", "operations must not be empty")
    if len(raw) > limits.max_operations:
        raise _Invalid(
            "limit_exceeded",
            f"operations has {len(raw)} entries, limit is {limits.max_operations}",
        )

    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"operations[{index}]"
        if not isinstance(item, Mapping):
            raise _Invalid("invalid_field", f"{where} must be an object")
        _check_keys(item, allowed=("kind", "span_ids", "replacement"))
        kind = item.get("kind")
        if not isinstance(kind, str):
            raise _Invalid("invalid_field", f"{where}.kind must be a string")
        dedicated = DEDICATED_KINDS.get(kind)
        if dedicated is not None:
            raise _Invalid(
                "wrong_tool",
                f"{where}.kind '{kind}' must be staged with {dedicated}",
                hint=f"call {dedicated} instead of clm_stage for '{kind}'",
            )
        if kind not in allow_kinds:
            raise _Invalid(
                "invalid_field",
                f"{where}.kind '{kind}' is not an editable operation",
                hint=f"allowed kinds: {', '.join(sorted(allow_kinds))}",
            )
        ids = _span_ids(item, "span_ids", limits=limits)
        overlap = seen.intersection(ids)
        if overlap:
            raise _Invalid(
                "conflicting_operations",
                f"{where} targets a span already edited in this batch: "
                f"{', '.join(sorted(overlap))}",
            )
        seen.update(ids)

        operation: dict[str, Any] = {"kind": kind, "span_ids": ids}
        replacement = item.get("replacement")
        if kind == "rewrite":
            if replacement is None:
                raise _Invalid("missing_field", f"{where}.replacement is required for rewrite")
            operation["replacement"] = _require_text(
                item, "replacement", max_bytes=limits.max_rewrite_bytes
            )
        elif replacement is not None:
            raise _Invalid(
                "invalid_field",
                f"{where}.replacement is only valid for kind 'rewrite'",
            )
        operations.append(operation)
    return operations


def _result_mapping(result: Any) -> dict[str, Any]:
    """Normalise a service result (mapping / dataclass / ``to_dict``) into a dict."""

    if result is None:
        return {"ok": True}
    if isinstance(result, Mapping):
        return dict(result)
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    for attr in ("to_dict", "as_dict"):
        converter = getattr(result, attr, None)
        if callable(converter):
            converted = converter()
            if isinstance(converted, Mapping):
                return dict(converted)
    try:
        return dict(vars(result))
    except TypeError as exc:  # pragma: no cover - defensive
        raise _Invalid(
            "service_error",
            f"service returned an unsupported result type: {type(result).__name__}",
        ) from exc


def _first_message(payload: Mapping[str, Any]) -> str | None:
    """First human-readable message in a store result's ``errors`` list."""

    errors = payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, Mapping) and item.get("message"):
                return _bounded(str(item["message"]))
    return None


def _service_result(result: Any, *, ok_key: str = "ok") -> dict[str, Any]:
    """Normalise a service result and normalise its ``ok`` flag."""

    payload = _result_mapping(result)
    payload.setdefault(ok_key, True)
    if not isinstance(payload.get("errors"), list):
        payload["errors"] = []
    return payload


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


class ToolHandlers:
    """Bound handlers plus the schemas that advertise them.

    ``resolve_service`` maps a session ID to that session's scoped service; it
    returns ``None`` when no store is attached, which becomes a ``no_session``
    error rather than a cross-session read. ``aux`` is an
    :class:`hermes_clm.auxiliary.AuxCompressor` (``None`` disables proposals).
    """

    def __init__(
        self,
        resolve_service: Callable[[str | None], Any],
        *,
        limits: ToolLimits | None = None,
        aux: Any = None,
    ) -> None:
        self._resolve_service = resolve_service
        self.limits = limits or ToolLimits()
        self.aux = aux

    # -- infrastructure ---------------------------------------------------- #

    def _service(self, session_id: str | None) -> Any:
        service = None
        if callable(self._resolve_service):
            service = self._resolve_service(session_id)
        if service is None:
            raise _Invalid(
                "no_session",
                "no CLM store is attached to this session",
                hint="the context engine initialises a store per session; start a new turn",
            )
        return service

    def _call(self, service: Any, method: str, /, *args: Any, **kwargs: Any) -> dict[str, Any]:
        handler = getattr(service, method, None)
        if not callable(handler):
            raise _Invalid(
                "service_error",
                f"CLM service does not implement {method}()",
            )
        payload = _service_result(handler(*args, **kwargs))
        if payload.get("ok") is False:
            # Service-level rejection (stale revision, conflict, invalid span):
            # surface it, plus a plain ``error`` string so the host marks the call
            # failed instead of reading a bare structured body as success.
            payload.setdefault("error", _first_message(payload) or "the store refused the request")
            payload.setdefault("receipts", [])
        return payload

    # -- reads ------------------------------------------------------------- #

    @_guarded
    def status(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(args, allowed=())
        payload = self._call(self._service(session_id), "status")
        if self.aux is not None:
            metrics = getattr(self.aux, "metrics", None)
            if callable(metrics):
                payload.setdefault("aux", metrics())
        return payload

    @_guarded
    def inspect(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(args, allowed=("cursor", "limit"))
        cursor = _optional_text(args, "cursor", max_bytes=256)
        limit = _optional_int(args, "limit", 50, maximum=self.limits.inspect_limit_max)
        return self._call(self._service(session_id), "inspect", cursor=cursor, limit=limit)

    @_guarded
    def search(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(args, allowed=("query", "cursor", "limit"), required=("query",))
        query = _require_text(args, "query", max_bytes=512)
        cursor = _optional_text(args, "cursor", max_bytes=256)
        limit = _optional_int(args, "limit", 20, maximum=self.limits.search_limit_max)
        return self._call(self._service(session_id), "search", query, cursor=cursor, limit=limit)

    @_guarded
    def retrieve(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(args, allowed=("span_id",), required=("span_id",))
        span_id = _span_id(args["span_id"])
        return self._call(self._service(session_id), "retrieve", span_id)

    # -- transaction lifecycle --------------------------------------------- #

    @_guarded
    def stage(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(
            args,
            allowed=("operations", "expected_revision", "txn_id"),
            required=("operations", "expected_revision"),
        )
        operations = _normalise_operations(args["operations"], self.limits)
        expected_revision = _require_int(args, "expected_revision")
        txn_id = _txn_id(args, required=False)
        payload = self._call(
            self._service(session_id),
            "stage",
            operations,
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=AGENT_ORIGIN,
        )
        payload.setdefault("txn_id", txn_id)
        payload.setdefault("expected_revision", expected_revision)
        if payload.get("ok") is not False:
            payload.setdefault("staged", True)
            payload.setdefault(
                "note", "staged only; call clm_commit with expected_revision to apply"
            )
        return payload

    @_guarded
    def commit(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(
            args, allowed=("txn_id", "expected_revision"), required=("txn_id", "expected_revision")
        )
        txn_id = _txn_id(args, required=True)
        expected_revision = _require_int(args, "expected_revision")
        payload = self._call(
            self._service(session_id),
            "commit",
            txn_id=txn_id,
            expected_revision=expected_revision,
        )
        payload.setdefault("txn_id", txn_id)
        return payload

    @_guarded
    def discard(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(args, allowed=("txn_id",), required=("txn_id",))
        txn_id = _txn_id(args, required=True)
        service = self._service(session_id)
        discard = getattr(service, "discard", None)
        if not callable(discard):
            raise _Invalid("service_error", "CLM service does not implement discard()")
        discard(txn_id=txn_id)
        return {"ok": True, "txn_id": txn_id, "discarded": True, "errors": [], "receipts": []}

    @_guarded
    def revert(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(
            args,
            allowed=("target_revision", "expected_revision", "txn_id"),
            required=("target_revision", "expected_revision"),
        )
        target_revision = _require_int(args, "target_revision")
        expected_revision = _require_int(args, "expected_revision")
        txn_id = _txn_id(args, required=False)
        payload = self._call(
            self._service(session_id),
            "revert",
            target_revision=target_revision,
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=AGENT_ORIGIN,
        )
        payload.setdefault("txn_id", txn_id)
        if payload.get("ok") is not False:
            payload.setdefault(
                "note", "revert created a new revision; earlier revisions remain in the audit trail"
            )
        return payload

    # -- dedicated operations ---------------------------------------------- #

    @_guarded
    def pin(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(
            args,
            allowed=("span_ids", "expected_revision", "txn_id"),
            required=("span_ids", "expected_revision"),
        )
        span_ids = _span_ids(args, "span_ids", limits=self.limits)
        expected_revision = _require_int(args, "expected_revision")
        txn_id = _txn_id(args, required=False)
        payload = self._call(
            self._service(session_id),
            "stage",
            [{"kind": "pin", "span_ids": span_ids}],
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=AGENT_ORIGIN,
        )
        payload.setdefault("txn_id", txn_id)
        if payload.get("ok") is not False:
            payload.setdefault("staged", True)
            payload.setdefault("pinned", span_ids)
            payload.setdefault(
                "note",
                "pin is one-way for an agent; commit to apply it. Only the operator can unpin.",
            )
        return payload

    @_guarded
    def boundary(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(
            args, allowed=("label", "expected_revision", "txn_id"), required=("expected_revision",)
        )
        expected_revision = _require_int(args, "expected_revision")
        txn_id = _txn_id(args, required=False)
        label = _optional_text(args, "label", max_bytes=_MAX_LABEL_CHARS)
        operation: dict[str, Any] = {"kind": "boundary", "span_ids": []}
        if label is not None:
            operation["replacement"] = label
        payload = self._call(
            self._service(session_id),
            "stage",
            [operation],
            expected_revision=expected_revision,
            txn_id=txn_id,
            actor_origin=AGENT_ORIGIN,
        )
        payload.setdefault("txn_id", txn_id)
        if payload.get("ok") is not False:
            payload.setdefault("staged", True)
            payload.setdefault("boundary", True)
            payload.setdefault("note", "boundary marker staged; commit to record it")
        return payload

    # -- auxiliary proposals ----------------------------------------------- #

    @_guarded
    def propose_compression(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(
            args,
            allowed=("span_ids", "instruction", "base_revision"),
            required=("span_ids",),
        )
        if self.aux is None:
            raise _Invalid(
                "aux_unavailable",
                "auxiliary compression is not configured",
                hint="set clm.aux_enabled true and use a host that can run auxiliary tasks",
            )
        span_ids = _span_ids(args, "span_ids", limits=self.limits)
        if len(span_ids) > self.limits.max_aux_spans:
            raise _Invalid(
                "limit_exceeded",
                f"span_ids has {len(span_ids)} entries, aux limit is {self.limits.max_aux_spans}",
            )
        instruction = _optional_text(
            args, "instruction", max_bytes=self.limits.max_instruction_bytes
        )
        base_revision = _require_int(args, "base_revision") if "base_revision" in args else None
        service = self._service(session_id)
        payload = self.aux.propose(
            service,
            span_ids=span_ids,
            instruction=instruction,
            base_revision=base_revision,
        )
        if not isinstance(payload, Mapping):
            raise _Invalid("internal_error", "auxiliary compressor produced a non-mapping result")
        return payload

    @_guarded
    def accept_proposal(
        self, args: dict[str, Any], *, session_id: str | None = None, **_: Any
    ) -> Mapping[str, Any]:
        _check_keys(
            args,
            allowed=("proposal_id", "proposal_sha256", "base_revision", "txn_id"),
            required=("proposal_id", "proposal_sha256", "base_revision"),
        )
        proposal_id = _require_text(args, "proposal_id", max_bytes=128)
        proposal_sha256 = _require_text(args, "proposal_sha256", max_bytes=64)
        if not _SHA256_RE.match(proposal_sha256):
            raise _Invalid("invalid_field", "proposal_sha256 must be 64 lowercase hex characters")
        base_revision = _require_int(args, "base_revision")
        txn_id = _txn_id(args, required=False)
        payload = self._call(
            self._service(session_id),
            "accept_proposal",
            proposal_id=proposal_id,
            proposal_sha256=proposal_sha256,
            base_revision=base_revision,
            txn_id=txn_id,
        )
        payload.setdefault("txn_id", txn_id)
        if payload.get("ok") is not False:
            payload.setdefault("staged", True)
            payload.setdefault(
                "note",
                "proposal staged only; review with clm_inspect and apply with clm_commit",
            )
        return payload


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

_SPAN_IDS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "pattern": _SPAN_ID_RE.pattern},
    "minItems": 1,
}

_OPERATIONS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "description": (
        "Edits to stage atomically. Each entry is "
        "{kind: keep|drop|rewrite|offload|restore, span_ids: [span ID], replacement?: string}. "
        "replacement is required for rewrite and forbidden otherwise."
    ),
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(STAGE_KINDS)},
            "span_ids": _SPAN_IDS_SCHEMA,
            "replacement": {"type": "string"},
        },
        "required": ["kind", "span_ids"],
        "additionalProperties": False,
    },
}


def _spec(
    name: str, description: str, parameters: Mapping[str, Any], handler: Callable[..., str]
) -> ToolSpec:
    schema = {
        "name": name,
        "description": description,
        "parameters": parameters,
    }
    return ToolSpec(name=name, schema=schema, handler=handler, description=description)


def build_tool_specs(handlers: ToolHandlers) -> list[ToolSpec]:
    """Return the tool advertisements for ``handlers`` (stable order)."""

    return [
        _spec(
            "clm_status",
            "Report the context-management revision, token pressure, span counts, "
            "store health and last error for this session. Read-only.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            handlers.status,
        ),
        _spec(
            "clm_inspect",
            "Page through the live editable context: span IDs, roles, origins and token "
            "estimates. Only this session is visible.",
            {
                "type": "object",
                "properties": {
                    "cursor": {
                        "type": "string",
                        "description": "Opaque cursor from a previous page.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": handlers.limits.inspect_limit_max,
                    },
                },
                "additionalProperties": False,
            },
            handlers.inspect,
        ),
        _spec(
            "clm_stage",
            "Stage keep/drop/rewrite/offload/restore edits against an expected revision. "
            "Nothing changes until clm_commit succeeds. Use clm_pin for pins and "
            "clm_boundary for task boundaries.",
            {
                "type": "object",
                "properties": {
                    "operations": _OPERATIONS_SCHEMA,
                    "expected_revision": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Revision the caller believes is current; stale values are rejected.",
                    },
                    "txn_id": {
                        "type": "string",
                        "description": "Idempotency key; reuse the same value to replay a staged transaction.",
                    },
                },
                "required": ["operations", "expected_revision"],
                "additionalProperties": False,
            },
            handlers.stage,
        ),
        _spec(
            "clm_commit",
            "Apply a staged transaction. Revalidates the expected revision inside the write "
            "transaction; replaying the same txn_id returns the original outcome.",
            {
                "type": "object",
                "properties": {
                    "txn_id": {"type": "string"},
                    "expected_revision": {"type": "integer", "minimum": 0},
                },
                "required": ["txn_id", "expected_revision"],
                "additionalProperties": False,
            },
            handlers.commit,
        ),
        _spec(
            "clm_discard",
            "Drop a staged transaction without applying it.",
            {
                "type": "object",
                "properties": {"txn_id": {"type": "string"}},
                "required": ["txn_id"],
                "additionalProperties": False,
            },
            handlers.discard,
        ),
        _spec(
            "clm_revert",
            "Restore an earlier revision as a new head revision. Earlier revisions stay in "
            "the audit trail; pins and host-present spans still apply.",
            {
                "type": "object",
                "properties": {
                    "target_revision": {"type": "integer", "minimum": 0},
                    "expected_revision": {"type": "integer", "minimum": 0},
                    "txn_id": {"type": "string"},
                },
                "required": ["target_revision", "expected_revision"],
                "additionalProperties": False,
            },
            handlers.revert,
        ),
        _spec(
            "clm_search",
            "Search this session's archive by text. Bounded and paginated.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "cursor": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": handlers.limits.search_limit_max,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handlers.search,
        ),
        _spec(
            "clm_retrieve",
            "Fetch one archived span from this session by span ID.",
            {
                "type": "object",
                "properties": {"span_id": {"type": "string"}},
                "required": ["span_id"],
                "additionalProperties": False,
            },
            handlers.retrieve,
        ),
        _spec(
            "clm_pin",
            "Stage a pin on span IDs so later edits cannot drop them. Pins are one-way for "
            "an agent: only the operator can unpin. Staging only; commit to apply.",
            {
                "type": "object",
                "properties": {
                    "span_ids": _SPAN_IDS_SCHEMA,
                    "expected_revision": {"type": "integer", "minimum": 0},
                    "txn_id": {"type": "string"},
                },
                "required": ["span_ids", "expected_revision"],
                "additionalProperties": False,
            },
            handlers.pin,
        ),
        _spec(
            "clm_boundary",
            "Stage an explicit task-boundary marker with an optional label, so a checkpoint "
            "is visible in the audit trail. Staging only; commit to apply.",
            {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "maxLength": _MAX_LABEL_CHARS},
                    "expected_revision": {"type": "integer", "minimum": 0},
                    "txn_id": {"type": "string"},
                },
                "required": ["expected_revision"],
                "additionalProperties": False,
            },
            handlers.boundary,
        ),
        _spec(
            "clm_propose_compression",
            "Ask the auxiliary compressor to propose rewrites for editable spans. Returns an "
            "immutable proposal (ID, hash, base revision) and review text. Nothing is staged "
            "or applied until clm_accept_proposal and clm_commit.",
            {
                "type": "object",
                "properties": {
                    "span_ids": _SPAN_IDS_SCHEMA,
                    "instruction": {
                        "type": "string",
                        "description": "Optional focus for the compressor.",
                    },
                    "base_revision": {"type": "integer", "minimum": 0},
                },
                "required": ["span_ids"],
                "additionalProperties": False,
            },
            handlers.propose_compression,
        ),
        _spec(
            "clm_accept_proposal",
            "Stage an immutable auxiliary proposal. Requires the exact proposal ID, hash and "
            "base revision; stages only, so a normal clm_commit is still required.",
            {
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "proposal_sha256": {"type": "string", "pattern": _SHA256_RE.pattern},
                    "base_revision": {"type": "integer", "minimum": 0},
                    "txn_id": {"type": "string"},
                },
                "required": ["proposal_id", "proposal_sha256", "base_revision"],
                "additionalProperties": False,
            },
            handlers.accept_proposal,
        ),
    ]
