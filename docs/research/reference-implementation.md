<!-- Source: internal research note, 2026-11-10. Sanitised for publication: local paths, host and profile details removed. Original analysis retained. -->

# Reference implementation: facebookresearch/context-language-models


- Repo: https://github.com/facebookresearch/context-language-models
- Commit inspected: `483198229ab7b90c0eed4651306ab3b3505aaf50` (2026-10-13, "Suffix Cache Reuse: put the SGLang and vLLM implementations side by side")
- Clone: a temporary shallow reference clone (not in the project tree)
- Size: 102 files, 3.5 MB, ~14.5k lines of py/yaml/md
- Paper: arXiv 2609.37725 (cited in `README.md:22`)

All paths below relative to repo root; line numbers at that commit.

## 1. License

| artifact | license | evidence |
|---|---|---|
| Whole repo (code, prompts, docs) | **CC BY-NC 4.0** | `LICENSE:1`, `README.md:116` |
| File headers | Meta copyright, "licensed under the license found in the LICENSE file" | e.g. `clm/clm_harness/context_env/env.py:1-5`; 64 files carry this header, no other copyright line |
| Contributing | CLA to Meta; contributions under same LICENSE | `CONTRIBUTING.md:13-19`, `:29-31` |
| Third-party | mini-swe-agent MIT (adapted task instructions + submit marker), Harbor MIT (dependency, not vendored) | `NOTICE:3-12` |
| ContextBench code | Apache-2.0 | external repo `RulinShao/ContextBench` metadata |
| ContextBench dataset | Apache-2.0 | HF `context-language-model/ContextBench` cardData `license: apache-2.0` |
| BrowseComp-Plus dataset | MIT | HF `Tevatron/browsecomp-plus` cardData `license: mit` |
| EdgeBench | CC-BY-4.0 data, Apache-2.0 code | HF `ByteDance-Seed/EdgeBench` cardData `license: cc-by-4.0`; ByteDance-Seed/EdgeBench repo |

Legal read:

- **CC BY-NC 4.0 is not a software license.** No patent grant (`LICENSE:199-219`), no source/object distinction, no warranty. Every `.py` here is NC-only.
- Commercial plugin = **no code copy, no prompt-text copy**. Even non-commercial reuse needs attribution, license URI, modification notice (`LICENSE:229-271`).
- Ideas, protocols, algorithms, constants, benchmark numbers = not copyrightable. Safe to re-derive.
- Do not vendor any file. Do not copy prompt strings sentence-for-sentence; paraphrase is still risky if structure + wording track closely — write own text from behavior spec.

## 2. Shape of the release

Core = one agent, one tool (`bash`), one editable file.

```
clm/clm_harness/clm_agent/harness.py      825  agent loop (ClmAgent)
clm/clm_harness/clm_agent/prompts.yaml     70  system prompt + observation truncation config
clm/clm_harness/context_env/env.py        316  mirror write / command / read-back / gate
clm/clm_harness/context_env/edit_gate.py   73  fit vs shrink rule
clm/clm_harness/context_env/paths.py       47  late-bound /tmp/.live_ctx paths
clm/clm_harness/context_utils/context_string.py 168  render <-> parse round trip
clm/clm_harness/utils/budget.py           412  nudges, rollback-retry, final turn, cap
clm/clm_harness/utils/tokens.py           179  tiktoken counting + snapshots + head/tail
clm/clm_harness/utils/resume.py           464  checkpoint + warm restart
clm/clm_harness/utils/finish_policy.py     82  submit vs open_ended
clm/clm_harness/agent_trajectory_format/  341+264+468+244  ATIF-CTX models/builder/export/validator
clm/clm_harness/flops_metrics/kv_cache_flops.py 758  cache-aware FLOPs
clm/clm_contextbench/clm_stream.py        234  ContextBench adapter
clm/clm_icl/                              428+360+145+125  skill evolution loop
clm/clm_rl/patches/                       2 patches only, no trainer
suffix_cache_reuse/sglang/                1935-line overlay (monkeypatch)
suffix_cache_reuse/vllm/                  scheduler subclass + KV connector
```

