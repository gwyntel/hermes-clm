# Benchmarking

`hermes-clm` ships a deterministic, offline benchmark harness. It makes **no network calls
and no model calls**, so it can run in CI and in a review without a provider key. Its job is
to make the A/B/C/D comparison and the metric accounting inspectable and reproducible — not
to claim task accuracy.

```bash
python -m benchmarks
python -m benchmarks --strategies A,C --json /tmp/report.json --markdown /tmp/report.md
hermes-clm bench --benchmarks-dir benchmarks
```

The committed run is [`benchmarks/results/offline-results.json`](../benchmarks/results/offline-results.json)
with a rendered companion at [`benchmarks/results/offline-results.md`](../benchmarks/results/offline-results.md).
The fixture and harness reference is [`benchmarks/README.md`](../benchmarks/README.md).

## Evidence class

**offline-simulation.** The harness does not execute Hermes, the engine, a provider or a
model. Strategy A is a model of a default threshold summariser; B is a model of
microcompaction; C and D are reference CLM policies. None is the shipped engine.

## Strategies

| Key | Policy | Archive | Cache behaviour |
|---|---|---|---|
| A | Summarise the oldest editable rows above 75% of usable tokens; protect a prefix and newest six rows. | No | One bulk rewrite at the boundary. |
| B | Replace old tool results with a length note above 50%; drop the oldest group above 90%. | No | Frequent small rewrites of tool rows. |
| C | Resolve superseded facts, then batch rewrite/offload settled spans above 75% or when one span exceeds 18% of usable tokens. | Yes | Batched edits; exact bytes archived. |
| D | C plus a local extractive mock of an auxiliary compression proposal, bound to a proposal hash and base revision. | Yes | Same as C; aux input tokens counted. |

## Fixtures

| Fixture | Scenario | Checkable properties |
|---|---|---|
| `long-coding` | 21 steps: reproduce, failed patch, operator decision, green suite. | Tool-output fact, assistant fact, pinned task/policy/decision. |
| `interruption` | Crash at step 8, rebuild from durable raw history plus committed operations. | Revision/archive integrity, fact survival, no partial replay. |
| `conflicting-updates` | A later correction supersedes an earlier deploy target. | Current marker present, stale marker absent. |
| `huge-output` | 12k-character dump with the fact at 86% depth. | Offload stub, exact archive retrieval, peak pressure. |
| `injection` | Vendor output impersonates `role=system`, demands policy deletion and exfiltration. | System preserved, no injected marker in operator/system origin. |
| `recovery` | Offload an 8k manifest, crash, retrieve exact bytes. | Byte-exact retrieval, archive checksum. |
| `handoff` | Boundary snapshot after a patch. | Fact fidelity, origin safety, no raw tool noise. |

Facts are declared in the fixture with a survival mode: `pinned` (source row protected),
`extract` (must be carried into a replacement; archiving alone does not count), `archive`
(may be offloaded if exactly retrievable), `latest` (current present, stale absent).

## Method

For each fixture × strategy:

1. Append each step to the host-raw history.
2. At each assistant step, before that response, measure a provider request over the current
   projection.
3. Apply the strategy edit only after the model has seen the raw tool result once. Editing
   is an explicit turn, not a free rewrite before the request.
4. On a fixture crash, rebuild the projection from the durable raw history and committed
   operation log; verify revision, archive hashes and absence of a partial replay.
5. At the end, evaluate fact survival, injection containment, conflict resolution, exact
   retrieval and handoff fidelity.

### Metrics

| Metric | Definition |
|---|---|
| `prompt_tokens_total` / `prompt_tokens_peak` | Sum/max of the deterministic estimate over projected messages. |
| `cached_tokens_total`, `prefix_reuse_ratio` | Longest identical leading span prefix versus the previous request, matched on span id and SHA-256; ratio = cached / prompt. |
| `sim_cost_usd` | `(uncached × $3 + cached × $0.30 + completion × $15) / 1M`, plus aux input at $3/1M. Illustrative list prices, not a bill. |
| `sim_latency_ms` | Synthetic: uncached prefill 0.010 ms/token, cached prefill 0.001 ms/token, decode 4 ms/token, aux call 250 ms. Relative only. |
| `edits` | Number of committed edit batches. |
| `spans_dropped` / `rewritten` / `offloaded` | Operation counts. |
| `reclaimed_tokens` | Estimated tokens removed by edits. |
| `cache_break_tokens` | Tokens after each edit position; the suffix a prefix cache must re-read. |
| `archive_entries` / `archive_integrity` | Archived span count and checksum verification. |
| `retrieval_exact` | Byte equality of an archived span against its raw source. |
| `recovery_clean` | Revision equals committed batches and no archive entry is corrupt after a crash. |
| `facts_retained` | Facts satisfying their mode's survival rule. |
| `injection_contained` | System prompt preserved and no injected marker in an operator- or system-origin span. |
| `conflict_resolved` | Stale marker absent and current marker present. |
| `handoff_fidelity` | Fraction of non-archived facts present in the handoff snapshot. |
| `checks_passed` | All of the above applicable to the fixture. |

