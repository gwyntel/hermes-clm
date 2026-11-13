# Hermes CLM architecture

## Control flow

```text
raw Hermes history ──ingest/redact──> per-session SQLite archive
                                      │  ▲
agent tools ──validate/stage/commit───┘  │ expected-revision CAS
                                      │  │ revision N
                                      ▼  │
request clones ──UID/subsequence──> projection ──> provider request
```

Main agent chooses operations. Auxiliary model only proposes selected-span compression. Store validates and persists. Host owns raw transcript and final wire sanitization.

## Tool surface (contract)

Names may share one `clm_context` dispatcher or separate schemas; behavior is fixed:

- `status`: revision, budget/pressure, active/offloaded/pinned counts, last error, store health, aux metrics.
- `inspect`: bounded active-context view with stable span IDs, roles, origins, token estimates; never another session.
- `stage`: keep/drop/rewrite/pin/offload/restore/boundary operations, `expected_revision`, idempotency key. Pin is one-way for agent.
- `commit` / `discard`: explicit transaction lifecycle; commit validates again inside write txn.
- `revert`: new revision restoring a prior projection subject to current pins and host-present UIDs.
- `search` / `retrieve`: bounded, paginated archive access by current-session span ID/query; no arbitrary paths/session selectors.
- `propose_compression`: selected editable span IDs; bounded aux call; returns immutable proposal and exact review text.
- `accept_proposal`: requires proposal ID + hash + base revision; stages only, then normal `commit`.

All handlers return JSON strings, reject unknown fields, and never expose provider/model fallback configuration.

## Store contract

Preferred public types (store delegate owns exact definitions):

- `SpanKey`: UID plus optional absorbed-UID alias; ephemeral rows are not editable.
- `EditOperation`: `{kind, span_ids, replacement?, expected_revision, txn_id, actor_origin}`; closed kinds only.
- `ProjectionResult`: messages plus applied/inert/conflicting operation diagnostics.
- `Proposal`: immutable structured operations + `proposal_id`, `proposal_sha256`, `base_revision`, `origin=aux`.
- `Revision`: monotonically increasing integer; revert references earlier revision without deleting later audit.

One SQLite transaction covers operation append, archive additions, revision bump, idempotency outcome and audit event. Derived index writes happen after commit. SQL triggers prevent archive and protected-row UPDATE/DELETE. Redaction precedes immutable archival. Media is content-addressed and verified before use.

## Projection invariants

1. Pure function of current host history + committed revision. No wall clock, aux call, token-dependent selection or store writes.
2. System, user, prefill, current-user and newest-tool rows pass through exactly.
3. Ordered unique subsequence alignment; unknown rows pass through; ambiguity affecting any op fails whole projection.
4. No unknown-role coercion, role inference from text, privileged-message synthesis, or persisted UID requirement bypass.
5. Complete tool groups only; group edits preserve IDs and call/result coverage.
6. Multimodal and unknown content-part types are protected/refused, never flattened.
7. Fresh deep copies returned; caller mutation cannot affect store or later projection.
8. Structural store corruption ⇒ return `None`. Derived-index corruption ⇒ rebuild index. Never return a guessed subset.
9. Origin survives rewrite: model and aux summaries remain untrusted, schema-like labels; never operator authority.
10. Offload stubs are non-imperative and contain only span ID/hash metadata.
11. `restore` emits the span's current `rewrite_text` when it still carries one and the archived original otherwise; `drop`/`offload` clear the rewrite, and a truncated snapshot (`operations_truncated`) is refused instead of projected from a partial log. `keep` mirrors `restore` for a `dropped`/`offloaded` span (rewrite first, archived original second, `max_rewrite_bytes` ceiling, protected rows refused) and is inert on an `active` one, matching the store's `already_active` receipt; it is never a receipt-only no-op.

## Token pressure and cache

- Reuse Hermes model/context-length state and rough estimators. Calibrate estimates against real `usage.prompt_tokens` with bounded ratio; record real cache read/write fields when supplied.
- Default configurable reserve and pressure tiers; `context_status`/tool receipts expose the same estimate used for edit validation.
- Existing Hermes pressure notices remain intact because newest tool output is protected.
- Routine projection does not inject changing status rows (would destroy cache). The one
  injected row is the **compaction notice**: after an operator `/compact` or an overflow
  offload, a single `[clm.notice rev=N ...]` row anchored right after the system row tells
  the model its earlier tool results moved to the archive. Its bytes are a pure function of
  the compaction revision, so it is byte-identical on every turn until the next compaction
  and the cached prefix survives; a host that echoes the projection back cannot accumulate
  a second one. Task-boundary tool creates explicit agent-chosen snapshots/audit markers.
