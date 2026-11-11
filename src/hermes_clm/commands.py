"""Operator-facing ``/clm`` command surface.

The agent's tool set deliberately omits two capabilities: unpinning and direct
rewriting without a staged review. Those belong to the human at the keyboard, so
they live here as a slash command rather than as tools the model can call.

Everything in this module is Hermes-free and returns plain text, which is what a
slash command surfaces. Store access goes through the same scoped service the
tools use; this module never opens a file or a database.

Service methods used here (in addition to the tool set):
    unpin(span_ids, *, expected_revision, txn_id, actor_origin) -> mapping

Operator edits are staged and committed in one call because the human typed the
instruction; the revision check is still enforced by both stage and commit.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .tools import SPAN_ID_PATTERN

__all__ = ["COMMAND_NAME", "USAGE", "handle", "parse"]

COMMAND_NAME = "clm"
OPERATOR_ORIGIN = "operator"

USAGE = """\
/clm — Hermes context management (operator)

  /clm status
      revision, token pressure, span counts, pins, store health, aux metrics
  /clm edit drop <span_id> [<span_id> ...]
      remove spans from the projected context (raw archive keeps them)
  /clm edit keep <span_id> [<span_id> ...]
      keep spans verbatim and stop later edits from touching them
  /clm edit rewrite <span_id> <text ...>
      replace one span's text
  /clm unpin <span_id> [<span_id> ...]
      release an operator or agent pin (agents cannot unpin)
  /clm help
      this text