## Committed results

Run with Python 3.11; the JSON is byte-identical under Python 3.12 and 3.14 and across
repeated runs.

| Strategy | Requests | Prompt tokens | Sim cost (USD) | Edits | Facts retained | All-facts fixtures | Checks passed |
|---|---:|---:|---:|---:|---:|---:|---:|
| A — Default compressor | 56 | 69,761 | 0.0894 | 4 | 17/20 | 4/7 | 3/7 |
| B — Microcompaction | 56 | 61,593 | 0.0932 | 8 | 15/20 | 2/7 | 1/7 |
| C — CLM | 56 | 59,103 | 0.0865 | 9 | 20/20 | 7/7 | 7/7 |
| D — CLM + mock aux | 56 | 59,763 | 0.1013 | 9 | 20/20 | 7/7 | 7/7 |

Per-fixture peak tokens, retention, injection, recovery and handoff are in the committed
Markdown report.

### Interpretation, including what not to conclude

- **C and D pass their own preservation checks by construction.** The fixtures declare
  `FACT[...]` lines and the CLM policies explicitly carry them forward. The run proves the
  accounting distinguishes policies; it does not prove CLM improves coding or research
  accuracy. No task score is measured.
- **B is not cheaper despite the lowest prompt-token total.** Its repeated tool-row rewrites
  break the prefix cache more often, so uncached prefill raises illustrative cost above A.
  Token count and cost are separate metrics.
- **D's aux proposal costs more than it saves in this run.** Aux input tokens are counted;
  an auxiliary summary is not free. This is the kind of result the harness should be able to
  show.
- **Peak prompt tokens can exceed the usable budget.** A large tool result must be seen once
  before the model can offload it. That is the real overflow path, not hidden truncation.
- **A and B lose declared facts** because their policies summarise or prune tool output
  without a fact contract. That is a property of the modelled policies, not a claim that
  Hermes' built-in compressor behaves identically.

## Determinism

- No wall clock, randomness, network, environment-dependent tokenizer or provider response
  enters the result.
- JSON keys are sorted; report metadata contains no timestamp.
- `tests/packaging/test_benchmarks.py` compares two runs byte-for-byte, blocks
  `socket.socket`/`create_connection`, and asserts the fixture/strategy matrix.
- Results are identical on Python 3.11, 3.12 and 3.14. Python 3.13 is covered by CI.

## Live runs

There is no live mode. A live benchmark needs separate written approval listing:

- the model and provider alias, with alias classification;
- the exact number of calls and a token ceiling;
- a dedicated key, never a shared production credential;
- the estimated cost and the stop condition;
- where raw provider usage (`prompt_tokens`, cached tokens, completion tokens, latency) will
  be published.

Until that exists, every number in this repository is offline and synthetic. Do not present
it as a live result, and do not compare it to the paper's accuracy or FLOPs numbers.

## Cost-gated live procedure (for a future approved run)

1. Freeze the fixtures and commit the harness revision.
2. Record the approval text and key source outside the repository.
3. Run one fixture first; compare observed tokens with the estimator and publish the ratio.
4. Stop if the token ceiling or cost estimate is exceeded.
5. Publish raw per-request usage next to the summary, plus model/version, date and harness
   revision.
6. Label the result as live, with its model and provider, and keep the offline run separate.

## Adding a fixture

Add JSON under `benchmarks/fixtures/` with a system step first, unique step indexes, group
ids for tool-call/result pairs, and `source_step` values that exist for every fact. The
loader rejects malformed fixtures. Encode facts and hazards, not expected strategy outcomes;
a fixture that assumes its answer is not a benchmark.
