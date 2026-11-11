# Known limitations

This project publishes its limits with the same prominence as its features. If a claim is
not enforceable, it is listed here rather than implied by the architecture.

## Integration status

Measured on 2026-11-10 from branch `fix/delivery` on macOS with CPython 3.12.13. These are
local results; no remote CI run exists yet. Owner names refer to the delegate that owns the
file, not to a person.

| Gate | Command | Result |
|---|---|---|
| Format | `ruff format --check .` | pass (117 files) |
| Lint | `ruff check .` | 43 findings: 42 in `tests/security/**` (39 auto-fixable), 1 in `src/hermes_clm/archive.py` |
| Types | `mypy src` | 18 errors in 7 files (package-name error fixed by `explicit_package_bases`/`mypy_path`) |
| Build | `python -m build`, `twine check`, `verify_wheel.py` | pass; wheel 19 package files, sdist 154 entries |
| Import isolation | wheel in a clean venv with `hermes*`/`agent*` imports blocked | pass |
| Benchmarks | two runs, `cmp` against committed JSON | byte-identical; also identical on CPython 3.14 |
| Tests | `pytest -m "not slow"` | 572 passed, 108 failed, 2 skipped, 1 xpassed (the CAS test in item 7 is `xfail`; stable across three runs) |
| Coverage | `pytest --cov` | 86% branch, floor 85% |

The failures are not distributed evenly. They collapse to a small number of contract
mismatches, all of which are findings for the owning delegate rather than test bugs to
paper over:

1. **`commit()` without `stage()`.** The security and fuzz suites ingest a conversation and
   then call `commit(txn_id=...)`; the store requires a staged transaction and raises
   `NotFoundError: unknown_txn`. This accounts for most of the red in
   `test_roles/test_paths/test_recovery/test_injection/test_redaction/test_revisions/
   test_proposals/test_concurrency/test_projection_purity/test_permissions`,
   `integration/test_engine_security.py` and all four fuzz modules. Either the suites need a
   `stage()` call (they claim to follow `docs/integration-contract.md`, which lists both
   entry points) or `ingest` is meant to create a committable transaction. Owner: store,
   with the security suite as the counterparty. Decision needed.
2. **`actor_origin` vocabulary.** The store accepts `agent|operator|aux|system`; the
   security adapter's `MODEL_ORIGINS` includes `model` and passes `actor_origin="model"`,
   which raises `ValueError`. Owner: `models.py` (vocabulary) plus the adapter.
3. **Session IDs are sanitised, not refused.** `for_session` accepts `.`, `..`, `a/b` and
   `../x` by hashing them into `session-<hash>` directories under `clm/sessions/`. Nothing
   escapes the root, but invariant I1 and T1 require rejection, not silent rewriting
   (an accepted ID must round-trip). Owner: store.
4. **Plugin passes a `dict` where an `EngineConfig` is required.**
   `plugin.build_engine` calls `CLMEngine(config=dict(config or {}))`; `CLMEngine.__init__`
   annotates `config: EngineConfig | None` and reads attributes. `mypy` reports
   `arg-type`; at runtime the engine would read attributes off a `dict` and fail. Owner:
   plugin/engine interface.
5. **Mutation harness cannot start.** `tests/conftest.py::pytest_configure` imports
   `hermes_clm.identity` unconditionally. The mutation harness runs probes in a subprocess
   whose `sys.path` points at the fixture package, which has no `identity` module, so every
   probe dies with `INTERNALERROR` instead of a clean red/green. Owner: shared conftest.
6. **Undefined name in the security suite.** `tests/security/test_media.py:136` calls
   `expect_refusal(...)` without importing it (`F821`); the test would raise `NameError` if
   reached. Owner: security suite.
7. **CAS loser can report corruption.** In `tests/unit/test_store_locking.py::
   test_concurrent_commits_lose_the_cas_race_cleanly`, one winner is always correct, but
   the loser intermittently raises `CorruptStoreError("store has no revisions")` instead of
   the contracted `stale_revision` refusal (about 1 run in 10-25). The test is marked
   `xfail(strict=False)` with the strict contract intact, so an XPASS signals the fix; it
   is not a relaxed assertion. Owner: store. The nondeterminism also moves the suite total
   by one between runs.

