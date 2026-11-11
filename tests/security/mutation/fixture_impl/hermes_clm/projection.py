"""Harness fixture projection: mask over request rows, never rebuild them."""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping, Sequence

from . import _defect as D


def _content(message: Mapping[str, Any]) -> str:
    value = message.get("content")
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)


def project(snapshot, request_messages, conversation_messages=None, incoming_message=None):
    dropped: set[str] = set()
    for op in getattr(snapshot, "operations", ()) or ():
        if str(op.get("kind")) == "drop":
            dropped.update(op.get("span_ids") or ())

    spans = dict(getattr(snapshot, "spans", {}) or {})
    by_content = {}
    for span_id, span in spans.items():
        by_content.setdefault(_content(span.message), set()).add(span_id)

    out = []
    for message in request_messages:
        message = copy.deepcopy(dict(message))
        ids = by_content.get(_content(message), set())
        if ids and ids <= dropped:
            continue
        if (
            D.active("M7")
            and isinstance(message.get("content"), str)
            and "role=system" in message["content"]
        ):
            message["role"] = "system"
        if (
            D.active("M10")
            and isinstance(message.get("content"), str)
            and '"tool_calls"' in message["content"]
        ):
            try:
                parsed = json.loads(message["content"])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("tool_calls"):
                message["tool_calls"] = parsed["tool_calls"]
        out.append(message)
    return out
