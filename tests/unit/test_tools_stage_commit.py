from __future__ import annotations

import json

from clm_fakes import FakeService

from hermes_clm.tools import ToolHandlers, build_tool_specs


def load(raw):
    return json.loads(raw)


def make_handlers(service):
    return ToolHandlers(lambda _session_id: service)


def ops():
    return [{"kind": "drop", "span_ids": ["m-1"]}]


def args(**extra):
    return {"operations": ops(), "expected_revision": 1, "txn_id": "txn-fast", **extra}


def test_no_commit_field_preserves_old_stage_path():
    service = FakeService()
    payload = load(make_handlers(service).stage(args()))
    assert payload["ok"] is True
    assert service.names() == ["stage"]
    assert "committed" not in payload


def test_commit_true_calls_stage_commit_once_and_marks_atomic():
    service = FakeService()
    payload = load(make_handlers(service).stage(args(commit=True)))
    assert payload["ok"] is True
    assert payload["revision"] == 2
    assert payload["committed"] is True
    assert payload["atomic"] is True
    assert service.names() == ["stage_commit"]


def test_minimum_requires_commit_and_invalid_values_do_not_write():
    service = FakeService()
    for bad in (
        {"min_saved_tokens": 5},
        {"commit": "true"},
        {"commit": True, "min_saved_tokens": True},
        {"commit": True, "min_saved_tokens": 0},
        {"commit": True, "min_saved_tokens": -1},
        {"commit": True, "min_saved_tokens": 1.5},
    ):
        payload = load(make_handlers(service).stage(args(**bad)))
        assert payload["ok"] is False, bad
        assert service.names() == [], bad


def test_unavailable_service_method_refuses_without_write():
    class NoFastCommit:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            if name == "stage_commit":
                raise AttributeError(name)
            return getattr(self.inner, name)

    service = FakeService()
    handlers = ToolHandlers(lambda _session_id: NoFastCommit(service))
    payload = load(handlers.stage(args(commit=True)))
    assert payload["errors"][0]["code"] == "service_error"
    assert service.names() == []


def test_insufficient_saving_is_not_marked_committed():
    service = FakeService()
    service.stage_commit_result = {
        "ok": False,
        "errors": [{"code": "insufficient_saving", "message": "too little"}],
    }
    payload = load(make_handlers(service).stage(args(commit=True, min_saved_tokens=10)))
    assert payload["ok"] is False
    assert "committed" not in payload
    assert service.names() == ["stage_commit"]


def test_schema_advertises_optional_fast_commit_arguments():
    specs = {spec.name: spec for spec in build_tool_specs(make_handlers(FakeService()))}
    parameters = specs["clm_stage"].schema["parameters"]
    assert set(parameters["required"]) == {"operations", "expected_revision"}
    assert parameters["properties"]["commit"]["default"] is False
    assert parameters["properties"]["min_saved_tokens"]["minimum"] == 1


def test_a_store_without_stage_commit_is_an_error_not_a_two_phase_fallback():
    """``ToolService`` calls ``store.stage_commit``; a store lacking it raises RuntimeError.

    That must surface as ``internal_error`` with no write, never as a silent stage+commit:
    the fast path is an opt-in the caller asked for, so degrading it quietly would commit
    work the caller never priced.
    """

    service = FakeService()

    def boom(*_args, **_kwargs):
        raise RuntimeError("clm store does not implement stage_commit()")

    service.stage_commit = boom
    payload = load(make_handlers(service).stage(args(commit=True)))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "internal_error"
    assert "stage" not in service.names() and "commit" not in service.names()
