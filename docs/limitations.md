# Known limitations

This project publishes its limits with the same prominence as its features. If a claim is
not enforceable, it is listed here rather than implied by the architecture.

## Current gates

Measured locally on macOS/CPython 3.14 with `CLM_HERMES_CHECKOUT` set. `v0.2.0` is a
GitHub release carrying wheel and sdist assets; no PyPI publication is claimed. The table
records the last measured run rather than a promise about the current branch — re-run the
commands to get today's totals, and never quote a number from here as a fresh measurement.

| Gate | Command | Result |
|---|---|---|
| Format | `ruff format --check .` | pass (129 files) |
| Lint | `ruff check .` | pass |
| Types | `mypy` | pass (19 modules) |
| Build | `python -m build`, `twine check dist/*` | pass (sdist + wheel) |
| Bandit | `bandit -q -r src -ll` | pass; 0 medium/high, 3 accepted Low (item 44) |
| Tests | `pytest -o addopts='-ra --import-mode=importlib'` | 867 passed, 3 skipped, 0 failed with `CLM_HERMES_CHECKOUT`; 836 passed, 34 skipped without it |
| Coverage | `pytest --cov=hermes_clm` | 87.07% branches, floor 85% |
| Security + fuzz | its own suite | all pass; mutation baseline green, 16/16 injected defects red |
| Benchmarks | offline simulator, two runs and CPython 3.11/3.12/3.14 | byte-identical JSON; simulator only, no model calls |

The three skips are expected and explicit: two media-blob probes do not apply to inline
archives, and one tool-schema probe has no module-level JSON schema object. Without a Hermes
checkout, 31 further real-discovery, prompt-section, dual-lane and lifecycle tests skip.

The format/type/test rows were re-measured locally after the dual-enablement and `keep`
work; no remote CI run covers those changes yet, so treat the row values as local until one
does.

Resolved during integration: the `commit()`-without-`stage()` test adaptation, the
`model`→`agent` actor vocabulary, hashed-but-stable session IDs, `EngineConfig` mapping
construction, mutation-fixture isolation, the missing `expect_refusal` import, the
intermittent CAS-loser corruption error, the duplicate `clm_*` tool surface under dual
enablement, and `keep` as a documented-but-unprojected reactivation. The CAS one is now
covered by a non-`xfail` test.

## Host and platform

1. **No supported mid-loop transcript commit.** Hermes' `select_context` is request-only;
   the persisted transcript is not rewritten. The engine therefore projects committed
   operations onto each request copy. A native commit closure would need a host change; the
   engine branch carries the proposal, and it is not required for this design.
2. **`api_content` wins over edited historical content.** For already-sent user/assistant
   rows, the host's replay sidecar overrides an edited `content`. Any future native commit
   must drop or restamp that sidecar, and clear the per-row persistence marker, or the edit
   is lost on restart.
3. **Request rows have no stable UID.** `message_uid` is stripped from the wire. Projection
   uses an ordered subsequence alignment on role, tool-call id and expected wire content;
   ambiguity fails the whole projection rather than guessing. A canonicalised prefix can
   make an operation inert.
4. **`compression.enabled: false` disables plugin engines.** The host gates engine
   selection and preflight on that switch. The engine cannot override it, and security
   decisions must not be routed through `compression.*`.
5. **Engine tools depend on the `context_engine` toolset.** If an explicit platform toolset
   list omits it, or `agent.disabled_toolsets` removes it, the model has no verb to stage or
   commit operations and the engine degrades to identity projection. Surface this loudly.
6. **`bind_session_state` is a convenience, not the session's authority.** The engine
   implements it (the gateway builds a one-off engine for a cold `session.mutate(compress)`
   and binds `session_db` + `session_id` before the forced `compress()`), but the host does
   not call it on every path, it is absent under `plugins.isolation: host`, and a failure is
   swallowed. The engine therefore treats it as an early hint: it adopts an id only while
   unbound, ignores the host database entirely, and still resolves `hermes_home` lazily at
   store-open time from `on_session_start`, then `clm.hermes_home`, then the host's
   `get_hermes_home()`. A session that never gets either hook refuses with `no_session`
   rather than opening a store somewhere arbitrary.
