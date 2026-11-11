"""Metric definitions for the offline benchmark.

Every value here is a deterministic function of the fixture, the strategy and these
constants. None of it is a provider measurement or a bill.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .model import Span, content_sha256, estimate_tokens

# Illustrative frontier list prices, USD per million tokens. Replace with the real
# invoice when reporting a live run; these are only a common ruler for A/B/C/D.
INPUT_USD_PER_MTOKEN = 3.00
CACHED_INPUT_USD_PER_MTOKEN = 0.30
OUTPUT_USD_PER_MTOKEN = 15.00

# Synthetic prefill/decode constants. Relative only; never compare to wall clock.
PREFILL_MS_PER_UNCACHED_TOKEN = 0.010
PREFILL_MS_PER_CACHED_TOKEN = 0.001
DECODE_MS_PER_TOKEN = 4.0
AUX_LATENCY_MS_PER_CALL = 250.0


@dataclass(frozen=True)
class RequestRecord:
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    over_budget: bool

    @property
    def uncached_tokens(self) -> int:
        return self.prompt_tokens - self.cached_tokens


def snapshot(spans: Sequence[Span]) -> list[tuple[str, str, int]]:
    """(span_id, content digest, tokens) for prefix-cache comparison."""
    return [
        (span.span_id, content_sha256(span.text + (span.tool_args or "")), span.tokens())
        for span in spans
    ]


def cached_prefix_tokens(previous: Sequence[tuple[str, str, int]], current: Sequence[Span]) -> int:
    cached = 0
    for (span_id, digest, tokens), span in zip(previous, current, strict=False):
        if span_id != span.span_id or digest != content_sha256(span.text + (span.tool_args or "")):
            break
        cached += tokens
    return cached


def measure_request(
    messages: Sequence[Span],
    previous: Sequence[tuple[str, str, int]],
    completion_text: str,
    *,
    usable_tokens: int,
) -> tuple[RequestRecord, list[tuple[str, str, int]]]:
    prompt = sum(span.tokens() for span in messages)
    cached = cached_prefix_tokens(previous, messages)
    completion = estimate_tokens(completion_text)
    record = RequestRecord(
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
        over_budget=prompt > usable_tokens,
    )
    return record, snapshot(messages)


def request_cost_usd(record: RequestRecord) -> float:
    return (
        record.uncached_tokens * INPUT_USD_PER_MTOKEN
        + record.cached_tokens * CACHED_INPUT_USD_PER_MTOKEN
        + record.completion_tokens * OUTPUT_USD_PER_MTOKEN
    ) / 1_000_000


def request_latency_ms(record: RequestRecord) -> float:
    return (
        record.uncached_tokens * PREFILL_MS_PER_UNCACHED_TOKEN
        + record.cached_tokens * PREFILL_MS_PER_CACHED_TOKEN
        + record.completion_tokens * DECODE_MS_PER_TOKEN
    )


def aggregate(records: Sequence[RequestRecord], *, usable_tokens: int) -> dict[str, float | int]:
    prompt = sum(record.prompt_tokens for record in records)
    cached = sum(record.cached_tokens for record in records)
    completion = sum(record.completion_tokens for record in records)
    return {
        "requests": len(records),
        "prompt_tokens_total": prompt,
        "prompt_tokens_peak": max((record.prompt_tokens for record in records), default=0),
        "cached_tokens_total": cached,
        "uncached_tokens_total": prompt - cached,
        "completion_tokens_total": completion,
        "prefix_reuse_ratio": round(cached / prompt, 4) if prompt else 0.0,
        "sim_cost_usd": round(sum(request_cost_usd(record) for record in records), 6),
        "sim_latency_ms": round(sum(request_latency_ms(record) for record in records), 1),
        "over_budget_requests": sum(1 for record in records if record.over_budget),
        "budget_tokens_usable": usable_tokens,
    }
