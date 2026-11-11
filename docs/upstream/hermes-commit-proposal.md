# Optional upstream proposal: a native transcript-commit hook for context engines

Status: **proposal, not a dependency.** Nothing in this repository requires it.
`hermes-clm` edits the *request*, not the transcript, and does not need Hermes to
change for v0.1.

Verified against Hermes `agent/` at
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` (v0.23.0 lineage). Line numbers are
from that checkout and will drift.

## Why the current seam is enough

`ContextEngine.select_context()` (`agent/context_engine.py:121`) runs once per
provider-bound iteration from `assemble_api_request()`
(`agent/turn_request_assembly.py:106` → `_apply_context_engine_selection()`,
`agent/conversation_loop.py:1351`). It receives structural clones of the request
and of the UID-bearing history, and returns either a replacement list for *that
call only* or `None`.

Consequences that make request-level CLM correct without upstream changes:

- An edit committed mid-turn changes the **next** request, because the engine
  re-reads its committed revision on every call.
- The returned list never reaches the transcript, so there is no re-anchoring,
  no marker bookkeeping and no chance of corrupting session state.
- Unchanged revision plus unchanged history yields byte-identical requests, so
  the provider prefix cache stays warm.
- The host strips persistence-only fields from the request copy before the hook
  (`agent/turn_context.py`, `PERSISTENCE_ONLY_MESSAGE_FIELDS`), and re-strips the
  returned list (`without_persistence_fields`), so an engine cannot smuggle
  `message_uid` onto the wire.

Consequence that motivates this proposal: the *persisted* transcript keeps the
original bytes. Every turn re-sends and re-projects them, and a host-side
compaction (or `/compress`) still walks the raw rows. That is a performance and
UX limit, not a correctness one.

## What a native commit closure would have to do

A `persist=True` mode would let an engine replace durable history rows for its
session. The host steps below are load-bearing; skipping any one of them
produces divergence that shows up turns later:

1. **Re-anchor the turn split.** `current_turn_user_idx` and
   `_persist_user_message_idx` address rows positionally; a commit that removes
   or rewrites rows before the current user message invalidates both. The host
   must re-resolve them by `message_uid`, never by index arithmetic.
2. **Restamp sidecars.** `api_content` records the exact bytes last sent for a
   row and overrides `content` on replay (`agent/turn_context.py`). A commit must
   either update or drop the sidecar for every rewritten row - a stale sidecar
   silently re-asserts the pre-edit bytes.
3. **Rewrite persistence markers.** `_last_flushed_db_idx`, the compaction
   marker, and the in-memory `_session_messages` mirror must all agree with the
   committed list, or the next flush re-inserts edited rows.
4. **Reset usage anchors.** `last_prompt_tokens` and the "provider proved this
   prompt fit" deferral state describe the *previous* prompt. A commit must
   invalidate them, exactly as a model switch does, or preflight compresses a
   request that no longer exists.
5. **Reset retry state.** In-flight retries hold a prepared request built from
   the old list; a commit between attempts must rebuild it or be deferred to the
   next iteration boundary.
6. **Choose in-place vs rotation.** In-place (`agent.compression_in_place`)
   keeps the session id; rotation writes a new session with `old_session_id`
   lineage. A commit must declare which one it performs and let the host finish
   the rotation bookkeeping.
7. **Respect the writer contract.** One writer per session DB, WAL,
   `BEGIN IMMEDIATE`, a CSI/lease so gateway eviction and a concurrent CLI cannot
   interleave. `compress_context()` (`agent/conversation_compression.py:3985`)
   already takes that lease; a commit hook must run inside it, not alongside it.

## Shape worth considering

Prefer an explicit opt-in return type over a boolean parameter, so a malformed
return can never be mistaken for a persisted edit:

```python
@dataclass(frozen=True)
class SelectedContext:
    messages: list[dict]  # request-only replacement
    persist: bool = False  # ask the host to commit these rows
    expected_uids: tuple[str, ...] = ()  # alignment assertion, host-side check
```

Host handling: if `persist` is set, the host re-runs the persistence path with the
returned rows as a new generation, applying steps 1-7, and refuses the commit
(using only `messages` for the request) when `expected_uids` does not match the
durable rows it is about to replace. A commit failure must never fail the
request.

An engine-side capability probe (`ContextEngine.supports_transcript_commit`)
keeps old engines working: absent or `False` means the host never persists.

## Risks and non-goals

- **Blast radius.** Request-level editing is contained by the host's existing
  fail-open wrapper. A persisted edit touches session DB, rotation, markers,
  usage anchors and the commit fence at once.
- **Two words for truth.** Once commits exist, the store and the session DB both
  claim authority over content. The store must stay the source of projected
  content (operations, not rewritten rows), and the commit must be a derived
  materialization that can be rebuilt after a crash.
- **Cache stability.** A commit invalidates the prefix from the earliest changed
  row. It must be batched at task boundaries and never on a per-iteration whim.
- **Not in scope.** Child→parent retrieval / lineage-aware reads (needs host
  lineage, not a commit hook), cross-session search, aux model routing, and any
  change to redaction or provider sanitizers.

## Recommendation

Do not pursue this for `v0.1.0-rc.1`. Ship the request-level engine
(`docs/adr/0001-request-projection.md`, decision 12 and "Why not native transcript
commits"), measure the cost of re-projecting unchanged history, and revisit only
if that cost dominates. If it is pursued later, treat the seven host steps above
as the acceptance criteria and keep the engine-side commit strictly optional.
