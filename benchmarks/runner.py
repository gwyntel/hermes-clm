"""Run the deterministic A/B/C/D benchmark and render JSON/Markdown reports."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import EVIDENCE_CLASS, SCHEMA_VERSION
from .metrics import (
    AUX_LATENCY_MS_PER_CALL,
    CACHED_INPUT_USD_PER_MTOKEN,
    DECODE_MS_PER_TOKEN,
    INPUT_USD_PER_MTOKEN,
    OUTPUT_USD_PER_MTOKEN,
    PREFILL_MS_PER_CACHED_TOKEN,
    PREFILL_MS_PER_UNCACHED_TOKEN,
    RequestRecord,
    aggregate,
    measure_request,
)
from .model import (
    BenchmarkError,
    Fact,
    Fixture,
    Session,
    Span,
    Step,
    estimate_tokens,
    has_fact,
    load_fixtures,
)
from .strategies import Strategy, build_strategies

DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures"

LIMITATIONS = [
    "Strategies are reference policies, not the shipped hermes-clm engine.",
    "Token counts use a fixed 4-characters-per-token estimator, not a provider tokenizer.",
    "Cost uses illustrative list prices and latency uses synthetic constants; neither is a bill or a timing measurement.",
    "No model is called, so retention measures structural survival of FACT lines, not task accuracy.",
    "Injection containment checks provenance/elevation only; it cannot measure whether a model would obey injected text.",
]


def _fact_retained(session: Session, fact: Fact, text: str) -> bool:
    in_context = has_fact(text, fact)
    if fact.mode in {"pinned", "extract"}:
        return in_context
    if fact.mode == "archive":
        return in_context or any(
            has_fact(record["text"], fact) for record in session.archive.values()
        )
    if fact.mode == "latest" and fact.current_marker:
        return (
            in_context
            and fact.current_marker in text
            and (fact.stale_marker is None or fact.stale_marker not in text)
        )
    return in_context


def _fact_summary(session: Session, text: str) -> tuple[int, int]:
    retained = 0
    for fact in session.fixture.facts:
        if _fact_retained(session, fact, text):
            retained += 1
    return retained, len(session.fixture.facts)


def _injection_report(session: Session) -> tuple[bool, bool]:
    markers = session.fixture.injection_markers
    if not markers:
        return True, True
    system_text = session.fixture.steps[0].text
    system_preserved = bool(session.messages) and session.messages[0].text == system_text
    elevated = False
    for span in session.messages:
        if span.role == "system" or span.origin == "operator":
            if span.step != session.fixture.steps[0].index and any(
                marker in span.text for marker in markers
            ):
                elevated = True
    return system_preserved, not elevated


def _handoff_report(session: Session, handoff: Sequence[Span] | None) -> tuple[float, bool, int]:
    if handoff is None:
        return 1.0, True, 0
    text = "\n".join(span.text for span in handoff)
    facts = [fact for fact in session.fixture.facts if fact.mode != "archive"]
    present = sum(1 for fact in facts if has_fact(text, fact))
    ratio = present / len(facts) if facts else 1.0
    markers = session.fixture.injection_markers
    origin_safe = not any(
        span.origin in {"operator", "system"} and any(marker in span.text for marker in markers)
        for span in handoff
    )
    return round(ratio, 4), origin_safe, estimate_tokens(text)


def _retrieval_exact(session: Session) -> bool:
    if session.fixture.retrieval_step is None:
        return True
    for fact in session.fixture.facts:
        if fact.mode != "archive":
            continue
        source = next((span for span in session.raw if has_fact(span.text, fact)), None)
        if source is None:
            return False
        return session.retrieve_exact(source.span_id, source.text)
    return True


def simulate(fixture: Fixture, strategy: Strategy) -> dict[str, Any]:
    session = Session(fixture)
    records: list[RequestRecord] = []
    previous: list[tuple[str, str, int]] = []
    recovery_clean = True
    handoff: list[Span] | None = None
    pending: Step | None = None

    for step in fixture.steps:
        if step.kind == "assistant":
            completion = step.text + (step.tool_args or "")
            record, previous = measure_request(
                session.messages,
                previous,
                completion,
                usable_tokens=fixture.usable_tokens,
            )
            records.append(record)
            if pending is not None:
                # The edit lands after the model has seen the raw tool result once.
                strategy.after_step(session, pending)
                pending = None
        session.append(step)
        if step.kind != "assistant":
            pending = step
        if fixture.crash_at_step == step.index:
            session.crash()
            strategy.on_restart(session)
            recovery_clean = session.verify()["recovery_clean"]
            previous = []
            pending = None
        if fixture.handoff_step == step.index:
            handoff = strategy.handoff_messages(session)
            session.handoff_used = True

    projected = session.projected_text()
    retained, total = _fact_summary(session, projected)
    system_preserved, injection_contained = _injection_report(session)
    conflicts = [
        conflict
        for conflict in fixture.conflicts
        if str(conflict["stale_marker"]) not in projected
        and str(conflict["current_marker"]) in projected
    ]
    conflict_resolved = len(conflicts) == len(fixture.conflicts)
    handoff_ratio, handoff_origin_safe, handoff_tokens = _handoff_report(session, handoff)
    retrieval_exact = _retrieval_exact(session)
    verify = session.verify()

    edits = session.edits
    result: dict[str, Any] = {
        "fixture": fixture.fixture_id,
        "fixture_title": fixture.title,
        "strategy": strategy.key,
        "strategy_label": strategy.label,
        **aggregate(records, usable_tokens=fixture.usable_tokens),
        "final_prompt_tokens": sum(span.tokens() for span in session.messages),
        "edits": len(edits),
        "spans_dropped": sum(event.kinds["drop"] for event in edits),
        "spans_rewritten": sum(event.kinds["rewrite"] for event in edits),
        "spans_offloaded": sum(event.kinds["offload"] for event in edits),
        "reclaimed_tokens": sum(event.reclaimed for event in edits),
        "cache_break_tokens": sum(event.tail_tokens for event in edits),
        "archive_entries": len(session.archive),
        "aux_calls": session.aux_calls,
        "aux_tokens": session.aux_tokens,
        "aux_cost_usd": round(session.aux_tokens * INPUT_USD_PER_MTOKEN / 1_000_000, 6),
        "aux_latency_ms": round(session.aux_calls * AUX_LATENCY_MS_PER_CALL, 1),
        "recoveries": session.recoveries,
        "facts_retained": retained,
        "facts_total": total,
        "retention_ratio": round(retained / total, 4) if total else 1.0,
        "system_preserved": system_preserved,
        "injection_contained": injection_contained,
        "conflict_resolved": conflict_resolved,
        "recovery_clean": recovery_clean,
        "archive_integrity": verify["archive_corrupt"] == 0,
        "retrieval_exact": retrieval_exact,
        "handoff_fidelity": handoff_ratio,
        "handoff_origin_safe": handoff_origin_safe,
        "handoff_tokens": handoff_tokens,
        "verify": verify,
    }
    result["sim_cost_usd"] = round(float(result["sim_cost_usd"]) + float(result["aux_cost_usd"]), 6)
    result["checks_passed"] = (
        retained == total
        and system_preserved
        and injection_contained
        and conflict_resolved
        and recovery_clean
        and verify["archive_corrupt"] == 0
        and retrieval_exact
        and handoff_ratio == 1.0
        and handoff_origin_safe
    )
    return result


def run_all(
    fixtures_dir: str | Path | None = None,
    strategy_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    directory = Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES
    fixtures = load_fixtures(directory)
    strategies = build_strategies(strategy_names)
    results = [simulate(fixture, strategy) for fixture in fixtures for strategy in strategies]
    totals: dict[str, dict[str, Any]] = {}
    for strategy in strategies:
        rows = [row for row in results if row["strategy"] == strategy.key]
        totals[strategy.key] = {
            "label": strategy.label,
            "fixtures": len(rows),
            "requests": sum(int(row["requests"]) for row in rows),
            "prompt_tokens_total": sum(int(row["prompt_tokens_total"]) for row in rows),
            "sim_cost_usd": round(sum(float(row["sim_cost_usd"]) for row in rows), 6),
            "edits": sum(int(row["edits"]) for row in rows),
            "aux_calls": sum(int(row["aux_calls"]) for row in rows),
            "facts_retained": sum(int(row["facts_retained"]) for row in rows),
            "facts_total": sum(int(row["facts_total"]) for row in rows),
            "fixtures_all_facts": sum(
                1 for row in rows if int(row["facts_retained"]) == int(row["facts_total"])
            ),
            "fixtures_checks_passed": sum(1 for row in rows if row["checks_passed"]),
            "fixtures_injection_contained": sum(
                1 for row in rows if bool(row["injection_contained"])
            ),
            "fixtures_recovery_ok": sum(
                1 for row in rows if bool(row["recovery_clean"]) and bool(row["retrieval_exact"])
            ),
            "fixtures_handoff_complete": sum(
                1 for row in rows if float(row["handoff_fidelity"]) == 1.0
            ),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "strategies": [
            {"key": strategy.key, "label": strategy.label, "description": strategy.description}
            for strategy in strategies
        ],
        "fixtures": [
            {
                "id": fixture.fixture_id,
                "title": fixture.title,
                "description": fixture.description,
                "budget_tokens": fixture.budget_tokens,
                "reserve_tokens": fixture.reserve_tokens,
                "steps": len(fixture.steps),
                "facts": len(fixture.facts),
                "notes": list(fixture.notes),
            }
            for fixture in fixtures
        ],
        "model": {
            "text_estimator": "ceil(len(text)/4) per message plus 4 tokens overhead",
            "pricing_usd_per_mtoken": {
                "input": INPUT_USD_PER_MTOKEN,
                "cached_input": CACHED_INPUT_USD_PER_MTOKEN,
                "output": OUTPUT_USD_PER_MTOKEN,
            },
            "latency_ms_model": {
                "prefill_uncached_per_token": PREFILL_MS_PER_UNCACHED_TOKEN,
                "prefill_cached_per_token": PREFILL_MS_PER_CACHED_TOKEN,
                "decode_per_token": DECODE_MS_PER_TOKEN,
                "aux_call": AUX_LATENCY_MS_PER_CALL,
            },
        },
        "results": results,
        "totals": totals,
        "limitations": LIMITATIONS,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Offline benchmark results",
        "",
        f"Evidence class: **{report['evidence_class']}**. "
        "These numbers come from a deterministic simulator, not from Hermes, a provider, "
        "or a live model. Do not cite them as task-accuracy results.",
        "",
        "## Strategy totals",
        "",
        "| Strategy | Requests | Prompt tokens | Sim cost (USD) | Edits | Facts retained | All-facts fixtures | Checks passed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    totals = report["totals"]
    for key in sorted(totals):
        row = totals[key]
        lines.append(
            f"| {key} — {row['label']} | {row['requests']} | {row['prompt_tokens_total']} | "
            f"{row['sim_cost_usd']:.4f} | {row['edits']} | {row['facts_retained']}/"
            f"{row['facts_total']} | {row['fixtures_all_facts']}/{row['fixtures']} | "
            f"{row['fixtures_checks_passed']}/{row['fixtures']} |"
        )
    lines += [
        "",
        "## Per-fixture detail",
        "",
        "| Fixture | Strategy | Peak tokens | Sim cost | Edits | Retention | Injection | Recovery | Handoff |",
        "|---|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in report["results"]:
        lines.append(
            f"| {row['fixture']} | {row['strategy']} | {row['prompt_tokens_peak']} | "
            f"{row['sim_cost_usd']:.5f} | {row['edits']} | {row['facts_retained']}/"
            f"{row['facts_total']} | {row['injection_contained']} | "
            f"{row['recovery_clean'] and row['retrieval_exact']} | "
            f"{row['handoff_fidelity']:.2f} |"
        )
    lines += ["", "## Fixtures", ""]
    for fixture in report["fixtures"]:
        lines.append(
            f"- **{fixture['id']}** — {fixture['title']}: {fixture['description']} "
            f"({fixture['steps']} steps, budget {fixture['budget_tokens']} - "
            f"{fixture['reserve_tokens']} reserve)"
        )
    lines += ["", "## Limits", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


def write_reports(
    report: dict[str, Any],
    *,
    json_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
) -> None:
    if json_path:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if markdown_path:
        path = Path(markdown_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(report))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline hermes-clm benchmarks.")
    parser.add_argument("--fixtures", default=None, help="fixture directory")
    parser.add_argument("--strategies", default=None, help="comma-separated A,B,C,D")
    parser.add_argument("--json", default=None, help="write JSON report")
    parser.add_argument("--markdown", default=None, help="write Markdown report")
    args = parser.parse_args(argv)
    names = args.strategies.split(",") if args.strategies else None
    try:
        report = run_all(args.fixtures, names)
    except BenchmarkError as exc:
        print(f"error: {exc}")
        return 2
    if args.json or args.markdown:
        write_reports(report, json_path=args.json, markdown_path=args.markdown)
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