## 3. Prompts

`clm/clm_harness/clm_agent/prompts.yaml`:

- `:5-14` system: helpful assistant, THOUGHT before action, exactly one `bash` call, rejection otherwise.
- `:16-27` "Managing your context": budget `{{context_budget}}`; conversation mirrored to `/tmp/.live_ctx/LIVE_CTX_MAIN.txt`, refreshed before every command; free context by editing that file; **editing turn is free only when command applies a real edit and prints nothing** — stdout/stderr, non-zero exit, or no-match edit costs a normal turn.
- `:29-40` locate with code, never retype; turns numbered `[[CTX_TURN i role=…]]`; gives a `python3 - <<'PY'` regex example that collapses one turn.
- `:42-45` don't `cat` the file; keep headers; emptied turn is dropped; prefix protected.
- `:47-56` compaction economics: edit forces re-read of everything after it, so batch, mind the tail, be generous in the summary.
- `:58` finish instructions injection; `:64-70` observation head 5000 / tail 5000 chars + too-long hint.

Task templates (`clm/clm_harness/task_templates/`): `terminal_agent_tasks.yaml` (mini-swe-agent-derived wording, `NOTICE:5-7`; persistent `cd`/`export` differs from mini-swe-agent), `open_problems.yaml` (bare `{{task}}`), `edgebench.yaml` (43 lines, heavy "measure, ledger, submit early and often" guidance).

ContextBench method skill is **not** in this repo: read from `contextbench` checkout `clm_exp/skills/clm/<family>.md`, sha256[:16]-pinned (`clm/clm_contextbench/clm_stream.py:70-77,109-125`).

## 4. Context representation

Mirror file = rendered editable region only. Protected prefix (system + task) never written (`clm/clm_harness/context_utils/context_string.py:90-101`, `protect=2` set at `clm/clm_harness/clm_agent/harness.py:106`).

Turn block:

```
[[CTX_TURN 1 role=assistant]]
<reasoning, then content, then `bash <args json>`>
```

- Header regex anchored per line: `clm/clm_harness/context_utils/context_string.py:41`; valid roles `:42`.
- `rendered_text` includes `reasoning_content`, text content, and tool-call name+arguments (`:63-87`) — because bash observations don't echo the command.
- Numbering is display-only, 1-based from top of editable region (`:94-100`); parser ignores the number.
- `parse_back` (`:104-139`): re-pins protected prefix from originals; surviving headers define turns; stray text before/between headers becomes `user`; only `assistant` survives as assistant, everything else folds to `user`; **tool-call structure is destroyed** — history reconstructed as role+text only (`:27-29`, `:136-138`).
- `_normalize` (`:142-168`): drop empty, merge consecutive same-role with `\n\n`, protected prefix verbatim.
- Paths are late-bound module globals so concurrent shards can rebind `_CTX_DIR` before constructing the agent (`clm/clm_harness/context_env/paths.py:7-14,28-47`).

Design consequence: mirror is lossy by construction. Any tool-call/tool-result pairing is gone after one edit; harness re-issues next turn as fresh assistant message.

## 5. Edit / commit behavior

Per turn (`clm/clm_harness/context_env/env.py:182-258`):

1. render + upload mirror (`:113-118`), `turns_since_edit += 1`
2. execute command in persistent shell wrapper that restores/saves cwd+env (`:94-110`)
3. `touched_ctx` heuristic: command mentions `LIVE_CTX_MAIN`, or mentions ctx dir and none of `SUBCTX|finished_subagents|subagents` (`:198-200`, `:40`)
4. download mirror, compare stripped text to rendered (`:211`)
5. changed → `_apply_edit`; unchanged but touched → "NO change" note with exit code or match-nothing hint (`:215-229`)
6. tool result = truncated stdout/stderr + `(exit_code=N)` (`:148-170`), submit-warning if marker not standalone (`:172-179`)
7. budget cap + readout appended (`:236-248`)

`_apply_edit` (`:260-315`):