7. **Host isolation taxes per-request hooks.** Under `plugins.isolation: host`, every
   `select_context` carries the transcript across an RPC boundary. `in_process` is the
   supported mode unless measured otherwise.
8. **Lineage comes from the host's compression notification, not from a session field.**
   `on_session_start` and `bind_session_state` never pass `parent_session_id`, so the only
   lineage the engine records is the rotation it is told about:
   `on_session_start(new_id, boundary_reason="compression", old_session_id=old_id)` where
   `old_id` is the session this engine actually serves. Guessing lineage from
   `conversation_id`, from directory order, or from "the newest store" is forbidden, and no
   tool argument can name a parent.
9. **Wire sanitizers repair silently.** Hermes drops unpaired tool results and stubs
   unanswered calls after selection. Group atomicity is validated in the store and
   projector, but a host-side assertion is still requested so a bad projection fails
   instead of degrading.
10. **Rollback and manual compression semantics differ from the paper.** `should_compress`
    is false in steady state; overflow and manual `/compress` are the only paths that
    return a new list, because Hermes scores shrink on the live list and fails the turn if
    an engine returns identity during provider-proven overflow. Manual compaction takes the
    committed projection when it already saves tokens, and otherwise commits a
    deterministic, archive-backed offload of the oldest unpinned tool groups until
    `clm.manual_target_ratio` (default 0.2) of the projection is freed. It never
    substitutes a prose summary for CLM state, never calls a model, and never reports
    success when nothing shrank: a session with nothing offloadable answers
    `manual_no_offloadable_groups` instead of "no changes from compression".
11. **`/compact` cannot reduce a session with nothing to offload.** Tool groups are the only
    offloadable unit, so a transcript of pure prose (no tool results, or all groups pinned
    or already offloaded) is unchanged and says so. Freeing tokens from that shape would
    require dropping or summarising user/assistant content, which this design refuses.
12. **`focus_topic` is accepted and ignored.** The host passes it to `compress()`; a
    deterministic offload has no topic to steer by, and inventing one would make the same
    store produce different results per invocation.
13. **A rotation is durable, but only once it has been written.** The host's post-commit
    notification adopts the parent's store in the live process (same handle, same
    revisions, pins and archive), and the engine also writes the relation to
    `<hermes_home>/clm/lineage/<session key>.json` (owner-only `0600`, atomically
    replaced). A cold process — the gateway's one-off engine per `/compact`, or the next
    turn after a restart — resolves the child id through that record to the same archive,
    transitively, so revisions, pins and committed offloads survive. What remains:
    * a record exists only for rotations made **after** this build; a child id rotated
      before the upgrade still starts a fresh lineage;
    * the record is per Hermes home, so a profile pointed at another home does not see it;
    * a failed write leaves the live process working (the adoption already happened) and
      sets `last_lineage_error` in `/clm status`: the child is then empty, and the status
      says why instead of pretending;
    * a rotation whose `old_session_id` is not the session this engine serves (or that
      follows `on_session_reset`) is treated as a new conversation: `/new`, `/resume` and
      `/branch` never inherit an archive.
14. **"A child reads its parent's archive" means it *is* the same archive.** The child id is
    an alias for one store; there is no second copy and no cross-store read. An unrelated
    session — including a subagent whose id was never the subject of a compression
    rotation — still cannot reach it: `service_for_session` refuses any id but the engine's
    own, and `ContextStore.for_session` resolves only recorded lineage.
15. **A long-lived host keeps the modules it loaded.** The gateway (and any `-z` client
    work, which always goes through the gateway) does not re-import plugin modules on
    `/reload` or on config reload: the process that has been running since before a fix
    keeps serving the old `hermes_clm` modules until it is restarted by the operator. The
    symptom of a stale load is a `/compact` that behaves like the previous build (for
    example a bare "no store" with no typed code, or a projection-only no-op). `hermes-clm
    status` reporting `engine_version` is the fastest way to see which build is live;
    restarting the gateway is the operator's decision, never something a plugin does.

## Store and security

16. **Same-UID code can read and rewrite the store.** A shell running as the agent's user
    bypasses every tool-scoped rule. Permissions, CAS, idempotency and checksums are
    authority and corruption controls, not confidentiality controls.
