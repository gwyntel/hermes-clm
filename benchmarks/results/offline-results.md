# Offline benchmark results

Evidence class: **offline-simulation**. These numbers come from a deterministic simulator, not from Hermes, a provider, or a live model. Do not cite them as task-accuracy results.

## Strategy totals

| Strategy | Requests | Prompt tokens | Sim cost (USD) | Edits | Facts retained | All-facts fixtures | Checks passed |
|---|---:|---:|---:|---:|---:|---:|---:|
| A — Default compressor (modelled) | 56 | 69761 | 0.0894 | 4 | 17/20 | 4/7 | 3/7 |
| B — Microcompaction (modelled) | 56 | 61593 | 0.0932 | 8 | 15/20 | 2/7 | 1/7 |
| C — CLM (model-directed edits) | 56 | 59103 | 0.0865 | 9 | 20/20 | 7/7 | 7/7 |
| D — CLM + mock aux proposal | 56 | 59763 | 0.1013 | 9 | 20/20 | 7/7 | 7/7 |

## Per-fixture detail

| Fixture | Strategy | Peak tokens | Sim cost | Edits | Retention | Injection | Recovery | Handoff |
|---|---|---:|---:|---:|---:|---|---|---|---|
| conflicting-updates | A | 2491 | 0.01056 | 1 | 2/2 | True | True | 1.00 |
| conflicting-updates | B | 2491 | 0.01056 | 1 | 1/2 | True | True | 1.00 |
| conflicting-updates | C | 1006 | 0.00958 | 4 | 2/2 | True | True | 1.00 |
| conflicting-updates | D | 1006 | 0.00958 | 4 | 2/2 | True | True | 1.00 |
| handoff | A | 1290 | 0.00639 | 0 | 3/3 | True | True | 0.67 |
| handoff | B | 1290 | 0.00639 | 0 | 3/3 | True | True | 0.67 |
| handoff | C | 1290 | 0.00639 | 0 | 3/3 | True | True | 1.00 |
| handoff | D | 1290 | 0.00639 | 0 | 3/3 | True | True | 1.00 |
| huge-output | A | 3659 | 0.01644 | 1 | 1/2 | True | False | 1.00 |
| huge-output | B | 3394 | 0.01534 | 1 | 1/2 | True | False | 1.00 |
| huge-output | C | 3394 | 0.01577 | 1 | 2/2 | True | True | 1.00 |
| huge-output | D | 3394 | 0.02505 | 1 | 2/2 | True | True | 1.00 |
| injection | A | 1691 | 0.00828 | 0 | 3/3 | True | True | 1.00 |
| injection | B | 1691 | 0.00828 | 1 | 3/3 | True | True | 1.00 |
| injection | C | 1378 | 0.00840 | 2 | 3/3 | True | True | 1.00 |
| injection | D | 1378 | 0.00840 | 2 | 3/3 | True | True | 1.00 |
| interruption | A | 1821 | 0.01101 | 0 | 3/3 | True | True | 1.00 |
| interruption | B | 1433 | 0.01333 | 2 | 2/3 | True | True | 1.00 |
| interruption | C | 1821 | 0.01101 | 0 | 3/3 | True | True | 1.00 |
| interruption | D | 1821 | 0.01101 | 0 | 3/3 | True | True | 1.00 |
| long-coding | A | 2217 | 0.01574 | 1 | 4/5 | True | True | 1.00 |
| long-coding | B | 1796 | 0.01835 | 2 | 4/5 | True | True | 1.00 |
| long-coding | C | 2217 | 0.01437 | 1 | 5/5 | True | True | 1.00 |
| long-coding | D | 2217 | 0.01989 | 1 | 5/5 | True | True | 1.00 |
| recovery | A | 3463 | 0.02098 | 1 | 1/2 | True | False | 1.00 |
| recovery | B | 3463 | 0.02098 | 1 | 1/2 | True | False | 1.00 |
| recovery | C | 3463 | 0.02098 | 1 | 2/2 | True | True | 1.00 |
| recovery | D | 3463 | 0.02098 | 1 | 2/2 | True | True | 1.00 |

## Fixtures

- **conflicting-updates** — Conflicting updates: A later operator correction supersedes an earlier config value; stale text must not survive as current. (13 steps, budget 3072 - 384 reserve)
- **handoff** — Session handoff: A boundary snapshot must carry operator constraints and verified facts without dragging raw tool output forward. (13 steps, budget 4096 - 512 reserve)
- **huge-output** — Huge tool output: A 12k-character dump with a fact near the end; the harness measures offload, stub safety and exact retrieval. (12 steps, budget 4096 - 512 reserve)
- **injection** — Prompt injection in tool output: Vendor output impersonates a system role and asks the agent to drop policy; provenance must not be elevated. (12 steps, budget 3072 - 384 reserve)
- **interruption** — Interruption and restart: Crash at step 8, rebuild the projection from durable raw history and committed operations, then resume. (16 steps, budget 3072 - 384 reserve)
- **long-coding** — Long coding session: Twenty turns of reproduction, failed patch, decision update and green suite under budget pressure. (21 steps, budget 3328 - 512 reserve)
- **recovery** — Recovery after restart: Offload an 8k-character manifest, crash, rebuild, and retrieve the exact archived bytes. (13 steps, budget 3072 - 384 reserve)

## Limits

- Strategies are reference policies, not the shipped hermes-clm engine.
- Token counts use a fixed 4-characters-per-token estimator, not a provider tokenizer.
- Cost uses illustrative list prices and latency uses synthetic constants; neither is a bill or a timing measurement.
- No model is called, so retention measures structural survival of FACT lines, not task accuracy.
- Injection containment checks provenance/elevation only; it cannot measure whether a model would obey injected text.
