"""Token estimation, pressure tiers and deterministic offload planning.

Stdlib only, and deliberately deterministic: the same message list always
yields the same estimate, so an unchanged revision plus unchanged history
projects to byte-identical requests and keeps the provider prefix cache warm.

These are *rough* estimates. The engine calibrates them against real
``usage.prompt_tokens`` with a bounded ratio (:class:`TokenCalibration`) and
reports the same number it uses for edit validation.

Token vocabulary
----------------
Three numbers exist and they are not interchangeable, so they are named:

``spans``
    raw ``chars/4`` estimate of the archived spans (what the store reports).
``calibrated``
    ``spans`` multiplied by the learned :class:`TokenCalibration` ratio. This is
    the number budgets, tiers and edit validation use.
``actual``
    the last ``usage.prompt_tokens`` the provider reported, or ``None`` when no
    response has been observed yet. Never derived, never estimated.

:func:`token_vocabulary` is the one place that names them, so a status payload
cannot report three token figures under one label.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Rough characters-per-token ratio for the fallback estimator.
CHARS_PER_TOKEN = 4
#: Per-message framing overhead (role plus delimiters) in tokens.
MESSAGE_OVERHEAD_TOKENS = 4
#: Tokens kept free for the next completion.
DEFAULT_RESERVE_TOKENS = 2048
#: Fraction-of-context boundaries for the pressure tiers.
DEFAULT_PRESSURE_THRESHOLDS: Tuple[float, ...] = (0.25, 0.5, 0.75)
#: Calibration is clamped to this band so one skewed sample cannot distort budgets.
MIN_CALIBRATION_RATIO = 0.25
MAX_CALIBRATION_RATIO = 4.0
#: Exponential moving average weight for a new calibration sample.
CALIBRATION_ALPHA = 0.25

TIER_NORMAL = "normal"
TIER_ELEVATED = "elevated"
TIER_HIGH = "high"
TIER_CRITICAL = "critical"


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def estimate_text_tokens(text: Any) -> int:
    """Rough token count for a single string."""
    if not isinstance(text, str) or not text:
        return 0
    return max(1, int(math.ceil(len(text) / CHARS_PER_TOKEN)))


def estimate_content_tokens(content: Any) -> int:
    """Rough token count for a message ``content`` in any supported shape."""
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, Mapping):
        return estimate_text_tokens(json.dumps(content, sort_keys=True, default=str))
    if isinstance(content, (list, tuple)):
        total = 0
        for part in content:
            if isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    total += estimate_text_tokens(text)
                else:
                    # Unknown part: price the structure, never pretend it is free.
                    total += estimate_text_tokens(json.dumps(part, sort_keys=True, default=str))
            elif isinstance(part, str):
                total += estimate_text_tokens(part)
        return total
    return estimate_text_tokens(repr(content))


def estimate_message_tokens(message: Any) -> int:
    """Rough token count for one provider message dict."""
    if not isinstance(message, Mapping):
        return 0
    total = MESSAGE_OVERHEAD_TOKENS + estimate_content_tokens(message.get("content"))
    calls = message.get("tool_calls")
    if isinstance(calls, (list, tuple)) and calls:
        total += estimate_text_tokens(json.dumps(calls, sort_keys=True, default=str))
    return total


def estimate_messages_tokens(messages: Iterable[Any]) -> int:
    """Rough token count for a message list."""
    total = 0
    for message in messages or ():
        total += estimate_message_tokens(message)
    return total


class TokenCalibration:
    """Bounded estimate-to-real ratio learned from ``usage.prompt_tokens``.

    The estimate is multiplied by a ratio that starts at 1.0 and moves toward
    each observed ``actual / estimated`` by :data:`CALIBRATION_ALPHA`, clamped to
    :data:`MIN_CALIBRATION_RATIO` .. :data:`MAX_CALIBRATION_RATIO`.
    """

    def __init__(self, ratio: float = 1.0) -> None:
        self.ratio = _clamp(float(ratio), MIN_CALIBRATION_RATIO, MAX_CALIBRATION_RATIO)
        self.samples = 0
        self.last_estimated = 0
        self.last_actual = 0

    def observe(self, estimated: int, actual: int) -> None:
        """Fold one real measurement into the ratio; non-positive inputs are ignored."""
        if not isinstance(estimated, int) or not isinstance(actual, int):
            return
        if estimated <= 0 or actual <= 0:
            return
        observed = actual / estimated
        blended = (1.0 - CALIBRATION_ALPHA) * self.ratio + CALIBRATION_ALPHA * observed
        self.ratio = _clamp(blended, MIN_CALIBRATION_RATIO, MAX_CALIBRATION_RATIO)
        self.samples += 1
        self.last_estimated = estimated
        self.last_actual = actual

    def adjust(self, estimate: int) -> int:
        """Apply the learned ratio to a raw estimate."""
        if estimate <= 0:
            return 0
        return int(round(estimate * self.ratio))

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable calibration state."""
        return {
            "ratio": round(self.ratio, 6),
            "samples": self.samples,
            "last_estimated": self.last_estimated,
            "last_actual": self.last_actual,
        }