The 18 `mypy` errors are in `store.py` (11), `engine.py` (2), and one each in `budget.py`,
`commands.py`, `identity.py`, `models.py`, `plugin.py`. They are real annotation mismatches
(SQLite `Row` vs `Mapping`, enum coercion, a base-class alias), not config noise. Missing
stubs for the optional host (`agent.*`, `hermes_cli.*`) are handled by a mypy override, so
no error is an import-resolution artefact.

Nothing in this section may be reported as green elsewhere.

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
6. **`bind_session_state` is not reliable.** It is absent under `plugins.isolation: host`
   and swallowed on failure. Derive the store from `hermes_home` in `on_session_start` and
   initialise lazily from the first hook.
7. **Host isolation taxes per-request hooks.** Under `plugins.isolation: host`, every
   `select_context` carries the transcript across an RPC boundary. `in_process` is the
   supported mode unless measured otherwise.
8. **No child-to-parent lineage.** `on_session_start` and `bind_session_state` do not pass
   `parent_session_id`, so child-to-parent archive retrieval is disabled. Guessing lineage
   from `conversation_id` is forbidden.
9. **Wire sanitizers repair silently.** Hermes drops unpaired tool results and stubs
   unanswered calls after selection. Group atomicity is validated in the store and
   projector, but a host-side assertion is still requested so a bad projection fails
   instead of degrading.
10. **Rollback and manual compression semantics differ from the paper.** `should_compress`
    is false in steady state; overflow and manual `/compress` are the only paths that
    return a new list, because Hermes scores shrink on the live list and fails the turn if
    an engine returns identity during provider-proven overflow.

## Store and security

11. **Same-UID code can read and rewrite the store.** A shell running as the agent's user
    bypasses every tool-scoped rule. Permissions, CAS, idempotency and checksums are
    authority and corruption controls, not confidentiality controls.
12. **At-rest encryption is not a control.** The plugin shares the model's process; any key
    it can read, a same-UID process can read. No encryption claim is made.
13. **The hash chain is unanchored unless the host supplies a sink.** Unanchored, it detects
    corruption and interrupted writes; it does not make tampering impossible, because the
    whole chain is rewritable by the same user. Reports must use that wording.
14. **The archive is a durable secret-retention surface.** Redaction at ingest, bounded
    retention and a purge command reduce the risk; redaction is best-effort and may miss a
    secret in an unusual format. Before this feature, trimming context was a deletion path
    for a leaked token; an append-only archive removes that accidental protection.
15. **Prompt injection is not prevented.** The guarantee is that injected text never gains
    operator or system authority, never changes pins and never authorises a side-effecting
    tool. Whether a model is persuaded by its own history or by tool output is not
    solvable at this layer.
16. **Pins and commits are operator-authority operations.** Agent tools are read-only for
    pins. If a future tool exposes a mutating pin or commit verb to the model, the
    persistence guarantee collapses.
17. **Path validation cannot stop an attacker with filesystem access.** Component-wise
    `lstat`, realpath containment, owner checks, `nlink == 1` and hash verification stop
    traversal and symlink swaps; they do not stop a process that can `sqlite3` the file.

## Projection

18. **Alignment is heuristic for a canonicalised prefix.** Rows the host drops, synthesises
    or rewrites cannot always be matched. The failure mode is an inert operation, never a
    malformed request, but an operation can silently not apply.
19. **Dropping an assistant row between user rows lets the host merge them.** Protected user
    text survives as concatenated content, not as separate rows.
20. **Rewriting a row breaks prefix-cache reuse from that point.** This is the accepted
    cost of mid-context edits; the prompt must teach batching and late edits.
21. **Multimodal and unknown part types are refused, not flattened.** Refusal is deliberate:
    flattening loses provenance and can turn an image into untrusted text.
22. **Tool-call fidelity is preserved only for unedited groups.** A rewritten group keeps
    call/result ids; an edited history is not reconstructed from stored bytes.

## Benchmarks