17. **At-rest encryption is not a control.** The plugin shares the model's process; any key
    it can read, a same-UID process can read. No encryption claim is made.
18. **The hash chain is unanchored unless the host supplies a sink.** Unanchored, it detects
    corruption and interrupted writes; it does not make tampering impossible, because the
    whole chain is rewritable by the same user. Reports must use that wording.
19. **The archive is a durable secret-retention surface.** Redaction at ingest, bounded
    retention and a purge command reduce the risk; redaction is best-effort and may miss a
    secret in an unusual format. Before this feature, trimming context was a deletion path
    for a leaked token; an append-only archive removes that accidental protection.
20. **Prompt injection is not prevented.** The guarantee is that injected text never gains
    operator or system authority, never changes pins and never authorises a side-effecting
    tool. Whether a model is persuaded by its own history or by tool output is not
    solvable at this layer.
21. **Only *unpin* is operator-authority.** Agent tools can pin (`clm_pin`) and can stage
    and commit edits (`clm_stage`/`clm_commit`); the operator-only verb is `unpin`
    (`/clm unpin`, `store.unpin` with `actor_origin="operator"`), which refuses an agent
    caller with `pin_authority`. A pin is therefore agent-supplied but operator-released:
    treat "pinned" as "the agent asked to keep this", not as an operator endorsement.
22. **Path validation cannot stop an attacker with filesystem access.** Component-wise
    `lstat`, realpath containment, owner checks, `nlink == 1` and hash verification stop
    traversal and symlink swaps; they do not stop a process that can `sqlite3` the file.

## Projection

23. **Alignment is heuristic for a canonicalised prefix.** Rows the host drops, synthesises
    or rewrites cannot always be matched. The failure mode is an inert operation, never a
    malformed request, but an operation can silently not apply.
24. **Dropping an assistant row between user rows lets the host merge them.** Protected user
    text survives as concatenated content, not as separate rows.
25. **Rewriting a row breaks prefix-cache reuse from that point.** This is the accepted
    cost of mid-context edits; the prompt must teach batching and late edits.
26. **Multimodal and unknown part types are refused, not flattened.** Refusal is deliberate:
    flattening loses provenance and can turn an image into untrusted text.
27. **Tool-call fidelity is preserved only for unedited groups.** A rewritten group keeps
    call/result ids; an edited history is not reconstructed from stored bytes.

## Benchmarks

28. **The offline harness is a simulator.** A/B are policy models, not Hermes; C/D are
    reference policies, not the shipped engine. No number is a task-accuracy result.
29. **Token counts are estimated at four characters per token.** They are not a provider
    tokenizer and do not capture tokenizer-specific behaviour or reasoning-token stripping.
30. **Cost and latency are synthetic.** Prices are illustrative list prices; latency uses
    fixed per-token constants. Neither is a bill or a wall-clock measurement.
31. **C and D pass their own preservation checks by construction.** The fixtures declare
    `FACT[...]` lines and the CLM policies explicitly carry them. The run demonstrates
    accounting and policy differentiation, not efficacy.
32. **No concurrency or real crash injection.** The harness simulates a restart by rebuilding
    from raw history and the committed operation log. The security suite owns real
    concurrency, kill, corruption and known-bad mutation tests.
33. **Injection containment is structural only.** It checks provenance elevation and system
    preservation; it cannot measure persuasion.

## Scope and expectations

34. **CLM-style gains are model-conditional.** Published results show weaker models losing
    to a summary harness and editing rarely. Do not advertise gains for mid-tier models
    without qualification, and do not build a scheduled auto-compactor and call it CLM.
35. **Suffix cache reuse is out of scope.** It requires an SGLang patch or vLLM scheduler
    subclass plus a KV connector, is approximate, and is not reachable through an
    OpenAI-compatible gateway. The plugin cannot buy the paper's server-side savings.
36. **RL, skill evolution and trajectory-format work are out of scope for v1.** They are
    separate systems with separate dependencies and licences.
37. **Upstream drift.** Host line numbers were verified against Hermes v0.23.0 and a main
    snapshot; Hermes moves. Re-verify the load-bearing hooks before claiming compatibility.