Edits apply immediately as a new revision with actor_origin=operator."""

_ALLOWED_EDIT_KINDS = frozenset({"drop", "keep", "rewrite"})
_MAX_SPAN_IDS = 32
_MAX_REWRITE_BYTES = 8000
_SPAN_ID_RE = re.compile(SPAN_ID_PATTERN)


def parse(raw_args: str) -> tuple[str, list[str]]:
    """Split raw slash-command arguments into ``(subcommand, tokens)``."""

    tokens = str(raw_args or "").split()
    if not tokens:
        return "help", []
    return tokens[0].lower(), tokens[1:]


def handle(
    raw_args: str,
    *,
    resolve_service: Callable[[str | None], Any],
    session_id: str | None = None,
) -> str:
    """Run one operator command and return display text.

    ``resolve_service`` maps the session to its scoped service, or returns
    ``None`` when no store is attached. Failures are explained in one line, never
    raised into the CLI or gateway.
    """

    subcommand, tokens = parse(raw_args)
    if subcommand in {"help", "?", "-h", "--help"}:
        return USAGE
    if subcommand == "status":
        if tokens:
            return _failure("usage", "status takes no arguments", hint="see /clm help")
        return _status(resolve_service, session_id)
    if subcommand == "edit":
        return _edit(tokens, resolve_service, session_id)
    if subcommand == "unpin":
        return _unpin(tokens, resolve_service, session_id)
    return _failure(
        "unknown_subcommand",
        f"unknown subcommand '{subcommand}'",
        hint="see /clm help",
    )


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def _status(resolve_service: Callable[[str | None], Any], session_id: str | None) -> str:
    service = _service(resolve_service, session_id)
    if service is None:
        return _failure("no_session", "no CLM store is attached to this session")
    status = _result(getattr(service, "status", None))
    if status is None:
        return _failure("service_error", "status() is unavailable")
    if status.get("ok") is False:
        return _failure("status_failed", _message(status))

    lines = [f"CLM · revision {_show(status.get('revision'), '?')}"]
    head_hash = status.get("head_hash")
    if head_hash:
        lines[0] += f" · head {str(head_hash)[:12]}"

    counts = _mapping(status.get("counts")) or {}
    span_line = _span_counts(status, counts)
    if span_line:
        lines.append(span_line)

    budget = _mapping(status.get("budget")) or _mapping(status.get("tokens")) or {}
    budget_line = _budget_line(status, budget)
    if budget_line:
        lines.append(budget_line)

    health = status.get("store_health", status.get("health"))
    if health is not None:
        lines.append(f"store: {_show(health, 'unknown')}")

    aux = _mapping(status.get("aux"))
    if aux:
        lines.append(_aux_line(aux))

    last_error = status.get("last_error")
    lines.append(f"last error: {_compact(last_error) if last_error else 'none'}")
    return "\n".join(lines)


def _edit(
    tokens: Sequence[str],
    resolve_service: Callable[[str | None], Any],
    session_id: str | None,
) -> str:
    if len(tokens) < 2:
        return _failure(
            "usage",
            "usage: /clm edit <drop|keep|rewrite> <span_id> [...]",
            hint="see /clm help",
        )
    kind = tokens[0].lower()
    if kind not in _ALLOWED_EDIT_KINDS:
        return _failure(
            "usage",
            f"unknown edit kind '{kind}'",
            hint=f"allowed: {', '.join(sorted(_ALLOWED_EDIT_KINDS))}",
        )
    span_ids = list(tokens[1:])
    replacement: str | None = None
    if kind == "rewrite":
        if len(tokens) < 3:
            return _failure("usage", "usage: /clm edit rewrite <span_id> <text ...>")
        span_ids = list(tokens[1:2])
        replacement = " ".join(tokens[2:])

    invalid = _validate_span_ids(span_ids)
    if invalid:
        return invalid
    if replacement is not None and len(replacement.encode("utf-8")) > _MAX_REWRITE_BYTES:
        return _failure("limit_exceeded", f"replacement exceeds {_MAX_REWRITE_BYTES} bytes")

    service = _service(resolve_service, session_id)
    if service is None:
        return _failure("no_session", "no CLM store is attached to this session")
    status = _result(getattr(service, "status", None))
    if status is None:
        return _failure("service_error", "status() is unavailable")
    revision = status.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        return _failure("service_error", "status() did not report a revision")

    operation: dict[str, Any] = {"kind": kind, "span_ids": span_ids}
    if replacement is not None:
        operation["replacement"] = replacement
    txn_id = f"operator-{uuid.uuid4().hex}"

    staged = _result(
        getattr(service, "stage", None),
        [operation],
        expected_revision=revision,
        txn_id=txn_id,
        actor_origin=OPERATOR_ORIGIN,
    )
    if staged is None:
        return _failure("service_error", "stage() is unavailable")
    if staged.get("ok") is False:
        return _failure("stage_failed", _message(staged))
    committed = _result(getattr(service, "commit", None), txn_id=txn_id, expected_revision=revision)
    if committed is None:
        return _failure("service_error", "commit() is unavailable")
    if committed.get("ok") is False:
        return _failure("commit_failed", _message(committed))

    return (
        f"CLM · {kind} applied to {', '.join(span_ids)} · "
        f"revision {_show(committed.get('revision', staged.get('revision')), '?')}"
    )


def _unpin(
    tokens: Sequence[str],
    resolve_service: Callable[[str | None], Any],
    session_id: str | None,
) -> str:
    if not tokens:
        return _failure("usage", "usage: /clm unpin <span_id> [...]", hint="see /clm help")
    invalid = _validate_span_ids(list(tokens))
    if invalid:
        return invalid
    service = _service(resolve_service, session_id)
    if service is None:
        return _failure("no_session", "no CLM store is attached to this session")
    status = _result(getattr(service, "status", None))
    revision = status.get("revision") if status else None
    if isinstance(revision, bool) or not isinstance(revision, int):
        return _failure("service_error", "status() did not report a revision")

    unpin = getattr(service, "unpin", None)
    if not callable(unpin):
        return _failure(
            "service_error",
            "this store does not expose operator unpin",
            hint="operator unpin requires the store's unpin() entry point",
        )
    payload = _result(
        unpin,
        list(tokens),
        expected_revision=revision,
        txn_id=f"operator-{uuid.uuid4().hex}",
        actor_origin=OPERATOR_ORIGIN,
    )
    if payload is None:
        payload = {"ok": True}
    if payload.get("ok") is False:
        return _failure("unpin_failed", _message(payload))
    return (
        f"CLM · unpinned {', '.join(tokens)} · "
        f"revision {_show(payload.get('revision', revision), '?')}"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _service(resolve_service: Callable[[str | None], Any] | None, session_id: str | None) -> Any:
    if not callable(resolve_service):
        return None
    try:
        return resolve_service(session_id)
    except Exception:
        return None


def _result(method: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Call a service method and normalise its result; ``None`` when unusable."""

    if not callable(method):
        return None
    try:
        raw = method(*args, **kwargs)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    import dataclasses as _dataclasses

    if _dataclasses.is_dataclass(raw) and not isinstance(raw, type):
        return _dataclasses.asdict(raw)
    for attr in ("to_dict", "as_dict"):
        converter = getattr(raw, attr, None)
        if callable(converter):
            converted = converter()
            if isinstance(converted, Mapping):
                return dict(converted)
    return None


