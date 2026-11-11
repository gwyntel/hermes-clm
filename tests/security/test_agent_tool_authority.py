"""T6/T16 at the tool boundary - no session selector, no provider config, no unpin verb.

Contract: docs/integration-contract.md section "Tools <-> engine/store" and the tool
surface in docs/architecture.md. Handlers return JSON strings and reject unknown fields.
"""

from __future__ import annotations

import inspect
import json

import pytest

from _support import contract as C

C.need(C.TOOLS_MOD)

pytestmark = pytest.mark.security

FORBIDDEN_PARAMS = {
    "session_id",
    "sid",
    "path",
    "paths",
    "hermes_home",
    "db_path",
    "store_root",
    "provider",
    "model",
    "base_url",
    "api_key",
    "api_base",
    "fallback",
}


def _handlers():
    module = C.module(C.TOOLS_MOD)
    out = {}
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        if not callable(obj) or inspect.isclass(obj):
            continue
        params = set(inspect.signature(obj).parameters)
        out[name] = params
    return out


@pytest.mark.t("T6")
def test_no_tool_accepts_a_session_or_path_selector():
    offenders = {
        name: sorted(params & FORBIDDEN_PARAMS)
        for name, params in _handlers().items()
        if params & FORBIDDEN_PARAMS
    }
    assert not offenders, f"tool handlers accept caller-selected session/path: {offenders}"


@pytest.mark.t("T16")
def test_no_tool_accepts_provider_configuration():
    offenders = {
        name: sorted(params & FORBIDDEN_PARAMS)
        for name, params in _handlers().items()
        if params & {"provider", "model", "base_url", "api_key", "api_base", "fallback"}
    }
    assert not offenders, f"tool schemas expose provider configuration: {offenders}"


@pytest.mark.t("T16")
def test_closed_operation_kinds_are_pin_inclusive_and_unpin_free():
    models = C.module(C.MODELS_MOD)
    kinds = None
    for name in dir(models):
        obj = getattr(models, name)
        if isinstance(obj, (frozenset, set, tuple, list)) and obj and all(
            isinstance(item, str) for item in obj
        ):
            lowered = {item.lower() for item in obj}
            if "drop" in lowered and "pin" in lowered:
                kinds = lowered
                break
    assert kinds is not None, "no closed operation-kind enum found in hermes_clm.models"
    assert "pin" in kinds, "agent pin must be a permitted operation (plan D)"
    assert "unpin" not in kinds, "unpin must not be an agent-reachable operation kind (I21)"
    assert kinds <= C.CLOSED_OP_KINDS | {"notes_untrusted"}, f"unexpected kinds: {kinds}"


@pytest.mark.t("T16")
def test_exposed_json_schemas_forbid_additional_properties():
    """Unknown fields must be rejected at the schema boundary, not merely ignored (I28)."""
    module = C.module(C.TOOLS_MOD)
    schemas = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        if isinstance(obj, dict) and obj.get("type") == "object":
            schemas.append((name, obj))
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                if isinstance(item, dict) and item.get("type") == "object":
                    schemas.append((f"{name}[]", item))
    if not schemas:
        pytest.skip(
            "no JSON schema objects are exposed at module level in hermes_clm.tools; "
            "unknown-field rejection is then only exercised through the store (T47)"
        )
    permissive = [
        name
        for name, schema in schemas
        if schema.get("additionalProperties") is not False
    ]
    assert not permissive, f"permissive tool schemas accept unknown fields: {permissive}"
