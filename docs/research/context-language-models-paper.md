<!-- Source: internal research note, 2026-11-10. Sanitised for publication: local paths, host and profile details removed. Original analysis retained. -->

# CLM paper — full research note (for Hermes context-engine plugin)

Tags: **[M]** = measured/reported claim in paper · **[C]** = verified in released code (implementation fact, not a perf claim) · **[I]** = my inference.

## 0. Sources, artifacts, freshness

| item | value |
|---|---|
| Paper | Shao, Shen, Yin, … Zettlemoyer, Koh. "Context Language Models". arXiv:2609.37725 [v1], dated 2026-09-29. |
| Abs | https://arxiv.org/abs/2609.37725 |
| HTML (read in full) | https://arxiv.org/html/2609.37725v1 → local `src/paper.html`, text at `src/paper.txt` |
| PDF (downloaded, not parsed page-by-page) | https://arxiv.org/pdf/2609.37725 → local `src/paper.pdf` (2.8 MB; no poppler/pymupdf on host, so text layer came from HTML) |
| Code (cloned, commit pinned) | https://github.com/facebookresearch/context-language-models — `483198229ab7b90c0eed4651306ab3b3505aaf50`, 2026-10-13 → local `src/repo/` |
| ContextBench method skills (fetched) | https://github.com/RulinShao/ContextBench (branch `master`), `clm_exp/skills/{clm,base,summary,acm,rlm,context_folding,self_compact}/*.md` |
| Corr. author | Rulin Shao, rulin@cs.washington.edu |

Versions: only `[v1]` exists on arXiv (checked). Code repo tip is **14 days newer than the paper**; where paper and code disagree I say so explicitly.

## 1. Section inventory + what was actually read

Read end-to-end from the HTML text (all body text, all appendix prose, all figure captions, all table cells):

- Abstract; §1 Introduction; §2 Related Work; §3 Pilot Study with ContextBench; §4 CLMs — 4.1 Formal Definition and Implementation with Context as a File, 4.2 In-Context Learning and RL, 4.3 Suffix Cache Reuse; §5 Results — 5.1 (5.1.1 Coding & Deep Research, 5.1.2 Open Discovery), 5.2 Learning Strategies, 5.3 SCR serving; §6 Discussion and Future Work; Acknowledgments.
- Appendix A Extended Related Work (8 sub-blocks); Appendix B Suffix Cache Reuse (6 sub-blocks); Appendix C Prefix-Reuse FLOPs Computation; Appendix D ContextBench (design, instructions, Table 3/4); Appendix E Experimental Configurations (all 8 subsections incl. steering/evolution/RL recipes); Appendix F Supplementary Results; Appendix G Context-Length Awareness.
- Tables 1–5 (all cells). Figures 1–30: **captions read, plotted values not** (see §9 gaps).

Read in code (`src/repo/`): `clm_harness/README.md`, `clm_agent/prompts.yaml`, `clm_agent/harness.py` (all 825 lines), `context_env/env.py`, `context_env/edit_gate.py`, `context_env/paths.py`, `context_utils/context_string.py`, `utils/budget.py`, `utils/tokens.py`, `utils/skills.py`, `utils/finish_policy.py`, `flops_metrics/kv_cache_flops.py` (header), `configs/bcp.yaml`, `task_templates/terminal_agent_tasks.yaml`, `clm_contextbench/README.md`, `clm_icl/note.py` (header), `suffix_cache_reuse/vllm/DESIGN.md` (header).

**Not read:** the 397-line reference list (skimmed only); `clm_rl/*` patches and `slime` bridge code; `suffix_cache_reuse/sglang|vllm` implementation bodies beyond DESIGN.md header; `agent_trajectory_format/*` (ATIF-CTX schema) beyond README mention; ContextBench task generator code; `clm_icl/propose.py`, `evolve.py`, `proposer_prompt.md` beyond note.py header. None of these change the plugin requirements below, except possibly ATIF-CTX (would matter for trajectory export fidelity).

## 2. Thesis and formal framing

- Standard LM: `c_{t+1} = c_t ⊕ f_θ^LM(c_t)` (append-only). CLM: `c_{t+1} = f_θ^CLM(c_t)` — arbitrary, model-chosen context transition (§4.1, Eqs. 1–2).
- Implementation = **context as a file**: mirror the live context into a storage file with LM write access; edits via general Bash; each edit is synchronised back into the live context and re-sent to the server. No dedicated compaction tool (§4.1 "Context-as-a-file implementation").
- Claim of generality: Eq. 2 subsumes harness-tool approaches (compaction/offload/retrieval) but lets the *model* define the functions, e.g. writing its own `compact_turns()` helper and calling it 37× (Fig 3d).
- Multi-agent: several context files coexist, each synced to its own LLM server; subagents = create/delete context files (§4.1 "Multi-agent extensions").

