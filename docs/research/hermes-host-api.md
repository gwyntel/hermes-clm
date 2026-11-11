<!-- Source: internal research note, 2026-11-10. Sanitised for publication: local paths, host and profile details removed. Original analysis retained. -->

# Hermes context-engine: actual API + runtime (research report)

Author: internal research note.

## 0. Provenance — main vs installed runtime

| Thing | Value |
|---|---|
| Reference checkout | `the Hermes checkout`, tag `v0.23.0`, SHA `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, 2026-11-05 |
| `origin/main` at research time | `2fd5066fdd9e143ee90d7d549527ea1e1e07cf5d`, 2026-11-10 19:57 -0800 (via `git ls-remote`) |
| Fresh read-only clone used for main-branch line numbers | a shallow clone of upstream `main`, separate from the installed release |
| Docs read | `website/docs/developer-guide/context-engine-plugin.md` (377 lines), `context-compression-and-caching.md`, `plugin-llm-access.md`, `memory-provider-plugin.md`, `plugins/index.md`, `architecture.md`, `agent-loop.md` |
| Issue read | An open upstream feature request for CLM-style model-editable context (context-as-file) as an opt-in compression mode, referencing arXiv 2609.37725 |

**Critical provenance facts.** On the files that define this API, main and the installed runtime are *identical*:

- `agent/context_engine.py` — `diff` against main: **0 lines**.
- `website/docs/developer-guide/context-engine-plugin.md` — **0 lines**.
- `plugins/context_engine/__init__.py` — **0 lines**.
- `agent/turn_context_compaction.py` — **0 lines**.

Files that differ (small, non-semantic for the hooks, line numbers shift by <10):

- `agent/conversation_loop.py` (+28 diff lines; only at `_content_policy_blocked_result` 1138 and `_close_durable_failed_turn` 1919; hook region identical)
- `agent/turn_request_assembly.py` (+16; empty-response retry hint, reasoning-token drop)
- `agent/agent_init.py` (+81; memory-provider kwargs move, codex checkpoint refusal removal)
- `agent/turn_preflight.py` (+69), `agent/conversation_compression.py` (+51), `agent/context_compressor.py` (+64), `agent/turn_context.py` (+120), `agent/turn_usage.py` (+13), `agent/turn_finalizer.py` (+3), `agent/tool_executor.py` (+4)

So: **everything below is true of both v0.23.0 (installed) and main `2fd5066`**, except where an explicit "(installed: line N)" annotation is given. Cite main line numbers by default.

No trace of the CLM feature in main: `grep -rni "model_managed|context_as_file|clm_harness|context-language-model"` → no hits; `compression.mode` does not exist in `hermes_cli/config_defaults.py`.

---

## 1. The upstream feature request — what is actually being asked

OPEN feature request, not a spec that exists in code. Core asks (verbatim substance):

- Opt-in `compression.mode: harness | model_managed`; default `harness` = today's behaviour.
- **Mirror**: "before each model call, the post-prompt live context is written to a file whose path is given in the system prompt. The agent edits it with existing file/terminal tools; edits parse back into the message list."
- **Nudges** at 25/50/75% of budget (`compression.nudges`), because models are bad at estimating their own context length.
- **Overflow**: roll back newest turns, tell the model to condense, retry up to `rollback_retries` (default 6).
- **Free edit turns**: an edit-only turn counts as a non-step (`agent.max_turns` interaction TBD).
- **Steering**: per-session one-liners.
- Pinned + non-editable: system prompt, task, memory injection.
- `edit_gate: fit|shrink`; safety note about model-authored text carrying instructions.

That maps onto host needs: (1) a per-model-call **write** of the post-prompt request, (2) a **parse-back into the live, persisted transcript, mid-loop**, (3) token pressure visible to the model, (4) rollback. Sections 5–7 answer which of those exist.

---

## 2. The `ContextEngine` ABC — exact surface

File: `agent/context_engine.py` (250 lines, identical in both trees).

### Required (abstract)

| Member | Line | Signature |
|---|---|---|
| `name` (property) | 51-54 | `-> str`; must equal `context.engine` value |
| `update_from_response` | 75-82 | `(usage: dict) -> None` |
| `should_compress` | 84-86 | `(prompt_tokens: int \| None = None) -> bool` |
| `compress` | 97-108 | `(messages, current_tokens=None, focus_topic=None, force=False, memory_context="") -> list` |

Note the two params the doc page's example signature (doc lines 61-70) omits: `force` and `memory_context`. The host filters by signature (`agent/conversation_compression.py:1694-1714` `_supported_compression_kwargs`), so an older 3-arg `compress` still works; it just never sees focus/force/memory context.

### Class attributes the host reads directly

`context_engine.py:56-73`: `last_prompt_tokens`, `last_completion_tokens`, `last_total_tokens`, `threshold_tokens`, `context_length`, `compression_count`, plus `threshold_percent` (0.75), `protect_first_n` (3), `protect_last_n` (6), `emit_automatic_compaction_status` (True).

`run_agent.py` and status surfaces read these fields directly (ABC docstring line 56: "run_agent.py reads them directly"). `get_status()` (`:213-224`) is essentially only used by tests (`tests/agent/test_context_engine.py:76`) — status UI reads the fields, not the dict. Design accordingly: keep the fields truthful.

### Optional, with working defaults

| Member | Line | Host call site |
|---|---|---|
| `should_compress_info` → `(bool, reason)` | 88-95 | `agent/turn_context_compaction.py:77` |
| `prune_tool_results_only(messages, current_tokens)` → `(msgs, n)` | 110-119 | `agent/turn_preflight.py:411-445` `_proactive_tool_result_prune` |
| `select_context(...)` | 121-141 | `agent/turn_request_assembly.py:147` |
| `on_turn_complete(...)` | 143-154 | `agent/turn_finalizer.py:760-775` |
| `should_compress_preflight(messages)` | 156-158 | `agent/turn_context_compaction.py:436-478` (one forced `compress()` pass, `trigger="engine_preflight"`) |
| `should_defer_preflight_to_real_usage(rough)` | 160-163 | `agent/turn_preflight_gate.py:95-98` |
| `get_automatic_compaction_status_message` | 165-174 | via `automatic_compaction_status_message()` helper (`:33-45`), phases `pre_api`, `preflight`, `compress`, `idle` |
| `has_content_to_compress` | 176-179 | `agent/conversation_compression_manual.py:107` (gateway `/compress`) |
| `on_session_start(session_id, **kwargs)` | 181-182 | `agent_init.py:2056-2065`, `run_agent.py:357-390`, `conversation_compression.py:2437-2465` |
| `on_session_end(session_id, messages)` | 184-185 | `run_agent.py:182-189` (called at 886/893) |
| `on_session_reset()` | 187-202 | `run_agent.py:357-390` transition |
| `get_tool_schemas()` | 204-206 | `agent_init.py:1967-2015` |
| `handle_tool_call(name, args, **kwargs)` | 208-211 | `agent/tool_executor.py:1690-1697` |
| `get_status()` | 213-224 | status/debug only |
| `clone_for_agent()` | 226-231 | `agent_init.py:1790-1802` — **plugin-system registrations only** |
| `update_model(model, context_length, base_url, api_key, provider, api_mode)` | 233-250 | `agent_init.py:1843-1852`, model switch/fallback |

### Undeclared, duck-typed extras the host also probes

Not in the ABC, not in the docs, but read with `getattr` — these are part of the *real* contract:

- `bind_session_state(session_db=..., session_id=...)` — `agent_init.py:1864-1867`, `conversation_compression.py:1633`, `background_review.py:893`, `gateway/run_turn_hygiene.py:714`.
- `carry_over_new_session_context(old_session_id, new_session_id)` — string-dispatched by `run_agent.py:157-164` `_call_engine_hook` from `_transition_context_engine_session` (`:357-390`).
- `note_usage_less_response()` — `agent/turn_usage.py:100-104`, `codex_runtime.py:165`.
- `awaiting_real_usage_after_compression` (attribute) — `turn_usage.py:98`, `turn_preflight.py:274`, `turn_context_compaction.py:163`. If set truthy, the host suppresses preflight compression until a real prompt count arrives.
- `get_active_compression_failure_cooldown()` — `turn_preflight.py:94-96`, `conversation_compression_codex.py:23`.
- `summary_target_ratio` — `conversation_compression.py:1924` (idle-compaction floor is `threshold_tokens * summary_target_ratio`, `turn_context_compaction.py:171`).
- `last_compression_rough_tokens` — `turn_context_compaction.py:178`.
- `emit_automatic_compaction_status` / `_last_compress_refused_would_grow` (`gateway/session_mutation_compress.py:126-131`).

Missing any of these degrades gracefully, but two matter operationally: without `bind_session_state` a gateway-cached agent keeps compressor state bound to a stale session; without the `awaiting_real_usage_after_compression` latch, preflight can re-compact immediately after a compaction.

### Doc-vs-implementation deltas worth knowing

1. Docs (`:135`, `:100`) say `select_context` is "evaluated per provider request (re-runs on retries)". **It does not re-run on retries.** The retry loop (`agent/conversation_loop.py:1577-1606`) calls `build_api_request` (`agent/turn_api_request.py`), which only re-decorates prompt cache (`_redecorate_prompt_cache_for_provider`, turn_request_assembly.py:1290-1339). `assemble_api_request` — and therefore `select_context` — runs once per loop iteration (`conversation_loop.py:1751-1757`).
2. Docs' `on_turn_complete` coverage caveat is accurate and conservative: it fires from `finalize_turn` only (`turn_finalizer.py:760-775`); content-policy blocks and terminal provider failures return earlier.
3. Docs don't say **`compression.enabled: false` disables plugin engines too**. It does: `turn_preflight.py:86`, `:298-305`, `conversation_compression.py:2032` all gate on `agent.compression_enabled`, which is `compression.enabled` (`agent_init.py:1868`). An external engine that wants to run must have `compression.enabled: true`; the host then leaves threshold *values* to the engine (`agent_init.py:1824-1830` deliberately drops the host threshold notice) but keeps the on/off switch.
4. The ABC's `handle_tool_call` docstring says "kwargs may include `messages` (live in-memory list)". True — and consequential (§6).
5. `select_context` returns `list | None`; the host also rejects `[]` and non-dict elements explicitly (`conversation_loop.py:1390-1404`). Non-`None` returns are stripped of persistence-only fields (`:1396-1399`).

---

## 3. Where each hook actually runs — turn-loop map

`run_conversation` → `_run_conversation_turn` (`conversation_loop.py:1646-1805`):

```
build_turn_context            # prologue: restore/build system prompt, turn-start compaction
  ├─ run_turn_start_compaction        (turn_context_compaction.py:129)
  │    ├─ _idle_compaction            (:144)  → engine.compress(trigger="idle")
  │    └─ _preflight_compression      (:230)  → engine.should_compress / compress(trigger="preflight")
  │         └─ _engine_preflight_maintenance (:436) → should_compress_preflight() → one forced compress()
