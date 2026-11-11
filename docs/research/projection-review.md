<!-- Source: internal research note, 2026-11-10. Sanitised for publication: local paths, host and profile details removed. Original analysis retained. -->

# Projection architecture — adversarial review

Scope: proposed design — raw host history immutable; plugin per-session durable store;
`select_context` projects committed keep/drop/rewrite/pin/offload/restore ops onto fresh
request copies every loop iteration; engine tools mutate only the store; system + all user
messages protected; assistant tool_call + all results one atomic group; aux via
`agent.auxiliary_client.call_llm(task="compression")` producing proposals only.
Evidence tree: `the Hermes checkout` @ `2bd378ed` (v0.23.0), paths cited relative to it.
Companion report: `hermes.md` (same dir).

## 0. Verdict

Design viable without core patches. It is *not* viable as naively implemented. Three gaps are
fatal if built the obvious way:

1. **No row identity in the request list.** `request_messages` carry no `message_uid`, and are
   not positionally or length-aligned with `conversation_messages`. Projecting "onto request
   copies" needs an alignment algorithm the host does not supply.
2. **Rebuilding rows destroys system/memory injection.** The selected list is final; nothing
   re-adds the system prompt, memory prefetch, or `pre_llm_call` text afterwards.
3. **Overflow liveness.** `compress()` returning identity on provider-proven overflow fails the
   turn *and* arms gateway auto-reset. Projection cannot rescue it because the host scores
   shrink on the live list.

Everything else is trap-level: fixable with named rules, no core change.

## 1. What the design gets for free (verified)

- `select_context` runs once per provider-bound iteration, and again after every restart
  (preflight compaction, failover, overflow) — `agent/conversation_loop.py:1745-1759`,
  `agent/turn_request_assembly.py:142-147`, `agent/turn_iteration_prep.py:519-546`.
- It runs **before** the cache plan, sanitizers, whitespace/canonicalization — so a projection
  is revalidated downstream and a no-op is byte-identical (`turn_request_assembly.py:149-231`).
- Returned list is request-only; host never writes it to `messages` (`conversation_loop.py:1351-1398`).
- Not re-run on retries → no per-retry revision churn (`conversation_loop.py:1571-1600`,
  `agent/turn_api_request.py:177-217`).
- Engine tool dispatch hands `messages=` the live list and takes a JSON string back; exceptions
  become `{"error": ...}` (`agent/tool_executor.py:1689-1695`).
- Tool dispatch is sequential-only for engine tools; the parallel planner does not know them →
  sequential barrier (`agent/tool_dispatch_helpers.py:182-258`).
- Usage anchor validates against the raw list, so an untouched live transcript keeps the
  host's pressure accounting coherent — an in-place commit would have had to clear
  `_usage_anchor` (`agent/usage_anchor.py:108-122`).
- Cache plan is built on the selected list, so a stable revision = stable prefix
  (`turn_request_assembly.py:203-231`).
- Raw history is genuinely not rewritten by the engine path: micro-compaction is a
  `ContextCompressor` mixin only (`agent/micro_compaction.py:39-41`), so it never runs on a
  plugin engine.

## 2. Fatal gap 1 — request list has no identity

Facts:

- `message_uid` is in `PERSISTENCE_ONLY_MESSAGE_FIELDS` (`agent/message_metadata.py:46-54`) and
  is popped from every outgoing copy (`agent/turn_context.py:1363-1365`). So `request_messages`
  have **no** uid; only the `conversation_messages` clones do (clones are full structural copies,
  `agent/conversation_loop.py:1092-1100`).
- `request_messages` ≠ `conversation_messages` in shape:
  - system row prepended (`turn_context.py:1430-1431`);
  - ephemeral prefill rows inserted after system (`turn_request_assembly.py:132-138`);
  - MoA context appended before the hook (`turn_request_assembly.py:129-130`);
  - historical prefix passed through `canonicalize_replay_history`, which can **drop** rows
    (dangling read-only tool tail), **add** rows (synthesized UNKNOWN results for side-effecting
    dangling calls), and **rewrite** user content (expired dangerous-confirmation sentinel) —
    `agent/replay_cleanup.py:120-145`, `:154-171`, `:189-193`;
  - vision replay re-attaches images to tool rows (`turn_context.py:1414-1417`);
  - `api_content` substitutes wire bytes for historical user/assistant rows (`turn_context.py:1381-1388`).
