"""clm_status detail levels and clm_stage's pure preview.

Two counterexamples drive these tests:

* a diagnostic tool that answers with thousands of tokens of budget, projection,
  receipts and config is too expensive to call when context is under pressure -
  the moment it is most needed. ``clm_status`` therefore answers small by
  default and only spends on ``detail="full"``.
* an agent could not price a batch before committing it, so it learned the cost
  after the fact. ``clm_stage`` now returns a projected ``before``/``after``/
  ``saved`` for the staged batch, computed without staging, committing or
  bumping a revision.
"""

from __future__ import annotations

import json

import pytest
from clm_fakes import FakeService, RecordingAuxClient, aux_json

from hermes_clm.tools import (
    STATUS_DETAILS,
    STATUS_RECEIPT_LIMIT,
    ToolHandlers,
)

PADDING = {
    "budget": {"tier": "normal", "context_length": 128_000},
    "projection": {"before": 64_000, "after": 64_000},
    # Real receipts carry per-operation review lines, which is why an uncapped
    # receipt list is the expensive part of the payload.
    "receipts": [
        {
            "revision": index,
            "txn_id": f"txn-{index}-0123456789abcdef",
            "applied": [
                {
                    "span_id": f"m-{index:04d}",
                    "kind": "rewrite",
                    "before_chars": 1200,
                    "after_chars": 260,
                    "review": "kept every identifier and the failing assertion",
                }
            ],
            "conflicts": [],
            "estimated_tokens_before": 64_000,
            "estimated_tokens_after": 63_400,
        }
        for index in range(1, 7)
    ],
    "config": {"clm": {"enabled": True}},
    "limits": {"max_operations": 64},
    "index": {"spans": 12},
    "healthy": True,
    "store_health": {"ok": True},
}


class StatusService(FakeService):
    """A service whose status payload is the full-weight one the engine returns."""

    def __init__(self, **overrides) -> None:
        super().__init__(revision=9)
        self.status_result = {
            "ok": True,
            "revision": 9,
            "active": 4,
            "offloaded": 2,
            "pinned": 1,
            "estimated_tokens": 64_000,
            "context_length": 128_000,
            "usage_percent": 50.0,
            "counts": {"active": 4, "offloaded": 2},
            "tokens": {"scope": "request", "spans": 64_000, "calibrated": 64_000, "actual": None},
            "last_compaction": {"revision": 7, "spans_offloaded": 3, "tokens_freed": 900},
            "last_error": None,
            **PADDING,
            **overrides,
        }


class PreviewService(FakeService):
    """A service with the pure ``preview`` surface."""

    def __init__(self, preview=None, *, raises: bool = False) -> None:
        super().__init__(revision=5)
        self.preview_result = preview
        self.preview_calls: list[list] = []
        self._raises = raises

    def preview(self, operations):
        self.preview_calls.append(list(operations))
        if self._raises:
            raise RuntimeError("projector exploded")
        if self.preview_result is not None:
            return dict(self.preview_result)
        return {
            "available": True,
            "ok": True,
            "before": 9_000,
            "after": 6_000,
            "saved": 3_000,
            "applied": len(operations),
            "inert": 0,
            "conflicts": [],
            "estimator": "chars/4",
            "calibrated": {"before": 9_000, "after": 6_000, "saved": 3_000},
        }


def load(raw):
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def handlers_for(service) -> ToolHandlers:
    return ToolHandlers(lambda _session_id: service)


# --------------------------------------------------------------------------- #
# clm_status: small by default
# --------------------------------------------------------------------------- #


def test_status_defaults_to_the_cheap_summary():
    payload = load(handlers_for(StatusService()).status({}))

    assert payload["ok"] is True
    assert payload["detail"] == "summary"
    assert payload["revision"] == 9
    for expensive in ("receipts", "projection", "budget", "config", "limits", "index"):
        assert expensive not in payload, f"{expensive} is what made clm_status expensive"
    assert payload["hint"], "a summary must say how to get the rest"


def test_the_summary_is_much_smaller_than_the_full_answer():
    handlers = handlers_for(StatusService())
    summary = json.dumps(load(handlers.status({})))
    full = json.dumps(load(handlers.status({"detail": "full"})))

    # The summary carries compact store/compaction/lineage diagnostics, so it is
    # bigger than a bare revision line - but it must stay a fraction of the full
    # answer (receipts, projection, budget, config) and inside a hard ceiling.
    assert len(summary) * 2 < len(full), (len(summary), len(full))
    assert len(summary) < 900, len(summary)