play_prelude                          # optional scripted tool calls
while api_call_count < max_iterations and budget:
    begin_iteration
    prepare_iteration
    assemble_api_request              # ← select_context() runs HERE (per iteration)
        build_api_messages(messages)  #     live transcript → wire copy
        + MoA context, + prefill
        → _apply_context_engine_selection(...)         (turn_request_assembly.py:147)
        + sanitizers, thinking-only drop, whitespace, surrogate scrub
        + prompt-cache plan (AFTER selection: doc claim holds)
        + pressure estimate (anchored or rough)
    run_preflight_gate                # may compress → verdict "continue" → loop re-assembles (select_context runs again on compacted list)
    announce_api_call
    _run_api_retry_loop               # retries: build_api_request only — NO select_context
        perform_api_call
        check_api_response
        record_response_usage         # → engine.update_from_response(usage_dict)  (turn_usage.py:144)
    run_tool_round (if tool_calls)    # engine tool dispatch + persist-before-execute
        compress_after_tool_results   # → engine.should_compress() → compress(trigger="post_tool")
    else finish_text_response
finalize_turn                         # persist, then on_turn_complete()  (turn_finalizer.py:760)
```

Consequences:

- **`select_context` runs once per provider-bound iteration**, plus once more after any preflight compaction re-run. On a 5-tool-call turn: ~5–6 evaluations.
- It runs **before** prompt-cache decoration, all request sanitizers, and the pressure estimate — so a replacement is re-validated, and a no-op is byte-identical.
- It receives **structural clones** of `conversation_messages` and `incoming_message` (`conversation_loop.py:1367-1382`); nested-container writes cannot reach persisted history.
- `update_from_response` runs once per completed provider response, and with `{}` when a response carries no usage while a compaction verdict is pending (`turn_usage.py:96-104`).
- `on_turn_complete` runs **after** `_persist_session` inside `finalize_turn`, receives structural clones and the last response's canonical usage dict (`agent._last_turn_usage`, `turn_usage.py:175`), plus `turn_id`, `task_id`, `api_call_count`, `interrupted`, `failed`, `turn_exit_reason`.

---

## 4. Tool registration

Three layers, all real:

1. **Schema injection at agent init** — `agent/agent_init.py:1967-2015` `_inject_context_engine_tools`: calls `engine.get_tool_schemas()`, normalizes (`agent/memory_manager_tools.normalize_tool_schema`), dedupes against existing tool names, appends `{"type":"function","function":schema}` to `agent.tools`, populates `agent.valid_tool_names` and `agent._context_engine_tool_names`.
   Gated on the `context_engine` toolset (`agent_init.py:1977-1980`): `enabled_toolsets is None or "context_engine" in enabled_toolsets`. `toolsets.py:126` declares the toolset **empty on purpose**; `hermes_cli/tools_config.py:531,625-626` auto-adds it when an engine is active.
2. **Re-injection for MCP/agent tool rebuilds** — `tools/mcp_tool_agent.py:446-455`, same gating, claims names into `staged_engine_names` so routing survives a rebuild.
3. **Dispatch** — `agent/tool_executor.py:1690-1697`: if `function_name in agent._context_engine_tool_names`:
   ```python
   agent.context_compressor.handle_tool_call(function_name, next_args, messages=messages)
   ```
   `messages` is **the live loop list**, not a clone. Result must be a JSON string; exceptions become `{"error": "Context engine tool '...' failed: ..."}`. No signature filtering here (unlike memory providers, `agent/memory_manager.py:269-272`) — an engine whose `handle_tool_call` lacks `**kwargs` raises `TypeError` on every call.
   Precedence in `_resolve_sequential_dispatch`: inline agent tools → `delegate_task` → context-engine tools → memory-provider tools → registry.

---

## 5. Mutable message flow across the multi-tool-call loop

Facts, in order:

1. **One live list per turn.** `messages` (the loop local, `_LoopState.messages`) is the transcript. `run_tool_round` appends assistant rows and tool results **in place** (`append_message(messages, ...)`, `turn_tool_round.py:113-130`, `:210-226`), persists the assistant row *before* executing tools, then calls `compress_after_tool_results`, whose verdict rebinds `messages` only if the engine returned a **new list object** (`turn_preflight.py:310-320`, `:411-445`).
2. **The wire copy is rebuilt from that list every iteration.** `build_api_messages(agent, messages, ...)` (`agent/turn_context.py:1422-1500`; installed `:1319`) walks `messages` live. Rows before `current_turn_user_idx` go through `canonicalize_replay_history` (`agent/replay_cleanup.py:155-172` — strips interrupted tool tails, dangling tool-call tails, expired dangerous confirmations; it never re-reads the DB).
3. **Trap: `api_content` sidecar wins over edited content.** For historical `user`/`assistant` rows, if the dict carries `api_content`, the wire copy's `content` is **replaced by that sidecar** (`turn_context.py:1487-1494`), because a historical row must replay the exact bytes previously sent (prompt-cache stability). `api_content` is persisted to the `messages` table (`agent/session_persistence.py:227,251`) and restored with history. Net effect: **editing the `content` of an already-sent user/assistant row mid-loop is a silent no-op on the wire** unless you also pop/restamp `api_content`. Tool rows and this-turn rows have no sidecar, so they do reflect edits.
4. **Persistence is by per-dict dedup marker, not by diff.** `_flush_messages_to_session_db_unlocked` (`session_persistence.py:548-560`) dedups with `_DB_PERSISTED_MARKER` on each dict ("dedup is the intrinsic `_DB_PERSISTED_MARKER` on each written dict"). A dict already marked persisted is **not rewritten**. So an in-place edit to an existing row is in-memory-only and is lost on restart/resume; only newly appended rows flush.
5. **Structural repair could fight you.** `_sanitize_api_messages` (`turn_request_assembly.py:159`) and the thinking-only drop / adjacent-user merge (`:186-189`) run after the selection hook on every request; orphaned tool results / unanswered tool calls get swept wire-side. A hand-edited transcript that breaks tool-call pairing degrades to a repaired request, not an error.
6. **`message_uid`** is the stable identity key across all of this (`doc:138-218`; `PERSISTENCE_ONLY_MESSAGE_FIELDS` at `agent/message_metadata.py:46-54` strips it from the wire).

### Answer to the central question

> Do live mid-loop edits affect the next model request?

- **Wire, same process, same turn: yes** — the next iteration's `assemble_api_request` rebuilds from the live `messages` list, so any dict-level mutation is visible… **except** historical `user`/`assistant` `content`, which `api_content` overrides (§5.3).
- **Durable transcript: no** — already-persisted rows aren't rewritten (§5.4); edits die with the process, never reach `state.db`, never survive resume/`/branch`/gateway restart.
- **Documented contract: no** — `select_context` is explicitly request-only ("the persisted conversation history is never mutated", ABC `:130-133`, doc `:134`); `on_turn_complete` is post-turn and read-only (`:143-154`); `handle_tool_call`'s `messages` kwarg is documented only as "live in-memory list", with no commit/persist semantics.
- **Therefore**: today a CLM-style engine can *read* the live list, *replace* the request per API call, and *observe* finished turns — but it cannot **commit** an edited transcript mid-loop. There is no supported verb for that. It is not "an undocumented hook you could lean on"; two host mechanisms (`api_content` replay, dedup marker) actively defeat naive implementations.

---

## 6. When context selection runs (precise)

| Trigger | Runs | Source |
|---|---|---|
| Each provider-bound loop iteration | `select_context` once | `conversation_loop.py:1751-1757` + `turn_request_assembly.py:147` |
| After a preflight compaction (`verdict "continue"`) | again, on the compacted list | `turn_preflight.py:182` |
| Retries / fallback within one iteration | **not run** | `conversation_loop.py:1577-1606` (only `build_api_request`) |
| Side-question / `ctx.side_question` requests | runs, separate assembly | `agent/side_question_request.py:134-160` |
| Turn-start preflight / idle compaction | not applicable (compress path) | `turn_context_compaction.py:129-352` |
| Manual `/compress` | not applicable | §13 |

Cost note: `_engine_overrides_hook` (`conversation_loop.py:1342-1354`) identity-checks the bound method against the ABC default, so non-implementing engines (including the built-in compressor) pay **zero** per-request work.

---

## 7. Session and subagent identity

- Engine instances are per-`AIAgent`. Directory-discovered engines: a fresh instance per load (`plugins/plugin_loader.py:240-256` `instance_from_module` calls `register(collector)` / `attr()` on each load, even though the module body is cached). Plugin-system registrations (`ctx.register_context_engine`) hold **one shared instance** and are cloned via `clone_for_agent()` at every agent init (`agent_init.py:1790-1802`), falling back to the built-in compressor with a warning if the clone raises.
- `on_session_start` kwargs (`agent_init.py:2056-2065`): `hermes_home`, `platform`, `model`, `context_length`, `conversation_id` (= `agent._gateway_session_key`, stable per chat/route).
- Session transitions (`run_agent.py:357-390`): `on_session_end(old, previous_messages)` → `on_session_reset()` → `on_session_start(new, old_session_id=..., carry_over_context=..., platform, model, context_length, conversation_id)` → optional `carry_over_new_session_context(old, new)`.
- Compression commits announce a boundary, not a new conversation: `on_session_start(new_session_id, boundary_reason="compression", old_session_id=..., platform, conversation_id)` (`conversation_compression.py:2437-2465`; gateway path `gateway/session_mutation_compress.py:110-114`). In-place compaction passes the same id twice. `on_session_end` is called for a session that had no live agent and got a throwaway engine.
- Subagents: `tools/delegate_tool.py:299` builds a full `AIAgent(platform="subagent", session_id=<reserved>, parent_session_id=<parent>)`; it runs the same `_build_context_engine` (`agent_init.py:1822`), so the child gets its **own** engine instance / its own `on_session_start(child_sid, platform="subagent")`. `session_db` is a child handle. Memory providers are forced into `agent_context="subagent"` (no `sync_turn`, `prefetch`, `on_turn_start`, `on_session_end`; `memory-provider-plugin.md:329-335`).
- Plugin isolation: `plugins.isolation` defaults to `in_process` (`hermes_cli/config_defaults.py:1714`). Under `host`, a **user** engine is loaded out-of-process and the agent holds an RPC proxy (`hermes_cli/plugin_host.py:269-290`, `hermes_cli/plugin_isolation.py:160-169`), so every hook call is cross-process and every message list round-trips as data. That matters for an engine that keeps a DB/DAG and for any per-request hook cost.

---

## 8. Auxiliary model infrastructure

What exists (host-side), and what an engine gets:

- The built-in compressor summarizes with a cheap aux model: `agent/context_compressor.py:20-32` imports `call_llm` from `agent/auxiliary_client.py`; the call is at `context_compressor.py:3819`; the route is pinned to task `"compression"` at `agent/context_compressor_summary.py:31-54` (`call_kwargs["task"] = authoritative_auxiliary_task(... or "compression")`), i.e. config `auxiliary.compression.*` (provider/model/base_url/timeout/fallback_chain).
- Feasibility/route resolution: `ensure_compression_feasibility_checked` (`turn_preflight.py:93`), aux failure cooldown (`get_active_compression_failure_cooldown`), main-model one-shot fallback (`context_compressor.py:3764-3790`), `_last_aux_resolved_model`.
- Compression runs, by default, on a pooled daemon thread with a host timeout (`compression.context_timeout_seconds`; doc `:356-371`), so `compress()` and the boundary callbacks must assume arbitrary-thread execution and a "work after timeout is discarded" rule.
- **Engines get no aux-model plumbing from the host.** A directory-discovered engine receives `_EngineCollector`, a `NoopPluginContext` subclass that overrides only `register_context_engine` and `register_command` (`plugins/context_engine/__init__.py:90-126`). The plugin LLM lane (`ctx.llm` / `complete()` / `complete_structured()` / `side_question`, `hermes_cli/plugins_context.py:184`, `doc:plugin-llm-access.md:209-330`) is **not** wired into that collector, so `ctx.llm` is an `AttributeError` inside a context engine. In-process engines can import `agent.auxiliary_client.call_llm` themselves (bundled engines do); under `plugins.isolation: host` they'd be calling it inside the host process with the host process's resolved secrets.
- No host-side "summarize this text" helper is exposed on the engine interface.

---

## 9. Memory-provider interaction

- Coexistence is by design: one memory provider (single-select, `memory.provider`) + one context engine, different axes. Doc `:228-235` is explicit: if a plugin only needs post-turn ingestion, implement a memory provider (`sync_turn()`), **not** a context engine.
- `on_pre_compress` → summary prompt: `agent/conversation_compression.py:2860-2905` `_pre_compress_memory_context` calls `MemoryManager.on_pre_compress(messages, evidence_messages=...)` (`agent/memory_manager.py:719`), sanitizes with `sanitize_memory_context` (`context_engine.py:25-30`, redaction + 6 KB elide), and hands the string to `compress(memory_context=...)`. If the engine's `compress` signature lacks `memory_context`, the host logs once and continues (`conversation_compression.py:1704-1713`).
- `checkpoint_required`: if `compression.checkpoint_required` is on, the pre-compress call demands checkpoint API v2 and **raises** if the provider can't take one; the proactive tool-result prune is suppressed while armed (`turn_preflight.py:411-425`).
- Turn ingestion: `sync_turn(...)` runs after each completed turn for the primary agent (`memory-provider-plugin.md:201`); prefetch text is injected into the **current turn's user message** at API-assembly time (`compose_user_api_content`, `turn_context.py:1475-1486`), not by mutating history.
- A memory provider never sees or drives `compress()`. Memory context arriving in the system prompt is a memory-store concern; an engine must not assume it can rewrite it (CLM's "pinned, non-editable" requirement is already the host's shape: system prompt is built once per session and replayed verbatim).

---

## 10. Token accounting

- Per response: `agent/turn_usage.py:115-136` builds the canonical `usage_dict` — `prompt_tokens` (Gemini long-decode uses `initial_prompt_tokens`), `completion_tokens`, `total_tokens`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, MoA advisor usage folded in; passed to `engine.update_from_response(usage_dict)` at `:144`. `agent._last_turn_usage` keeps the latest for `on_turn_complete` (`:175-178`).
- Thresholds: `threshold_tokens = int(context_length * threshold_percent)`, recomputed by `update_model` (`context_engine.py:233-250`) with `model_thresholds` longest-substring resolution (`compression.model_thresholds` reaches the engine *before* the first `update_model`, `agent_init.py:1846-1852` — the one documented `compression.*` key that is part of the engine contract).
- Pressure used to decide compaction: provider-reported real counts first, usage anchor second, rough estimate last — `compress_after_tool_results` (`turn_preflight.py:276-296`) and `assemble_api_request` (`turn_request_assembly.py:250-290`). Estimators: `agent.model_metadata.estimate_messages_tokens_rough`, `estimate_request_tokens_rough`, `estimate_tokens_rough`; anchored path `agent/usage_anchor.py`.
- Post-compaction sentinel: `last_prompt_tokens == -1` means "compaction just ran, awaiting real usage"; `0` means "no real usage yet, use the estimate" (`context_engine.py:187-202` spells out the difference and forbids conflating them).
- The engine's own `should_compress(prompt_tokens)` is called with the host's chosen pressure figure (`turn_preflight.py:305`, `:362`), and `prune_tool_results_only` runs on a separate low trigger.
- `get_status()` shape is documented (`:213-224`) but display reads the raw fields.

---

## 11. Plugin discovery / activation / config

- Directory layout: `plugins/context_engine/<name>/__init__.py`, discovered by text heuristic (`"register_context_engine" in source or "ContextEngine" in source`, `plugins/context_engine/__init__.py:22-29`), bundled first then `$HERMES_HOME/plugins/<name>/` (`:32-57`), bundled wins collisions. User engines load under a synthetic parent package `_hermes_user_context_engine` (`:19,76`).
- Activation is **config only**: `context.engine` (`hermes_cli/config_defaults.py:1280-1284`, default `"compressor"`), resolved in `agent/agent_init.py:1752-1815` `_select_context_engine`: `compressor` → `None` (built-in); else directory load → else general-plugin registry (`hermes_cli/plugins.py:287,1325` `get_plugin_context_engine`) → else warn + built-in. A user engine needs **no** `plugins.enabled` entry.
- `ctx.register_context_engine` (`hermes_cli/plugins_context.py:495-511`) accepts one engine only; second registration is rejected; registration is ledgered with a restore lambda.
- Surfaces: `hermes plugins` → "Context Engine" (`hermes_cli/plugins_cmd_toggle.py:32-59`), dashboard (`hermes_cli/web_routers/dashboard_ui.py:340-356`, `web_server_dashboard.py:821-831`), config (`hermes_cli/plugins_cmd.py:947-949`).
- `safe_worker_enabled()` short-circuits selection to `None` (`agent_init.py:1756-1758`) — subagent "safe worker" mode never loads a plugin engine.
- Docs: `context-engine-plugin.md:272-303` (registration, `clone_for_agent`), `:325-334` (config).

---

## 12. Manual command hooks

- `/compress [focus]`, `/compress here [N]`, `--preview` — CLI: `hermes_cli/cli_session_mixin.py:957-1035` `_manual_compress` → `agent/conversation_compression_manual.py` → engine `compress(force=True, focus_topic=...)`; no `compression_enabled` gate. Compaction is announced afterwards via `finalize_context_engine_compression_notification` (`:970,1019,1030`).
- Gateway/Desktop/messaging `/compress`: `gateway/session_mutation_compress.py:60-130`. If no resident agent, it builds a **fresh engine** via `_select_context_engine(config)` (`:177-178`), runs one forced `compress()` on `json.loads(json.dumps(head))`, refuses a growing result (`_last_compress_refused_would_grow`), rejoins head+tail (`rejoin_compressed_head_and_tail`), then `on_session_end` if it created the engine.
- Engine-provided slash commands: `register_command(name, handler, description, args_hint)` on the collector (`plugins/context_engine/__init__.py:101-126`) — conflict-checked against built-ins and existing plugin commands, then stored in `manager._plugin_commands`. That is the supported way to add e.g. `/lcm-status`.
- Programmatic compress callers filter kwargs by signature (`conversation_compression.py:1694-1714`), so an engine can accept `bypass_cooldown` for provider-overflow recovery or ignore it.

---

## 13. Lifecycle, complete

```
instantiate (directory load | plugin registration + clone_for_agent)
  → bind_session_state(session_db, session_id)          # duck-typed
  → update_model(model, context_length, ...)            # once at init
  → on_session_start(session_id, hermes_home, platform, model, context_length, conversation_id)