def _mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _validate_span_ids(span_ids: Sequence[str]) -> str | None:
    if not span_ids:
        return _failure("usage", "span IDs are required")
    if len(span_ids) > _MAX_SPAN_IDS:
        return _failure("limit_exceeded", f"at most {_MAX_SPAN_IDS} span IDs per command")
    for span_id in span_ids:
        if not isinstance(span_id, str) or not _SPAN_ID_RE.match(span_id):
            return _failure("invalid_field", f"malformed span ID: {span_id!r}")
    return None


def _span_counts(status: Mapping[str, Any], counts: Mapping[str, Any]) -> str | None:
    active = status.get("active", counts.get("active"))
    offloaded = status.get("offloaded", counts.get("offloaded"))
    pinned = status.get("pinned", counts.get("pinned"))
    parts = []
    for label, value in (("active", active), ("offloaded", offloaded), ("pinned", pinned)):
        if value is not None:
            parts.append(f"{value} {label}")
    return "spans: " + " · ".join(parts) if parts else None


def _budget_line(status: Mapping[str, Any], budget: Mapping[str, Any]) -> str | None:
    used = budget.get("estimated_tokens", status.get("estimated_tokens"))
    limit = budget.get("context_length", status.get("context_length"))
    reserve = budget.get("reserve_tokens", status.get("reserve_tokens"))
    pressure = budget.get("pressure", status.get("pressure"))
    if used is None and limit is None:
        return None
    if isinstance(used, int) and isinstance(limit, int) and limit > 0:
        pct = used / limit * 100
        line = f"budget: {used} / {limit} tokens ({pct:.0f}%)"
    else:
        line = f"budget: {_show(used, '?')} / {_show(limit, '?')} tokens"
    if reserve is not None:
        line += f" · reserve {reserve}"
    if pressure is not None:
        line += f" · pressure {_show(pressure, '?')}"
    return line


def _aux_line(aux: Mapping[str, Any]) -> str:
    task = aux.get("task", "compression")
    enabled = aux.get("enabled")
    state = "enabled" if enabled else "disabled"
    calls = aux.get("calls", 0)
    failures = aux.get("failures", 0)
    latency = aux.get("latency_ms_total")
    line = f"aux: {task} {state} · {calls} call(s) · {failures} failure(s)"
    if isinstance(latency, int) and isinstance(calls, int) and calls > 0:
        line += f" · avg {latency // calls}ms"
    if aux.get("last_error_code"):
        line += f" · last error {aux['last_error_code']}"
    return line


def _show(value: Any, default: str) -> str:
    return default if value is None else str(value)


def _compact(value: Any) -> str:
    if isinstance(value, Mapping):
        code = value.get("code")
        message = value.get("message") or value.get("error")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
        return json.dumps(value, ensure_ascii=False)[:200]
    text = " ".join(str(value).split())
    return text[:200] + ("…" if len(text) > 200 else "")


def _message(payload: Mapping[str, Any]) -> str:
    errors = payload.get("errors")
    code = None
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, Mapping) and item.get("message"):
                code = item.get("code")
                message = str(item["message"])
                return f"{code}: {message}" if code else message
    text = payload.get("error") or payload.get("message") or "the store refused the request"
    return _compact(text)


def _failure(code: str, message: str, *, hint: str | None = None) -> str:
    text = f"CLM · {message}"
    return f"{text} ({hint})" if hint else text