- Host-side tests pin that uids are only guaranteed on `compress()` input, `post_llm_call`
  history and `on_turn_complete` — not on `select_context`
  (`tests/agent/test_context_engine_message_uid.py:1-8`); the strip test asserts a
  selection made of `conversation_messages` clones still gets stripped
  (`tests/agent/test_context_engine_message_uid.py:389-419`).

Consequence: positional mapping (`api_idx = conv_idx + 1`) is wrong the moment prefill is set or
the prefix is canonicalized, and *silently* wrong (no error, just a projected row in the wrong
place, or the system row dropped).

Mitigation (no core change):

- Treat ops as **masks keyed by uid over `conversation_messages`**, and apply them to
  `request_messages` through an in-order subsequence alignment:
  match on `(role, tool_call_id, expected_wire_content)` where `expected_wire_content` =
  `api_content if str else content` from the conversation row (both are present on the clone).
  Tool rows without ids match by content; image rows match by `tool_call_id` only.
- Unmatched `request_messages` rows **pass through untouched** (system, prefill, MoA, canonicalized
  synthesis, image-rewritten rows). Unmatched conversation rows mean the host already dropped the
  row → the op is inert.
- Never rebuild a row from stored bytes. Ops produce a *decision* (keep / drop / replace content /
  stub); kept rows are the host's dicts, verbatim.
- Ambiguity (two candidate rows, digest mismatch) → fail closed: leave the request unchanged and
  return an error string from the tool so the model knows the op did not apply.

## 3. Fatal gap 2 — system prompt and memory injection live only in `build_api_messages`

Facts:

- Memory prefetch (Honcho etc.) and `pre_llm_call` user context are composed into the current
  turn's user row **at prologue** into `api_content` (`agent/turn_context.py:997-1013`,
  `:1264-1286`) and replayed into the wire copy (`turn_context.py:1369-1380`).
- The system prompt is prepended at the end of `build_api_messages` (`turn_context.py:1424-1431`).
- After `select_context`, the host applies only rejected-thinking suppression, sanitizers, image
  eviction, whitespace, tool-call canonicalization, cache plan, MoA. Nothing re-adds system or
  injection (`turn_request_assembly.py:149-231`). The returned list is used as-is
  (`conversation_loop.py:1388-1393`).

Consequence: an engine that rebuilds the list from `conversation_messages` (the obvious way to
get uids) ships a request with **no system prompt** and **no recall block**. The model still
answers, so this fails silently and looks like "persona drift", not a bug.

Mitigation: `request_messages[0]` (when `role == "system"`) and the row identified by
`incoming_message` are copied byte-identically and are never op targets. Enforce in code:
protected set = {system row, every `role == "user"` row, prefill rows, current user row} and the
projection is a filter+transform over the remaining assistant/tool rows only.

## 4. Fatal gap 3 — `compress()` must be a real compactor (liveness)

Facts:

- Provider overflow recovery calls `agent._compress_context(..., bypass_cooldown=True,
  trigger="overflow")` (`agent/turn_overflow.py:140-178`).
- If the returned list is the same live list (`messages is before`) or does not shrink it, the
  host scores `shrank=False` and **fails the turn**: "conversation is too long … cannot be shrunk
  further" (`turn_overflow.py:431-451`); 413 path fails with "Payload too large and cannot
  compress further" (`turn_overflow.py:219-275`).
- `fail_turn` defaults `compression_exhausted=True` (`turn_overflow.py:87-90`) → the gateway
  context-recovery / auto-reset contract (`agent/conversation_loop.py:1795-1799`).
- Engine kwargs are signature-filtered; `bypass_cooldown` is only ever passed when True
  (`agent/conversation_compression.py:1691-1711`). So its **presence** is the only reliable
  "provider proved overflow" discriminator; `trigger` never reaches the engine.
- Manual paths: CLI `/compress` → engine `compress(force=True)`; identity result is reported
  honestly as "No changes from compression" (`agent/manual_compression_feedback.py:29-71`).
  Gateway `/compress` on a cold session builds a fresh engine, binds it, and treats
  `compressed == head` as nothing to report (`gateway/session_mutation_compress.py:119-135`,
  `:165-197`) — the cold path never calls `on_session_start`.
- Same-object mutation is read as a no-op: host compares identity
  (`turn_overflow.py:153-159`, `agent/turn_preflight.py:316-346`). A real commit must be a **new
  list**.