- candidate = `parse_back(read_back, prefix)`; count tokens of candidate AND of round-tripped baseline — one ruler for both (`:269-272`).
- growth rejected only if `after > baseline` and gate says no (`:273-280`) → `edit_gate.rejected_note`.
- accepted → replace `messages[:]` in place, `n_ctx_syncs += 1`; "real" sync only if delta ≥ `max(32 tokens, 1% of editable)` (`:35-36`, `:287-289`); counts grew / rejected; `budget.note_compaction()`; resets `tokens_at_last_edit`, `turns_since_edit`.
- three receipt variants: grew-but-fits, applied-but-still-over, applied (`:296-315`).

Free turn (`clm/clm_harness/clm_agent/harness.py:127-130`, used `:638-647`): free only if mirror changed AND stdout+stderr empty AND exit 0. Everything else consumes `step`. Separate `lm_call_cap` bounds calls (default `2*max_steps+24`, `:309-313`) because free turns don't consume steps; loop also bounded by `max_total_iters = 2*max_steps+8+min(retries,max_steps)` (`:507`).

Gate (`clm/clm_harness/context_env/edit_gate.py`): `fit` default = accept if result fits `strict_target`; `shrink` = must shrink (`:21`, `:49-51`, `:53-58`). `may_grow` returns False when no budget (`:57-58`). Env `CLM_EDIT_GATE`; kwarg wins (`:38-46`). Rejection note wording is frozen ("its wording must not change", `:9-11`, `:67-73`).

Counters exported to `usage.json`: `n_ctx_syncs`, `n_ctx_syncs_real`, `n_ctx_grew`, `n_ctx_rejected`, `free_ctx_turns`, nudges, retries, rolled-back turns, code fingerprint (`clm/clm_harness/clm_agent/harness.py:658-694`).

## 6. Validation

What exists:

- Run-start hard failures: missing budget (`clm/clm_harness/clm_agent/harness.py:394-407`); removed experimental kwargs (`:111-124`, raises, so stale launcher cannot silently run different config); bad `ctx_hint` (`:290-294`); unknown `flops_model_key` (`clm/clm_harness/flops_metrics/kv_cache_flops.py:444-450`).
- Edit-level: only token-size gate above. **No structural, semantic, or fact-preservation check.** Deletion of needed facts caught by prompt text, not code.
- Round-trip tolerance: parser recovers from damaged/missing headers, non-ASCII, arbitrary edits (`clm/clm_harness/context_utils/context_string.py:104-139`; tests `clm/tests/test_context_string.py:95-113`).
- Submit validation: exact regex `^\s*(?:/bin/)?echo\s+['\"]?MARKER['\"]?\s*$` (`clm/clm_harness/utils/finish_policy.py:75-82`) plus `return_code == 0` and marker present in tool content (`clm/clm_harness/clm_agent/harness.py:820-825`).
- Trajectory schema validation: sequential segment ids, branch on non-first, parent = id-1, non-empty `input_context`, `source_step_id` in parent, unique `sub_id`, spawn/fold reference real `(segment,step)` in parent, sequential step ids, agent-only fields, ISO-8601 timestamps, tool-result refs (`clm/clm_harness/agent_trajectory_format/validator.py:106-216`). CLI entry `:224-244`.
- Export self-check: agent steps vs LM-call snapshots vs per-call metrics must match, recorded in `trajectory.extra["export_check"]` (`clm/clm_harness/agent_trajectory_format/harness_export.py:379-403`).
- ContextBench: skill sha pin, instruction sha recorded, stream integrity checks happen in external `contextbench.harbor.mixin` (`clm/clm_contextbench/clm_stream.py:121-124,201-206`).

What does not exist: no fixture-based tests for the gate, budget state machine, rollback, resume, or export splitting; no property tests; no golden trajectory files.

## 7. Rollback

Two distinct mechanisms.

**(a) Budget rollback-retry** (`clm/clm_harness/utils/budget.py`):

