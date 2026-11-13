"""Chunked auxiliary rewrites: lossless split, bounded calls, whole-span output.

The old contract refused any span longer than one prompt (4000 chars), which
excluded exactly the tool results worth compressing. The contract now is
map-reduce: split losslessly, one bounded request per chunk, reassemble, and
refuse *before the first call* when the budget cannot cover the span. These
tests are the counterexamples that keep that safe: a rewrite may never be built
from a prefix, and a partly-returned span may never be joined.
"""

from __future__ import annotations

import json
import re

import pytest
from clm_fakes import aux_json

from hermes_clm.auxiliary import (
    MAX_AUX_CALLS,
    MAX_PROMPT_SPAN_CHARS,
    MAX_SPAN_CHUNKS,
    AuxCompressor,
    AuxLimits,
    _split_chunks,
)


class SequenceClient:
    """Aux client returning one reply per call, in order, and recording prompts."""

    def __init__(self, replies: list[object]) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("more auxiliary calls than the plan allows")
        return self._replies.pop(0)

    def prompts(self) -> list[str]:
        return [call["messages"][1]["content"] for call in self.calls]


def span_text(sentences: int, *, line: str = "tool output line") -> str:
    """A realistic multi-line tool result (newline boundaries are preferred cuts)."""

    return "\n".join(f"{line} {index:04d} ok" for index in range(sentences))


def rewritten(span_id: str, text: str) -> object:
    return aux_json({"rewrites": [{"span_id": span_id, "text": text}]})


def _span_block(prompt: str, span_id: str) -> str:
    """The span text carried by one auxiliary prompt (between the markers)."""

    match = re.search(
        rf"\nSPAN {re.escape(span_id)}[^\n]*\n(.*)\nEND SPAN {re.escape(span_id)}",
        prompt,
        re.DOTALL,
    )
    assert match is not None, prompt[:200]
    return match.group(1)


class PlanClient:
    """Aux client whose reply is computed from the prompt it receives."""

    def __init__(self, plan) -> None:
        self._plan = plan
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) > 32:
            raise AssertionError("unbounded auxiliary calls")
        return self._plan(kwargs["messages"][1]["content"], len(self.calls))

    def prompts(self) -> list[str]:
        return [call["messages"][1]["content"] for call in self.calls]


@pytest.fixture()
def service():
    from clm_fakes import FakeService

    return FakeService(revision=3)


# --------------------------------------------------------------------------- #
# The split primitive
# --------------------------------------------------------------------------- #


def test_split_chunks_is_lossless_and_bounded():
    text = span_text(400)
    for size in (1, 7, 50, 999, 10_000):
        chunks = _split_chunks(text, size)
        assert "".join(chunks) == text, f"size={size} lost or reordered bytes"
        assert all(len(chunk) <= size for chunk in chunks), f"size={size} exceeded the cap"


def test_split_chunks_does_not_split_a_short_span():
    assert _split_chunks("abc", 10) == ["abc"]
    assert _split_chunks("", 10) == [""]


# --------------------------------------------------------------------------- #
# A large span is rewritten whole, in bounded requests
# --------------------------------------------------------------------------- #


def test_large_span_is_rewritten_in_chunks_and_reassembled(service):
    text = span_text(600)  # ~15k chars, well over the old 4k refusal threshold
    assert len(text) > MAX_PROMPT_SPAN_CHARS
    service.add_span("m-0100", text)
    client = SequenceClient([rewritten("m-0100", f"part {index}") for index in range(1, 4)])
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=6_000))

    result = compressor.propose(service, span_ids=["m-0100"])

    assert result["ok"] is True, result
    assert len(client.calls) > 1, "a span over one chunk must not ride in a single call"
    # Every chunk of the span reached the model exactly once, so the rewrite
    # covers the whole span rather than a prefix of it.
    sent = "".join(_span_block(prompt, "m-0100") for prompt in client.prompts())
    assert sent == text
    for index, prompt in enumerate(client.prompts(), start=1):
        assert f"part={index}/" in prompt, "a chunked request must say which part it carries"
        assert len(prompt) <= 24_000

    rewrite = result["proposal"]["operations"][0]["replacement"]
    assert rewrite == "\n".join(f"part {index}" for index in range(1, 4))
    assert result["proposal"]["operations"][0]["kind"] == "rewrite"


def test_chunked_request_keeps_the_original_span_id_and_metadata(service):
    service.add_span("m-0101", span_text(200))
    client = SequenceClient([rewritten("m-0101", "compressed")] * 8)
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=1_000))

    result = compressor.propose(service, span_ids=["m-0101"])

    assert result["ok"] is True, result
    operations = result["proposal"]["operations"]
    assert len(operations) == 1
    assert operations[0]["span_ids"] == ["m-0101"]
    first = client.prompts()[0]
    assert "role=tool" in first and "origin=host" in first