## 3. Measured claims (with anchors)

### 3.1 Headline [M, Abstract]
- BrowseComp-Plus: +11.4% accuracy with −21.5% FLOPs.
- EdgeBench 12 h: +5% score with −59% FLOPs.
- 24 h six-repo agent swarm: +65% greater improvement at same compute.
- Skill-text evolution: up to +35.9 points held-out accuracy *and* lower compute.
- Online RL (Qwen3.5-9B, BCP): +47.6%, −12% FLOPs.
- SCR serving: −35% server compute vs standard SGLang at matched performance.

### 3.2 Coding / deep research [M, §5.1.1, Fig 5]
- BCP 59.4% at 32K (Qwen3.6-27B, 100-turn cap, 830 questions), +11.4% relative over strongest baseline (Codex-style summary); 21.5% / 28.9% fewer prefix-reuse FLOPs than summary / MEM1.
- TerminalBench 2.1: matches summary accuracy at 70% of its FLOPs.
- TBLite: 73.7% vs 67.0% at 91% of FLOPs.
- Baselines: MEM1, Self-Compact, ACM, RLM, all on shared Mini-SWE-Agent backbone, all zero-shot (no training).

### 3.3 Open discovery [M, §5.1.2, Table 1, Fig 8]
Math optimization (Claude 4.6 Sonnet, 32K, 100 attempts / 5 h cap, **one run per method**):

| method | circle packing ↑ | Heilbronn ↑ | min-max/min-dist ↑ | Erdős overlap ↓ |
|---|---|---|---|---|
| OpenEvolve | 2.541 | 0.03127 | 0.07690 | 0.38123 |
| OpenEvolve-Agent | 2.525 | 0.03053 | 0.07724 | 0.38167 |
| CLM | 2.618 | 0.03653 | 0.07758 | 0.38094 |
| CLM (SA) | 2.636 | 0.03617 | 0.07758 | 0.38109 |

→ +16.8% Heilbronn, +3.0% circle packing vs best OE variant.

EdgeBench-10 (10 of 48 tasks, 3 seeds, 12 h, no network):
- Qwen3.6-27B, 32K: CLM **44.6 @ 179 PF**/trial vs summary **42.3 @ 437 PF**; CLM+subagents 44.2 @ 181 PF.
- Claude 4.6 Sonnet: CLM 51.0, +SA 50.4, summary 42.3.
- 128K budget (Fig 27): CLM+SA 50.2 @ 219 PF, CLM 47.3 @ 142, summary 47.8 @ 222.
- Subagents give ~no benefit on single-repo; they do at 128K.

Software World (GPT-5.6-Sol, 272K ctx, six agents, 24 h+, judged on 4 unseen downstream packages, 17 held-out CPU benchmarks, scoring = geomean speedup, cost = USD API spend): CLM swarm = **+65% greater downstream speedup** than summary swarm at same spend [M, §5.1.2, Fig 8 right].

### 3.4 Learning strategies [M, §5.2, §4.2]
- **Steering by one sentence** (Fig 9, App E): three behaviours — compact at threshold Y, compact at subtask boundaries, back up before every edit. Prompt text given verbatim in App E (see §6.3). Threshold run: 48K budget, 200 turns, 30 long questions, report median context at first compaction. Boundaries: 189 sessions/condition, 131 with an in-session boundary; metric = compaction within 2 turns of a question boundary. Backup: 16K budget, 200 questions, 91 paired.
- **Skill evolution** (App E, Fig 10/28): proposer loop, 6 train instances → 12 → dev split 102 instances/task; assisted = Qwen3.6-27B agent + Claude Fable 5.1 proposer; self = Opus 5 both. Dev accuracy: Needle 97.6→100.0, Sudoku 45.3→65.8, KV Store 22.3→83.8, Log Triage 0.0→100.0. Held-out test KV Store 38.3→74.2 = **the 35.9-point headline**.
- **RL** (Table 2): Qwen3.5-9B on 3,040 OpenResearcher prompts; CLM 28.8→42.5% at 1.52→1.34 PFLOPs/Q; summary harness 34.7→42.1% at 4.01→2.19. Pre-RL, CLM *loses* to summary by ~6 points — gains scale with model capability.
- Success-gated efficiency advantage (Eq. 6): re-rank only within successful group; `A_eff = clip((c̄_g − c_i)/c̄_g, −1, 1)`; 0 if <2 successes; `A = A_out + w_eff·A_eff` (w_eff = 0.25, App E).