- Overflow = `count > strict_target` where `strict_target = budget - reserve` (`:83`, default reserve 2048).
- If retries remain, `strict_target > retry_edit_margin`, and something droppable → action `rollback` (`:179-192`).
- `rollback_to_margin` pops NEWEST turns until `margin` free tokens under target, never touches prefix, and never leaves an assistant `tool_calls` turn without its result (`:287-308`).
- Margin grows only after `_ROLLBACK_ESCALATE_AFTER = 3` consecutive ignored rollbacks, capped at `max(3/4 target, margin)` (`:49`, `:273-285`) — anti-livelock.
- `note_compaction()` resets escalation; dipping under the limit does not (`:267-271`).
- After retries exhausted or margin impossible → one `final_turn` notice, then `stop` (`:193-200`).
- Dropped turns leave the log for good; only a retry count survives (`:19-22`).

**(b) Rollback ledger** (`clm/clm_harness/clm_agent/harness.py:419-449`): on first rollback, insert a `user` message at index `protect`, grow `_protect` by 1, push it into budget and ContextEnv; later rollbacks rewrite that message in place. Ledger names up to the last 5 commands whose output blew the budget (`:434-438`, `clm/clm_harness/utils/budget.py:310-331`). Rationale: without it the model re-issues the same command and livelocks (`:422-427`).

Note asymmetry: ledger survives edits because it lives inside the protected prefix, which `parse_back` re-pins from copies (`:446-448`).

## 8. Archive / recovery

No archive-of-dropped-context feature in this release. `ctx_archive`, `obs_offload`, `subagents`, `context_tool` are explicitly removed and raise (`clm/clm_harness/clm_agent/harness.py:111-124`). "archive/" in `clm/clm_harness/utils/resume.py:12,61,82-84` refers to task-side agent files in the EdgeBench workspace, not harness machinery.

What exists:

- **Timer checkpoint** (`clm/clm_harness/utils/resume.py:207-321`): every `checkpoint_interval_s`, download sandbox workdir to `artifacts/workspace/` via `.workspace.partial` then atomic swap; context written after workspace so context never describes uncaptured files (`:290-308`). Never raises; one JSONL line per attempt (`:243-269`).
- **Thinning** (`:165-204`): > 20k files (`RESUME_MAX_FILES`) → hardlink newest 2000 per dir, write `RESTORE_TRUNCATED.json`.
- **Warm restart** (`:331-443`): restore workspace into env's real `_workdir` (`:347-366`); prior context from `agent/trajectory.ctx.json` last segment, else `agent/messages.json` (`:369-401`); keep caller's own system+task prefix, drop same count from replay (`:403-443`); cap replay at `max_ratio=0.5` of budget using tiktoken over all prompt text including tool arguments (`:100-119`); drop trailing unanswered `tool_calls` (`:122-137`); cold note when files survived but conversation did not.
- **Segment archive** = trajectory format: context-management action closes a segment; each new segment stores verbatim `input_context` (`clm/clm_harness/agent_trajectory_format/models.py:7-33`, `:196-213`). Segment split is inferred from consecutive per-call snapshots: next snapshot not extending current ⇒ branch; the reply that ended the epoch is located explicitly; a reply removed by rollback becomes a content-less `_discarded_reply` stand-in so steps stay 1:1 with LM calls (`clm/clm_harness/agent_trajectory_format/harness_export.py:212-332`).
- Recovery rule: `input_context` is authoritative; never reconstruct across a segment boundary (`clm/clm_harness/agent_trajectory_format/models.py:29-33`).

Hard limit by design: rolled-back and compacted text is unrecoverable from the conversation. Only sandbox files and the segment `input_context`s survive.

## 9. Auxiliary compression

**No auxiliary summarizer model.** Compression is done by the task model itself, in bash, on its own mirror (`clm/clm_harness/clm_agent/prompts.yaml:22-56`; `clm/clm_harness/context_env/env.py`). Nothing in the loop calls a second model to summarize.

Auxiliary LLM appearances elsewhere:

