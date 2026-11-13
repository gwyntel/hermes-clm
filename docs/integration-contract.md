# Cross-branch integration contract

Delegates may refine internals, not these public semantics. Python type annotations are indicative; keep names stable until lead integration review.

## Store entry points (`store.py`, `models.py`)

- `ContextStore.for_session(hermes_home: str | Path, session_id: str, *, read_only: bool = False) -> ContextStore`
- `ingest(messages: Sequence[dict[str, Any]]) -> IngestResult` — write-through, redact, deduplicate UID/content versions; never mutate input.
- `snapshot() -> StoreSnapshot` — immutable, deep-copy-safe view; no SQLite handles.
- `stage(operations: Sequence[EditOperation], *, expected_revision: int, txn_id: str, actor_origin: str) -> StageResult`
- `commit(*, txn_id: str, expected_revision: int) -> CommitResult` — idempotent replay returns original outcome; stale revision rejected.
- `discard(*, txn_id: str) -> None`
- `revert(*, target_revision: int, expected_revision: int, txn_id: str, actor_origin: str) -> CommitResult` — new head revision, no truncation.
- `status() -> dict[str, Any]`
- `inspect(*, cursor: str | None = None, limit: int = 50) -> dict[str, Any]`
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
- `on_session_start(new_id, boundary_reason="compression", old_session_id=old_id)` is the host's **post-compaction rotation**, with no reset in between (`agent/conversation_compression.py`). When `old_id` is the session this engine already serves, the store is *adopted*: same handle, same revision lineage, pins and archive intact, new id — **and the relation is written to `clm/lineage/`**, so a later process that only has the child id resolves it to the same archive ([store-format](store-format.md#session-lineage)). A rotation that is not `compression`, that names another session, or that follows a reset is a new conversation with its own store; `old_id == new_id` records nothing. A failed lineage write does not undo the live adoption and is reported as `last_lineage_error` in `status`.
- `bind_session_state(session_db, session_id)` adopts an id only while unbound, and never reads or writes the host database.
- A compaction that cannot be performed is not reported as one. `compress()` returns identity with `_last_compress_aborted = True` and a typed `_last_summary_error`: `store_unavailable:<code>: …`, `store_read_only: …`, `manual_no_offloadable_groups`, `manual_commit_refused:<code>`, `manual_no_saving`, `projection_refused:<codes>`.


## Tools ↔ engine/store

Engine exposes `store` only through its own scoped tool-service methods. Tool handlers never accept `session_id`, paths, provider, model, base URL or API keys. All return JSON strings. Tool schemas contain no provider configuration.

## Configuration

Read only `clm.*` from profile config plus Hermes-supplied model/context state. Suggested keys:

- `clm.reserve_tokens` (default 2048)
- `clm.pressure_thresholds` (default `[0.25, 0.5, 0.75]`)
- `clm.aux_enabled` (default true)
- `clm.aux_timeout_seconds` (default 45, bounded below sequential tool deadline)
- `clm.archive_redact` (default true)
- `clm.retention_sessions` (default 32; pinned content exempt until operator purge)
- `clm.max_retrieve_bytes`, `clm.max_rewrite_bytes`, `clm.max_aux_spans`

No model IDs or provider fallback configuration. Do not change production config to test defaults; inject config into constructors in tests.

## Packaging imports

Runtime package must import without Hermes installed. Hermes imports are lazy inside engine/plugin/auxiliary adapter paths or guarded `TYPE_CHECKING`; core store/models/projection remain stdlib-only. Delivery delegate configures dev/integration extras accordingly.