## Release and CI

38. **No PyPI project or pending publisher exists yet.** The publish workflow is disabled
    unless repository variable `PYPI_PUBLISH_ENABLED` is `true` and the `pypi` environment
    exists with a trusted publisher configured for `publish.yml`. GitHub releases and their
    artifacts do not depend on PyPI.
39. **CI results are local until pushed.** The workflows are committed but no remote run has
    been observed from this branch. Do not report a green CI run that has not happened.
40. **Only CPython 3.12 was exercised locally for the full delivery.** 3.11, 3.13 and 3.14
    are CI-only for the suite; 3.14 was used locally only to confirm the benchmark JSON is
    byte-identical. The package uses no version-specific syntax, but the matrix is the
    proof.
41. **Release Please owns version bumps.** Hand-editing `pyproject.toml` or `_version.py`
    without the release PR breaks the single-source contract; the packaging test fails if
    they disagree. Conventional Commits drive the next version, and `v0.1.0`/`v0.2.0`
    both shipped that way.
    No PyPI publication is claimed. `v0.2.0` is a GitHub release whose assets are the
    wheel and sdist; the publish workflow stays disabled until the `pypi` environment and
    trusted publisher exist, and release-please owns the version bump either way.
42. **The benchmark harness is not in the wheel.** `hermes-clm bench` and
    `python -m benchmarks` require a source checkout (a git clone or an unpacked sdist);
    the wheel excludes `benchmarks/` and `tests/` on purpose. The installed CLI reports the
    missing harness and exits nonzero.
43. **The coverage floor is 85%, not a target.** The current full-suite measurement under a
    Hermes checkout is 87% branches over a green suite (867 passed, 3 skipped); the floor
    stays where it is so an unrelated refactor cannot quietly delete coverage, and raising
    it is a follow-up, not a claim of completeness. Coverage is measured in the test job;
    the build job does not re-measure it.
44. **The mutation harness is a separate CI gate.** It re-runs probe tests in subprocesses
    with a fixture implementation on `sys.path`; it has its own job so a broken harness
    cannot be mistaken for a broken suite. It passes locally (16/16 injected defects go
    red); the harness only sees `src/` through that fixture, so a mutation in a module the
    fixture does not import survives — coverage of the harness is not coverage of the
    package.
45. **Bandit gates at medium severity.** Three Low findings are accepted and recorded:
    `engine.py:827` swallows a logger failure (`B110`), `store.py:108` names the estimator
    constant `"chars/4"` (`B105`, a false positive), and `store.py:1894` asserts an
    internal invariant (`B101`, which disappears under `python -O`). The `B101` one is a
    real robustness question for the store owner, not a style nit.

## Model guidance (system-prompt section)

46. **The engine-only load path carries no guidance.** Hermes' directory-loaded engine
    collector (`plugins/context_engine/_EngineCollector`) has no
    `register_system_prompt_section` API, so a profile that only sets
    `context.engine: hermes-clm` gets the engine and the twelve tools but no instructions
    for using them. The registration is `getattr`-guarded and the plugin loads normally;
    enabling the plugin through `plugins.enabled` is what adds the section.
47. **The section is frozen per session and cannot be updated in place.** The host renders
    it at session start and restores those exact bytes on resume, so editing the guidance
    affects new sessions only. It is also static: it cannot report the current revision,
    pressure or pinned spans, and advice that turns out to be wrong cannot be corrected
    mid-session — the tools' own receipts carry the live state.
48. **Guidance follows engine selection, not per-platform toolset gates.** The section is
    registered at plugin load, before `enabled_toolsets`/`agent.disabled_toolsets` are
    resolved. An explicit platform toolset that omits `context_engine` leaves the model
    with instructions for tools it cannot call (the same gap as item 5, seen from the
    prompt side). A `context.engine` change needs a restart **and** a new session: the
    host selects the engine at agent initialisation, and the prompt bytes are already
    frozen.

## Plugin enabled and engine selected

