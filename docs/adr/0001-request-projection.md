# ADR 0001: Durable request projection for live CLM editing

- Status: accepted
- Date: 2026-11-11
- Authors: Claire / GwynTel

## Context

Hermes `ContextEngine.select_context()` runs before each provider-bound agent-loop iteration. It receives structural request clones plus UID-bearing conversation history. Returned messages affect only that request; host transcript remains unchanged. Naive in-place transcript edits fail because historical `api_content` overrides edited content and persisted rows are deduplicated. Reference CLM code is CC BY-NC 4.0 and destroys tool-call structure; no code or prompt text may be copied.

## Decision

Implement CLM as a **durable, UID-addressed projection over raw Hermes history**.

1. Raw host history remains immutable. A per-session store archives messages, edit operations, immutable proposals, pins, revisions and audit records.
2. Agent tools commit operations against the store. The next `select_context()` applies the new revision to fresh request copies. This is live mid-loop editing, not post-turn summarization.
3. Projection aligns UID-bearing conversation history to request messages using an ordered, unique subsequence. Request rows lacking identity pass through. Ambiguity affecting an edited row aborts projection (`None`) and exposes diagnostics.
4. Kept request rows remain host dictionaries (deep-copied). Rewrite/offload changes only `content`; roles, tool IDs, call structure and multimodal parts are never synthesized.
5. System/developer rows, every user row, current incoming user content (including memory/plugin injections), prefill and newest tool result are protected. Protected rows pass through byte-identically. Agent pin is supported but never changes origin or authority; agent unpin is absent. Operator unpin is a manual command.
6. Assistant `tool_calls` plus all results form one atomic editable group. Partial groups are rejected before commit and before projection. Operations only target persisted `message_uid` rows; ephemeral/ambiguous rows are not editable.
7. Revisions are monotonic. Every mutation is a write-through SQLite transaction with expected-revision CAS and idempotency key. Revert creates a new revision; history is never truncated. Restore is inert when host history no longer contains the UID.
8. `active.json` is a non-authoritative index `{db_id, session_id, revision, row hashes}`, never a second payload copy. SQLite is authoritative. Store files are `0600`, directories `0700`; sibling `flock`, WAL, `BEGIN IMMEDIATE`, busy timeout and single-writer enforcement are required.
9. Raw means unsummarized, not secret-preserving. Host redaction runs at archive ingestion by default; exact opt-out is documented as sensitive. Retention and operator purge are explicit. Hash chains detect corruption/interruption only; no tamper-proof claim against same-UID shell access.
10. `should_compress()`, routine preflight and tool pruning stay disabled. `compress()` is identity except manual `force=True` or overflow `bypass_cooldown=True`. Overflow may make a real host-history commit using the validated projection plus deterministic archive-backed offload, never silent user-message deletion.
11. Auxiliary compression calls `agent.auxiliary_client.call_llm(task="compression", ..., timeout=<bounded>)`. It creates an immutable proposal keyed by proposal hash + base revision. Main agent must explicitly accept and commit. No model IDs, provider settings or fallback chains are implemented here. No aux call occurs in `select_context()`.
12. Only `plugins.isolation: in_process` is supported initially. Each agent/subagent gets an independent store; cross-session and child→parent reads are absent. Hermes core and production config are not modified.

## Rewrite lifetime and restore (added after review)

A `rewrite` sets the span's `rewrite_text`; it is the projector's content for an active span. `drop` and `offload` **clear** it — the row leaves the projection, so the rewrite has no reader, and the checkpoint, the replay (`_replay_states`), `revert` and `project()` all record `(state, None)`. `keep`/`restore` **carry it through**: a `restore` applied to an active, rewritten span keeps the rewrite, and the projector emits `span.rewrite_text` for a `restore` whenever the span still carries one, falling back to the archived content only when it does not. Consequences:

- `rewrite → drop` is not a `verify()` divergence, and `recover()`/`rebuild_state()` cannot write a stale rewrite onto a dropped span.
- Operations that committed `inert` are replayed inert: `_replay_states` runs every recorded operation through `_replay_transition`, the mirror of the planner's `_transition`. `operations` records the *planned* operations of a revision, so replaying them unconditionally would resurrect a dropped span on a later `rewrite`, flip a dropped span to `offloaded`, and make `verify()`/`recover()` disagree with a correct checkpoint. `restore` of a UID the host no longer sends stays inert too (presence is a current flag, so replay uses the current one).
- `rewrite → drop → restore` reinstates the **archived original**; the rewrite is not resurrected. Re-`rewrite` if the shorter text is still wanted.
- `keep` is `restore` for a `dropped`/`offloaded` span: the same content rule (rewrite first, archived original second, `max_rewrite_bytes` ceiling), the same protected-row refusal, and the same reactivation recorded by `_transition`/`_replay_transition`. It is only a no-op on an `active` span, where the store commits `already_active` and the projector emits nothing for it — treating `keep` as a receipt-only kind (as `pin`/`boundary` are) silently dropped a documented reactivation path while `verify()` and the checkpoint disagreed with projection.
- A store that caps its replayed operation log (`operations_truncated`) is refused by projection (`operations_truncated`) rather than projected from a partial log, because a missing drop would put a row back on the wire.

## Why not native transcript commits

A native commit closure would require re-anchoring, sidecar restamping, persistence-marker rewrites, usage-anchor reset and retry reset. That remains a useful optional upstream extension, but is unnecessary for request-level CLM semantics and increases blast radius. See `upstream/hermes-commit-proposal.md` (implementation delegate).

## Consequences

- Valid edits affect the next model request and survive restart because operations, not rewritten host rows, persist.
- Unchanged revision + unchanged history yields byte-stable projection and preserves prefix caching. An edit invalidates cache from its earliest changed row; guidance says batch edits and edit late.
- Raw host `/branch`, `/rewind`, `/undo` and `/retry` win: operations on absent UIDs become inert; deleted content is never resurrected.
- Host may merge adjacent protected user rows after assistant removal. Their text survives, but provider-side row boundaries are not guaranteed.
- Gateway eviction and cold `/compress` require lazy init and write-through behavior.
- All archived/tool/aux text remains untrusted data. Provenance is structural, never inferred from message text.

## Independent implementation / prior art

Concepts derive from Shao et al., arXiv:2609.37725, and facebookresearch/context-language-models@483198229ab7b90c0eed4651306ab3b3505aaf50. Upstream is CC BY-NC 4.0. This project uses independent code, schemas, names and prompt text. Do not vendor upstream files, copy prompt strings, reproduce `[[CTX_TURN]]`, `LIVE_CTX_MAIN.txt`, or mechanically transform upstream source. ContextBench task data may be adapted under Apache-2.0 with attribution and pinned revisions.