per API response   → update_from_response(usage_dict)
per iteration      → select_context(request_messages, conversation_history, incoming_message, budget_tokens)
pre-generation     → should_compress_preflight() → one forced compress(trigger="engine_preflight")
turn start         → should_compress / compress(trigger="idle"|"preflight")
post-tool round    → should_compress → compress(trigger="post_tool") | prune_tool_results_only
manual             → compress(force=True, focus_topic, memory_context)
commit             → on_session_start(new_sid, boundary_reason="compression", old_session_id)
turn end           → on_turn_complete(messages_copy, usage, turn_id, task_id, api_call_count, interrupted, failed, turn_exit_reason)
/new /reset /branch→ on_session_end(old, msgs) → on_session_reset() → on_session_start(new, old_session_id, carry_over_context)
                   → carry_over_new_session_context(old, new)        # duck-typed, optional
process exit       → on_session_end(session_id, messages)
```

---

## 14. Smallest upstream extension to support CLM (recommendation, not implemented)

Requirement gap, stated precisely: CLM needs **one committing, mid-loop replacement of the live transcript, driven from inside the tool loop**. Everything else already exists (`select_context` can write the mirror file from `request_messages`; `handle_tool_call` receives the live list; token pressure is available; rollback can be engine-internal because the engine holds the mirror).

**Recommended minimal change — a commit affordance on the existing tool-dispatch seam.** No new ABC method, no new hook:

1. `agent/tool_executor.py:1690-1697` — add one kwarg to the existing call:
   `handle_tool_call(function_name, next_args, messages=messages, commit_messages=<closure>)`.
   `handle_tool_call(self, name, args, **kwargs)` already accepts it; the ABC signature is unchanged, so every existing engine keeps working (kwargs are additive and already documented as open-ended).
2. The closure (defined in the loop/tool-round layer, not in the executor) does what `compress()`'s commit path already does, and nothing invented:
   - replace the loop list's contents in place (`messages[:] = new_list`) so `_LoopState.messages` identity survives;
   - re-anchor `current_turn_user_idx` (`agent/turn_context_compaction.py:116-123` `_reanchor`);
   - re-baseline `conversation_history` (`conversation_history_after_compression`);
   - drop/restamp `api_content` on every rewritten row (`agent/turn_context.py:1487-1494`) — **required**, else edited historical rows silently don't reach the wire;
   - clear the `_DB_PERSISTED_MARKER` on rewritten rows and force a durable rewrite (`agent/session_persistence.py:548-560`), or the edit dies with the process;
   - clear `agent._usage_anchor` (`agent/usage_anchor.py`) so the next estimate isn't computed against the pre-edit prefix;
   - `_reset_retry_state_after_compaction(agent)` and refund nothing (no API call was consumed).
3. Gate it behind config (`compression.mode: model_managed`, issue default `harness`) and behind `compression.checkpoint_required` refusal (same rule the prune follows, `turn_preflight.py:411-425`): an edit is a content-loss boundary, so it must not run where a checkpoint is contractually required.

Estimated footprint: ~1 small module (commit helper) + ~15 lines in the tool-round/loop layer + 1 line in the executor + config default + tests. Keeps `select_context` request-only, keeps `on_turn_complete` observational, keeps prompt-cache semantics (the engine's own edits are the only prefix change, which CLM's design already accepts).

Alternatives, ranked worse:

- **Extend `select_context`'s return contract** (`{"messages": [...], "commit": True}`) — same commit helper, but it fires on *every* request, including retries-free re-assemblies after compaction, so an engine must self-dedupe commits; and it can't be triggered by a specific tool call. Workable, second choice; useful if the edit needs to be applied *before* the first request of a turn (e.g. mirror pre-load).
- **New hook `on_mid_turn_edit(messages) -> list|None`** — cleanest intention, but adds an ABC method + doc + contract suite for something one closure already covers, and needs its own frequency decision.
- **Abuse `should_compress_preflight` + `compress()` as a per-iteration edit channel** — the anti-pattern the docs explicitly closed off (doc `:106-109`); it also routes through the summary machinery and the attempt budget.
- **Post-turn summarizer** — explicitly excluded by the architecture gate, and cannot satisfy "edits affect the next model request".

Risks to flag to the owner: (a) any commit breaks prompt-cache reuse from the edit point for the rest of the turn, so the system prompt must teach "edit late, batch edits"; (b) a transcript missing tool-pairing will be repaired wire-side by the sanitizers, i.e. edits that delete a tool call but keep its result get silently dropped — the commit helper should validate pairing and refuse (fail closed) rather than let the sanitizer degrade it; (c) `handle_tool_call` currently has no timeout/lock discipline, and compression normally runs on a pooled thread under a host timeout — the commit path is on the conversation thread, so it is *safer* than `compress()`, but the engine must not hold the compression lock while calling it.

---

## 15. Verification / test coverage that pins these claims

`tests/agent/`: `test_context_engine.py` (ABC contract), `test_context_engine_host_contract.py`, `test_context_engine_select_context.py` (fail-open, request-only, `[]`/non-dict rejection), `test_context_engine_on_turn_complete_usage.py`, `test_context_engine_message_uid.py`, `test_plugin_context_engine_clone.py`, `test_plugin_context_engine_init.py`, `test_commit_memory_session_context_engine.py`; plus `tests/hermes_cli/test_cli_manual_compress.py`, `test_plugins_cmd_category_discovery.py`.

---

## 16. Open questions

1. **`api_content` policy for edited rows.** Is there any existing host path that intentionally rewrites a persisted row's `content` *and* its sidecar mid-turn (sanitizer row-addressed rewrite, `message_sanitization.py:869`)? If yes, reuse its exact contract for CLM commits instead of inventing one — I did not fully trace that path.
2. **Rollback semantics.** "Roll back newest turns" on overflow: does the host already offer a supported truncation primitive for the *live* list (pre-compaction checkpoint restore, `agent/prepared_compaction.py`), or must CLM own its own snapshot?
3. **Edit-only turns.** Does an assistant response with tool calls but no prose already end the turn cleanly today (`_LINE_IS_REPLY_TOOLS`, `turn_tool_round.py:22-26`)? Free-edit accounting vs `max_turns`/`iteration_budget` needs a decision (issue flags it as TBD).
4. **`compression.enabled` coupling.** Should an external engine be exempt from `compression.enabled`? Today it isn't (§2 delta 3). If CLM ships as `compression.mode`, this must be resolved or `model_managed` will be silently dead under `compression.enabled: false`.
5. **Host-isolated engines.** If `plugins.isolation: host` is ever the default for user engines, per-iteration `select_context` + a mid-loop commit become RPC round-trips carrying whole message lists. Does CLM ship as a bundled engine (in-process) to dodge that?
6. **Docs drift.** `select_context` "re-runs on retries" (doc `:135`) is false in code. Worth a doc patch upstream regardless of CLM.
7. **Mirror write location.** Writing the mirror from inside `select_context` is legal (it's a side effect, return `None`), but it fires *after* `build_api_messages` and before sanitizers — so the mirrored bytes are pre-sanitization. Decide whether the mirror should reflect pre- or post-sanitizer bytes; the latter needs a seam after `_sanitize_api_messages` (`turn_request_assembly.py:159`), which does not exist as a hook today.