- **Cost accounting slot only**: `aux_prefill_tokens`, `aux_gen_tokens`, `n_aux_calls` folded into FLOPs as one-off full prefills (`clm/clm_harness/flops_metrics/kv_cache_flops.py:507-546`, docstring `:522-526`: "e.g. summary/compaction calls a harness makes"). Plumbed through `clm/clm_harness/agent_trajectory_format/harness_export.py:423-425` but the harness never passes them.
- **ICL proposer** = auxiliary model role, not inline compression: candidate `SKILL.md` rewrites from contrastive run notes (`clm/clm_icl/README.md:1-39`, `clm/clm_icl/note.py:33-145`, `clm/clm_icl/propose.py:84-125`), gated by `d > SE` or tie + cheaper (`README.md:21-36`), history and gate results in `state.json`.
- Retry ledger message is harness-authored text, not model-generated (`clm/clm_harness/utils/budget.py:310-331`).

So: "auxiliary compression" is a documented cost hook, not an implemented compressor. If the plugin wants an external compactor, that is additive design space, not a port.

## 10. Budget signals

Four signal channels, all deterministic tiktoken-based:

- **Per-turn readout**: `[context: ~N/M tokens]` plus ` — OVER; compact <path> now` (`clm/clm_harness/context_env/env.py:236-248`). Uses the calibrated count and `strict_target`, i.e. same number the gate enforces.
- **Escalating nudges** at `nudge_ratios` (default 0.25/0.5/0.75, `clm/clm_harness/clm_agent/harness.py:196`): each fires once, re-arms after dropping below (`clm/clm_harness/utils/budget.py:177-178`, `:224-236`). Tier texts differ: ≤25% informational, no how-to; ≤50% finish unit then tidy once; higher: compact settled spans, don't wipe, plus note contract + overflow consequence (`:363-398`).
- **Note contract** (`:356-361`): copy facts forward (ruled-out candidates + "do not retry", queries tried, VERIFIED/UNVERIFIED values, NEXT line).
- **Persistent urgent nudge**: fixed fraction (`0.9` in BCP) or `adaptive` default. Adaptive band = `max(10% of target, mult × largest of last 3 tool outputs)`, capped at 50% of target, mult default 2.0 (`:239-265`). Rationale: a fixed band misses when one output is wider than the band (`:246-254`).
- **Context status tool**: not advertised, but honored if called; returns used/total, %, free, recent growth rate, turns of headroom (`clm/clm_harness/context_env/env.py:127-145`, dispatch `clm/clm_harness/clm_agent/harness.py:149-150,598-607`).
- **Finalize nudge**: last N turns (whichever of steps or LM calls runs out first), `[SYSTEM NOTICE] FINAL TURN…` (`clm/clm_harness/clm_agent/harness.py:725-746`).
- **Newest-output cap**: head/tail truncate only the newest tool message so earlier cached messages are untouched (`clm/clm_harness/utils/budget.py:400-412`, `clm/clm_harness/utils/tokens.py:105-108`).

Tokenizer calibration (`clm/clm_harness/utils/budget.py:143-169`): ratio = server `prompt_tokens` / local tiktoken count, clamped [0.5, 3.0], disablable via `CLM_TOK_RATIO=0`. Counts with tiktoken `o200k_base`, fallback `cl100k_base`, then chars/4 (`clm/clm_harness/utils/tokens.py:31-53,93-102`). `_iter_text` deliberately includes `tool_calls[*].function.arguments` — otherwise a bash-heavy prompt undercounts ~2× (`:56-62`).

## 11. Cache behavior

Three layers, keep separate.

**Prompt-construction cache friendliness**: only the newest message is ever truncated (`clm/clm_harness/utils/budget.py:404-405`, `clm/clm_harness/utils/tokens.py:105-108`); the mirror edit is the sole intended cache break, and the prompt tells the model the cost grows with text after the edit (`clm/clm_harness/clm_agent/prompts.yaml:47-56`). Edits register as explicit divergence: snapshot is taken of the exact list about to be sent (`clm/clm_harness/clm_agent/harness.py:373-392`).