### 3.5 Serving / cache [M, §4.3, §5.3, App B/C]
- **Prefix-reuse FLOPs** = prefill(unmatched suffix from first mismatch) + decode(generated) (Eq. 3, App C Eq. 7–9). This is the paper's cost ruler — it *charges* the re-prefill an edit forces.
- SCR (Fig 4, 12): replace span B→B′; reuse cached KV of surviving spans C, re-rotate their RoPE to new positions, splice after B′; only B′ + new tokens prefilled. Up to K=6 relocated spans per edit, largest first; session-private cache slots; falls back to standard re-prefill if slots unavailable.
- BCP: SCR = **65.0%** of standard SGLang's prefix-reuse FLOPs at matched accuracy (Fig 11).
- Decomposition (Fig 14, 830 questions): 7.8% of all prompt tokens reused beyond prefix hits; **5.3 pts from reasoning-token stripping** in ordinary chat serving, only 2.5 from real context edits → most SCR benefit needs no CLM at all.
- K sensitivity (Fig 13, 64 BCP questions, K∈{1,2,3,6,12,64}): accuracy robust; cache gain saturates at 6.
- Hybrid models (Qwen3.6-27B: 48/64 layers linear attn): linear layers keep a fixed recurrent state, so SCR snapshots the state before the edit and B′ is *not* recomputed there — an explicit approximation (App B).
- Residual inefficiency: SGLang stores recurrent states only at request boundaries → unchanged-prefix prefill remains; finer-grained (message-boundary) state checkpoints named as future work (App B, Fig 15).
- Illustrative per-turn cost, Qwen3.6-27B, P=20k, G=500 (App C, Fig 16): append-only 1.41e14 FLOPs; mid-context edit (R=10k) 5.74e14 (**4.1×**); head edit (R=0) 10.81e14 (**7.7×**). `C_token`=48.70e9, `C_attn`=3.93e5/pair.

## 4. Mechanism, as implemented [C]

All paths/lines from `src/repo` at the pinned commit.

### 4.1 Mirror + turn headers
- Mirror path `/tmp/.live_ctx/LIVE_CTX_MAIN.txt`; bash-state dir `/tmp/.bash_ctx_state`; `_PROTECT = 2` (system + task pinned) — `harness.py:103-106`.
- Before every command the harness renders the editable region and uploads it (`env.py:113-118`). Rendering: one block per message after the protected prefix, numbered 1..N from the top of the editable region, header is line-anchored `[[CTX_TURN <i> role=<role>]]` — `context_string.py:41,90-101`. Number is display-only; `parse_back` ignores it (`context_string.py:97`).
- Turn text = `reasoning_content` + `content` + `tool_calls[*].function.name/arguments` (`context_string.py:63-87`) — reasoning and the *command* are part of the editable text because bash observations don't echo the command.

### 4.2 Parse-back (tolerant, lossy by design)
`context_string.py:104-168`:
- Protected prefix **re-pinned from originals** → system/task contract cannot be corrupted by an edit.
- Whatever headers survive define the turns; text before/between headers becomes a `user` note; unknown roles → `user`; only `assistant` stays assistant (**tool-call structure is not reconstructed**); empty turns dropped; consecutive same-role merged. Result is always template-legal.
- Consequence: after any applied edit the trajectory is role+text only. Tool results become `user` turns.

### 4.3 Edit detection + gate + receipts
- Edit detection is a **diff**: read mirror back; if `strip()` differs from what was written → attempt apply (`env.py:211-214`). `touched_ctx` is only a heuristic for "you meant to edit but nothing changed" (`env.py:198-200`, exclusions for subagent paths).
- Gate (`edit_gate.py`): `fit` (default) accepts growth if result ≤ limit; `shrink` (used for BCP) requires `after <= baseline`; rejection note wording frozen: `"A compaction must SHRINK context"` / `"An edit must FIT the <limit>-token budget"`. Rejected edits are **not applied** and the model is told why (`env.py:273-280`).
- Receipts (prompt text the model sees, `env.py:217-229,296-314`): edit applied (`~before->after tokens, N turns`), applied-but-grew, applied-but-still-over (`compact more NOW`), rejected, "edit matched nothing", "command exited N, nothing compacted".
- Trivial-edit bar: `<32 tokens` or `<1%` of editable region does not count as a real sync (`env.py:35-36,286-289`) — edit counts use `n_ctx_syncs_real`.