Consequence: `should_compress() -> False` (required to keep raw history immutable) plus
`compress() -> identity` is a correct steady state and an incorrect failure state. Under a
pathological turn — one protected user message larger than the window, a provider-adopted smaller
window (`turn_overflow.py:315-340`), an under-sized offload — the turn dies and the session is
flagged compression-exhausted.

Mitigation (still no core patch): two-mode `compress()`.

- `should_compress()` always False; `should_compress_preflight()` False;
  `prune_tool_results_only` = ABC default no-op (`agent/context_engine.py:110-119`).
- `compress(..., bypass_cooldown=False)` and `force=False` → return `messages` unchanged
  (identity), never a copy.
- `compress(..., bypass_cooldown=True)` (overflow) → engine-authored **new** list: one
  summary/handoff marker row + protected user messages + verbatim tail; let the existing commit
  machinery persist and re-anchor (`turn_preflight.py:348-361`, `turn_iteration_prep.py:519-523`).
  This is a *real* commit on a real content-loss boundary — the one case where the design must
  give up immutability. Treat it as CLM's "rollback + condense" step.
- `force=True` (manual `/compress`) → either the same compaction or identity; if identity, the
  UI says "no changes", which is honest but means `/compress` no longer does work. Decide and
  document.

## 5. `message_uid` availability — good, with three holes

Good path:

- Minted at first insert (`agent/message_metadata.py:164-174`, `hermes_state_messages.py:416`) and
  copied back onto the live dict (`agent/transcript_repair.py:403-413`).
- Turn prologue persists the inbound user row before the first API call
  (`agent/turn_context.py:1076-1090`, `:1288`).
- The assistant tool-call row is flushed **before** tools execute
  (`agent/turn_tool_round.py:110-143`); each tool result is flushed as it lands
  (`agent/tool_executor.py:201-224`, called at `:1164`, `:354`, `:1753`).
- So at every `select_context`, every row of the previous iteration — including the current
  user row — carries a uid. Current user row is also handed over directly as
  `incoming_message`, which is the robust key for it.

Holes:

1. **Persistence disabled or flush failure** → no uid ever minted on live dicts (stamps only
   happen on successful write, `agent/session_persistence.py:363-374`). Mitigation: ephemeral
   key = `(role, tool_call_id, sha1(wire content), ordinal)` in a per-turn namespace; ops on
   ephemeral keys expire at turn end and are never durable.
2. **Ephemeral scaffolding** is skipped by the flush (`session_persistence.py:325-330`) → no uid.
   Same fallback. Host control rows are the ones to care about (see §6).
3. **Merged rows**: consecutive-user merges and alternation repair keep the survivor's uid and
   record the absorbed ones in `_absorbed_message_uids`
   (`agent/message_metadata.py:229-251`; merges from `agent/agent_runtime_helpers.py:1206-1246`,
   `agent/turn_iteration_prep.py:219-236`). The store must resolve absorbed uid → survivor, else a
   pinned/kept row silently vanishes after a merge.

## 6. New tool outputs — reconciliation works; three traps

Works: engine tool mutates store, result row is appended after dispatch
(`tool_executor.py:1689-1695`; result appended later in the same call), next iteration's
`select_context` sees the new revision and the appended group. No ordering hazard.

Trap 1 — **host control notices ride inside the newest tool result.** Run-budget wrap-up
(`agent/conversation_loop.py:126-158`) and iteration-budget warning
(`agent/turn_iteration_prep.py:48-99`) are appended to `messages[-1]` when it is a not-yet-persisted
tool row, then flushed. If a projection rewrites or offloads that row, the "wrap up now" notice
disappears and the agent loses the budget signal it was given. Mitigation: protected live tail —
the current turn's newest tool result (and any row after the newest assistant tool-call group) is
never a rewrite/offload target.

Trap 2 — **self-effacing op.** Op target may be the group that carries the engine tool call
itself (assistant row + results). Dropping it erases the model's own evidence that the op ran.
Mitigation: ops are no-ops when the target group contains the current tool call; validate before
commit.

Trap 3 — **host mutates live rows between iterations.** Alternation repair merges user rows,
`_sanitize_tool_call_arguments` rewrites tool-call args in place
(`turn_iteration_prep.py:199-210`), `_neutralise_replay_echo_ghosts` replaces rows
(`turn_iteration_prep.py:113-141`), and host notices append text. Therefore stored *content*
must never be replayed for a "kept" row; ops are masks, content comes from the host copy. This
is the same rule as §2 but it is load-bearing here too — an op that pins stored bytes will
resurrect content the host intentionally repaired.