def _as_int(value: Any) -> Optional[int]:
    """``value`` as an int, or ``None`` - never an exception on a hostile payload.

    A provider usage field can arrive as a string, a float, ``None`` or a bool;
    a status payload must stay constructible in all of those cases, because the
    alternative is losing the whole status read to one malformed number.
    """

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def token_vocabulary(
    *,
    spans: int,
    calibration: Optional["TokenCalibration"] = None,
    actual: Optional[int] = None,
) -> Dict[str, Any]:
    """The three token numbers, each under the label that describes it.

    ``spans`` is the raw ``chars/4`` estimate of archived spans, ``calibrated``
    applies the learned ratio, and ``actual`` is the provider's own
    ``usage.prompt_tokens`` for the last request (``None`` until one is seen).
    A caller that sizes an edit off ``spans`` while the budget uses
    ``calibrated`` is planning against a number the plugin knows is wrong, so
    every payload reports both rather than one ambiguous figure.
    """

    raw = max(0, _as_int(spans) or 0)
    calibrated = max(0, int(calibration.adjust(raw))) if calibration is not None else raw
    observed = _as_int(actual)
    observed = None if observed is None else max(0, observed)
    if observed == 0:
        # A provider reporting zero prompt tokens is "no measurement", not a
        # measurement of zero: keep the field honest.
        observed = None
    return {
        "spans": raw,
        "calibrated": calibrated,
        "actual": observed,
        "estimator": "chars/4",
        "calibration_ratio": round(float(calibration.ratio), 6) if calibration else 1.0,
    }


def pressure_tier(ratio: float, thresholds: Sequence[float] = DEFAULT_PRESSURE_THRESHOLDS) -> str:
    """Bucket a usage ratio into a tier name (``normal`` .. ``critical``)."""
    ordered = sorted(float(value) for value in (thresholds or DEFAULT_PRESSURE_THRESHOLDS))
    if not ordered:
        return TIER_NORMAL
    if ratio >= ordered[-1]:
        return TIER_CRITICAL
    if len(ordered) > 1 and ratio >= ordered[-2]:
        return TIER_HIGH
    if len(ordered) > 2 and ratio >= ordered[-3]:
        return TIER_ELEVATED
    return TIER_NORMAL