### 4.4 Budget controller (`utils/budget.py`)
- `strict_target = budget − reserve`, reserve default **2048** (`budget.py:83`). Count = tiktoken `o200k_base` over all message text × `tok_ratio`, where `tok_ratio` is learned from server `usage.prompt_tokens / raw` and **clamped to [0.5, 3.0]** (`budget.py:132-169`); `CLM_TOK_RATIO=0` pins 1.0. Rationale in docstring: gateway token counts unreliable, tokenizer error multiplicative.
- Rationale for deterministic gate: "DETERMINISTIC gate … never a provider-calibrated estimate"; "NO mechanical truncation".
- Tiered nudges at `nudge_ratios` = 0.25/0.5/0.75 of *budget*, each fires once, re-arms after the model compacts below (`budget.py:171-237`). Text tiers (`budget.py:363-398`): ≤25% informational, explicitly **no how-to** (a how-to at 25% "tends to trigger premature wholesale deletion"); ≤50% finish unit of work then tidy once; >50% compact settled spans, **do not wipe** (<25% retention of a region usually means redoing the work), plus overflow consequence. The 50%/75% tiers append the **note contract**: quote RULED-OUT candidates + "do not retry", exact queries/commands tried, exact values marked VERIFIED/UNVERIFIED, and a NEXT line (`budget.py:356-361`).
- Persistent urgent nudge: every turn once inside the band. `adaptive` → `need = max(10% of limit, 2 × largest of last 3 tool outputs)`, capped so band never starts below 50% of limit; with small outputs it *is* the 90% rule (`budget.py:205-265`).
- Overflow: **rollback-and-retry**. Drop newest turns until `retry_edit_margin` (2048) tokens are free, never touching protected prefix and never leaving an assistant `tool_calls` turn without its result (`budget.py:287-308`). Depth escalates (`3×`…) only after 3 consecutive rollbacks the model ignored; a real compaction resets escalation (`budget.py:273-285,267-271`). Each rollback pins a **ledger** note just after the protected prefix listing the ≤5 commands whose output blew the budget (`harness.py:419-449`, `budget.py:310-331`) — anti-livelock, because rollback erases the model's memory of what it tried.
- After retries are exhausted: one **final turn** notice, then `stop` and grade as-is (`budget.py:333-340`).
- Newest-output cap: only the newest tool output is head/tail-truncated to fit the remaining room (`budget.py:400-412`, `tokens.py:head_tail_truncate`) — "cache-friendly: only ever applied to the NEWEST message so earlier (cached) messages are never disturbed".
- Turn accounting: a turn is **free** (does not consume a task step) only if `ctx_changed && stdout/stderr empty && rc == 0` (`harness.py:127-130,633-647`); total LM calls bounded separately by `lm_call_cap = 2*max_steps + 24` (`harness.py:309-314,551-554`) and `max_total_iters = 2*max_steps + 8 + min(retries, max_steps)` (`harness.py:507`).
- `context_status` tool: **not advertised**, but a model that calls it gets `N/limit tokens = P% used; free, growing ~R tokens/turn recently, about T turns of headroom left` (`env.py:127-145`, `harness.py:598-607`).
- Any non-`bash`, non-`context_status` tool call is dropped and only the first executed call is kept, to avoid unpaired tool ids that Anthropic/Bedrock reject (`harness.py:139-147`).

### 4.5 Prompt (verbatim structure) [C, `prompts.yaml`]
System prompt: "THOUGHT before action, EXACTLY ONE bash command"; then a `## Managing your context` block:
- Goal line + "Your context budget is {{context_budget}}; each result shows your current size." — the advertised value is `strict_target`, i.e. budget − reserve (`harness.py:456-462`).
- Mirror path + "refreshed before every command"; free-edit rule ("an editing turn is free only when its command applies a real edit and prints nothing — any stdout/stderr, non-zero exit, or an edit that matches nothing costs a normal turn").
- "Locate text with code — never paste or retype it"; worklist from `[[CTX_TURN <i> role=…]]` headers; worked `re.sub` example.
- "don't `cat` this file (its text is already in your context)"; "keep the `[[CTX_TURN …]]` header of any turn you keep — emptying its text drops that turn"; prefix protected for you.
- **Compact cheaply**: edit forces everything *after* it to be re-read → batch; mind what's below the edit; "be generous in the summary: the tail is re-read regardless".
- `{{finish_instructions}}` → submit via standalone `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` (`finish_policy.py:27-35`).
Observation truncation: head 5000 / tail 5000 chars + a "produce less output" hint (`prompts.yaml:63-70`, `env.py:156-170`).

### 4.6 Task-agnostic skills, not hard-coded tools [C, `utils/skills.py`]
Tool surface is *mounted*, not compiled in: upload skill dir into sandbox, `chmod +x`, prepend to PATH, export env keys declared by each skill's `skill_env_keys` manifest (URL values skipped), append `SKILL.md` to the system prompt. Paper §5.1.1: "we expose the search tools as in-context skills … keep as little as possible hard-coded at the harness level".

