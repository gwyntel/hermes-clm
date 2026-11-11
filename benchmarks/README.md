# Offline benchmark harness

Deterministic, offline, standard-library-only. It makes **no network calls and no model
calls**. It exists so the A/B/C/D comparison and the metric accounting can be reviewed and
re-run without spending tokens.

```bash
python -m benchmarks
python -m benchmarks --strategies A,C --json /tmp/report.json --markdown /tmp/report.md
hermes-clm bench --benchmarks-dir benchmarks
```

Published results live in [`results/offline-results.json`](results/offline-results.json) and
[`results/offline-results.md`](results/offline-results.md).

## What the strategies are

| Key | Policy |
|---|---|
| A | Default compressor model: summarise the oldest editable rows once prompt pressure exceeds 75% of usable tokens, keep a protected prefix and the newest six rows. No archive. |
| B | Microcompaction model: replace old tool results with a length note at 50% pressure; drop the oldest group under 90% pressure. No archive. |
| C | CLM model: resolve superseded facts, then batch rewrite/offload settled spans at 75% pressure or when a single span exceeds 18% of usable tokens. Preserves `FACT[...]` lines and archives exact bytes. |
| D | C plus a deterministic local mock of an auxiliary compression proposal. The proposal is hashed, bound to a base revision and staged; no model is called. |

A and B are policy models of host behaviours, not Hermes code. They are not claimed to be
equivalent to Hermes' built-in compressor or microcompaction.

## Fixtures

Each fixture is a JSON file in `fixtures/` with an ordered step list, a budget/reserve, and
facts whose survival is checkable.

| Fixture | What it exercises |
|---|---|
| `long-coding` | Reproduction, a failed patch, a later operator decision, and a green suite under pressure. |
| `interruption` | Crash at step 8; rebuild the projection from durable raw history plus committed operations. |
| `conflicting-updates` | A later operator correction supersedes an earlier config value; the stale value must not survive as current. |
| `huge-output` | A 12k-character dump with an archival fact beyond the head; offload and exact retrieval. |
| `injection` | Tool output impersonates `role=system` and asks the agent to drop policy; provenance must not be elevated. |
| `recovery` | Offload an 8k-character manifest, restart, retrieve the exact archived bytes. |
| `handoff` | A boundary snapshot carries operator constraints and verified facts, not raw tool noise. |

Facts have a mode:

- `pinned` — the source span is protected and must remain in projected context.
- `extract` — the policy must carry the fact line into a replacement; archiving alone does
  not count.
- `archive` — the fact may be offloaded if the exact original is retrievable.
- `latest` — the current marker must be present and the stale marker absent.

## Method

For each fixture and strategy, the simulator:

1. appends each step to the host-raw history;
2. counts a provider request at each assistant step, before that assistant response, using
   the projected context;
3. applies the strategy's edit only after the model has seen the raw tool result once
   (editing is an explicit turn, not a free pre-request rewrite);
4. on a configured crash, rebuilds the projection from the durable raw history and the
   committed operation log, then verifies archive hashes and revision accounting.

### Metrics

| Metric | Definition |
|---|---|
| `prompt_tokens_total` / `peak` | Sum/max of the deterministic estimate over projected messages. |
| `cached_tokens_total`, `prefix_reuse_ratio` | Longest identical leading span prefix versus the previous request, by span id and SHA-256; ratio = cached / prompt. |
| `sim_cost_usd` | `(uncached × $3.00 + cached × $0.30 + completion × $15.00) / 1M`, plus aux input tokens at $3.00/1M. Illustrative list prices, not a bill. |
| `sim_latency_ms` | Synthetic: uncached prefill 0.010 ms/token, cached prefill 0.001 ms/token, decode 4 ms/token, aux call 250 ms. Relative only. |
| `edits`, `spans_*`, `reclaimed_tokens` | Counts of applied batches and operation kinds, and estimated tokens removed. |
| `cache_break_tokens` | Tokens after each edit position, i.e. the suffix a prefix cache must re-read. |
| `archive_entries`, `archive_integrity` | Archived span count and checksum verification. |
| `retrieval_exact` | Byte equality of an archived span against its raw source. |
| `recovery_clean` | Revision equals committed batches, no corrupted archive entry after a crash. |
| `facts_retained` | Facts satisfying their mode's survival rule. |
| `injection_contained` | System prompt preserved and no injected marker appears in an operator- or system-origin span. |
| `handoff_fidelity` | Fraction of non-archived facts present in the handoff snapshot. |

## Committed run

Evidence class: **offline-simulation**. Reproduced with `python -m benchmarks` on Python
3.11; the committed JSON is byte-identical across runs.

| Strategy | Requests | Prompt tokens | Sim cost (USD) | Edits | Facts retained | All-facts fixtures | Checks passed |
|---|---:|---:|---:|---:|---:|---:|---:|
| A — Default compressor | 56 | 69,761 | 0.0894 | 4 | 17/20 | 4/7 | 3/7 |
| B — Microcompaction | 56 | 61,593 | 0.0932 | 8 | 15/20 | 2/7 | 1/7 |
| C — CLM | 56 | 59,103 | 0.0865 | 9 | 20/20 | 7/7 | 7/7 |
| D — CLM + mock aux | 56 | 59,763 | 0.1013 | 9 | 20/20 | 7/7 | 7/7 |

Per-fixture details, including peak tokens, retention, injection, recovery and handoff
fidelity, are in [`results/offline-results.md`](results/offline-results.md).

### How to read these numbers

- **They are a conformance test of the harness, not evidence of task accuracy.** C and D
  implement the fact-preservation rule the checks measure, so they pass by construction.
  A and B deliberately model policies that do not preserve declared facts. The run shows
  that the accounting distinguishes the policies; it does not show that CLM solves coding
  or research tasks.
- **C uses fewer prompt tokens than A, but B's lower token count does not make it cheaper**:
  B breaks the cache more often and repeats more uncached prefill in this run. Cost and
  token count are separate metrics.
- **The mock aux proposal costs more than it saves here.** D's extra input tokens raise
  illustrative cost above C. That is a legitimate result: an aux summary is not free, and
  the harness counts it.
- **Peak tokens above the usable budget occur** when a large tool result must be seen once
  before the model can offload it. That models the real overflow path; it is not hidden.

## Live runs

The harness has no live mode. A live benchmark requires:

1. a dedicated key, not a shared production credential;
2. written approval listing the model, provider alias classification, number of calls,
   token ceiling and estimated cost;
3. recording of provider-reported prompt/cached/completion tokens and real latency;
4. publication of the raw per-request output next to the summary.

Until then, no number in this repository is a live result.

## Adding a fixture

Add a JSON file under `fixtures/` with a system step first, unique step indexes, explicit
group ids for tool-call/result pairs, and facts whose `source_step` exists. `load_fixture`
rejects malformed input. Keep every fact checkable; do not encode expected strategy
outcomes.