@dataclass(frozen=True)
class PressureState:
    """The single budget number the engine validates edits against."""

    estimated_tokens: int
    context_length: int
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS
    threshold_percent: float = 0.75
    calibrated_tokens: int = 0
    last_prompt_tokens: int = 0
    thresholds: Tuple[float, ...] = DEFAULT_PRESSURE_THRESHOLDS
    #: Raw ``chars/4`` estimate *before* calibration. ``estimated_tokens`` and
    #: ``calibrated_tokens`` both carry the calibrated figure (that is what tiers
    #: and edit validation use); this field keeps the raw number recoverable so a
    #: reader can see how far calibration moved it.
    raw_estimated_tokens: int = 0

    @property
    def usable_limit(self) -> int:
        """Context length minus the reserve, floored at zero."""
        return max(0, self.context_length - self.reserve_tokens)

    @property
    def ratio(self) -> float:
        """Projected usage as a fraction of the raw context length."""
        if self.context_length <= 0:
            return 0.0
        return self.estimated_tokens / self.context_length

    @property
    def tier(self) -> str:
        """Pressure tier for this projection."""
        return pressure_tier(self.ratio, self.thresholds)

    @property
    def fits(self) -> bool:
        """True when the projection leaves the reserve intact."""
        return self.context_length <= 0 or self.estimated_tokens <= self.usable_limit

    @property
    def overflow_tokens(self) -> int:
        """Tokens that must be shed to restore the reserve (0 when it fits).

        An unknown context length means there is no proven overflow, so nothing
        is ever shed on a guess.
        """
        if self.context_length <= 0:
            return 0
        return max(0, self.estimated_tokens - self.usable_limit)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable budget snapshot."""
        return {
            "estimated_tokens": self.estimated_tokens,
            "calibrated_tokens": self.calibrated_tokens,
            "raw_estimated_tokens": self.raw_estimated_tokens,
            "context_length": self.context_length,
            "reserve_tokens": self.reserve_tokens,
            "usable_limit": self.usable_limit,
            "threshold_percent": self.threshold_percent,
            "ratio": round(self.ratio, 6),
            "tier": self.tier,
            "fits": self.fits,
            "overflow_tokens": self.overflow_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
        }


def pressure_state(
    *,
    estimated_tokens: int,
    context_length: int,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
    threshold_percent: float = 0.75,
    thresholds: Sequence[float] = DEFAULT_PRESSURE_THRESHOLDS,
    calibration: Optional[TokenCalibration] = None,
    last_prompt_tokens: int = 0,
) -> PressureState:
    """Build a :class:`PressureState` from a raw estimate plus live model state."""
    calibrated = (
        calibration.adjust(estimated_tokens) if calibration is not None else estimated_tokens
    )
    return PressureState(
        estimated_tokens=max(0, int(calibrated)),
        context_length=max(0, int(context_length or 0)),
        reserve_tokens=max(0, int(reserve_tokens)),
        threshold_percent=float(threshold_percent),
        calibrated_tokens=max(0, int(calibrated)),
        last_prompt_tokens=max(0, int(last_prompt_tokens or 0)),
        thresholds=tuple(float(value) for value in (thresholds or DEFAULT_PRESSURE_THRESHOLDS)),
        raw_estimated_tokens=max(0, int(estimated_tokens or 0)),
    )


@dataclass(frozen=True)
class OffloadCandidate:
    """One tool group the overflow path may replace with a stub."""

    order: int
    group_id: str
    uids: Tuple[str, ...]
    tokens: int
    protected: bool = False
    pinned: bool = False
    #: Store-facing span ids for this group (same order as ``uids``).
    span_ids: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        """Whether this group may be offloaded at all."""
        return bool(self.uids) and not self.protected and not self.pinned and self.tokens > 0

    @property
    def edit_span_ids(self) -> tuple[str, ...]:
        """Ids to put in an ``offload`` operation: span ids, falling back to uids."""
        return self.span_ids or self.uids


def plan_offload(
    candidates: Sequence[OffloadCandidate], *, need_tokens: int
) -> List[OffloadCandidate]:
    """Select whole tool groups, oldest first, until ``need_tokens`` is freed.

    Deterministic and bounded: pinned and protected groups are never selected,
    a group is never split, and the planner stops as soon as the deficit is
    covered (or the candidates run out - a shortfall is reported by the caller,
    never papered over by dropping user or system content).
    """
    if need_tokens <= 0:
        return []
    selected: List[OffloadCandidate] = []
    freed = 0
    for candidate in sorted(candidates, key=lambda item: item.order):
        if freed >= need_tokens:
            break
        if not candidate.eligible:
            continue
        selected.append(candidate)
        freed += candidate.tokens
    return selected