49. **Dual enablement requires the selected engine to own the tools.** The canonical
    profile sets both `plugins.enabled: [hermes-clm]` and `context.engine: hermes-clm`.
    They are not interchangeable: the plugin entry is the only lane with a system-prompt
    section API, and the selected engine is the only lane that can bind the session store.
    When both are present the general-plugin lane detects the selection (from its injected
    `engine_selected` fact, else the profile's `context.engine`) and registers the section
    only — no tools, no `/clm`, no engine object. Registering the tools there would win the
    host lookup over the engine's own copy while handing every call to a second engine; the
    store's exclusive `flock` admits one writer, the second engine swallows `StoreBusyError`
    on lazy attach, and all twelve calls answer `no_session` while the prompt advertises
    them as callable. One lane owns each concern, and the lane that owns the tools owns the
    writer.
50. **A failed engine load leaves the section without tools.** The deferral decides at
    plugin-load time; if the selected engine then fails to initialise (host-side error, a
    missing dependency, a corrupt store it refuses to open), the profile has instructions
    for tools that are not registered. There is no supported host hook to observe a later
    engine load from the plugin lane, so the plugin cannot fall back after the fact. This
    is the intended trade: instructions without tools are inert noise, whereas tools bound
    to a store-less engine report failures on every call. Check `/clm status` (or the
    host's engine list) after changing provider or engine configuration. The gate reads the
    same profile file the host reads (`context.engine`, resolved through `HERMES_HOME`); a
    deployment that points plugin loading at a different home than the agent would gate on
    the wrong profile, which is why `register()` also accepts an injected
    `engine_selected` for embedding hosts and tests.
51. **`/clm` has one owner.** In a dual-enabled profile the selected engine registers the
    command; the plugin lane skips it. On hosts whose general plugin context also accepts
    `register_context_engine`, the plugin lane registers the engine and the command itself
    and registers no tools — the engine injects them. Whichever lane handed the engine over
    also owns `/clm`; there is never a second command registration to shadow it.
52. **Two engine objects can exist in one process.** On a host that both directory-loads
    this engine and accepts the plugin's `register_context_engine`, the process holds two
    `CLMEngine` instances. The host resolves `context.engine` by name, so only the selected
    instance receives session hooks and only it opens the store; the other stays `detached`
    and its schemas are never injected (verified by the real-host probe: 12 unique names, one
    `attached` store). Nothing is shared between them — there is no second writer and no
    cross-instance state — but they are two objects, and a future host change that attaches
    stores eagerly rather than on the selected instance's first hook would break that
    guarantee.

## Ergonomic fast paths

53. **`stage_commit` decides on a preview it cannot re-verify.** The fast path previews the
    batch through the engine's pure projection and only then commits, so a pin or an
    ingestion landing between the two is caught by the store's own CAS (`stale_revision`) or
    by the in-transaction re-validation, not by the preview. The refusal is safe — nothing is
    written — but the saving the caller was promised is the pre-commit estimate.
54. **`min_saved_tokens` compares the calibrated estimate.** It is a guard against a
    disappointing edit, not a cost guarantee: calibration is a bounded ratio over a
    four-chars-per-token estimator (item 29), so a measured saving near the threshold can
    land on either side of it.
55. **`patch` cannot be replayed and therefore cannot fast-commit.** A literal patch resolves
    against the span's *current* text, which an earlier transaction may have changed; replaying
    the `txn_id` would re-resolve different text, so `commit=true` with `kind: patch` is
    refused by design. Stage the patch, inspect the receipt, then `clm_commit`. The resolved
    rewrite is a whole-span edit, so the audit trail shows the full replacement text rather
    than the patched region.
56. **Prose offload is a heuristic over "settled", not a judgement about importance.** It
    only offers assistant rows a couple of rows behind the newest tool call, and it skips
    pinned, protected, multimodal, duplicate and oversized spans, but a long settled
    explanation can still be a more useful thing to keep than a short tool result. The
    candidate list is a suggestion the model or operator accepts; nothing offloads
    automatically, and every offloaded span is restorable from the archive byte-for-byte.
57. **`include_stale` is a pagination-wide decision.** The store filters before it paginates,
    so flipping the flag mid-pagination skips or repeats rows; `clm_inspect` therefore warns
    in its schema and callers must restart without a cursor. Absent rows are omitted by
    default because a page of archive residue is worse than a short page.
