"""Budget: deterministic estimates, bounded calibration, pressure tiers, offload plan."""

from __future__ import annotations

from _clm_fakes import assistant_call, coding_turns, message, tool_result
from hermes_clm.budget import (
    CALIBRATION_ALPHA,
    DEFAULT_RESERVE_TOKENS,
    MAX_CALIBRATION_RATIO,
    MIN_CALIBRATION_RATIO,
    OffloadCandidate,
    TokenCalibration,
    estimate_content_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
    plan_offload,
    pressure_state,
    pressure_tier,
    token_vocabulary,
)


def test_text_estimate_is_deterministic_and_monotone():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens(None) == 0
    short = estimate_text_tokens("abcd")
    long = estimate_text_tokens("abcd" * 100)
    assert short == 1
    assert long == 100
    assert estimate_text_tokens("abcd") == estimate_text_tokens("abcd")


def test_message_estimate_prices_role_content_and_tool_calls():
    plain = message("assistant", "hello world")
    with_calls = assistant_call("a1", "call_1")
    assert estimate_message_tokens(plain) > 0
    assert estimate_message_tokens(with_calls) > estimate_message_tokens(plain)
    assert estimate_message_tokens("not a dict") == 0


def test_message_list_estimate_sums_rows():
    history = coding_turns()
    assert estimate_messages_tokens(history) == sum(estimate_message_tokens(row) for row in history)


def test_content_estimate_handles_unknown_part_shapes():
    assert estimate_content_tokens([{"type": "text", "text": "abcd"}]) == 1
    assert estimate_content_tokens([{"type": "image_url", "image_url": {"url": "x"}}]) > 0
    assert estimate_content_tokens({"k": "v"}) > 0


def test_calibration_starts_neutral_and_adjusts():
    calibration = TokenCalibration()
    assert calibration.ratio == 1.0
    assert calibration.adjust(1000) == 1000

    calibration.observe(1000, 2000)
    assert calibration.ratio == 1.0 + CALIBRATION_ALPHA * 1.0
    assert calibration.adjust(1000) == int(round(1000 * calibration.ratio))
    assert calibration.samples == 1


def test_calibration_is_bounded_in_both_directions():
    low = TokenCalibration()
    for _ in range(50):
        low.observe(1000, 1)
    assert low.ratio == MIN_CALIBRATION_RATIO

    high = TokenCalibration()
    for _ in range(50):
        high.observe(1, 1000)
    assert high.ratio == MAX_CALIBRATION_RATIO


def test_calibration_ignores_non_positive_or_non_integer_samples():
    calibration = TokenCalibration()
    calibration.observe(0, 10)
    calibration.observe(10, 0)
    calibration.observe(-5, -5)
    calibration.observe("10", 10)  # type: ignore[arg-type]
    assert calibration.samples == 0
    assert calibration.ratio == 1.0


def test_calibration_state_is_json_serializable():
    calibration = TokenCalibration()
    calibration.observe(100, 120)
    payload = calibration.to_dict()
    assert set(payload) == {"ratio", "samples", "last_estimated", "last_actual"}
    assert payload["samples"] == 1


def test_pressure_tiers_follow_the_thresholds():
    thresholds = (0.25, 0.5, 0.75)
    assert pressure_tier(0.1, thresholds) == "normal"
    assert pressure_tier(0.3, thresholds) == "elevated"
    assert pressure_tier(0.6, thresholds) == "high"
    assert pressure_tier(0.8, thresholds) == "critical"


def test_pressure_state_reports_fit_reserve_and_overflow():
    state = pressure_state(estimated_tokens=9000, context_length=10000, reserve_tokens=2048)
    assert state.usable_limit == 7952
    assert not state.fits
    assert state.overflow_tokens == 1048
    assert state.tier == "critical"
    payload = state.to_dict()
    assert payload["overflow_tokens"] == 1048
    assert payload["fits"] is False


def test_pressure_state_with_unknown_context_always_fits():
    state = pressure_state(estimated_tokens=999999, context_length=0)
    assert state.fits
    assert state.overflow_tokens == 0
    assert state.ratio == 0.0


def test_pressure_state_uses_the_calibrated_estimate():
    calibration = TokenCalibration()
    calibration.observe(1000, 1500)  # blended ratio > 1
    raw = pressure_state(estimated_tokens=8000, context_length=10000, calibration=None)
    adjusted = pressure_state(estimated_tokens=8000, context_length=10000, calibration=calibration)
    assert adjusted.estimated_tokens > raw.estimated_tokens
    assert adjusted.calibrated_tokens == adjusted.estimated_tokens


def test_default_reserve_is_used_when_not_supplied():
    state = pressure_state(estimated_tokens=10, context_length=10000)
    assert state.reserve_tokens == DEFAULT_RESERVE_TOKENS


