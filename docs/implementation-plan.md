# Implementation plan and delegation contracts

Trunk: `main`. Each delegate gets a detached-from-`origin/main` branch + separate worktree. No shared files. Conventional Commits. Tests before implementation where practical. Commit verified milestones; do not push until lead reviews.

## Phase 1 — parallel implementation

### A. Engine / Hermes integration — `feat/engine`

Own:
- `src/hermes_clm/engine.py`
- `src/hermes_clm/projection.py`
- `src/hermes_clm/identity.py`
- `src/hermes_clm/budget.py`
- `tests/unit/test_projection*.py`, `tests/unit/test_identity*.py`, `tests/unit/test_budget*.py`
- `tests/integration/test_engine_contract.py`
- `docs/upstream/hermes-commit-proposal.md`

Accept: ABC complete; per-iteration projection; unique alignment; protected rows; atomic tool groups; pure/deep-copy behavior; identity/manual/overflow compression modes; lifecycle/clone/model/token usage; no Hermes checkout edits. Fake host tests prove an edit committed after tool result changes the next assembled request. Document optional native commit closure, not required.

### B. Format / transactional store / recovery — `feat/store`

Own:
- `src/hermes_clm/models.py`, `errors.py`, `schema.py`, `store.py`, `archive.py`, `recovery.py`
- `tests/unit/test_store*.py`, `test_archive*.py`, `test_recovery*.py`
- `docs/store-format.md`

Accept: per-session SQLite; safe derived paths; 0700/0600; redaction at ingestion; append-only/raw archive; stable span IDs + absorbed aliases; CAS + idempotency; pin rules; revision revert; snapshots/audit/token accounting; single writer + WAL; crash/corruption recovery; derived `active.json` index only; strict schemas; bounded retrieval.

### C. Tools / aux / plugin registration — `feat/tools`

Own:
- `src/hermes_clm/tools.py`, `auxiliary.py`, `commands.py`, `plugin.py`
- root `__init__.py`, `plugin.yaml`
- `tests/unit/test_tools*.py`, `test_auxiliary*.py`, `test_plugin*.py`
- `skills/context-management/SKILL.md`, `examples/AGENTS.md`

Accept: JSON tool handlers + strict args; inspect/stage/commit/discard/revert/search/retrieve/pin/boundary; aux `task="compression"`, explicit timeout, no model/provider config, proposal hash+base revision and explicit accept/commit; operator manual edit/status/unpin command; independent guidance prompts; no copied upstream wording. Skill frontmatter includes `author: gwyntel`, `license: MutuaL-1.2`, description ≤60 chars.

### D. Security / concurrency / adversarial tests — `test/security`

Own only:
- `tests/security/**`
- `tests/fuzz/**`
- `docs/security-testing.md`

Accept: implement design review T1–T49 where applicable; explicitly test permitted agent pin/commit/revert instead of forbidding them; path/session isolation, symlink/hardlink, role spoofing, tool pairing, multimodal refusal, pin immutability/authority, stale proposals, CAS/replay, crashes, read-only second writer, subagent isolation, permissions/redaction, projection side-effect freedom. Include known-bad mutation checks proving tests go red. No production edits; report required fixes to lead.

### E. Benchmarks / docs / packaging / CI — `feat/delivery`

Own:
- `pyproject.toml`, `_version.py`, `py.typed`, CLI module
- `README.md`, `LICENSE`, `NOTICE.md` (only if needed), `CONTRIBUTING.md`, `CHANGELOG.md`, `SECURITY.md`, `.gitignore`
- `benchmarks/**`, `docs/benchmarking.md`, `docs/limitations.md`, `docs/research/**`
- `.github/**`, `release-please-config.json`, `.release-please-manifest.json`
- `tests/packaging/**`

Accept: installable src package >=3.11, MutuaL-1.2 as `LicenseRef-MutuaL-1.2`; no Hermes replacement dependency; offline deterministic A/B/C/D harness; exact fixtures for long coding, interruption, conflicting updates, huge outputs, injection, recovery, handoff; token/latency/cost/cache/edit/recovery metrics; live runs cost-gated and require dedicated key + explicit model/volume approval; CI lint/format/type/test/security/build matrix 3.11–3.14 + honest aggregate gate; release-please + generated notes + GitHub artifacts + OIDC PyPI workflow disabled until publisher exists; attribution and limitations.

## Phase 2 — integration

1. Merge store → engine → tools → delivery → security into separate integration worktree.
2. Resolve only contract conflicts; delegate substantive corrections to owning agent.
3. Run full gates and known-bad tests.
4. Run clean-checkout package install + Hermes discovery against isolated temporary `HERMES_HOME`.

## Phase 3 — acceptance

Use fake OpenAI-compatible scripted server, no paid tokens:

1. Load plugin by `context.engine=hermes-clm` without core replacement.
2. Script main-model tool call that drops/rewrites/offloads a prior span; capture next request and assert exact effect.
3. Assert system + original/later user constraints + memory injection survive byte-identically.
4. Retrieve exact nonsecret archived detail after offload; restart and recover revision; concurrent parent/child isolation.
5. Mock `call_llm` and assert `task="compression"`, no hardcoded route, proposal cannot auto-commit, stale proposal rejected.
6. Inject overflow and verify real shrink/liveness without raw-user loss.

## Phase 4 — benchmarks and release

- Run offline deterministic A=default, B=microcompaction, C=CLM, D=CLM+aux mock. Publish actual outputs; label limits.
- Do not run live-token benchmark without separate approval listing model, alias classification, calls, token ceiling, key and estimated cost.
- Push only green integration. Verify GitHub CI on clean checkout.
- Apply public-repo ruleset requiring aggregate gate where API permits.
- Tag `v0.1.0-rc.1` prerelease only after green CI; generated notes, wheel/sdist, provenance. Verify remote tag/release/artifacts.
- PyPI trusted publishing requires Gwyn to create pending publisher; GitHub release can proceed without it. Report PyPI blocked honestly.