## 7. Host sanitizers after selection — what they do to a projection

Runs after the hook, unconditionally (`turn_request_assembly.py:153-198`):

- `sanitize_api_messages`: results that do not immediately follow their assistant call are
  **dropped**; declared calls without a result get the stub
  `"[Result unavailable — see context summary above]"` injected
  (`agent/agent_runtime_helpers.py:2911-2976`, entry `:3106-3117`). So a partial group is not an
  error — it is a silent rewrite. Consequence: a drop that keeps results (or keeps the call but
  drops results) diverges from store state without any signal.
- `drop_thinking_only_and_merge_users`: a projected-out assistant row between two user rows makes
  the host **merge** them into one user message, content `a + "\n\n" + b`
  (`agent/agent_runtime_helpers.py:1206-1246`). "Protected user messages" therefore survive as
  text but lose row boundaries on the wire. Document; never depend on user-row boundaries for
  provider-side structure.
- `repair_empty_non_final_messages` fills empty projected rows with placeholder text
  (`agent_runtime_helpers.py:3106-3111`) — an op that empties content gets rewritten.
- Thinking-only assistant rows cannot be kept at all (dropped last, `:1220-1224`).
- `evict_stale_outbound_tool_images` strips retained images regardless of store state
  (`turn_request_assembly.py:162-165`).
- Only after all of that: whitespace strip, tool-call canonicalization, cache plan
  (`turn_request_assembly.py:189-231`). Projection-injected text is whitespace-stripped; harmless
  but do not rely on leading/trailing whitespace.

Rule to write down: **group atomicity is the store's invariant, not the wire's.** Validate
call↔result coverage per group before an op commits; refuse an op that would leave a partial
group. Do not let the sanitizer be the enforcer, because its enforcement is invisible.

## 8. Persistence / resume

- Resumed rows carry uids (`tests/agent/test_context_engine_message_uid.py:88-112`), so ops
  re-apply after resume/restart. Unknown uid → pass through. Crash-safety comes free: raw history
  is flushed progressively, so a store that is one revision behind still projects coherently.
- **Write-through store required.** A gateway-cached agent can be evicted without `on_session_end`
  (`run_agent.py:182-189` is the only end hook), and a gateway `/compress` cold engine never gets
  `on_session_start` at all (`gateway/session_mutation_compress.py:165-197`). Initialize lazily from
  the first hook (or `bind_session_state`) keyed by `session_id`, and commit per op.
- Compression boundary: a committed compaction starts a **new** session id with
  `boundary_reason="compression"` and `old_session_id`
  (`agent/conversation_compression.py:2469-2493`); in-place compaction passes the same id twice.
  That call carries only `boundary_reason`, `old_session_id`, `platform`, `conversation_id` — **no
  `hermes_home`, `model`, or `context_length`** — so a store that derives its path from
  `hermes_home` must remember it from the init call, not re-derive it here. Callback failure is
  swallowed at debug level (`:2489-2493`).
  Store must carry over by `old_session_id` or all ops orphan against a rewritten history
  (survivors keep uids, so ops on kept rows still resolve — ops on summarized rows become inert).
- `/branch`, `/rewind`, `/undo`, `/retry` can drop or rewrite host rows while the store keeps
  them. A `restore` op that re-injects stored text would resurrect content the user removed.
  Mitigation: restore/offload resolve only against uids still present in the current host history;
  anything else is inert. Never re-materialize a row the host no longer has.
- Subagents: own agent, own engine instance, own session id (`agent_init.py:1822+` selection path;
  report §7). No parent-store sharing — a delegated child sees raw history, not the projection.

## 9. Retries / failover / cache stability

- Retries reuse the assembled request; `select_context` is not re-run (`conversation_loop.py:1571-1600`,
  `turn_api_request.py:177-217`). Good: one projection per iteration.
- Failover and overflow restarts **do** go back to `assemble_api_request`
  (`turn_iteration_prep.py:519-546`) → projection re-runs. Therefore the projection must be a
  **pure function of (store revision, host history)**. Any dependence on `budget_tokens`,
  token estimates, or "fill until X%" makes the prefix flip when `context_length` changes
  mid-turn (`update_model` on failover, provider-adopted limit at `turn_overflow.py:315-340`).