**Measurement**: `clm/clm_harness/flops_metrics/kv_cache_flops.py` replays per-turn prompts through three models — `naive` (full prefill every turn), `cache_aware` (prefix-matched, block granularity), `freeze` (only inserted/replaced tokens prefilled, survivors keep frozen KV, difflib on consecutive turns) (`:9-37`, `:246-303`, `:340-379`). Prefill source priority: server `prompt_tokens` + `cached_tokens` (exact) → real `prompt_token_ids` → model tokenizer → tiktoken → chars/4 (`:39-50`). Attention term added separately, coefficient `4*n_layers*hidden` per causal pair, hybrid geometry table `{"9b": (8,4096), "27b": (16,6144)}` — only 1 in 4 Qwen3.5/3.6 layers are full attention, else 4× overstatement (`:70-77`, `:474-496`). Headline = linear + attention when geometry known, else linear only, which understates compaction benefit (`:52-59`).

**Serving-level reuse (SCR)** — separate subsystem, `suffix_cache_reuse/`:

- Idea: after an in-middle edit `[A B C] → [A B' C]`, reuse cached KV of surviving span(s) C, not just prefix A; relocate up to K spans, re-rotate RoPE keys to new positions (`suffix_cache_reuse/README.md:36-70`).
- Defaults: K=6, min span 64 tokens, last 16 tokens of each span prefilled, 12 held conversations, 30k held tokens, 25% KV budget, match needs ≥1024 common prefix tokens (`suffix_cache_reuse/vllm/vllm_scr/config.py:14-33`; planner `:181-245`).
- Deliberate approximation: relocated KV was computed under the old prefix; only RoPE corrected; staleness compounds across turns, bounded by K (`suffix_cache_reuse/vllm/DESIGN.md:153-163`).
- vLLM integration = scheduler subclass + KV connector, no source patch, version pin 0.19.1 with seam check (`suffix_cache_reuse/vllm/DESIGN.md:25-86`). SGLang = 1935-line start-up monkeypatch (`suffix_cache_reuse/sglang/README.md:8-12`).
- Bonus: reasoning-token stripping by chat templates is treated as an edit (5.4 of 7.8 points of extra reuse on BCP, `README.md:77-83`).

Relevance to a Hermes plugin: SCR is inference-stack work. Out of scope unless we own serving. The reusable part is the measurement discipline (report naive vs cache-aware vs freeze) and the "edit = cache break" framing.

## 12. Tests

Real inventory (counted `def test_`):

| file | tests | covers |
|---|---|---|
| `clm/tests/test_context_string.py` | 6 | render/parse round trip, role folding, prefix pinning, empty-turn drop, merge, damaged headers, content parts, command ordering |
| `suffix_cache_reuse/sglang/tests/unit_test.py` | 7 | planner/overlay units |
| `suffix_cache_reuse/vllm/tests/test_sim.py` | 9 | real scheduler in vLLM `Scheduler` with simulated worker |
| `suffix_cache_reuse/vllm/tests/unit_test.py` | 3 | planner, matching, RoPE re-rotation |

Ran the harness suite locally (no model needed):

```
PYTHONPATH=clm python3 -m unittest discover -s clm/tests -v
Ran 6 tests in 0.001s — OK
```

Untested by any test: `clm/clm_harness/utils/budget.py` (412 lines: nudges, adaptive band, rollback margin escalation, retry accounting), `clm/clm_harness/utils/resume.py` (464), `clm/clm_harness/context_env/edit_gate.py`, `clm/clm_harness/context_env/env.py` step/gate integration, `clm/clm_harness/clm_agent/harness.py` loop and free-turn rule, `clm/clm_harness/agent_trajectory_format/validator.py`, `clm/clm_harness/agent_trajectory_format/harness_export.py` segment splitting, `clm/clm_harness/flops_metrics/kv_cache_flops.py`. Those are smoke-by-run-only.

Serving tests are heavier and honest: exactness on GPU, simulation harness asserting every position's KV equals its own token+position, neutral-when-off schedule equality, negative controls (`suffix_cache_reuse/vllm/DESIGN.md:264-288`).

## 13. Benchmarks

