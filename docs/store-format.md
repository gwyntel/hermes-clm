# Store format, transaction and recovery specification

Status: implemented by `feat/store` (`src/hermes_clm/{models,errors,schema,store,archive,recovery}.py`).
Authoritative for on-disk format, transactional semantics and the store-facing API. Public
*semantics* follow `docs/integration-contract.md`; any addition beyond that contract is listed in
[Additive API surface](#additive-api-surface-beyond-the-integration-contract).

Everything here is stdlib-only: no Hermes import, no provider/model configuration, no network.

## 1. Layout

```text
<hermes_home>/clm/sessions/<session-key>/
├── session.db          authoritative SQLite (WAL, STRICT tables, triggers)
├── session.db-wal      WAL (transient)
├── session.db-shm      WAL index (transient)
├── store.lock          sibling flock file; exactly one writer
└── active.json         DERIVED index only; never authoritative, never a payload copy
```

* Directories `0700`, every file `0600` (re-applied on open, so a loose umask or a
  pre-existing directory cannot widen access).
* `session.db` is the only source of truth. Deleting `active.json` loses nothing.
* Nothing is written outside the session directory; there is no cross-session file.

### Session key derivation

`session_key(session_id)` = `<slug>-<digest>` where

* `slug` = `session_id` with every character outside `[A-Za-z0-9._-]` collapsed to `-`,
  `..` replaced, trimmed of leading/trailing `.-`, truncated to 48 chars, falling back to
  `session`;
* `digest` = `sha256("clm-session\0" + session_id)[:16]`.

Accepted ids: non-empty `str`, ≤ 256 chars, no `NUL`. Everything else raises
`ValidationError(invalid_session_id)`. Consequences: `../../etc/passwd`, `/etc/shadow`,
unicode lookalikes and 4096-char ids cannot escape the sessions root, cannot collide, and two
different ids never map to one directory (the digest is over the raw id, the slug is cosmetic).

Guards applied when opening: containment check against the realpath of the sessions root,
`lstat`-based symlink refusal for the directory, the database file and the index, and refusal of
non-regular files. A symlinked session directory raises `PathSafetyError(symlink_refused)`, as does
a symlinked `session.db`. Read-only opens never create directories or files.

## 2. Connection and concurrency model

| Setting | Value | Why |
| --- | --- | --- |
| `journal_mode` | `WAL` | readers never block the writer; crash-safe rollback |
| `synchronous` | `FULL` | durability across power loss |
| `foreign_keys` | `ON` | referential integrity for spans/state/pins |
| `busy_timeout` | configurable, default 5000 ms | bounded wait instead of busy-loop |
| `temp_store` | `MEMORY` | no temp files leaking outside the directory |
| transactions | `BEGIN IMMEDIATE` | write lock taken up front; no upgrade deadlock |
| `query_only` | `ON` for read-only handles | second "writer" cannot write even by accident |

Single-writer enforcement: a writer takes `flock(LOCK_EX | LOCK_NB)` on `store.lock`; a second
writer in the same or another process fails fast with `StoreBusyError(store_busy)`. The lock is
released on `close()` (and on process exit by the kernel). **Readers take no flock**: WAL snapshot
isolation makes concurrent readers safe, so a read-only handle can inspect a live session. Readers
cannot mutate: `ingest/stage/commit/revert/discard/unpin/create_proposal/accept_proposal/rebuild_*`
raise `ReadOnlyStoreError(read_only_store)`.

Write transactions are serialised inside a process by an `RLock`; nested writes raise
`CLMError(nested_transaction)` rather than silently re-entering SQLite.

## 3. Schema

All tables are `STRICT`. `CHECK` constraints close value sets (`span_state.state`,
`revisions.kind`, `txns.state`, `pin_events.action`, `proposals.origin`), booleans are `INTEGER
IN (0,1)`, and counts/token estimates are `>= 0`.

### 3.1 Immutable payload tables (append-only)

| Table | Purpose |
| --- | --- |
| `spans` | one row per (`uid`, content version): redacted payload JSON, origin, role, group, protection flags, seq, version, hash |
| `span_aliases` | alias edges created when content changes (`old_span_id → new_span_id`) |
| `archive` | append-only archive record per archived payload with `prev_hash`/`entry_hash` chain |
| `operations` | committed operation per revision/ordinal, with replacement and provenance |
| `revisions` | one row per revision: `parent_revision`, `base_revision`, kind, head hash, counts, token estimates |
| `audit` | append-only event log (`ingest`, `stage`, `commit:*`, `discard`, `operator_unpin`, `proposal_created`, `expire_staged`, `state_rebuilt`, `store_created`) |
| `tokens` | per-revision aggregate and per-span token accounting rows |
| `snapshots` | per-revision projection summary: counts, `est_tokens`, `active_set_sha256` |
| `pin_events` | pin/unpin history |

Triggers `trg_<table>_no_update` / `trg_<table>_no_delete` abort **every** `UPDATE`/`DELETE` on
those tables with `E_APPEND_ONLY:<table>`. History cannot be rewritten by a bug, a tool handler or
a shell with the same UID — only appended to (a same-UID attacker can still drop the trigger; the
hash chains make that visible, which is the honest limit).

### 3.2 Mutable tables (narrow transitions)

| Table | Mutable how | Trigger guard |
| --- | --- | --- |
| `meta` | `schema_version`, `session_id`, `db_id`, `created_at` (upsert on create) | — |
| `current_spans` | `span_id`/`present`/`last_seen_seq` as content versions arrive | `uid` never changes (`E_IMMUTABLE:current_spans_uid`) |
| `span_state` | derived checkpoint of state + rewrite | protected spans: never off `active` (`E_PROTECTED:span_state_offmove`), never rewritten (`E_PROTECTED:span_state_rewrite`), never deleted (`E_PROTECTED:span_state_delete`) |
| `txns` | `staged → committed` / `staged → discarded` (one-way) | committed rows cannot regress (`E_TXN_STATE:regress`) |
| `pins` | insert on pin, delete on operator unpin | no `UPDATE` (`E_IMMUTABLE:pins`) |
| `proposals` | acceptance bookkeeping (`accepted_txn_id`, `accepted_at`); payload columns never change | — |

`revisions` additionally refuses gaps (`E_REVISION_GAP`): revision *N* requires *N-1* to exist, so
an interrupted commit cannot leave a hole that later looks like a valid head.

`span_state` is a **derived checkpoint**, not a source of truth: it is fully rebuildable from
`operations` (see §7.4) and recovery recomputes it when it diverges. This keeps the triggers simple
(`state` must live somewhere mutable) without giving up append-only operations.

Superseded (historical) span versions keep their state rows, but replay, `verify()` and
`rebuild_state()` compare only **current** versions, selected through `current_spans`, so content
versioning never masquerades as divergence.

### 3.3 Why spans are immutable

Keeping `spans` append-only means a protected row can never be silently modified, and a content
change is a *new* span version plus an alias edge instead of an `UPDATE`. "Current" is expressed in
`current_spans` (one row per UID). Historical versions stay queryable through the alias chain and
through `retrieve()`.

## 4. Revisions

`revision` is a monotonically increasing integer starting at 1 (`kind='init'`, zero operations).
Every commit writes exactly one revision and never rewrites an older one.

* `parent_revision` — the previous head (audit only).
* `base_revision` — the **state root**: replaying the operation chain of `base_revision` yields the
  state this revision starts from. For a normal edit `base == parent`; for a revert
  `base == target_revision`.
* `head_hash` = `sha256(canonical_json({prev, revision, parent, base, kind, actor, txn, ops}))`,
  where `ops` are the *persisted* operation payloads (`kind`, `span_ids`, `actor_origin`,
  `provenance`, optional `replacement`, optional `txn_id`) and `prev` is the previous revision's
  `head_hash` (`64×"0"` for revision 1). Any change to a committed operation, kind, actor or token
  order changes every later hash.

Revert semantics:

1. Walk `base_revision` edges from the target back to revision 1.
2. Replay the operations of those revisions in ascending order, **skipping revert revisions** (their
   recorded operations are audit-only; the revision's `base_revision` already selects a state root,
   so skipping them makes replay deterministic and idempotent).
3. Overlay current pins (a currently pinned span is never forced off `active` — defensive; the pin
   rules make that state unreachable in practice).
4. Compare with the live `span_state`; apply the difference and record synthesised
   `drop`/`offload`/`rewrite`/`restore` operations for audit visibility.
5. Bump the head; never delete anything.

`restore`/`revert` are inert when the host history no longer contains the UID (`reason:
uid_absent`): deleted content is never resurrected. Presence is derived from ingestion (§5.2).

Guarantees: revisions increase by exactly one; history is never truncated; replaying a committed
`txn_id` returns the original outcome with `replayed=True` and does not add a revision.

## 5. Ingestion

`ingest(messages)` is write-through, idempotent and redaction-first. It never advances the
revision: ingestion changes the archive and the live span set, not the projection edit state.

### 5.1 Per-message pipeline

1. **Identity** — `message_uid` (or `uid`) must be a non-empty string. Messages without one are
   ephemeral: counted in `ephemeral_skipped`, never archived, never editable (the host passes them
   through; projection handles them).
2. **Redaction** — the message is deep-copied and scrubbed (§6). Caller objects are never mutated or
   aliased into the archive: two snapshots never share a message object.
3. **Content version** — `content_sha256 = sha256(canonical_json(redacted_message))` over the whole
   message (role, content, `tool_calls`, ids). Any change is a new version.
4. **Dedupe** — (uid, content hash) already present ⇒ `duplicates += 1`, no new span, presence
   refreshed.
5. **New version** — insert a span, set `current_spans.uid → new span`, and append an alias edge
   `old_span_id → new_span_id` (reason `content_version`). Chains resolve old→new→current, so stable
   span ids keep working across content revisions and `retrieve(old_id)` returns the current
   payload. Alias rows are append-only; resolution follows edges, never rewrites them.
6. **Archive** — append an `archive` row whose `entry_hash` links to the previous row
   (`GENESIS_HASH` for the first).

### 5.2 Presence and the full-history assumption

`ingest` treats its argument as the caller's **complete current host history**
(`StoreConfig.assume_full_history=True`, the default): UIDs seen are marked `present=1`, previously
seen UIDs absent from the batch are marked `present=0`. This is what lets revert/restore report
`uid_absent` instead of resurrecting content the host deleted (`/undo`, `/rewind`, `/branch`).
Setting `assume_full_history=False` disables the clearing (only seen UIDs are marked present), for
callers that ingest incrementally. Presence is *evidence*, not authority: the engine still decides
what is really in the request.

### 5.3 Origin, protection and groups

| Role | Origin | Protected | Editable |
| --- | --- | --- | --- |
| `system`, `developer` | `system` | yes | no |
| `user` | `user` | yes | no |
| `assistant`/`model` | `assistant` | no | yes |
| `tool`/`function` | `tool` | no | yes |
| anything else | `unknown` | yes | no |

Additional fail-closed rules: multimodal content (any content part that is not plain text) and
unknown roles are protected; `protected: true` from the host may escalate but `protected: false`
can never unprotect a system/user/prefill row.

Tool groups: an assistant message with `tool_calls` starts `group_id = "grp_" + sha256(uid)[:16]`;
results whose `tool_call_id`/`tool_use_id` matches a call in the same batch join that group.
Unpaired results stay ungrouped (the store cannot pair across batch boundaries; projection owns
pairing refusal).

## 6. Redaction

Applied before archival; `clm.archive_redact` defaults to `true`. The `archive.redacted` column
records whether scrubbers ran, so the sensitive opt-out is visible in the data itself.

Rules (ordered, deterministic, labelled `[REDACTED:<rule>]`): private-key blocks, `sk-ant-…`,
`sk-…`, `ghp_/gho_/ghu_/ghs_/ghr_`, `github_pat_`, `xox[abpros]-`, AWS `AKIA/ASIA` ids, Google
`AIza…`, JWTs, `Bearer <token>`, credentials inside connection URLs, and `api_key/secret/password/
token = value` assignments. Extra rules can be injected via `StoreConfig.extra_redactions`.

Redaction preserves structure (dicts/lists/keys untouched, only string values scrubbed) and is
idempotent: scrubbed text contains no match for the same rules twice. Raw in this project means
*unsummarized*, never *secret preserving*. Anything not matched is archived as-is; the opt-out
(`archive_redact=False`) is documented as sensitive.

## 7. Operations and transactions

### 7.1 Kinds

| Kind | Effect | Notes |
| --- | --- | --- |
| `keep` | `dropped`/`offloaded` → `active` | inert `already_active`; keeps existing rewrite |
| `drop` | `active` → `dropped` | refuses protected/pinned |
| `offload` | `active` → `offloaded` | archived payload kept; projection shows a non-imperative stub with span id + hash only |
| `rewrite` | replaces `content` text | role/tool structure preserved; refused for protected/pinned/multimodal; bounded by `max_rewrite_bytes` |
| `pin` | adds a pin (one-way for agents) | inert `already_pinned` / `not_active` |
| `restore` | reinstate from dropped/offloaded | inert `uid_absent` when the host no longer has the UID |
| `boundary` | audit/snapshot marker | no span ids, no state change, revision kind `boundary` |

Inert reasons are explicit in receipts: `already_active`, `not_active`, `already_pinned`,
`uid_absent`, `pinned`, `not_pinned`.

### 7.2 Batch validation (before anything is written)

* closed kind set; unknown fields rejected; `span_ids` unique across the whole batch
  (`duplicate_span`); non-empty except `boundary`.
* every span must exist for this session (`unknown_span`) — unknown/other-session ids never leak.
* protected spans refuse `drop`/`offload`/`rewrite` (`protected_span`), pinned spans refuse the same
  (`span_pinned`); `rewrite` requires `editable` text (`not_editable`).
* rewrite size ≤ `max_rewrite_bytes` (`rewrite_too_large`), operations ≤
  `max_operations_per_txn` (`too_many_operations`).
* **atomic tool groups**: `keep`/`drop`/`offload`/`restore` must include every still-active member
  of the span's group (`incomplete_group`, with `missing` ids). `rewrite`/`pin` may target one
  member because structure survives.
* the caller's `actor_origin` must match every operation's `actor_origin` (`actor_mismatch`).

### 7.3 Stage / commit / discard / CAS

`stage(operations, expected_revision, txn_id, actor_origin)` validates against the *current* state,
persists the transaction as `staged` and returns the effects it would have (`applied`/`inert`). It
never changes the head. `expected_revision` must equal the head (`stale_revision`).

`commit(txn_id, expected_revision)`:

1. `txn_id` unknown ⇒ `unknown_txn`; `discarded` ⇒ `txn_discarded`.
2. `committed` ⇒ return the stored outcome verbatim with `replayed=True` (idempotent replay; a stale
   `expected_revision` is irrelevant here — the effect already happened).
3. otherwise compare-and-swap: `expected_revision != head` ⇒ `stale_revision`; the staged row's own
   base ≠ head ⇒ `stale_stage`.
4. **re-validate** the operations inside the write transaction (pins or ingestion may have changed
   since staging), then apply.

`discard(txn_id)` marks a staged transaction `discarded` (unknown id: no-op; committed id:
`already_committed`). A discarded id can never be reused (`txn_discarded`). Every commit path stores
a request signature in `txns.staged_sha256` — the staged operation payloads for `stage`, a
`{op: "revert", target, expected}` object for `revert`, `{op: "unpin", span_id}` for unpin — so
replaying a `txn_id` with a *different* request raises `txn_payload_mismatch` instead of silently
returning the earlier outcome.

One `BEGIN IMMEDIATE` transaction covers, atomically: operation rows, `span_state` changes, pins and
pin events, the revision row, the idempotency outcome (`txns.result_json`), the snapshot row, token
rows and the audit event. A crash at any point leaves either all of it or none of it (§10).

### 7.4 Derived checkpoint

`span_state` is recomputed by replaying the operation chain (`rebuild_state()`, and automatically by
recovery). Replay starts from "all spans active, no rewrite", applies each committed revision's
operations in ascending order, and skips revert revisions. Divergence between checkpoint and replay
is reported by `verify()` and repaired by `recover()`.

## 8. Pins and authority

* Agents may `pin` (one-way). There is no agent unpin operation; the kind set has no `unpin`.
* `unpin(span_id, expected_revision, txn_id, actor_origin)` requires `actor_origin="operator"` and
  raises `PinAuthorityError(pin_authority)` otherwise; `operator_unpin(...)` is the convenience
  wrapper for the operator command path. Both write a revision (`kind='operator'`) so the head
  ordering, audit and idempotency stay uniform.
* Pinning never changes a span's `origin` or authority: a pinned aux summary is still an untrusted
  aux summary. Unpinning of a non-pinned span is inert (`not_pinned`).
* Pins are current state, not revision state: they survive `revert`.
* Pin/unpin history lives in `pin_events` (append-only).

## 9. Proposals

`create_proposal(operations, base_revision)` accepts aux-authored operations only
(`actor_origin="aux"`; anything else is `actor_mismatch`), validates them structurally (unknown
span, protected/pinned, group completeness, rewrite limits), bounds them by `max_aux_spans`
(`too_many_aux_spans`), and stores an immutable record keyed by
`proposal_id = "prop_" + sha256(canonical({base, origin, operations}))[:24]`, with
`proposal_sha256` over the same canonical payload. Creating a proposal never changes the head and
never applies anything.

`accept_proposal(proposal_id, proposal_sha256, base_revision, txn_id)` requires the exact hash and
base revision (`proposal_hash_mismatch`, `proposal_base_mismatch`), and refuses a proposal whose
base is no longer the head (`stale_proposal`). Acceptance rewrites each operation with
`actor_origin="agent"` and provenance carrying `origin="aux"`, `proposal_id`, `proposal_sha256` —
the content stays untrusted aux text — and **only stages**: an explicit `commit` is still required.

## 10. Integrity, corruption and recovery

Two independent chains detect tampering or interruption:

1. **Archive chain** — `archive.entry_hash = sha256(canonical({prev, span_id, content_sha256,
   payload_sha256, revision}))`; `prev_hash` links to the previous row. Re-hashing one forged row
   breaks the next link.
2. **Revision chain** — §4. Any edit to a committed operation changes the head hash and every later
   one.

Plus per-span payload hashes (`sha256(canonical_json(message_json)) == content_sha256`).

Verification levels (deliberate cost/coverage trade):

| Surface | Checks | Cost |
| --- | --- | --- |
| `snapshot()` | head revision hash (2 queries) | O(1) per call |
| `snapshot()` with `snapshot_verify_hashes=True` | + payload hash of every current span | O(spans) |
| `retrieve(span_id)` | payload hash of that span | O(1) |
| `status()` | head revision hash + counts | O(1) |
| `verify()` | `PRAGMA integrity_check`, full archive chain, every payload hash, every revision hash, checkpoint-vs-replay consistency | full scan |
| `recover()` | `verify()` + checkpoint rebuild + stale-txn expiry + index rebuild | full scan |

Corruption policy: **never guess**. `verify()`/`recover()` report problems; payload tables are not
rewritten (triggers forbid it). A structural problem means the caller must treat the store as
untrustworthy (projection returns `None` rather than a subset). Reading a corrupt payload raises
`CorruptStoreError(payload_hash_mismatch)` with the span id.

Recovery steps (`recovery.recover(store)`, `recovery.recover_session(home, session_id)`):

1. `PRAGMA integrity_check`.
2. Full `verify()` (archive chain, payload hashes, revision chain).
3. Rebuild `span_state` if it diverges from replayed operations (`rebuilt_span_state`).
4. Expire staged transactions older than `stale_txn_ttl_seconds` (`expired_staged_transactions`) so
   a crash between `stage` and `commit` cannot be replayed later.
5. Remove `.<active.json>.<pid>.tmp` files left by an interrupted index write
   (`removed_stale_index_tmp`).
6. Rewrite `active.json` from SQLite (`rebuilt_index`).
7. Repair a schema interrupted mid-creation (`schema_repaired`): `recover_session` notices
   `schema_incomplete`/`schema_missing`, runs `CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS` in a
   transaction and reopens. Existing rows are never dropped.

`RecoveryReport` carries `ok`, `head_revision`, `actions`, `problems`, `corrupt_spans`,
`discarded_txns`, `index_state`, `schema_repaired`, `state_rebuilt`.

## 11. Derived index (`active.json`)

`{format, db_id, session_id, revision, head_hash, counts, row_hashes{spans,operations,revisions},
rows{spans,operations,revisions}, generated_at}`, written atomically (temp file + `os.replace`,
`0600`) after every ingest/commit and best-effort: a failed write sets `store.index_state="failed"`
and never fails the commit. It contains **no message payloads** and no absolute paths.

Readers validate `format`, `db_id` and `session_id`; anything else is treated as missing and rebuilt
from SQLite. `index_status()` reports `ok`/`stale`/`rebuilt`/`missing_or_corrupt`, and always
`authoritative: False`. Read-only handles never write it.

## 12. Bounded retrieval

| Config (default) | Bound |
| --- | --- |
| `max_retrieve_bytes` (65536) | `retrieve` payload size; over limit returns `ok=False` with `retrieve_too_large` instead of silently truncating |
| `max_rewrite_bytes` (16384) | rewrite replacement size |
| `max_aux_spans` (32) | spans touched by one aux proposal |
| `max_operations_per_txn` (128) | operations per stage/commit |
| `max_operations_in_snapshot` (500) | operations carried in a snapshot (most recent, `operations_truncated=True`) |
| `max_query_chars` (512) | `search` query length |
| `inspect_limit_max` (200) / `search_limit_max` (100) | page size ceilings; larger requests are clamped |
| `stale_txn_ttl_seconds` (3600) | staged-transaction age before recovery expires it |
| `retention_sessions` (32) | declared retention for operator purge; the store never deletes automatically |

All read surfaces are strictly session-scoped — they take no session argument and cannot address
another store. `inspect` returns ≤ `limit` spans with 240-char previews; `search` returns snippets
and opaque cursors (base64 `{v:1, after:seq}`) that are rejected if tampered; `retrieve` returns the
redacted archived message, hashes, state and (for offloaded spans) the stub. Dropped/offloaded
payloads remain retrievable: that is the point of offload.

## 13. API

Exact contract signatures:

```python
ContextStore.for_session(hermes_home, session_id, *, read_only=False) -> ContextStore
ingest(messages) -> IngestResult
snapshot() -> StoreSnapshot
stage(operations, *, expected_revision, txn_id, actor_origin) -> StageResult
commit(*, txn_id, expected_revision) -> CommitResult
discard(*, txn_id) -> None
revert(*, target_revision, expected_revision, txn_id, actor_origin) -> CommitResult
status() -> dict
inspect(*, cursor=None, limit=50) -> dict
search(query, *, cursor=None, limit=20) -> dict
retrieve(span_id) -> dict
create_proposal(operations, *, base_revision) -> Proposal
accept_proposal(*, proposal_id, proposal_sha256, base_revision, txn_id) -> StageResult
close() -> None
```

`models.py` value types: `StoreSnapshot(revision, operations, spans, pins, head_hash, …)`,
`ArchivedSpan(span_id, uid, aliases, message, group_id, origin, protected, content_sha256, …)`,
`EditOperation(kind, span_ids, replacement, actor_origin, provenance, …)`, `Proposal(proposal_id,
proposal_sha256, base_revision, operations, origin="aux", …)`, `IngestResult`, `StageResult`,
`CommitResult`, `Receipt`. All frozen; mappings are read-only proxies, sequences are tuples;
`copy.deepcopy` returns the same object. Results are JSON-serializable via `to_dict()`, carry no
live connections and no paths outside the store (`status()["store_key"]` is the sanitized directory
name only).

### Additive API surface beyond the integration contract

Additions, not replacements — nothing above was changed:

* `for_session(..., config=StoreConfig | Mapping | None)` keyword: limits are injected (the engine
  owns `clm.*` parsing; the store never reads Hermes config). Unknown config keys raise
  `ValidationError(unknown_config)`.
* `unpin(span_id, *, expected_revision, txn_id, actor_origin)` and
  `operator_unpin(span_id, *, expected_revision, txn_id)` — the operator unpin path the manual
  command needs. `pin_requires_operator(actor_origin)` is available as a guard for tool layers.
* `verify()`, `rebuild_state()`, `rebuild_index()`, `index_status()`,
  `expire_staged_transactions(*, ttl_seconds=None)`, `ContextStore.list_sessions(home)`,
  `ContextStore.purge_session(home, session_id)`, plus `recovery.recover`, `recover_session`,
  `verify_session`, `rebuild_index`.
* `ArchivedSpan` extra fields: `role`, `editable`, `present`, `current`, `state`, `rewrite_text`,
  `rewrite_sha256`, `pinned`, `est_tokens`, `version`, `seq`, `created_revision`, plus
  `effective_message()` / `stub()` helpers. `StoreSnapshot` extras: `aliases`, `counts`,
  `est_tokens`, `operations_truncated`, `session_id`, `db_id`.
* `models.offload_stub(span_id, content_sha256)` — the deterministic stub text the projection should
  use so store and engine agree byte-for-byte.
* `IngestResult` also exposes `applied`, `inert`, `receipts` (always empty, since ingestion edits no
  projection state) so generic consumers can read any result type the same way.
* `transaction` semantics addition: `revert` and `unpin`/`operator_unpin` also replay an existing
  `txn_id` (signature-checked).

Deviations worth flagging to integration:

* `retrieve` returns `{"ok": False, "errors": [{"code": "retrieve_too_large", …}]}` instead of
  raising for oversized payloads (tools already branch on `ok`).
* `ingest` treats its batch as the complete current history by default (§5.2).
* `stage` on an already-committed `txn_id` returns `StageResult(already_committed=True)`.
* This branch does not create `src/hermes_clm/__init__.py` (owned by the tools delegate); imports
  work through the namespace package. Delivery's packaging config must include
  `hermes_clm` as a namespace/`find` package.
* A rejected `commit` (e.g. `stale_stage`) leaves the transaction staged; the caller must `discard`
  or re-stage. Nothing is auto-discarded except by TTL expiry in recovery.

## 14. Token accounting

`est_tokens` is `ceil(bytes / 4)` per payload (`TOKEN_ESTIMATOR = "chars/4"`), recorded per revision
in `revisions.est_tokens_before/after`, in `snapshots.est_tokens` and in `tokens` rows (aggregate
`span_id IS NULL`, plus one row per applied span edit with its delta). This is a **rough store-side
accounting unit** for receipts and prefix-reuse analysis — it is not the engine's calibrated
estimate. Tools should expose the engine's estimate and label the store figure as approximate.

## 15. Integration notes

Engine (`feat/engine`):

* Call `ingest(history)` once per iteration with the full host history, then `snapshot()`; both are
  cheap (`snapshot` caches per generation and verifies only the head hash).
* `snapshot.spans` maps `span_id → ArchivedSpan` for every **current** version (historical versions
  are reachable through `snapshot.aliases` / `snapshot.resolve`); use `snapshot.active_spans()` for the
  seq-ordered active tuple, `snapshot.span(id)` for an alias-resolving lookup. `snapshot.aliases`
  maps absorbed ids to current ones; `snapshot.resolve(id)` does the same. Use
  `ArchivedSpan.state`/`rewrite_text`/`effective_message()` or implement the same rules — either
  way the store's `retrieve`/`verify` must agree.
* The store never aligns request rows, never inspects the provider request, and knows nothing about
  protection of the *newest* tool result (engine-side rule). Store-side protection is role/structural
  only.
* `CorruptStoreError` from `snapshot()`/`retrieve()` ⇒ return `None` from projection; do not build a
  subset.

Tools (`feat/tools`):

* Handlers must pass `actor_origin="agent"`; `EditOperation.from_mapping` rejects unknown fields, so
  reject unknown tool arguments before constructing operations.
* `stage` then `commit` are separate calls on purpose: never auto-commit an aux proposal.
* `pin` is available to agents; there is no agent unpin. Operator commands call
  `operator_unpin(...)`.
* Pass `txn_id` from the tool call (idempotency key) and `expected_revision` from `status()`.

Delivery (`feat/delivery`):

* `hermes_clm` must be packaged as a namespace package (no `__init__.py` here) and `src/` must be on
  the path for tests; `tests/unit/` uses stdlib `unittest` only (`python -m unittest discover -s
  tests/unit -p 'test_*.py'`), no third-party test dependency required.

Security (`test/security`):

* Permitted, not forbidden: agent pin/commit/revert, stage→commit idempotent replay, operator
  unpin. Adversarial cases to target: alias/version confusion, cross-session id probing,
  symlinked store paths, trigger removal (must be *detected* by `verify()`), stale proposal
  acceptance, `txn_id` reuse with different payloads, oversized retrieval.

## 16. Verification coverage

`tests/unit/` is stdlib `unittest` only; run it from a clean checkout with no install step:

```sh
cd tests/unit && python3 -m unittest discover -s . -p 'test_*.py'
```

| Module | Covers |
| --- | --- |
| `test_store_models.py` | canonical JSON/hash stability, strict operation parsing, read-only value types |
| `test_store_paths.py` | session-key derivation/safety, symlink refusal, `0700`/`0600`, isolation |
| `test_store_ingest.py` | redaction at rest, uid/content dedupe, alias chains, no caller mutation/aliasing, protections |
| `test_store_ops.py` | stage/commit/discard, CAS (`stale_revision`/`stale_stage`), replay, group atomicity, authority |
| `test_store_revisions.py` | monotonic revisions, revert-as-new-head with no truncation, pins surviving revert |
| `test_store_triggers.py` | append-only and protected-row triggers abort at SQL level, STRICT typing |
| `test_store_proposals.py` | immutable proposals, hash/base pinning, never auto-committed |
| `test_store_index.py` | `active.json` shape, tracking, self-healing, never authoritative |
| `test_store_locking.py` | WAL/pragmas, private modes, single writer, concurrent CAS loser |
| `test_store_retrieval.py` | bounded inspect/search/retrieve, cursor tamper rejection |
| `test_store_api_contract.py` | exact documented signatures/result fields; closed-handle refusals |
| `test_archive_redaction.py` | every redaction rule, idempotence, archive hash chain verification |
| `test_recovery_crash.py` | `os._exit` crash mid-stage/commit/ingest: WAL rollback, no partial rows, stale-txn expiry |
| `test_recovery_corruption.py` | tamper detection, corrupt-open errors, derived-state rebuild, temp cleanup, head repair |

Crash tests use a child process (`store_crash_child.py`) that dies with `os._exit()` inside the
library call, so the assertions are about real SQLite recovery, not simulated exceptions.

## 17. Known limits

* Same-UID shell access can drop triggers, rewrite files or read `0600` files; hash chains make
  tampering visible but are not tamper-proof.
* No cross-session or child→parent reads: the store is per session by construction.
* Presence tracking cannot distinguish "host deleted the message" from "caller passed a partial
  batch" unless `assume_full_history` matches the caller's actual behaviour.
* Schema migrations: only version 1 exists; a newer `schema_version` is refused
  (`schema_too_new`) rather than guessed at.
* `retention_sessions` is a declared limit for the operator purge tooling; the store never
  deletes sessions on its own.
* There is no per-message size cap: a single archived message is bounded only by caller memory and
  SQLite limits. Host history size is the host's contract; redaction bounds neither size nor count.
  Rewrites (agent-authored text) *are* bounded by `max_rewrite_bytes`.