- Cache: plan is derived post-selection, so unchanged revision = byte-stable prefix. A revision
  bump from an engine tool breaks reuse from the edit point for the rest of the turn — that is the
  design's accepted cost; system prompt must teach "batch edits, edit late".
- `budget_tokens` delivered to the engine is `engine.context_length`, not
  `threshold_tokens` (`conversation_loop.py:1375`). Compute your own budget.
- Token accounting: engine must set `last_prompt_tokens` from real usage in
  `update_from_response` (`agent/turn_usage.py:118-145`); sentinel rules at
  `agent/context_engine.py:187-202` (-1 "await real usage", 0 "no usage yet"). Never -1 unless a
  real compaction commit happened.

## 10. Tool plumbing gates that can silently disable the whole design

- Engine tools are injected only when the `context_engine` toolset is enabled
  (`agent/agent_init.py:2016-2034`). Auto-added for a non-default engine
  (`hermes_cli/tools_config.py:625-626`) **except** when an explicit empty platform toolset list
  is saved, and `agent.disabled_toolsets` wins last. Then the model has no verb to commit ops —
  the projection silently becomes pure-identity.
- `handle_tool_call` receives no signature filtering (unlike memory providers): it must accept
  `**kwargs` (`agent/tool_executor.py:1689-1695`); exceptions become JSON error strings; return
  must be a JSON string.
- Sequential tool calls run under the concurrent-executor deadline
  (`tool_executor.py:840-906`). A slow aux/proposal call inside a tool handler can trip that
  deadline and yield a synthesized timeout result while the store op is half-applied. Make each
  tool handler transactional: validate → aux → commit → return; nothing durable before
  validation.

## 11. `clone_for_agent` and host isolation

- Plugin-system registration holds **one shared instance**; each agent gets `deepcopy`
  (`agent/context_engine.py:226-231`, `agent/agent_init.py:1824-1852`). An engine holding an
  open SQLite connection or lock will raise and the agent **silently falls back to the built-in
  compressor** (`agent_init.py:1836-1846`) — i.e. raw history gets compacted by the host and the
  whole design is off. Override `clone_for_agent()`: share the durable backend by handle/reopen
  by path, copy only budget/state scalars. Also override `__deepcopy__` or ensure the store object
  is copyable-by-design.
- Directory-discovered engine (`context.engine: <name>`) loads a fresh module instance per agent
  (report §7), so no clone is involved; the same store-path rule still applies.
- `plugins.isolation` defaults to `in_process` (`hermes_cli/config_defaults.py:1714-1719`). Under
  `host`, the engine is an RPC proxy: values cross as JSON, live handles become `Opaque`
  (`hermes_cli/plugin_host_wire.py:1-16`). Consequences: every `select_context` ships the full
  transcript across the boundary twice, and `bind_session_state(session_db=...)` fails — silently,
  because agent init suppresses it (`agent_init.py:1915-1918`). So: never depend on
  `bind_session_state`; derive the store path from `hermes_home` in `on_session_start`; document
  `in_process` as the supported mode, or accept the RPC cost knowingly.
- `on_session_start` failures are swallowed at debug level (`agent_init.py:2054-2065`) — a store
  that fails to open looks like "no projection", not an error. Log loudly to a file the operator
  actually reads, and expose state via a registered command (`plugins/context_engine/__init__.py:101-126`).

## 12. Aux call — signature and task reuse verified

- Signature is real and sync/blocking:
  `call_llm(task=None, *, provider, model, base_url, api_key, main_runtime, messages, temperature,
  max_tokens, tools, timeout, extra_body, reasoning_config, extra_headers, api_mode, stream,
  stream_options, route_info, latency_info)` — `agent/auxiliary_client.py:7905-7913`.
- `task="compression"` is exactly what the built-in summarizer uses
  (`agent/context_compressor.py:3793`), and the route pin helper confirms the task name
  (`agent/context_compressor_summary.py:47-63`).
- Reusing it inherits, by design:
  - `auxiliary.compression.*` route and fallback chain (`agent/auxiliary_client.py:6172`, `:3966`);
  - **300 s timeout floor** (`_DEFAULT_AUX_TIMEOUT = 30.0`, `:6132`; floor
    `_COMPRESSION_TIMEOUT_FLOOR_SECONDS = 300.0`, `:6142`; applied `:6246-6252`);
  - **no same-provider retry on timeout** (`_TIMEOUT_NO_RETRY_TASKS` includes `"compression"`,
    `:3450`);
  - compression-specific max-token / minimum-context handling (`:6197`, `:4257`);
  - per-task concurrency semaphore (`:6345-6351`).