23. **The offline harness is a simulator.** A/B are policy models, not Hermes; C/D are
    reference policies, not the shipped engine. No number is a task-accuracy result.
24. **Token counts are estimated at four characters per token.** They are not a provider
    tokenizer and do not capture tokenizer-specific behaviour or reasoning-token stripping.
25. **Cost and latency are synthetic.** Prices are illustrative list prices; latency uses
    fixed per-token constants. Neither is a bill or a wall-clock measurement.
26. **C and D pass their own preservation checks by construction.** The fixtures declare
    `FACT[...]` lines and the CLM policies explicitly carry them. The run demonstrates
    accounting and policy differentiation, not efficacy.
27. **No concurrency or real crash injection.** The harness simulates a restart by rebuilding
    from raw history and the committed operation log. The security suite owns real
    concurrency, kill, corruption and known-bad mutation tests.
28. **Injection containment is structural only.** It checks provenance elevation and system
    preservation; it cannot measure persuasion.

## Scope and expectations

29. **CLM-style gains are model-conditional.** Published results show weaker models losing
    to a summary harness and editing rarely. Do not advertise gains for mid-tier models
    without qualification, and do not build a scheduled auto-compactor and call it CLM.
30. **Suffix cache reuse is out of scope.** It requires an SGLang patch or vLLM scheduler
    subclass plus a KV connector, is approximate, and is not reachable through an
    OpenAI-compatible gateway. The plugin cannot buy the paper's server-side savings.
31. **RL, skill evolution and trajectory-format work are out of scope for v1.** They are
    separate systems with separate dependencies and licences.
32. **Upstream drift.** Host line numbers were verified against Hermes v0.23.0 and a main
    snapshot; Hermes moves. Re-verify the load-bearing hooks before claiming compatibility.

## Release and CI

33. **No PyPI project or pending publisher exists yet.** The publish workflow is disabled
    unless repository variable `PYPI_PUBLISH_ENABLED` is `true` and the `pypi` environment
    exists with a trusted publisher configured for `publish.yml`. GitHub releases and their
    artifacts do not depend on PyPI.
34. **CI results are local until pushed.** The workflows are committed but no remote run has
    been observed from this branch. Do not report a green CI run that has not happened.
35. **Only CPython 3.12 was exercised locally for the full delivery.** 3.11, 3.13 and 3.14
    are CI-only for the suite; 3.14 was used locally only to confirm the benchmark JSON is
    byte-identical. The package uses no version-specific syntax, but the matrix is the
    proof.
36. **Release Please owns version bumps.** Hand-editing `pyproject.toml` or `_version.py`
    without the release PR breaks the single-source contract; the packaging test fails if
    they disagree. The manifest is seeded at `0.0.0` with `initial-version: 0.1.0`, so the
    first release PR should produce `v0.1.0`; later releases follow Conventional Commits.
    No tag or release exists yet.
37. **The benchmark harness is not in the wheel.** `hermes-clm bench` and
    `python -m benchmarks` require a source checkout (a git clone or an unpacked sdist);
    the wheel excludes `benchmarks/` and `tests/` on purpose. The installed CLI reports the
    missing harness and exits nonzero.
38. **The coverage floor is 85%, not a target.** It reflects an 86% branch measurement over
    a suite with 108 red tests, so it can only fall as the suite is repaired; raising it is
    a follow-up, not a claim of completeness. Coverage is measured in the test job; the
    build job does not re-measure it.
39. **The mutation harness is a separate CI gate.** It re-runs probe tests in subprocesses
    with a fixture implementation on `sys.path`; it has its own job so a broken harness
    cannot be mistaken for a broken suite. It currently fails for the reason in
    [Integration status](#integration-status) item 5.
40. **Bandit gates at medium severity.** Three Low findings are accepted and recorded:
    `engine.py:428` swallows a logger failure (`B110`), `store.py:107` names the estimator
    constant `"chars/4"` (`B105`, a false positive), and `store.py:1721` asserts an
    internal invariant (`B101`, which disappears under `python -O`). The `B101` one is a
    real robustness question for the store owner, not a style nit.