def test_plan_offload_is_oldest_first_and_stops_at_the_deficit():
    candidates = [
        OffloadCandidate(order=2, group_id="g3", uids=("c",), tokens=100),
        OffloadCandidate(order=0, group_id="g1", uids=("a",), tokens=100),
        OffloadCandidate(order=1, group_id="g2", uids=("b",), tokens=100),
    ]
    plan = plan_offload(candidates, need_tokens=150)
    assert [candidate.group_id for candidate in plan] == ["g1", "g2"]


def test_plan_offload_skips_pinned_and_protected_groups():
    candidates = [
        OffloadCandidate(order=0, group_id="pinned", uids=("a",), tokens=500, pinned=True),
        OffloadCandidate(order=1, group_id="protected", uids=("b",), tokens=500, protected=True),
        OffloadCandidate(order=2, group_id="free", uids=("c",), tokens=500),
    ]
    plan = plan_offload(candidates, need_tokens=400)
    assert [candidate.group_id for candidate in plan] == ["free"]


def test_plan_offload_returns_nothing_when_nothing_is_needed():
    candidates = [OffloadCandidate(order=0, group_id="g", uids=("a",), tokens=100)]
    assert plan_offload(candidates, need_tokens=0) == []


def test_plan_offload_can_fall_short_and_says_so():
    candidates = [OffloadCandidate(order=0, group_id="g", uids=("a",), tokens=10)]
    plan = plan_offload(candidates, need_tokens=1000)
    assert len(plan) == 1  # best effort, no invented content


def test_plan_offload_never_selects_a_group_without_members():
    candidates = [OffloadCandidate(order=0, group_id="empty", uids=(), tokens=100)]
    assert plan_offload(candidates, need_tokens=50) == []


def test_tool_result_rows_are_not_grouped_by_the_budget_module():
    """Grouping is identity's job; budget only prices what it is handed."""
    rows = [tool_result("t1", "call_1", "x" * 100), message("assistant", "done", "a1")]
    assert estimate_messages_tokens(rows) > 0


# --------------------------------------------------------------------------- #
# Token vocabulary: one label per number, never one number under two labels
# --------------------------------------------------------------------------- #


def test_token_vocabulary_keeps_raw_and_calibrated_apart():
    """The counterexample: a status payload reporting calibrated as if raw."""

    calibration = TokenCalibration(ratio=1.5)
    vocabulary = token_vocabulary(spans=1000, calibration=calibration)

    assert vocabulary["spans"] == 1000, "spans is the raw chars/4 estimate, untouched"
    assert vocabulary["calibrated"] == 1500
    assert vocabulary["calibration_ratio"] == 1.5
    assert vocabulary["estimator"] == "chars/4"
    assert vocabulary["actual"] is None, "no provider measurement has been seen yet"


def test_token_vocabulary_reports_the_provider_number_when_it_exists():
    vocabulary = token_vocabulary(spans=400, actual=512)
    assert vocabulary["spans"] == 400
    assert vocabulary["calibrated"] == 400, "no calibration means calibrated == spans"
    assert vocabulary["actual"] == 512


def test_token_vocabulary_never_reports_a_zero_measurement_as_actual():
    """A provider that reports zero prompt tokens did not measure zero."""

    for value in (0, -5, None):
        assert token_vocabulary(spans=10, actual=value)["actual"] is None


def test_token_vocabulary_is_total_for_hostile_input():
    vocabulary = token_vocabulary(spans=None, calibration=None, actual="nope")  # type: ignore[arg-type]
    assert vocabulary["spans"] == 0
    assert vocabulary["actual"] is None


def test_pressure_state_keeps_the_raw_estimate_recoverable():
    calibration = TokenCalibration(ratio=2.0)
    state = pressure_state(estimated_tokens=1_000, context_length=10_000, calibration=calibration)

    assert state.raw_estimated_tokens == 1_000
    assert state.estimated_tokens == 2_000, "tiers and edit validation use calibrated"
    assert state.calibrated_tokens == 2_000
    assert state.to_dict()["raw_estimated_tokens"] == 1_000
    assert state.to_dict()["estimated_tokens"] == 2_000


def test_pressure_state_raw_estimate_equals_calibrated_without_calibration():
    state = pressure_state(estimated_tokens=1_000, context_length=10_000)
    assert state.raw_estimated_tokens == state.estimated_tokens == 1_000


def test_token_vocabulary_declares_request_scope():
    assert token_vocabulary(spans=400, actual=512)["scope"] == "request"


def test_budget_has_one_calibrated_spelling_and_a_scope():
    state = pressure_state(
        estimated_tokens=1_000,
        context_length=10_000,
        calibration=TokenCalibration(ratio=2.0),
    )
    payload = state.to_dict()
    assert payload["scope"] == "request"
    assert payload["estimated_tokens"] == 2_000
    assert payload["raw_estimated_tokens"] == 1_000
    assert "calibrated_tokens" not in payload
    assert state.calibrated_tokens == 2_000