- **ContextBench**: 4 families (kv_store, log_triage, needle_retention, sudoku_sketchpad), streamed ops as user turns, stream controls pacing via `READY_FOR_NEXT_OP`, reward read from surviving context not disk (`clm/clm_contextbench/README.md:1-18`; external repo README). Paper budget 32768 − 2048 reserve, no nudges, no rollback (`clm/clm_contextbench/clm_stream.py:49-68`); 21600 s per trial is the task's `[agent] timeout_sec` (`clm/clm_contextbench/README.md:71-72`).
- **BrowseComp-Plus**: config only, 28672 budget, `shrink` gate, persistent 0.9 nudge, 6 retries, 60k-char observations, `open_problems` template (`clm/clm_harness/configs/bcp.yaml:1-27`). Tasks/dataset external (MIT).
- **EdgeBench**: config only, 32000 budget, `open_ended`, 50 retries, hourly checkpoints, custom finalize message (`clm/clm_harness/configs/edgebench.yaml:1-23`). Tasks external (CC-BY-4.0 data, Apache-2.0 code).
- Headline claims are paper claims, not re-measured here: +11.4% accuracy at −21.5% FLOPs on BCP; +5% at −59% on EdgeBench; +65% improvement same compute on 24h multi-repo (`README.md:34-44`).

Dataset licensing is clean for reuse of tasks; no benchmark task data is vendored in this repo, so no NC contamination from data.

## 14. What may be reused vs copied

**Reusable conceptually (safe, re-derive):**

1. Context-as-file with per-turn headers; protected prefix; edit = arbitrary text transform.
2. Free-turn accounting rule: free iff mirror changed AND no output AND exit 0.
3. Fit/shrink gate as a two-mode policy; rejection as actionable receipt, not silent drop.
4. Trivial-edit threshold to separate real compaction from noise.
5. Tiered nudges + adaptive urgent band sized from recent observation widths.
6. Rollback-to-margin + pinned ledger naming the commands that blew budget + escalation after repeated ignores.
7. Tokenizer calibration against server-reported prompt tokens, clamped.
8. Segment-based trajectory: one verbatim `input_context` per context break; never reconstruct across breaks; stand-in for rolled-back replies to keep 1:1 with LM calls.
9. Checkpoint workspace before context, atomic swap, thin oversized workspaces, replay capped at fraction of budget, drop trailing unanswered tool calls.
10. Newest-message-only truncation for cache friendliness; snapshot the exact list pre-send.
11. Report naive / prefix-cache / freeze prefill side by side; separate O(L²) attention from linear term.
12. Suffix KV reuse idea and its bounded approximation (serving layer, likely out of scope).

**Not reusable (copy):** any `.py`, any prompt string, any YAML template, any doc text, the frozen rejection wording, the note contract wording, the `[[CTX_TURN …]]` header format as literal text, the `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` convention (that one originates from MIT mini-swe-agent, `NOTICE:5-7` — still not ours to lift without attribution). All under CC BY-NC 4.0.

**Grey:** constants and thresholds (32 tokens, 1%, 2048 reserve, 0.25/0.5/0.75, K=6, 64/16 span rules, 20k/2000 file caps). Facts/parameters are not copyrightable, but copying a whole tuned table verbatim is evidence of derivation. Pick own defaults, document why, cite the source of the idea.

## 15. Recommended independent implementation boundaries

Goal: behavior parity where it matters, zero shared expression, clean license story.