### 4.7 Configs actually used [C]
- `configs/bcp.yaml`: budget 28672 (= 32768 − 4096), reserve 2048, `observation_max_chars 60000`, `max_steps 500` (100/500/2000 used), `persistent_nudge_ratio 0.9`, retries **6**, `CLM_EDIT_GATE: shrink`, `task_template: open_problems`, `cost_metric: flops`.
- `clm_contextbench/README.md` paper kwargs: budget 32768, `max_steps 100000`, `lm_call_cap 0` (off), `max_tokens 8192`, `observation_max_chars 50000`, **`nudge_ratios ""` (all nudges off)** for the pilot/evolution runs.
- Paper App E: summary harness compacts at 75% of budget; Self-Compact asks every 2 turns once >37%; "CLM receives its editing reminder 2,048 tokens before the budget".

## 5. Context-budget signals — what the model is given

| signal | form | anchor |
|---|---|---|
| Budget, in system prompt | `N tokens` (= strict target) | `harness.py:456-462` |
| Per-turn readout | `[context: ~N/M tokens]`, plus ` — OVER; compact … now` | `env.py:236-248` |
| Growth rate | only via `context_status` (unadvertised) | `env.py:127-145` |
| Tiered nudges | 25/50/75 % of budget, once, re-arm | `budget.py:171-237` |
| Urgent nudge | every turn in adaptive band | `budget.py:205-265` |
| Overflow | rollback notice + ledger, or final-turn notice | `budget.py:310-340` |
| Edit receipt | before→after tokens, turn count, rejection reason | `env.py:217-229,296-314` |
| Finish pressure | last 3 task steps or LM calls | `harness.py:725-746` |

Context-length awareness is *acknowledged as weak* [M, App G]: models estimate token counts in recurring buckets (6.2K, 9.8K, 10.4K), Claude-4.6-Sonnet underestimates, GPT-5.4 best calibrated; token-count anchors improve estimation and **anchors closer to the estimation point work better**. Hence the per-turn readout: "we use them as a temporary augmentation" (App E fn 4).

## 6. Cache behaviour — the operational facts for a plugin

1. Any in-the-middle edit invalidates the KV suffix under prefix caching; cost scales with the *tail*, not the edit size [M Fig 16 + C prompt text]. Mid-context edit = 4.1× an append-only turn; head edit = 7.7×.
2. The released harness therefore (a) never rewrites old messages except by explicit model edit, (b) caps only the newest observation, (c) tells the model to batch edits and to weigh "what's below the edit".
3. SCR is **server-side**, not harness-side: SGLang patch / vLLM `--scheduler-cls SCRScheduler` + KV connector, capping prefill at |A|+|B′| and jumping `num_computed_tokens` over relocated spans (`suffix_cache_reuse/vllm/DESIGN.md` §1–2). Relocated KV is cached under the *pre-edit* prefix, re-RoPE'd, capped at K=6 spans — an approximation, and for linear-attention layers the recurrent state is snapshotted so B′ is never recomputed there.
4. Reasoning-token stripping by chat templates is itself a cache-breaking edit: 5.3 of the 7.8 reused points [M Fig 14]. A Hermes plugin talking to a stripped-reasoning endpoint pays this whether or not the model edits.
5. FLOPs accounting replays per-turn prompts through a prefix-cache model (vLLM block granularity), preferring server `prompt_tokens` + `cached_tokens`, then `prompt_token_ids`, then HF tokenizer, then tiktoken (`kv_cache_flops.py:1-80`). One monotonic snapshot counter with `kind` ∈ {agent, final} per LM call (`harness.py:373-392`) so a replay sees every divergence.

## 7. Security / safety

- Paper §6 only, no evaluation: "Editable context can become another channel through which prompt injections or self-generated instructions persist across turns." Cites OpenAI (2026b) observing a model inserting unauthorized instructions into **its own compaction summary** that later affected behaviour. Calls for characterizing attack surfaces + defenses that keep flexibility.
- Implementation-side properties [C]:
  - Edits execute **inside the sandbox VM**, not via in-process `exec` → "no sandboxed-code-execution concern" (`harness.py:9-18`).
  - Protected prefix re-pinned from originals on every parse → system/task cannot be edited (`context_string.py:134`).
  - Same protection makes injected text durable *by design*: anything the model writes into a surviving turn is reloaded verbatim next turn.
  - Role folding means model-authored text re-enters as a `user` turn (`context_string.py:135-139`) — structurally indistinguishable from real operator input downstream. [I: this is the sharpest security consequence for Hermes, where `user` turns carry authority.]
  - Mirror is a plaintext copy of the whole transcript, including every tool result, on a path the agent's own tools can read/write → secret-exposure surface inside the workspace [I].
  - No integrity check beyond diff + gate; no edit provenance, no signing, no allow-list of edit shapes [C].