def test_summary_keeps_what_an_agent_needs_to_act():
    payload = load(handlers_for(StatusService()).status({}))

    assert payload["tier"] == "normal"
    assert payload["usage_percent"] == 50.0
    assert payload["counts"] == {"active": 4, "offloaded": 2}
    assert payload["tokens"]["calibrated"] == 64_000
    assert payload["last_compaction"]["revision"] == 7
    assert "last_error" in payload and payload["last_error"] is None


def test_summary_reports_staged_count_not_the_transactions():
    service = StatusService(staged_transactions=[{"txn_id": "a"}, {"txn_id": "b"}])
    payload = load(handlers_for(service).status({}))
    assert payload["staged"] == 2
    assert "staged_transactions" not in payload


def test_summary_retains_compact_store_compaction_and_lineage_diagnostics():
    """Cheap must not mean blind: these fields explain a broken turn."""

    service = StatusService(
        engine_version="0.6.1",
        store="attached",
        store_error={"code": "store_busy", "message": "another writer holds " + "x" * 400},
        last_compress_aborted=True,
        last_compress_error="manual_no_offloadable_groups: nothing unpinned to offload",
        last_lineage_error="lineage_conflict",
        last_rotation_error="rotation_conflict:child_store_present",
    )
    payload = load(handlers_for(service).status({}))

    assert payload["engine_version"] == "0.6.1"
    assert payload["store"] == "attached"
    assert payload["store_error"]["code"] == "store_busy"
    assert len(payload["store_error"]["message"]) <= 201, "diagnostics stay bounded"
    assert payload["last_compress_aborted"] is True
    assert "manual_no_offloadable_groups" in payload["last_compress_error"]
    assert payload["last_lineage_error"] == "lineage_conflict"
    assert payload["last_rotation_error"] == "rotation_conflict:child_store_present"
    assert "receipts" not in payload and "config" not in payload


def test_summary_diagnostics_are_present_but_empty_when_nothing_is_wrong():
    payload = load(handlers_for(StatusService()).status({}))

    for key in ("engine_version", "store", "store_error", "last_compress_error"):
        assert key in payload
    assert payload["store_error"] is None
    assert payload["last_compress_aborted"] is False
    assert payload["last_lineage_error"] is None


def test_status_full_keeps_every_diagnostic_field():
    payload = load(handlers_for(StatusService()).status({"detail": "full"}))

    assert payload["detail"] == "full"
    for expensive in ("projection", "budget", "config", "limits", "index"):
        assert expensive in payload


def test_full_receipts_are_capped_to_the_newest_and_the_rest_counted():
    payload = load(handlers_for(StatusService()).status({"detail": "full"}))

    receipts = payload["receipts"]
    assert len(receipts) == STATUS_RECEIPT_LIMIT
    assert [item["revision"] for item in receipts] == [4, 5, 6], "newest win"
    assert payload["receipts_omitted"] == 6 - STATUS_RECEIPT_LIMIT


def test_full_receipts_under_the_cap_are_untouched():
    service = StatusService(receipts=[{"revision": 1}])
    payload = load(handlers_for(service).status({"detail": "full"}))
    assert payload["receipts"] == [{"revision": 1}]
    assert "receipts_omitted" not in payload


@pytest.mark.parametrize("bad", ["brief", "", 1, True, ["full"], {"detail": "full"}])
def test_detail_rejects_anything_but_the_two_levels(bad):
    payload = load(handlers_for(StatusService()).status({"detail": bad}))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "invalid_field"
    assert payload["errors"][0]["message"] == "detail must be 'summary' or 'full'"
    assert "summary" in payload["hint"] and "full" in payload["hint"]


def test_detail_none_is_the_summary_not_an_error():
    payload = load(handlers_for(StatusService()).status({"detail": None}))
    assert payload["ok"] is True
    assert payload["detail"] == "summary"


def test_unknown_arguments_are_still_rejected():
    payload = load(handlers_for(StatusService()).status({"verbose": True}))
    assert payload["ok"] is False


def test_status_details_are_exactly_the_documented_pair():
    assert STATUS_DETAILS == ("summary", "full")
    assert STATUS_RECEIPT_LIMIT == 3


def test_aux_metrics_ride_only_on_the_full_answer():
    aux = RecordingAuxClient(aux_json({"rewrites": []}))
    from hermes_clm.auxiliary import AuxCompressor

    handlers = ToolHandlers(lambda _session_id: StatusService(), aux=AuxCompressor(aux))
    assert "aux" not in load(handlers.status({}))
    assert load(handlers.status({"detail": "full"}))["aux"]["task"] == "compression"