1. **Separate protocol, own syntax.** Do not use `[[CTX_TURN n role=x]]` or `LIVE_CTX_MAIN.txt`. Choose own marker (`<ctx:t12 role=assistant>` or similar), own path, own refresh contract. Header format is the most load-bearing copied surface — avoid it.
2. **Own round-trip module.** Write `render`/`parse` from the invariants, not from the file: protected prefix re-pinned from originals; only assistant survives as assistant; stray text becomes a note; empty turns dropped; consecutive same-role merged; tool-call structure explicitly declared lost. Own tests: damaged header, duplicate index, non-ASCII, empty edit, header-only edit, role spoofing, multimodal part.
3. **Own budget state machine.** Model it as explicit states (`ok / warned(tier) / urgent / overflow / retry(n) / final / stopped`) with a pure decision function and an injectable clock/counter. Reimplement adaptive band and escalation from behavior, not code. Unit-test every transition — this is the biggest untested surface upstream, so parity-by-test is more valuable than parity-by-reading.
4. **Own gate.** Two named policies (`fit`, `shrink`), one predicate, one receipt renderer. Keep receipts short and machine-checkable; do not copy upstream sentence wording.
5. **Own ledger.** Ring buffer of N recent overflow-causing actions, injected into a pinned slot that survives edits. Choose own N (5 is upstream's; justify ours).
6. **Own recovery layer.** Checkpoint = (workspace snapshot, message list, counters) with atomic swap and explicit truncation manifest. Replay = trim to fraction of budget, drop trailing unanswered calls, cold-start note when only files survived. No shared code path with upstream.
7. **Own trajectory schema.** Either plain JSONL of context epochs, or an independent ATIF-CTX reader/writer implemented from the published invariants — not from upstream's pydantic models. If we claim compatibility, validate with our own validator and cross-check against upstream samples only as test fixtures, not as code.
8. **Metrics parity table, not code parity.** Publish a table: signal, our behavior, upstream behavior, why we differ. Cheapest defense against accidental derivation and best doc for reviewers.
9. **Attribution hygiene.** README "Prior art" section naming Shao et al. 2026, repo URL, commit SHA, and stating "behavior re-implemented independently; no code or prompt text copied; upstream is CC BY-NC 4.0." Keep upstream clone out of the repo tree and out of CI.
10. **Hard no-vendor rule.** No file copied, no `pip install` of `clm`, no git submodule, no generated file derived by mechanical transformation of upstream source. Reviewers should be able to grep our tree for upstream identifiers (`LIVE_CTX_MAIN`, `CTX_TURN`, `n_ctx_syncs`, `strict_target`) and find none.
11. **If NC code ever becomes necessary**: isolate behind an optional adapter, separate process or separate package, not linked into the commercial core, and get legal sign-off first. CC BY-NC has no patent grant; that risk does not go away with isolation.
12. **Scope boundary.** Exclude SCR (serving), RL patches (training), ICL evolution loop (offline optimizer) from v1. They are separate systems with separate dependencies; porting them expands the license and maintenance surface without improving the plugin's core loop.

## 16. Gaps and risks

- **Test coverage is thin where risk is high**: 6 unit tests total for the agent harness; budget/rollback/resume/gate all untested. Any parity claim must come from our own tests, not upstream's.
- **No archive of dropped context.** By design rolled-back and compacted text is gone (`clm/clm_harness/utils/budget.py:19-22`). If our plugin needs recoverability, that is new design, not a port.
- **Release omits features the format and paths still reference**: subagents/`SUBCTX_*`, offload, `ctx_archive` are rejected kwargs (`clm/clm_harness/clm_agent/harness.py:111-124`) yet appear in `clm/clm_harness/agent_trajectory_format/models.py:215-249`, `clm/clm_harness/context_env/env.py:40`, `clm/clm_harness/agent_trajectory_format/harness_export.py:423-426`. Treat subagent trajectory support as unproven-by-code.
- **`parse_back` is lossy**: tool-call structure destroyed after every edit; role folding can turn a tool observation into user text. Upstream accepts this. Our design should decide explicitly, and test what happens when a tool result is rewritten as user content.
- **Ledger memory is 5 commands** (`clm/clm_harness/clm_agent/harness.py:437`); longer thrash loses early causes.
- **Gate is size-only**; nothing prevents deleting load-bearing facts. Upstream relies on prompt text and nudge note contracts.
- **Evaluation provenance is partial**: ContextBench skills are sha-pinned, but no dataset revision/hash pin for BCP or EdgeBench runs in-repo; only configs.
- **No benchmark runners in-repo** for BCP/EdgeBench; runs go through external repos, so "as published" reproduction depends on third-party checkouts.
- **CC BY-NC 4.0 applied to source code** is legally awkward (no patent grant, NC scope, no source/object terms). Any team considering even non-commercial code reuse needs counsel; assumption should be "concepts only."
- **Paper numbers unverified here** — no model calls were made, per task constraints.
- Clone is depth 1, so no history/provenance review; findings are for commit `483198229ab7b90c0eed4651306ab3b3505aaf50` only.
