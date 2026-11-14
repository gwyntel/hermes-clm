# Cross-branch integration contract

Delegates may refine internals, not these public semantics. Python type annotations are indicative; keep names stable until lead integration review.

## Store entry points (`store.py`, `models.py`)

- `ContextStore.for_session(hermes_home: str | Path, session_id: str, *, read_only: bool = False) -> ContextStore`
- `ingest(messages: Sequence[dict[str, Any]]) -> IngestResult` — write-through, redact, deduplicate UID/content versions; never mutate input.
- `snapshot() -> StoreSnapshot` — immutable, deep-copy-safe view; no SQLite handles.
- `stage(operations: Sequence[EditOperation], *, expected_revision: int, txn_id: str, actor_origin: str) -> StageResult`
- `commit(*, txn_id: str, expected_revision: int) -> CommitResult` — idempotent replay returns original outcome; stale revision rejected.
- `stage_commit(operations, *, expected_revision: int, txn_id: str, actor_origin: str) -> CommitResult` — stage and commit one new transaction inside a single write transaction. Same conflict validation and audit events as `stage` + `commit`; a refusal (stale revision, protected span, bad operation) leaves **no** transaction row, and reusing an existing staged `txn_id` raises `txn_already_staged` instead of consuming it. Replaying a committed `txn_id` with an identical payload returns the original result; a different payload raises `txn_payload_mismatch`.
- `discard(*, txn_id: str) -> None`
- `revert(*, target_revision: int, expected_revision: int, txn_id: str, actor_origin: str) -> CommitResult` — new head revision, no truncation.
- `status() -> dict[str, Any]`
- `last_compaction() -> dict[str, Any] | None` — newest revision carrying a `drop` or `offload`, as `{revision, at, actor, kind, spans_offloaded, tokens_freed}`; `None` when nothing was ever compacted. A revert does not erase it, and it counts distinct spans. Optional: a store without it is supported (the engine then reports no compaction).
- `inspect(*, cursor: str | None = None, limit: int = 50, include_stale: bool = False) -> dict[str, Any]` — paginates aligned spans. Absent rows (archived, no longer supplied by the host) are omitted by default; `include_stale=True` returns them with their presence/status labels and reports `alignment`. Callers must not change `include_stale` mid-pagination.
- `search(query: str, *, cursor: str | None = None, limit: int = 20) -> dict[str, Any]`
- `retrieve(span_id: str) -> dict[str, Any]` — current session only.
- `create_proposal(operations, *, base_revision: int) -> Proposal`
- `accept_proposal(*, proposal_id: str, proposal_sha256: str, base_revision: int, txn_id: str) -> StageResult` — stages only.
- `close() -> None`

## Immutable snapshot types (`models.py`)

- `StoreSnapshot`: `revision`, `operations: tuple[EditOperation, ...]`, `spans: Mapping[str, ArchivedSpan]`, `pins`, `head_hash`.
- `ArchivedSpan`: `span_id`, `uid`, `aliases`, `message`, `group_id`, `origin`, `protected`, `content_sha256`. `message` is redacted archived raw structure, never a live host object.
- `EditOperation`: closed `kind` (`keep|drop|rewrite|pin|offload|restore|boundary`), `span_ids`, optional `replacement`, `actor_origin`, provenance.
- `Proposal`: `proposal_id`, `proposal_sha256`, `base_revision`, immutable structured operations, `origin="aux"`.
- Results carry JSON-serializable `ok`, `revision`, `errors`, `applied`, `inert`, `receipts`; no live connections or paths outside store.

## Engine ↔ store

Engine calls only `for_session`, `ingest`, `snapshot`, `status`, lifecycle/close. Projection logic stays in `projection.py`; store never aligns request rows. Store owns operation conflict validation; engine independently refuses protected/ambiguous/invalid projections.

### Session attach and rotation

