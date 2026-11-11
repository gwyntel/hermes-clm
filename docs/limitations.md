# Known limitations

This project publishes its limits with the same prominence as its features. If a claim is
not enforceable, it is listed here rather than implied by the architecture.

## Current gates

Measured locally on macOS/CPython 3.14 from `feat/integration`, with matching remote CI on
GitHub (run 43091861486). Only a GitHub prerelease exists; no PyPI or production-Hermes
deployment has occurred.

| Gate | Command | Result |
|---|---|---|
| Format | `ruff format --check .` | pass (124 files) |
| Lint | `ruff check .` | pass |
| Types | `mypy src/hermes_clm` | pass (18 modules) |
| Build | `python -m build`, `twine check dist/*` | pass |
| Tests | `pytest -o addopts='-ra --import-mode=importlib'` | 776 passed, 3 skipped, 0 failed with `CLM_HERMES_CHECKOUT`; 754/25 without it |
| Security + fuzz | its own suite | all pass; mutation baseline green, 16/16 injected defects red |
| Benchmarks | offline simulator, two runs and CPython 3.11/3.12/3.14 | byte-identical JSON; simulator only, no model calls |

The three skips are expected and explicit: two media-blob probes do not apply to inline
archives, and one tool-schema probe has no module-level JSON schema object. Without a Hermes
checkout, the 21 real-discovery/lifecycle tests also skip.

Resolved during integration: the `commit()`-without-`stage()` test adaptation, the
`model`→`agent` actor vocabulary, hashed-but-stable session IDs, `EngineConfig` mapping
construction, mutation-fixture isolation, the missing `expect_refusal` import, and the
intermittent CAS-loser corruption error. The last is now covered by a non-`xfail` test.

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
