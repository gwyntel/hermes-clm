"""Core data model, deterministic estimator and session simulator."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FACT_PREFIX = "FACT["


class BenchmarkError(RuntimeError):
    """Invalid fixture or benchmark configuration."""


class StrategyError(BenchmarkError):
    """A strategy attempted an operation the invariants refuse."""


def estimate_tokens(text: str) -> int:
    """Deterministic, tokenizer-free estimate: four characters per token, rounded up."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Fact:
    fact_id: str
    mode: str  # pinned | extract | archive | latest
    source_step: int
    stale_marker: str | None = None
    current_marker: str | None = None

    @property
    def marker(self) -> str:
        return f"{FACT_PREFIX}{self.fact_id}]"


@dataclass(frozen=True)
class Step:
    index: int
    kind: str  # system | user | assistant | tool
    text: str
    tool_name: str | None = None
    tool_args: str | None = None
    group: str | None = None
    facts: tuple[str, ...] = ()
    injection: bool = False
    boundary: bool = False


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    title: str
    description: str
    budget_tokens: int
    reserve_tokens: int
    steps: tuple[Step, ...]
    facts: tuple[Fact, ...]
    crash_at_step: int | None = None
    injection_markers: tuple[str, ...] = ()
    retrieval_step: int | None = None
    handoff_step: int | None = None
    conflicts: tuple[dict[str, Any], ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def usable_tokens(self) -> int:
        return self.budget_tokens - self.reserve_tokens


@dataclass
class Span:
    span_id: str
    role: str
    origin: str  # operator | system | model | tool | aux
    text: str
    step: int
    group: str | None = None
    tool_name: str | None = None
    tool_args: str | None = None
    protected: bool = False
    pinned: bool = False
    offloaded: bool = False

    def tokens(self) -> int:
        return estimate_tokens(self.text) + estimate_tokens(self.tool_args or "") + 4

    def key(self) -> tuple[str, str]:
        return (self.span_id, content_sha256(self.text + (self.tool_args or "")))


@dataclass(frozen=True)
class Op:
    kind: str  # drop | rewrite | offload
    span_ids: tuple[str, ...]
    replacement: str | None = None


@dataclass
class EditEvent:
    reason: str
    revision: int
    position: int
    before_tokens: int
    after_tokens: int
    tail_tokens: int
    kinds: dict[str, int]
    spans: tuple[str, ...]

    @property
    def reclaimed(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)


@dataclass
class Session:
    fixture: Fixture
    raw: list[Span] = field(default_factory=list)
    messages: list[Span] = field(default_factory=list)
    ops_log: list[tuple[Op, ...]] = field(default_factory=list)
    archive: dict[str, dict[str, str]] = field(default_factory=dict)
    revision: int = 0
    edits: list[EditEvent] = field(default_factory=list)
    recoveries: int = 0
    abandoned_edits: int = 0
    overflow_events: int = 0
    aux_calls: int = 0
    aux_tokens: int = 0
    handoff_used: bool = False

    # -- ingest ---------------------------------------------------------
    def append(self, step: Step) -> Span:
        origin = {
            "system": "system",
            "user": "operator",
            "assistant": "model",
            "tool": "tool",
        }.get(step.kind, "model")
        fact_modes = {fact.fact_id: fact.mode for fact in self.fixture.facts}
        pinned = any(fact_modes.get(fact_id) == "pinned" for fact_id in step.facts)
        protected = step.kind in {"system", "user"} or pinned
        span = Span(
            span_id=f"s{step.index:04d}",
            role=step.kind,
            origin=origin,
            text=step.text,
            step=step.index,
            group=step.group,
            tool_name=step.tool_name,
            tool_args=step.tool_args,
            protected=protected,
            pinned=pinned,
        )
        self.raw.append(copy.deepcopy(span))
        self.messages.append(span)
        return span

    # -- projection -----------------------------------------------------
    def _expand_groups(self, span_ids: Iterable[str]) -> list[str]:
        wanted = set(span_ids)
        groups = {span.group for span in self.messages if span.span_id in wanted and span.group}
        for span in self.messages:
            if span.group in groups:
                wanted.add(span.span_id)
        return sorted(wanted)

    def apply(self, ops: Sequence[Op], *, reason: str, archive: bool = True) -> EditEvent | None:
        """Apply an atomic edit. Refuses protected/pinned spans and partial groups."""
        resolved: list[Op] = []
        for op in ops:
            if op.kind not in {"drop", "rewrite", "offload"}:
                raise StrategyError(f"unknown operation kind: {op.kind}")
            ids = self._expand_groups(op.span_ids)
            spans = [span for span in self.messages if span.span_id in ids]
            if len(spans) != len(ids):
                raise StrategyError(f"operation names unknown spans: {op.span_ids}")
            if any(span.protected or span.pinned for span in spans):
                raise StrategyError("operation targets a protected or pinned span")
            if op.kind == "rewrite" and op.replacement is None:
                raise StrategyError("rewrite requires a replacement")
            resolved.append(Op(op.kind, tuple(ids), op.replacement))

        index = {span.span_id: i for i, span in enumerate(self.messages)}
        position = min(index[span_id] for op in resolved for span_id in op.span_ids)
        before = sum(span.tokens() for span in self.messages)
        tail = sum(span.tokens() for span in self.messages[position:])

        by_id = {span.span_id: span for span in self.messages}
        drop: set[str] = set()
        replacements: dict[str, str] = {}
        kinds: dict[str, int] = {"drop": 0, "rewrite": 0, "offload": 0}
        for op in resolved:
            for span_id in op.span_ids:
                span = by_id[span_id]
                if archive:
                    self.archive.setdefault(
                        span_id,
                        {
                            "text": span.text,
                            "sha256": content_sha256(span.text),
                            "tool_args_sha256": content_sha256(span.tool_args or ""),
                        },
                    )
                if op.kind == "drop":
                    drop.add(span_id)
                elif op.kind == "rewrite":
                    replacements[span_id] = op.replacement or ""
                    span.offloaded = True
                else:
                    replacements[span_id] = offload_stub(span_id, span.text)
                    span.offloaded = True
                kinds[op.kind] += 1

        for span_id, text in replacements.items():
            by_id[span_id].text = text
        if drop:
            self.messages = [span for span in self.messages if span.span_id not in drop]

        self.revision += 1
        self.ops_log.append(tuple(resolved))
        event = EditEvent(
            reason=reason,
            revision=self.revision,
            position=position,
            before_tokens=before,
            after_tokens=sum(span.tokens() for span in self.messages),
            tail_tokens=tail,
            kinds=kinds,
            spans=tuple(sorted({span_id for op in resolved for span_id in op.span_ids})),
        )
        self.edits.append(event)
        return event

    # -- recovery -------------------------------------------------------
    def crash(self) -> None:
        """Simulate process death: raw history is durable, projection is rebuilt from ops."""
        self.recoveries += 1
        self.messages = copy.deepcopy(self.raw)
        for batch in self.ops_log:
            self._replay(batch)

    def _replay(self, ops: Sequence[Op]) -> None:
        by_id = {span.span_id: span for span in self.messages}
        drop: set[str] = set()
        for op in ops:
            for span_id in op.span_ids:
                span = by_id.get(span_id)
                if span is None:
                    continue
                if op.kind == "drop":
                    drop.add(span_id)
                elif op.kind == "rewrite":
                    span.text = op.replacement or ""
                else:
                    span.text = offload_stub(span_id, span.text)
        if drop:
            self.messages = [span for span in self.messages if span.span_id not in drop]

    def verify(self) -> dict[str, Any]:
        """Check archive integrity and revision/projection agreement after a restart."""
        bad_archive = [
            span_id
            for span_id, record in self.archive.items()
            if content_sha256(record["text"]) != record["sha256"]
        ]
        return {
            "archive_entries": len(self.archive),
            "archive_corrupt": len(bad_archive),
            "revision": self.revision,
            "committed_batches": len(self.ops_log),
            "raw_spans": len(self.raw),
            "projected_spans": len(self.messages),
            "recovery_clean": not bad_archive and self.revision == len(self.ops_log),
        }

    # -- retrieval / handoff --------------------------------------------
    def retrieve(self, span_id: str) -> dict[str, str] | None:
        return self.archive.get(span_id)

    def retrieve_exact(self, span_id: str, expected_text: str) -> bool:
        record = self.archive.get(span_id)
        return bool(
            record
            and record["text"] == expected_text
            and record["sha256"] == content_sha256(expected_text)
        )

    def projected_text(self) -> str:
        parts = [span.text for span in self.messages]
        parts.extend(span.tool_args or "" for span in self.messages)
        return "\n".join(parts)


def offload_stub(span_id: str, text: str) -> str:
    """Schema-like, non-imperative stub. Never contains the archived bytes."""
    payload = {
        "offloaded_span": span_id,
        "sha256": content_sha256(text)[:16],
        "chars": len(text),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def summarize_preserving_facts(text: str, *, limit: int = 240) -> str:
    """Deterministic extractive summary that carries every FACT line forward."""
    facts = [line.strip() for line in text.splitlines() if line.strip().startswith(FACT_PREFIX)]
    prose = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(FACT_PREFIX)
    ]
    head = prose[0][:limit] if prose else ""
    if facts:
        return "\n".join([*facts, head]).strip()
    return head or text[:limit]


def has_fact(text: str, fact: Fact) -> bool:
    return fact.marker in text


def load_fixture(path: Path) -> Fixture:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read fixture {path}: {exc}") from exc

    steps = tuple(
        Step(
            index=int(raw["index"]),
            kind=str(raw["kind"]),
            text=str(raw["text"]),
            tool_name=raw.get("tool_name"),
            tool_args=raw.get("tool_args"),
            group=raw.get("group"),
            facts=tuple(raw.get("facts", ())),
            injection=bool(raw.get("injection", False)),
            boundary=bool(raw.get("boundary", False)),
        )
        for raw in payload["steps"]
    )
    facts = tuple(
        Fact(
            fact_id=str(raw["id"]),
            mode=str(raw.get("mode", "extract")),
            source_step=int(raw["source_step"]),
            stale_marker=raw.get("stale_marker"),
            current_marker=raw.get("current_marker"),
        )
        for raw in payload.get("facts", ())
    )
    if not steps or steps[0].kind != "system":
        raise BenchmarkError(f"fixture {path.name}: first step must be the system message")
    if payload["reserve_tokens"] >= payload["budget_tokens"]:
        raise BenchmarkError(f"fixture {path.name}: reserve must be smaller than budget")
    fixture = Fixture(
        fixture_id=str(payload["id"]),
        title=str(payload["title"]),
        description=str(payload.get("description", "")),
        budget_tokens=int(payload["budget_tokens"]),
        reserve_tokens=int(payload["reserve_tokens"]),
        steps=steps,
        facts=facts,
        crash_at_step=payload.get("crash_at_step"),
        injection_markers=tuple(payload.get("injection_markers", ())),
        retrieval_step=payload.get("retrieval_step"),
        handoff_step=payload.get("handoff_step"),
        conflicts=tuple(payload.get("conflicts", ())),
        notes=tuple(payload.get("notes", ())),
    )
    known = {step.index for step in steps}
    for fact in facts:
        if fact.source_step not in known:
            raise BenchmarkError(f"fixture {path.name}: fact {fact.fact_id} has no source step")
    return fixture


def load_fixtures(directory: Path) -> list[Fixture]:
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise BenchmarkError(f"no fixtures found in {directory}")
    return [load_fixture(path) for path in paths]
