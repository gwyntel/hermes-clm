"""Reference strategies for the offline benchmark.

These are *policy models*, not the shipped engine. They exist to make the A/B/C/D
comparison reproducible without a host, a provider or a model call. Each one obeys the
same archive/protection rules; only its compaction policy differs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .model import (
    FACT_PREFIX,
    Op,
    Session,
    Span,
    Step,
    StrategyError,
    estimate_tokens,
    summarize_preserving_facts,
)


def _tokens(spans: Sequence[Span]) -> int:
    return sum(span.tokens() for span in spans)


def _grouped_candidates(session: Session, *, keep_groups: int = 2) -> list[list[Span]]:
    """Editable groups, oldest first, excluding the newest ``keep_groups`` groups."""
    groups: dict[str, list[Span]] = {}
    order: list[str] = []
    loose: list[list[Span]] = []
    for span in session.messages:
        if span.group:
            if span.group not in groups:
                groups[span.group] = []
                order.append(span.group)
            groups[span.group].append(span)
        else:
            loose.append([span])
    ordered = [groups[key] for key in order] + loose
    editable = [
        group for group in ordered if not any(span.protected or span.pinned for span in group)
    ]
    return editable[:-keep_groups] if keep_groups else editable


@dataclass
class Strategy:
    key: str
    label: str
    description: str

    def after_step(self, session: Session, step: Step) -> None:
        return None

    def on_restart(self, session: Session) -> None:
        return None

    def handoff_messages(self, session: Session) -> list[Span]:
        """Default handoff: protected constraints plus the newest three rows."""
        protected = [
            span
            for span in session.messages
            if span.origin in {"system", "operator"} or span.pinned
        ]
        recent = [span for span in session.messages[-3:] if span not in protected]
        return [*protected, *recent]

    def _over(self, session: Session, ratio: float) -> bool:
        return _tokens(session.messages) > int(session.fixture.usable_tokens * ratio)


class DefaultCompressor(Strategy):
    """A: threshold summary at 75%, protected prefix and newest six rows kept."""

    def __init__(self) -> None:
        super().__init__(
            key="A",
            label="Default compressor (modelled)",
            description="Summarise the oldest editable rows once prompt pressure exceeds 75% of usable tokens.",
        )

    def after_step(self, session: Session, step: Step) -> None:
        if step.kind == "assistant" or not self._over(session, 0.75):
            return
        eligible: list[Span] = []
        for index, span in enumerate(session.messages):
            if index < 3 or index >= len(session.messages) - 6:
                continue
            eligible.append(span)
        if len(eligible) < 2:
            return
        summary = "\n".join(
            f"[{span.role}] {span.text.strip()[:120]}" for span in eligible if span.text.strip()
        )
        first = eligible[0]
        ops: list[Op] = [Op("rewrite", (first.span_id,), summary)]
        ops.extend(Op("drop", (span.span_id,)) for span in eligible[1:])
        try:
            session.apply(ops, reason="default-summary", archive=False)
        except StrategyError:
            return


class Microcompaction(Strategy):
    """B: low-trigger pruning of old tool results; no summary, no archive."""

    def __init__(self) -> None:
        super().__init__(
            key="B",
            label="Microcompaction (modelled)",
            description="Replace old tool results with a length note at 50% pressure; drop the oldest groups under hard pressure.",
        )

    def after_step(self, session: Session, step: Step) -> None:
        if step.kind == "assistant":
            return
        tool_spans = [
            span
            for span in session.messages
            if span.role == "tool" and not span.protected and not span.pinned
        ]
        if self._over(session, 0.5):
            old = tool_spans[:-2]
            ops = [
                Op("rewrite", (span.span_id,), f"[tool result pruned: {len(span.text)} chars]")
                for span in old
                if not span.protected and not span.pinned
            ]
            if ops:
                try:
                    session.apply(ops, reason="micro-prune", archive=False)
                except StrategyError:
                    pass
        if self._over(session, 0.9):
            groups = _grouped_candidates(session, keep_groups=1)
            if groups:
                oldest = groups[0]
                try:
                    session.apply(
                        [Op("drop", tuple(span.span_id for span in oldest))],
                        reason="micro-drop",
                        archive=False,
                    )
                except StrategyError:
                    pass


class ClmPolicy(Strategy):
    """C: model-directed span edits with archive-backed offload and fact preservation."""

    def __init__(self) -> None:
        super().__init__(
            key="C",
            label="CLM (model-directed edits)",
            description="Resolve stale facts, then batch rewrite/offload settled spans, preserving FACT lines and archiving exact bytes.",
        )
        self._resolved: set[str] = set()

    def after_step(self, session: Session, step: Step) -> None:
        if step.kind == "assistant":
            return
        self._resolve_conflicts(session)
        self._pressure_edit(session, step)

    def _resolve_conflicts(self, session: Session) -> None:
        for conflict in session.fixture.conflicts:
            fact_id = str(conflict["fact"])
            stale = str(conflict["stale_marker"])
            at_step = int(conflict["superseded_at_step"])
            replacement = f"{FACT_PREFIX}{fact_id}]: superseded at step {at_step}"
            ops = [
                Op("rewrite", (span.span_id,), replacement)
                for span in session.messages
                if stale in span.text and not span.protected and not span.pinned
            ]
            if not ops:
                continue
            try:
                session.apply(ops, reason=f"resolve-{fact_id}")
            except StrategyError:
                continue

    def _needed(self, session: Session) -> int:
        return max(0, _tokens(session.messages) - int(session.fixture.usable_tokens * 0.55))

    def _pressure_edit(self, session: Session, step: Step) -> None:
        usable = session.fixture.usable_tokens
        current = _tokens(session.messages)
        huge = [
            span
            for span in session.messages
            if not span.protected and span.tokens() > usable * 0.18
        ]
        if current <= usable * 0.75 and not huge:
            return
        needed = max(self._needed(session), 1)
        reclaimed = 0
        ops: list[Op] = []
        for group in reversed(_grouped_candidates(session, keep_groups=2)):
            if reclaimed >= needed:
                break
            for span in group:
                if span.protected or span.pinned:
                    continue
                if span.role == "tool" and span.tokens() > usable * 0.12:
                    if self._has_source_fact(session, span):
                        ops.append(
                            Op(
                                "rewrite",
                                (span.span_id,),
                                summarize_preserving_facts(span.text),
                            )
                        )
                    else:
                        ops.append(Op("offload", (span.span_id,)))
                    reclaimed += max(0, span.tokens() - estimate_tokens(span.text[:240]))
                elif span.role == "assistant" and span.tokens() > usable * 0.06:
                    ops.append(
                        Op(
                            "rewrite",
                            (span.span_id,),
                            summarize_preserving_facts(span.text),
                        )
                    )
                    reclaimed += max(0, span.tokens() - estimate_tokens(span.text[:240]))
        if ops:
            deduped: dict[str, Op] = {op.span_ids[0]: op for op in ops}
            try:
                session.apply(list(deduped.values()), reason=f"clm-edit-step-{step.index}")
            except StrategyError:
                return

    @staticmethod
    def _has_source_fact(session: Session, span: Span) -> bool:
        return any(fact.source_step == span.step for fact in session.fixture.facts)

    def handoff_messages(self, session: Session) -> list[Span]:
        facts: list[str] = []
        operators: list[str] = []
        for span in session.messages:
            if span.origin == "operator":
                operators.append(span.text)
        for fact in session.fixture.facts:
            for span in session.messages:
                if fact.marker in span.text:
                    line = next(
                        (line for line in span.text.splitlines() if fact.marker in line),
                        f"{fact.marker}: {span.text[:120]}",
                    )
                    facts.append(line.strip())
                    break
        body = "\n".join(
            [
                "[context handoff snapshot]",
                *operators,
                *dict.fromkeys(facts),
                "origin=system; untrusted spans were not carried forward",
            ]
        )
        return [
            Span(
                span_id="handoff-system",
                role="system",
                origin="system",
                text=body,
                step=10_000,
                protected=True,
                pinned=True,
            )
        ]


class ClmWithAux(ClmPolicy):
    """D: C plus a deterministic mock of the auxiliary compression proposal."""

    def __init__(self) -> None:
        Strategy.__init__(
            self,
            key="D",
            label="CLM + mock aux proposal",
            description="C plus a local extractive 'aux' proposal; no model or network call is made.",
        )
        self._resolved = set()
        self._aux_used = 0

    def after_step(self, session: Session, step: Step) -> None:
        self._propose(session)
        super().after_step(session, step)

    def _propose(self, session: Session) -> None:
        if self._aux_used >= 2:
            return
        if _tokens(session.messages) <= session.fixture.usable_tokens * 0.75:
            return
        candidates = [
            span
            for group in _grouped_candidates(session, keep_groups=2)
            for span in group
            if span.role in {"tool", "assistant"} and span.tokens() > 80
        ]
        if not candidates:
            return
        target = max(candidates, key=lambda span: span.tokens())
        replacement = summarize_preserving_facts(target.text, limit=200)
        proposal = {
            "proposal_id": hashlib.sha256(
                f"{target.span_id}:{session.revision}".encode()
            ).hexdigest()[:16],
            "proposal_sha256": hashlib.sha256(replacement.encode()).hexdigest(),
            "base_revision": session.revision,
            "origin": "aux",
        }
        session.aux_calls += 1
        session.aux_tokens += estimate_tokens(target.text) + estimate_tokens(replacement)
        # Acceptance binds id + hash + base revision; a stale base revision is refused.
        if proposal["base_revision"] != session.revision:
            return
        if hashlib.sha256(replacement.encode()).hexdigest() != proposal["proposal_sha256"]:
            return
        try:
            session.apply(
                [Op("rewrite", (target.span_id,), replacement)],
                reason="aux-proposal-accepted",
            )
        except StrategyError:
            return
        self._aux_used += 1


STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "A": DefaultCompressor,
    "B": Microcompaction,
    "C": ClmPolicy,
    "D": ClmWithAux,
}


def build_strategies(names: Sequence[str] | None = None) -> list[Strategy]:
    keys = [key.strip().upper() for key in names] if names else sorted(STRATEGIES)
    unknown = [key for key in keys if key not in STRATEGIES]
    if unknown:
        raise StrategyError(f"unknown strategy: {', '.join(unknown)}")
    return [STRATEGIES[key]() for key in keys]