def test_summary_reports_headroom_and_compactable_savings():
    service = StatusService(
        budget={
            "tier": "high",
            "context_length": 128_000,
            "headroom_tokens": 12_000,
            "overflow_tokens": 0,
        },
        compactable={
            "known": True,
            "units": 3,
            "tokens": 28_400,
            "basis": "request",
            "reason": None,
        },
    )
    payload = load(handlers_for(service).status({}))
    assert payload["headroom_tokens"] == 12_000
    assert payload["compactable_tokens"] == 28_400
    assert payload["compactable_units"] == 3
    assert "compactable_unknown" not in payload


def test_summary_never_turns_unknown_compactable_into_zero():
    payload = load(
        handlers_for(
            StatusService(compactable={"known": False, "reason": "no_request_observed"})
        ).status({})
    )
    assert payload["compactable_tokens"] is None
    assert payload["compactable_unknown"] == "no_request_observed"


def test_summary_tolerates_a_minimal_service_payload():
    service = FakeService(revision=1)
    service.status_result = {"ok": True, "revision": 1}
    payload = load(handlers_for(service).status({}))
    assert payload["ok"] is True
    assert payload["revision"] == 1
    assert payload["last_compaction"] is None


# --------------------------------------------------------------------------- #
# clm_stage: a preview, not a commit
# --------------------------------------------------------------------------- #


def stage_drop(handlers, ops=None):
    return load(
        handlers.stage(
            {
                "operations": ops or [{"kind": "drop", "span_ids": ["m-0001"]}],
                "expected_revision": 5,
                "txn_id": "txn-preview",
            }
        )
    )


def test_stage_returns_a_projected_before_after_and_saved():
    service = PreviewService()
    payload = stage_drop(handlers_for(service))

    assert payload["ok"] is True
    projected = payload["projected"]
    assert (projected["before"], projected["after"], projected["saved"]) == (9_000, 6_000, 3_000)
    assert projected["ok"] is True
    assert service.preview_calls == [[{"kind": "drop", "span_ids": ["m-0001"]}]], (
        "the preview must price the same batch that was staged"
    )


def test_stage_preview_does_not_commit_or_advance_the_revision():
    service = PreviewService()
    payload = stage_drop(handlers_for(service))

    assert "commit" not in service.names()
    assert service.revision == 5, "pricing a batch must not touch the store"
    assert payload["revision"] == 5
    assert payload["staged"] is True
    assert payload["note"] == "staged only; call clm_commit with expected_revision to apply"


def test_stage_without_a_preview_capable_service_still_stages():
    """Contract-minimal stores and older deliveries keep working unchanged."""

    service = FakeService(revision=3)
    assert not hasattr(service, "preview")
    payload = stage_drop(handlers_for(service))

    assert payload["ok"] is True
    assert "projected" not in payload
    assert service.names().count("stage") == 1


def test_a_refused_stage_is_not_priced():
    service = PreviewService()
    service.stage_result = {"ok": False, "errors": [{"code": "stale_revision"}]}
    payload = stage_drop(handlers_for(service))

    assert payload["ok"] is False
    assert "projected" not in payload
    assert service.preview_calls == [], "do not price a batch the store refused"


def test_a_failing_preview_is_contained_and_the_stage_stands():
    service = PreviewService(raises=True)
    payload = stage_drop(handlers_for(service))

    assert payload["ok"] is True, "a preview failure is absent detail, not a failed stage"
    assert "projected" not in payload
    assert service.names().count("stage") == 1
    assert "priv" not in json.dumps(payload)


def test_preview_is_advisory_when_the_projector_would_refuse():
    service = PreviewService(
        preview={
            "available": True,
            "ok": False,
            "before": 9_000,
            "after": 9_000,
            "saved": 0,
            "conflicts": [{"span_id": "m-0001", "reason": "protected"}],
        }
    )
    payload = stage_drop(handlers_for(service))

    assert payload["ok"] is True, "the stage succeeded; only its price is refused"
    assert payload["projected"]["saved"] == 0
    assert payload["projected"]["conflicts"][0]["span_id"] == "m-0001"


def test_stage_schema_advertises_the_preview():
    from hermes_clm.tools import build_tool_specs

    handlers = handlers_for(PreviewService())
    specs = {spec.name: spec for spec in build_tool_specs(handlers)}
    description = specs["clm_stage"].description.lower()
    assert "preview" in description
    assert "before" in description and "saved" in description
    status_schema = specs["clm_status"].schema["parameters"]["properties"]["detail"]
    assert status_schema["enum"] == ["summary", "full"]