- Record before/after estimated tokens for every edit, aux usage/latency when available, and revision snapshots sufficient for prefix-reuse analysis.

## Compression and lifecycle

- `should_compress=False`, `should_compress_preflight=False`, default pruning no-op.
- Manual `/compress`: commit current valid projection only; if no reduction, report no changes.
- Overflow (`bypass_cooldown=True`): archive first, then commit projection; if needed, deterministically offload oldest unpinned tool groups until reserve fits. Never delete system/user/pinned content.
- Lazy initialization from host `session_id`; no reliance on `bind_session_state` or `on_session_end`.
- `clone_for_agent()` returns a fresh engine with reopened store, never deep-copies SQLite/locks.
- Compression boundaries carry state through the explicit `old_session_id` lifecycle: the store is
  adopted in place **and** the relation is recorded in `clm/lineage/`, so a cold process resolves
  the rotated child to the same archive. Every other id change gets an isolated store. No lineage
  guessing from `conversation_id`.

## Model guidance (system-prompt section)

The tool surface is useless if the model does not know the operating rules, so the plugin
registers one static text block through the host's section API
(`ctx.register_system_prompt_section`): id `hermes-clm.usage`, position `after_memory`,
`max_chars` 4000 (the host ceiling; the text is ~3.5 KB with headroom because an
oversized section is skipped, not truncated).

- **Static by construction.** The section is registered as a string, never a callable.
  Archive bytes, tool results and user text are never interpolated: the block is
  high-trust prompt bytes, and a callable would turn session data into instructions.
- **Frozen per session.** The host renders sections once at session start and persists
  the rendered prompt; a resumed session restores those exact bytes rather than re-running
  plugin code. Editing the text therefore changes new sessions only.
- **Gated on the live tool surface.** The section is registered only when the host offers
  the API *and* the `clm_*` tools are callable: the general-plugin lane registers it when it
  registered the tools itself (no engine selected), and also when this engine *is* selected,
  because the engine's own registration supplies the tools. A profile that installs the
  plugin but selects another engine gets the engine registration and no instructions.
- **Dual enablement is asymmetric on purpose.** The canonical profile enables the plugin
  *and* selects this engine. Exactly one lane owns the tools and the store: the selected
  engine injects the `clm_*` schemas, registers `/clm` and opens the session store; the
  general-plugin lane sees the selection (injected `engine_selected`, else the profile's
  `context.engine`) and registers only the section, with no engine object, no tools and no
  command. The alternative — a plugin-lane engine — would answer `no_session` on every call:
  the store's sibling `flock` admits one writer, and the second engine swallows
  `StoreBusyError` on lazy attach. See [limitations](limitations.md#plugin-enabled-and-engine-selected).
- **Capability-missing hosts still load.** Hermes' directory-loaded engine path
  (`_EngineCollector`) has no section API; registration is `getattr`-guarded, so the
  engine still loads and simply carries no guidance. A rejected registration is recorded
  on the runtime (`prompt_section_error`) and logged as a warning carrying only the
  section id and exception type (the entry-point shim discards the runtime, and host
  exception text is untrusted) instead of breaking the plugin.

## Security limits

Plugin cannot defend against arbitrary same-UID shell access. It can enforce tool authority, file permissions, redaction, origin preservation, CAS, idempotency and corruption detection. It cannot make self-authored instructions unpersuasive, guarantee host post-sanitizer bytes, or read a subagent's parent archive (lineage is only the
  compression rotation the host reports, never a tool argument). These limits belong in README and benchmark reporting.

## Reference host facts

Verified on Hermes v0.23.0 `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`; context-engine files matched main `2fd5066fdd9e143ee90d7d549527ea1e1e07cf5d` during research. Load-bearing paths: `agent/conversation_loop.py:1351`, `agent/turn_request_assembly.py:123`, `agent/context_engine.py:121`, `agent/tool_executor.py:1689`, `agent/auxiliary_client.py:7905`.