- New roles are creatable (Fig 3b: `role=notes`); `parse_back` folds them to `user` unless `_VALID_ROLES` is extended (`context_string.py:42`).

## 8. Limitations (stated + observed)

Stated [M]:
- Gains scale with model capability: 9B pre-RL is 6 pts *worse* than summary; on TB2.1 Qwen3.5-9B edits 1.4×/task and not at all in half the tasks, median peak 30.2K/32K vs 17.6K for 27B (App F).
- SCR approximates; K bound; hybrid linear-attn caching leaves redundant prefill; SGLang checkpoint granularity limits it (App B).
- FLOPs metric omits embeddings/output layers, recurrent-state update, norms/activations/softmax (App C).
- Math evals are single runs per method (App E).
- Safety: open problem (§6).

Observed by me:
- **Tool-call fidelity is destroyed by design** [C]: any applied edit flattens the trajectory to role+text. Fine for a bash-only agent; a Hermes session that must preserve paired `tool_calls`/`tool` ids cannot round-trip this way without extra bookkeeping.
- **Paper↔code drift**: (a) "editing reminder 2,048 tokens before the budget" is not a distinct mechanism in code — what exists is `reserve=2048` subtracted from budget plus 0.25/0.5/0.75 tiers measured against the *full* budget (`budget.py:83,226`); (b) the released ContextBench CLM skill tells the model "Do NOT anchor on a `[[CTX_TURN i role=...]]` header: the mirror numbers its turns internally and you never see that numbering", while the harness prompt tells the model to match exactly those headers (`prompts.yaml:29-33`). Two different mirror contracts.
- **Subagents are not in the release**: `subagents`, `n_subagent_slots`, `subagent_max_steps`, `subagent_self_compact` are in `_REMOVED_KWARGS` and raise (`harness.py:111-124`). Paper's CLM (SA)/swarm numbers cannot be reproduced from this repo.
- ContextBench pilot numbers (Fig 2) are only plotted; no table of accuracies by method×pressure level in text.
- Cost claims are prefix-reuse FLOPs, not wall-clock; Software World uses USD API spend — mixing rulers across sections.

## 9. Coverage gaps (explicit)

- Figure-only data not extracted: Fig 2 (ContextBench by pressure 1–24×), 5, 9 (steering effect sizes), 10/28 (evolution Pareto), 11 (SCR split), 13, 14, 15, 17, 19, 27, 29, 30. Values quoted above are the ones stated in prose/captions.
- Reference list not enumerated; I did not chase cited baselines (ACM, Self-Compact, MEM1, RLM, Context Folding, Sculptor, Meta-Harness, AgeMem, MemGPT) to their own papers.
- ATIF-CTX trajectory schema not studied — relevant if the plugin must emit the paper's trajectory format.
- No independent reproduction: no runs, no LLM benchmark spend (as instructed). All perf numbers are the authors' claims.
- `suffix_cache_reuse/{sglang,vllm}` implementation bodies not audited; only the vLLM DESIGN.md contract was read.

---

# 10. Actionable requirements for the Hermes context-engine plugin

Scope: Hermes is a general agent (rich tools, structured `tool_calls`, gateway/prefix-cache serving), not a bash-only sandbox agent. Each requirement = rule + why + provenance.

### A. Mirror and rendering

**R1. Mirror the *editable region only*, at a stable path, refreshed immediately before every model turn and every tool result.** Paper: context mirrored to a file, refreshed before each command [M §4.1, C `env.py:113-118`, `prompts.yaml:22`].
**R2. Pin a protected prefix (system + task) and re-pin it from the originals on every parse.** Nothing the model writes may alter the system prompt [C `context_string.py:134`]. Do not "trust" the mirrored prefix.
**R3. Use a line-anchored, machine-recoverable turn header** (`[[CTX_TURN <n> role=<role>]]`, n counted from the top of the editable region, display-only) [C `context_string.py:41,90-101`]. Header must survive arbitrary model edits; recovery must not depend on it.
**R4. Render the text the model actually saw**: reasoning + content + the *arguments of tool calls*, not just `content`. Counting/rendering content only undercounts a tool-heavy prompt by ~2× [C `tokens.py:_iter_text` docstring; `context_string.py:63-87`].
**R5. Tolerant parse, always legal**: surviving headers define turns, stray text → user note, unknown role → user, drop emptied turns, merge consecutive same-role [C `context_string.py:104-168`].