# --------------------------------------------------------------------------- #
# Known counterexamples: refuse, never half-rewrite
# --------------------------------------------------------------------------- #


def test_span_needing_more_chunks_than_the_budget_is_refused_before_any_call(service):
    service.add_span("m-0102", "y" * 20_000)
    client = SequenceClient([rewritten("m-0102", "short")])
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=1_000, max_span_chunks=4))

    result = compressor.propose(service, span_ids=["m-0102"])

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "span_too_large"
    assert "20000" in result["errors"][0]["message"]
    assert client.calls == [], "an unaffordable span must not spend a single call"
    assert compressor.metrics()["calls"] == 0


def test_batch_needing_more_calls_than_the_budget_is_refused_before_any_call(service):
    for index in range(3):
        service.add_span(f"m-020{index}", "z" * 5_000)
    client = SequenceClient([rewritten("m-0200", "short")] * 8)
    compressor = AuxCompressor(
        client,
        limits=AuxLimits(prompt_span_chars=1_000, max_span_chunks=8, max_aux_calls=3),
    )

    result = compressor.propose(service, span_ids=["m-0200", "m-0201", "m-0202"])

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "span_too_large"
    assert "auxiliary calls" in result["errors"][0]["message"]
    assert client.calls == [], "the batch is refused as a unit"


def test_partly_returned_chunked_span_is_refused_rather_than_joined(service):
    """Joining 2 of 3 parts would present a truncated rewrite as the whole span."""

    service.add_span("m-0103", span_text(300))

    def plan(prompt: str, index: int):
        # The model answers the first part and then declines the rest, which is
        # the shape that would produce a "whole span" rewrite from its prefix.
        if "part=1/" in prompt:
            return rewritten("m-0103", "compressed part")
        return aux_json({"rewrites": []})

    client = PlanClient(plan)
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=1_000))

    result = compressor.propose(service, span_ids=["m-0103"])

    assert result["ok"] is False
    assert result["errors"][0]["code"] in {"aux_incomplete", "aux_invalid_output"}
    assert "create_proposal" not in service.names()


def test_assembled_rewrite_over_the_byte_cap_is_refused_even_when_parts_fit(service):
    """Two parts under the cap can sum past it; the whole is what the store takes."""

    text = span_text(120)
    service.add_span("m-0104", text)
    part = "a" * 60
    client = SequenceClient([rewritten("m-0104", part)] * 40)
    compressor = AuxCompressor(
        client,
        limits=AuxLimits(prompt_span_chars=500, max_rewrite_bytes=100, max_aux_calls=8),
    )

    result = compressor.propose(service, span_ids=["m-0104"])

    assert result["ok"] is False, "a rewrite whose parts each fit must still fit whole"
    assert result["errors"][0]["code"] == "aux_invalid_output"
    assert "create_proposal" not in service.names()


def test_small_spans_still_travel_in_one_packed_call(service):
    """Chunking must not turn a normal batch into one call per span."""

    for index in range(3):
        service.add_span(f"m-030{index}", f"short output {index}")
    client = SequenceClient(
        [
            aux_json(
                {
                    "rewrites": [
                        {"span_id": f"m-030{index}", "text": f"s{index}"} for index in range(3)
                    ]
                }
            )
        ]
    )
    compressor = AuxCompressor(client)

    result = compressor.propose(service, span_ids=["m-0300", "m-0301", "m-0302"])

    assert result["ok"] is True, result
    assert len(client.calls) == 1
    assert [op["span_ids"][0] for op in result["proposal"]["operations"]] == [
        "m-0300",
        "m-0301",
        "m-0302",
    ]


def test_default_chunk_budget_is_documented_by_the_constants():
    assert MAX_PROMPT_SPAN_CHARS == 8_000
    assert MAX_SPAN_CHUNKS == 8
    assert MAX_AUX_CALLS == 8
    assert MAX_PROMPT_SPAN_CHARS * MAX_SPAN_CHUNKS >= 27_000, (
        "the largest realistic tool result (27k chars) must fit the chunk budget"
    )


def test_chunked_run_reports_metrics_for_every_call(service):
    service.add_span("m-0105", span_text(200))
    client = SequenceClient([rewritten("m-0105", "compressed")] * 8)
    compressor = AuxCompressor(client, limits=AuxLimits(prompt_span_chars=1_000))

    result = compressor.propose(service, span_ids=["m-0105"])

    assert result["ok"] is True, result
    metrics = compressor.metrics()
    assert metrics["calls"] == len(client.calls) > 1
    assert metrics["failures"] == 0
    assert json.loads(json.dumps(metrics))  # metrics stay serialisable