- Verdict: reuse is faithful and correct for a summarizer/proposer. Two required adjustments:
  1. pass an explicit `timeout=` (explicit wins, `:6246-6250`) so a proposal call cannot block a
     tool round for 300 s — e.g. 30-60 s, well under the sequential tool deadline;
  2. handle `None`/exception as a first-class outcome (no retry will happen on timeout) and
     return a JSON error string; never let a failed proposal mutate the store.
- Do **not** call aux from `select_context`: it runs on the conversation thread every iteration
  before the request is built; blocking there is per-request latency, and the fail-open wrapper
  would log a warning per iteration (`conversation_loop.py:1377-1382`).
- In-process engines can import `call_llm` directly; under `plugins.isolation: host` the call runs
  in the plugin-host process with the host's resolved config — verify the aux route resolves
  there (it reads the host config via `_get_auxiliary_task_config`) before relying on it.

## 13. Design rules to write into the spec (each closes a found gap)

1. Projection is a **mask over `conversation_messages`, applied by subsequence alignment to
   `request_messages`**; unmatched request rows pass through; ambiguity → no-op + error string.
2. **Never rebuild a row.** Kept rows are host dicts. Rewrite replaces `content` (and for
   tool rows, the id stays).
3. Protected set is enforced twice: by the store (op validation) and by the projector (never map
   an op onto system/user/prefill/current-user rows).
4. **Group atomicity** validated before commit: an assistant `tool_calls` row + every result row
   (including stubs) either all kept or all dropped; partial → refuse.
5. **Protected live tail**: the current turn's newest tool result is never rewritten/offloaded
   (host budget notices ride there).
6. **Ops carry no content for kept rows**; content is always read from the host copy. Only
   offload stubs and restored text carry engine-authored bytes, and restore is inert when the uid
   is gone from host history.
7. **Projection is pure**: inputs are `(store revision, host history uids/content)`. No token
   counts, no budget thresholds, no time. Guarantees prefix stability per revision.
8. **`compress()` modes**: identity except `bypass_cooldown=True` (overflow) and optionally
   `force=True` (manual). Always a new list when committing; never same-object mutation.
9. **`should_compress()` False, `prune_tool_results_only` default, `should_compress_preflight`
   False** — the host must never compact raw history on its own.
10. **Write-through store**, `clone_for_agent` override sharing the backend, lazy init keyed by
    `session_id` (never rely on `bind_session_state`), carry-over on `old_session_id` boundaries.
11. **Capability probe at init**: if the `context_engine` toolset is absent, log a loud warning and
    surface it in a status command — otherwise the design is a silent no-op.
12. Aux proposals: `task="compression"`, explicit short `timeout=`, transactional commit after
    validation, `None` handled.

## 14. Residual risks (accepted, name them for the owner)

- Alignment is heuristic for the canonicalized prefix (dropped/synthesized rows). Correctness
  degrades to "op inert", never to a malformed request — but an op can silently not apply.
- Mid-turn revision bumps break prompt-cache reuse from the edit point; cost is real on long
  turns with many small edits.
- Dropping an assistant row adjacent to user rows causes host-side merge of protected user text
  (visible to the model as concatenation).