- A store always lives under `<hermes_home>/clm/sessions/<session-key>/`. A blank, relative or non-absolute home is `ValidationError(code="invalid_home")`; there is no working-directory fallback, because a store beside the gateway's cwd is invisible to the next process and shared between profiles.
- `hermes_home` is resolved lazily at store-open time: `on_session_start(hermes_home=…)`, then `clm.hermes_home`, then the host's `get_hermes_home()`. Never at import: one process serves several profiles.
- The attached handle is keyed by `(session_id, hermes_home)`. Either changing closes the old store and attaches the new one.
- `on_session_start(new_id, boundary_reason="compression", old_session_id=old_id)` is the host's **post-compaction rotation**, with no reset in between (`agent/conversation_compression.py`). When `old_id` is the session this engine already serves, the store is *adopted*: same handle, same revision lineage, pins and archive intact, new id — **and the relation is written to `clm/lineage/`**, so a later process that only has the child id resolves it to the same archive ([store-format](store-format.md#session-lineage)). A rotation that is not `compression`, that names another session, or that follows a reset is a new conversation with its own store; `old_id == new_id` records nothing. A child id that **already owns a store** is never aliased over the parent archive (`rotation_conflict`): the child keeps its own store, the refusal is receipted as `session_rotation_refused`, and `last_rotation_error` in `status` names the conflict. A failed lineage write does not undo the live adoption and is reported as `last_lineage_error` in `status`.
- `bind_session_state(session_db, session_id)` adopts an id only while unbound, and never reads or writes the host database.
- A compaction that cannot be performed is not reported as one. `compress()` returns identity with `_last_compress_aborted = True` and a typed `_last_summary_error`: `store_unavailable:<code>: …`, `store_read_only: …`, `manual_no_offloadable_groups`, `manual_commit_refused:<code>`, `manual_no_saving`, `projection_refused:<codes>`.


## Tools ↔ engine/store

Engine exposes `store` only through its own scoped tool-service methods. Tool handlers never accept `session_id`, paths, provider, model, base URL or API keys. All return JSON strings. Tool schemas contain no provider configuration.

`clm_status` takes an optional `detail`: `summary` (default) returns revision, tier, usage, token vocabulary, counts, `last_compaction`, `last_error`, the compact store/compression/lineage diagnostics (`engine_version`, `store`, `store_error`, `last_compress_aborted`, `last_compress_error`, `last_lineage_error`, and `last_rotation_error` when set) and a hint; `full` adds budget, projection, config, limits, health, aux metrics and receipts capped to the newest three with `receipts_omitted`.

Token fields are named by scope. Everything the request budgets — `tokens` (`scope: "request"`), `headroom_tokens`, `overflow_tokens`, `compactable_tokens`/`compactable_units` — is measured against the live request; `all_spans_est_tokens` is the separate store-wide aggregate over every archived span and is **not** what the window is checked against. `headroom_tokens` is `None` when no context length is known (never `0`), and `compactable_tokens` is `None` with `compactable_unknown` naming the reason when no request has been observed.

`clm_stage` prices a batch it just staged through the engine's pure `preview(operations) -> {before, after, saved, calibrated, conflicts}` and reports it as `projected`; no `preview` means absent detail, never a failed stage. `clm_stage` also accepts an opt-in fast path: `commit=true` performs the store's `stage_commit` for that batch and returns `committed`/`atomic`. Its preconditions are all-or-nothing — the engine previews first and refuses with `preview_unavailable`, `projection_refused` or `insufficient_saving` (`min_saved_tokens` compares the calibrated saving) without writing anything, and a store without `stage_commit` is a `service_error` rather than a silent two-phase fallback. Default behaviour is unchanged: no `commit` field means one `stage` call plus one preview.

`kind: "patch"` is a tool-level kind, not a persisted one. It names exactly one span plus a literal `old` that must occur exactly once in the span's current text (a committed `rewrite_text` when present, otherwise the archived content) and a possibly empty `new`. The handler resolves it into a whole-span `rewrite` with `provenance.via = "patch"` before anything is written, echoes a digest receipt (`patched`) instead of the expanded text, and refuses with `patch_not_found`, `patch_ambiguous`, `patch_noop`, `patch_unsupported_content`, `protected_span`, `span_pinned` or `span_not_active`. Because resolution is against current text, `patch` deliberately does not support `commit=true`: stage it, then `clm_commit`.

The plugin-owned `pressure` hint rides only on newly created CLM tool responses. It is never inserted into, or used to alter, any projected transcript row, so the projection stays token-independent.

## Configuration

Read only `clm.*` from profile config plus Hermes-supplied model/context state. Suggested keys:

- `clm.reserve_tokens` (default 2048)
- `clm.pressure_thresholds` (default `[0.25, 0.5, 0.75]`)
- `clm.aux_enabled` (default true)
- `clm.aux_timeout_seconds` (default 45, bounded below sequential tool deadline)
- `clm.archive_redact` (default true)
- `clm.retention_sessions` (default 32; pinned content exempt until operator purge)
- `clm.max_retrieve_bytes`, `clm.max_rewrite_bytes`, `clm.max_aux_spans`
- `clm.max_span_chunks`, `clm.max_aux_calls` (bounded chunking for long auxiliary spans; defaults 8 and 8)

No model IDs or provider fallback configuration. Do not change production config to test defaults; inject config into constructors in tests.

## Packaging imports

Runtime package must import without Hermes installed. Hermes imports are lazy inside engine/plugin/auxiliary adapter paths or guarded `TYPE_CHECKING`; core store/models/projection remain stdlib-only. Delivery delegate configures dev/integration extras accordingly.