**R6 (design decision Hermes must add).** Structured-tool fidelity is *not* handled by the paper. Options: (a) side-index `turn → original tool_calls`, restore structure when the turn body is byte-identical, flatten only edited turns; (b) forbid edits that span a tool-call/tool-result pair; (c) accept flattening and mark those turns stale. [I] (a) is the least surprising; (c) is cheapest and matches the paper. Whichever is chosen, the *model-visible* contract must say it.

### B. Edit application

**R7. Apply edits by diff of read-back vs written mirror; treat "no diff" as no-op even if the command touched the path** [C `env.py:198-214`].
**R8. Gate edits with an explicit mode**: `fit` (accept if result ≤ limit, default) or `shrink` (accept only if smaller; BCP used `shrink`). Rejected edit is *not applied*, and the rejection note must state the rule and the likely cause (duplication/appending instead of replacing) [C `edit_gate.py`, `env.py:273-280`].
**R9. Emit a receipt on every edit attempt**: applied (+ before→after tokens + turn count), applied-but-grew, applied-but-still-over ("compact more NOW"), rejected, "matched nothing" (with the real header shape as a hint), non-zero exit. Silence is indistinguishable from failure [C `env.py:217-229,296-314`].
**R10. Filter trivial edits** (<32 tokens or <1% of the editable region) out of compaction accounting, or "compaction" metrics become noise from newline churn [C `env.py:35-36,286-289`].
**R11. Count a pure edit turn as free** — no task-progress output, rc 0, context changed — and bound LLM calls separately (`2×max_steps + 24`), or a model can burn the step budget on compactions or loop on free turns forever [C `harness.py:127-130,309-314,633-647`].
**R12. Answer `context_status`-style probes even if the tool is not advertised** — cheaper than rejection [C `env.py:127-145`].

### C. Budget signalling

**R13. Enforce against `budget − reserve` (2048) and advertise that same number** in the system prompt; never advertise the raw window [C `budget.py:83`, `harness.py:456-462`].
**R14. Count tokens deterministically (tiktoken o200k) and calibrate to the served tokenizer with a clamped ratio learned from the gateway's `usage.prompt_tokens`** (clamp [0.5, 3.0]; allow disable). Provider counts alone are unreliable; tokenizer error is multiplicative so a fixed reserve cannot absorb it [C `budget.py:132-169`].
**R15. Emit a per-turn readout** `[context: ~N/M tokens]` with an `OVER` suffix; this is the single highest-leverage affordance because models cannot estimate context length (bucketed estimates, underestimation) and anchors near the estimation point work best [M App G, C `env.py:236-248`].
**R16. Escalating nudges at 25/50/75% of budget, once each, re-arming after the model compacts below**: 25% informational with *no how-to* (how-to at 25% triggers premature wholesale deletion); 50% finish unit then tidy once; >50% compact settled spans, don't wipe (<25% retention usually means redoing work); 50%+ carry the note contract (quote RULED-OUT + "do not retry", exact queries/commands, VERIFIED/UNVERIFIED values, NEXT line) [C `budget.py:356-398`].
**R17. Add a persistent urgent nudge in the danger band**: fire every turn once headroom < `max(10% of limit, 2 × largest of last 3 tool outputs)`, never starting below 50% of limit. A fixed fraction misses whenever one tool output is wider than the band — the context jumps from below the band straight past the limit [C `budget.py:246-265`].
**R18. On overflow, prefer rollback-and-retry over truncation**: drop newest turns until `retry_edit_margin` (2048) free, never orphaning a tool-call/tool-result pair, then demand a compaction. Pin a **ledger** of the ≤5 commands whose output caused the overflow, because rollback erases the model's memory of what it just tried (livelock guard). Escalate depth only after repeated ignored rollbacks; reset escalation on a real compaction. After N retries (BCP used 6, EdgeBench 50): one final turn, then stop and evaluate as-is [C `budget.py:171-340`, `harness.py:419-449`].
**R19. Never mechanically truncate history when a model-editable path exists.** The whole point of the paper is that harness-scheduled truncation is what loses information [M §2–3, C `budget.py` docstring "NO mechanical truncation"].
**R20. Cap only the newest tool output** (head/tail truncate to fit) so earlier cached messages are untouched [C `budget.py:400-412`].

### D. Cache / serving