- Offload stubs are engine-authored text in the transcript; `sanitize_context` / whitespace passes
  apply, and a stub that reads like an instruction is exactly CLM's prompt-injection surface
  (the upstream feature request's safety note). Stub text must be schema-like, never imperative.
- `/compress` semantics change: it becomes "compaction on demand" or "no changes". Pick one.
- Under `plugins.isolation: host`, per-iteration transcript RPC is a measurable tax and
  `bind_session_state` is dead; treat `in_process` as required unless measured otherwise.

## 15. Evidence index (paths relative to `the Hermes checkout`)

| Claim | Evidence |
|---|---|
| select_context call site, args, clones | `agent/conversation_loop.py:1351-1398`; `agent/turn_request_assembly.py:142-147` |
| returned list accepted verbatim; no re-add of system/injection | `agent/conversation_loop.py:1384-1398`; `agent/turn_request_assembly.py:149-231` |
| system row prepended last | `agent/turn_context.py:1424-1431` |
| prefill inserted after system | `agent/turn_request_assembly.py:132-138` |
| MoA context before hook | `agent/turn_request_assembly.py:129-130` |
| memory prefetch into `api_content`, wire replay | `agent/turn_context.py:997-1013`, `:1264-1286`, `:1369-1388` |
| uid stripped from wire | `agent/message_metadata.py:46-54`; `agent/turn_context.py:1363-1365` |
| uid minted at insert, copied back | `agent/message_metadata.py:164-174`; `agent/transcript_repair.py:403-413` |
| prologue persists user row | `agent/turn_context.py:1076-1090`, `:1288` |
| tool-round persist-before-execute + per-result flush | `agent/turn_tool_round.py:130-143`; `agent/tool_executor.py:201-224`, `:1164` |
| engine tool dispatch (`**kwargs`, JSON string, error wrap) | `agent/tool_executor.py:1689-1695` |
| sequential-only barrier for unknown tools | `agent/tool_dispatch_helpers.py:182-258` |
| sequential-call deadline | `agent/tool_executor.py:840-906` |
| canonicalize can drop/add/rewrite prefix rows | `agent/replay_cleanup.py:120-145`, `:154-171`, `:189-193` |
| sanitizer drops positional orphans, stubs unanswered calls | `agent/agent_runtime_helpers.py:2911-2976`, `:3106-3117` |
| user merge on adjacent users | `agent/agent_runtime_helpers.py:1206-1246` |
| alternation repair merges user rows mid-turn | `agent/turn_iteration_prep.py:219-236` |
| absorbed-uid witness | `agent/message_metadata.py:229-251` |
| host budget notices appended into newest tool result | `agent/conversation_loop.py:126-158`; `agent/turn_iteration_prep.py:48-99` |
| steering inserts a mid-list user row | `agent/turn_iteration_prep.py:175-181`, `:284-295` |
| overflow compress → fail + compression_exhausted=True | `agent/turn_overflow.py:87-90`, `:140-178`, `:431-451`; `agent/conversation_loop.py:1795-1799` |
| 413 no-shrink → fail | `agent/turn_overflow.py:219-275` |
| engine kwargs filter (`bypass_cooldown` only when True) | `agent/conversation_compression.py:1691-1711` |
| manual compress feedback on identity | `agent/manual_compression_feedback.py:29-71` |
| gateway /compress cold engine, `compressed == head` → none | `gateway/session_mutation_compress.py:119-135`, `:165-197` |
| compression boundary new session id (no `hermes_home`) | `agent/conversation_compression.py:2469-2493` |
| retries skip select_context | `agent/conversation_loop.py:1571-1600`; `agent/turn_api_request.py:177-217` |
| restarts re-assemble | `agent/turn_iteration_prep.py:519-546` |
| budget_tokens = context_length | `agent/conversation_loop.py:1375` |
| usage anchor vs raw list | `agent/usage_anchor.py:108-122` |
| usage dict → engine | `agent/turn_usage.py:118-145` |
| sentinel -1 / 0 contract | `agent/context_engine.py:187-202` |
| clone_for_agent deepcopy + fallback warning | `agent/context_engine.py:226-231`; `agent/agent_init.py:1824-1852` |
| bind_session_state suppressed; on_session_start swallowed | `agent/agent_init.py:1915-1918`, `:2054-2065` |
| toolset gating for engine tools | `agent/agent_init.py:2016-2034`; `hermes_cli/tools_config.py:625-626` |
| isolation default in_process; host wire = JSON, Opaque handles | `hermes_cli/config_defaults.py:1714-1719`; `hermes_cli/plugin_host_wire.py:1-16`; `hermes_cli/plugin_host.py:252-298` |
| aux call signature | `agent/auxiliary_client.py:7905-7913` |
| compression task route/limits | `agent/auxiliary_client.py:3450`, `:3966`, `:6132`, `:6142`, `:6172`, `:6197`, `:6246-6252`, `:6345-6351`; `agent/context_compressor.py:3793`; `agent/context_compressor_summary.py:47-63` |
| micro-compaction is compressor-only | `agent/micro_compaction.py:39-41`, `:44-63` |
| select_context request-only + uid-strip contract tests | `tests/agent/test_context_engine_select_context.py:137-203`; `tests/agent/test_context_engine_message_uid.py:1-8`, `:88-112`, `:389-419` |
