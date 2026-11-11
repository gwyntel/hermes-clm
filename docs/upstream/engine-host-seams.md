# Engine ↔ host seams

What the engine layer guarantees to the Hermes host and to the plugin shim, and
where a seam is still carried by another delivery unit.

Verified against the host checkout at `agent/context_engine.py`
(`ContextEngine`): the engine subclasses the ABC when a checkout is on
`sys.path`, and `test_engine_subclasses_the_real_host_abc_when_a_checkout_is_available`
fails if a host signature drifts.

## Configuration

- `CLMEngine(config=...)` accepts the flat `clm.*` mapping that
  `plugin.build_engine()` passes. `EngineConfig.from_mapping` normalises it, and
  the raw mapping is preserved for `ToolLimits.from_config`, so a declared key
  the engine does not interpret still reaches the tool layer.
- Model, provider, and credential keys are rejected
  (`clm config accepts no model/provider keys: [...]`). The engine never selects
  a model; `update_model()` only recomputes `threshold_tokens`.

## Session scoping

- `service_for_session(session_id=None)` returns a `ToolService` for this
  engine's session, or for the first explicit session id an unbound engine is
  handed. Any other session returns `None` — a subagent clone can never read its
  parent's archive, and an engine that has no session yet returns `None`
  (`no_session`) rather than a detached service.
- The store stays lazy: `default_store_factory` imports `hermes_clm.store` on
  first use, so `hermes_clm.engine` imports without the store delivery unit, and
  a store-open failure is recorded and degrades to detached mode instead of
  breaking the host turn.
- `clone_for_agent()` builds a fresh engine: no store handle, `session_id=None`,
  frozen config/factory/`AuxCompressor` shared by reference, mutable token and
  context metrics copied. No `deepcopy`, no shared SQLite connection.

## Tool dispatch

- `get_tool_schemas()` returns the `clm_*` schemas from
  `hermes_clm.tools.build_tool_specs()`; a failure yields `[]`, never an
  exception.
- `handle_tool_call(name, args, messages=...)` returns a JSON **string**, as the
  host requires, for every path including unknown names and bad arguments. The
  call is dispatched through `ToolHandlers`, which is bound to this engine's
  scoped service; a foreign `session_id` becomes `no_session`.

## Overflow

- Overflow is a durable, `system`-actor offload: `OperationSpec.actor_origin` is
  `"system"`, transactions are `overflow-<hex>`, and offload operations carry
  store span ids (not host uids) because the store resolves span ids after
  archive supersession.
- Pin snapshots arrive as `Mapping[span_id, PinRecord]`; both the engine and
  `projection._pin_ids` accept that shape as well as structural test snapshots,
  so a pinned group is never offloaded.

## Remaining seam — proposal digest

The store and the auxiliary layer hash the same proposal differently:

- store: `sha256(canonical_json({"base", "origin", "operations": [op.to_payload()]}))`
- auxiliary: `sha256(canonical_json({"base_revision", "operations", "origin"}))`
  over plain operation dicts (`canonical_proposal_sha256`)

The two can never agree, so a hash produced by one side is rejected by the
other. Until the store and `auxiliary.py` settle on one formula, `ToolService`
bridges both: `create_proposal()` returns the aux canonical digest as the
reviewed `proposal_sha256`, carries the store's digest as
`store_proposal_sha256`, and `accept_proposal()` accepts either — translating
the aux digest to the recorded store digest for exactly the operations it
created, in the same engine. Both digests are deterministic over identical
operation content, so no forgery window opens; the stored record and the audit
trail keep the store's digest.

This bridge is process-local. A proposal created before a restart, or by a
different engine instance, must be accepted with the store's digest.