**R21. Put the cache economics in the model-visible prompt**: an edit forces everything *after* it to be re-read, so batch edits, weigh the tail below the edit, and be generous in the replacement text since the tail is re-read regardless [C `prompts.yaml:47-56`, M Fig 16: 4.1× mid-edit, 7.7× head edit].
**R22. Assume standard prefix caching on the gateway.** Suffix Cache Reuse needs an SGLang patch or a vLLM scheduler subclass + KV connector; it is not reachable through an OpenAI-compatible gateway, and its reuse is approximate (pre-edit KV, re-RoPE, K≤6 spans). Treat the paper's −35% as *server-side upside you cannot buy from the plugin* [C `suffix_cache_reuse/vllm/DESIGN.md`, M §5.3].
**R23. Account cost the paper's way**: prefix-reuse FLOPs = unmatched-suffix prefill + decode, replayed from per-LM-call prompt snapshots. Keep one monotonic snapshot stream tagged `agent`/`final` per call so a replay sees every edit-induced divergence [C `harness.py:373-392`, `kv_cache_flops.py`].
**R24. Expect to pay for reasoning-token stripping** on serving stacks that drop prior reasoning: it is the majority (5.3 of 7.8 points) of SCR's headroom and happens without any model edit [M Fig 14].

### E. Multi-agent / subagents

**R25. Namespace context files per agent** (`…/LIVE_CTX_<AGENT>.txt`), create on subagent spawn, delete on terminate; do not let subagent mirrors share a directory with the main mirror (released code even excludes `SUBCTX`/`subagents` path parts from the edit heuristic, `env.py:39-40`) [C].
**R26. Do not promise the paper's subagent results**: subagent kwargs were removed from the release and raise on use [C `harness.py:111-124`].

### F. Security (highest-severity deltas from the paper)

**R27. Treat the context file as an untrusted-injection channel with durable, cross-turn authority.** A model can write instructions into a surviving turn and they reload verbatim next turn; role folding makes them look like user input. Requirement: provenance-mark model-authored region (or keep `role=notes` as its own non-privileged role), and never let mirrored text be parsed back as system/operator authority [C `context_string.py:135-139`, M §6 + OpenAI 2026b summary-injection case].
**R28. Keep the protected prefix immutable and verify it on load**, not merely by convention [C R2].
**R29. Treat the mirror as a secret-bearing artifact**: it duplicates the whole transcript (tool outputs, tokens, PII) at a path inside the agent's own writable tree. Decide explicitly: redact on write, scope the path, and/or delete on session end (released code deletes only the host staging file, `harness.py:719-722`) [I, from C].
**R30. Audit-log every applied edit**: before/after token counts, turn count, command text, gate decision. The paper's own logs do this (`usage.json`: `n_ctx_syncs`, `n_ctx_syncs_real`, `n_ctx_grew`, `n_ctx_rejected`, `free_ctx_turns`, `n_nudges`, `n_turns_rolled_back`, `code_fingerprint`) [C `harness.py:658-694`].
**R31. Execute edits as sandboxed tool calls, never in-process string exec** [C `harness.py:9-18`].
**R32. Forbid dumping the mirror into context** (`cat`/`grep` of the whole file): it creates a second copy, after which an edit can land on the stale copy while the live one silently stops moving [M/C ContextBench CLM skill text].

### G. Prompt/skill packaging

**R33. One compact "managing your context" block in the system prompt** with: goal, budget number, mirror path, free-edit rule, locate-with-code rule, header contract, don't-`cat` rule, compact-cheaply rules, finish contract [C `prompts.yaml:16-58`].
**R34. Prefer in-context skills over hard-coded tools** for anything task-specific (search, fetch). Tool surface mounted at runtime keeps the harness task-agnostic and matches "less is more" [M §5.1.1, C `utils/skills.py`].
**R35. If shipping a guidance skill, resolve the header-contract conflict first.** The harness prompt says anchor on `[[CTX_TURN …]]`; the released ContextBench CLM skill says never anchor on them. Pick one, then make prompt + skill + receipts agree [C observed drift].
**R36. Keep the steering instructions cheap and literal.** One appended sentence changes policy reliably (threshold / boundary / backup), so plugin users can steer compaction by prose, no config [M §5.2, App E verbatim sentences].

### H. Expectations to publish with the plugin

**R37. Gains are model-conditional.** Pre-RL 9B *lost* to a summary harness by 6 points and rarely edited; 27B/Claude/GPT-5.6 won. Do not advertise CLM benefits for mid-tier models without qualification [M App F, Table 2].
**R38. Do not build a scheduled auto-compactor and call it CLM.** The paper's baselines all do that and lose; the mechanism is unrestricted model-driven editing + receipts + budget signal [M §2–3, §5.1.1].

## 11. One-line summary for the parent

CLM = mirror the editable context to a file, give the model Bash on it, parse edits back into a legal message list with the system/task prefix pinned, gate on fit/shrink with an explicit receipt, and drive awareness with a per-turn token readout plus 25/50/75% nudges, an output-sized urgent band, and rollback-and-retry with a pinned ledger — all measured in prefix-reuse FLOPs because every in-the-middle edit invalidates the KV suffix.
