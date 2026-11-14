"""The engine-side gates on the one-call fast commit.

``ToolService.stage_commit`` is the layer that decides whether a batch may be
written in one transaction. Its refusals must all happen *before* the store is
touched, because a fast path that writes first and reports a problem second is
exactly the class of bug the two-phase default exists to avoid. The tool handler
tests drive a fake service, so these engine-level gates live here.
"""

from __future__ import annotations

from typing import Any, cast

from hermes_clm.budget import TokenCalibration
from hermes_clm.engine import ToolService


class RecordingStore:
    def __init__(self, revision: int = 1) -> None:
        self.revision = revision
        self.calls: list[tuple[str, dict]] = []

    def status(self) -> dict:
        return {"revision": self.revision, "ok": True}

    def stage_commit(self, operations, **kwargs):
        self.calls.append(("stage_commit", {"operations": list(operations), **kwargs}))
        self.revision += 1
        return {
            "ok": True,
            "revision": self.revision,
            "applied": len(operations),
            "errors": [],
            "receipts": [],
        }

    def stage(self, operations, **kwargs):  # pragma: no cover - must not be reached
        self.calls.append(("stage", {"operations": list(operations), **kwargs}))
        return {"ok": True, "revision": self.revision, "errors": [], "receipts": []}

    def commit(self, **kwargs):  # pragma: no cover - must not be reached
        self.calls.append(("commit", dict(kwargs)))
        return {"ok": True, "revision": self.revision, "errors": [], "receipts": []}


class FakeEngine:
    """The slice of ``CLMEngine`` ``ToolService`` reaches for."""

    def __init__(self, store, projected=None, *, preview_raises: bool = False) -> None:
        self.store = store
        self._store_error = None
        self._last_projection = None
        self.calibration = TokenCalibration()
        self._projected = projected
        self._preview_raises = preview_raises
        self.errors: list[BaseException] = []
        self.preview_calls = 0

    def preview(self, operations):
        self.preview_calls += 1
        if self._preview_raises:
            raise RuntimeError("preview exploded")
        if self._projected is None:
            return {"available": False, "reason": "no_request_observed"}
        return dict(self._projected)

    def pressure_hint(self) -> dict:
        return {"scope": "request", "tier": "normal", "headroom_tokens": 1, "overflow_tokens": 0}

    def _record_error(self, exc: BaseException) -> None:
        self.errors.append(exc)


def ops():
    return [{"kind": "drop", "span_ids": ["sp-1"]}]


def projected(**overrides):
    payload = {
        "available": True,
        "ok": True,
        "before": 1000,
        "after": 400,
        "saved": 600,
        "applied": 1,
        "inert": 0,
        "conflicts": [],
        "estimator": "chars/4",
        "calibrated": {"before": 900, "after": 360, "saved": 540},
    }
    payload.update(overrides)
    return payload


def test_a_stale_revision_is_refused_before_anything_is_previewed_or_written():
    store = RecordingStore(revision=4)
    engine = FakeEngine(store, projected())
    out = ToolService(cast(Any, engine)).stage_commit(
        ops(), expected_revision=3, txn_id="txn-stale"
    )
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "stale_revision"
    assert out["revision"] == 4
    assert store.calls == []
    assert engine.preview_calls == 0


def test_a_missing_preview_refuses_instead_of_falling_back_to_two_phase():
    store = RecordingStore()
    engine = FakeEngine(store, None)
    out = ToolService(cast(Any, engine)).stage_commit(
        ops(), expected_revision=1, txn_id="txn-nopreview"
    )
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "preview_unavailable"
    assert store.calls == []


def test_an_exploding_preview_is_a_refusal_not_a_write():
    store = RecordingStore()
    engine = FakeEngine(store, projected(), preview_raises=True)
    out = ToolService(cast(Any, engine)).stage_commit(
        ops(), expected_revision=1, txn_id="txn-raise"
    )
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "preview_unavailable"
    assert store.calls == []
    assert len(engine.errors) == 1


def test_a_refused_projection_writes_nothing_and_returns_its_conflicts():
    store = RecordingStore()
    engine = FakeEngine(store, projected(ok=False, saved=0, conflicts=[{"code": "protected_row"}]))
    out = ToolService(cast(Any, engine)).stage_commit(
        ops(), expected_revision=1, txn_id="txn-refused"
    )
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "projection_refused"
    assert out["projected"]["conflicts"] == [{"code": "protected_row"}]
    assert store.calls == []


def test_the_minimum_is_compared_against_the_calibrated_saving():
    store = RecordingStore()
    engine = FakeEngine(store, projected())

    # Raw says 600 but calibrated says 540: the budget the model is held to is the
    # calibrated one, so a 550 minimum must fail even though raw would pass.
    out = ToolService(cast(Any, engine)).stage_commit(
        ops(), expected_revision=1, txn_id="txn-min", min_saved_tokens=550
    )
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "insufficient_saving"
    assert out["saved_tokens"] == {"raw": 600, "calibrated": 540}
    assert store.calls == []

    ok = ToolService(cast(Any, engine)).stage_commit(
        ops(), expected_revision=1, txn_id="txn-min-ok", min_saved_tokens=540
    )
    assert ok["ok"] is True, ok


def test_a_passing_batch_writes_once_and_reports_the_saving():
    store = RecordingStore()
    engine = FakeEngine(store, projected())
    out = ToolService(cast(Any, engine)).stage_commit(ops(), expected_revision=1, txn_id="txn-ok")
    assert out["ok"] is True
    assert out["atomic"] is True
    assert out["saved_tokens"] == {"raw": 600, "calibrated": 540}
    assert out["projected"]["saved"] == 600
    assert [name for name, _ in store.calls] == ["stage_commit"]
    assert store.calls[0][1]["actor_origin"] == "agent"
    assert store.calls[0][1]["txn_id"] == "txn-ok"


def test_the_fast_path_never_calls_stage_then_commit():
    store = RecordingStore()
    engine = FakeEngine(store, projected())
    ToolService(cast(Any, engine)).stage_commit(ops(), expected_revision=1, txn_id="txn-once")
    names = {name for name, _ in store.calls}
    assert names == {"stage_commit"}
