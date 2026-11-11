# hermes-clm — model-directed context management for Hermes Agent

**Give the agent control of its own context, without giving up the raw transcript, the
operator's constraints, or the audit trail.**

`hermes-clm` is a context engine for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It archives each session in a per-session SQLite store, lets the model inspect, stage,
commit, offload and restore individual spans through explicit tools, and projects the
committed revision onto each provider request as a pure function. Raw history is never
rewritten, tool-call groups stay atomic, and system / user / pinned rows pass through
byte-identically.

> **Status: pre-release, integrated tree.** The engine, store, tools, plugin, benchmark
> harness and packaging are integrated. Local gates: **776 passed, 3 skipped, 0 failed**
> with `CLM_HERMES_CHECKOUT` set (754 passed, 25 skipped without the checkout);
> `ruff check`, `ruff format --check`, `mypy src`, `python -m build` and
> `twine check` pass. The remaining skips are two media-blob probes that do not apply to
> inline archives and one tool-schema probe with no module-level schema object.
> Remote CI passes lint, types, tests on Python 3.11–3.14, mutation, security, build and
> the aggregate gate. Only a GitHub prerelease exists until operator acceptance completes;
> no PyPI publication has occurred. Public APIs may change before `v0.1.0`.
> Exact commands and remaining limitations are in
> [`docs/limitations.md`](docs/limitations.md#current-gates).

## The problem

Agents lose information when context fills up. Harness-scheduled compaction picks a
boundary the model did not choose, drops tool evidence, and often deletes the fact that
mattered six turns later. The model cannot see what was removed and cannot ask for it back.

## The solution

- **Model-directed edits.** The model stages `keep`, `drop`, `rewrite`, `pin`, `offload`,
  `restore` and `boundary` operations against stable span IDs. A commit is an explicit
  transaction, not an implicit side effect.
- **Durable archive.** Offloaded spans keep their exact bytes in a per-session store, so a
  later `retrieve` returns the original text — not a summary of it.
- **Pure projection.** Each request is rebuilt from the current host history plus the
  committed revision. No wall clock, no model call, no token-dependent selection.
- **Protection by construction.** System, user, current-user and newest-tool rows are never
  edit targets; tool-call groups move as a unit; multimodal parts are refused rather than
  flattened.
- **Optional auxiliary compression.** A bounded `task="compression"` call may *propose*
  selected-span edits. A proposal is immutable and cannot auto-commit; acceptance binds the
  proposal hash and base revision.

The design and its invariants are specified in [`docs/architecture.md`](docs/architecture.md),
[`docs/adr/0001-request-projection.md`](docs/adr/0001-request-projection.md) and
[`docs/integration-contract.md`](docs/integration-contract.md).

## Requirements

- Python 3.11 or newer (CI covers 3.11, 3.12, 3.13 and 3.14).
- Hermes Agent is an optional host. The runtime package imports and its CLI runs without
  Hermes installed; host adapters import Hermes lazily.
- No runtime dependencies. Every shipped module imports with the standard library alone.

## Install

There is no PyPI project yet, so install from a clone. Two paths, and they are different
things:

**As a Hermes plugin (the engine + tools + `/clm`).** Hermes loads a plugin from a
directory, so clone the checkout into the plugin directory of the profile that should use
it, then install the library into the environment Hermes runs in:

```bash
git clone https://github.com/gwyntel/hermes-clm "$HERMES_HOME/plugins/hermes-clm"
python -m pip install -e "$HERMES_HOME/plugins/hermes-clm"
```

`$HERMES_HOME` is the Hermes home for the active profile (commonly `~/.hermes`). The
checkout's root `__init__.py` is the entry point Hermes calls; `plugin.yaml` declares the
`clm_*` tools and the `clm.*` config keys. The host must be a build that supports
context-engine plugins; the hook line numbers were verified against Hermes v0.23.0.

**As a library or CLI only (no Hermes).** This is what the wheel is for:

```bash
python -m pip install .
hermes-clm version
```

Use `python -m pip install -e ".[dev]"` inside a virtualenv for tests, linting and builds.

### Configure

```yaml
# profile config
context:
  engine: hermes-clm
compression:
  enabled: true     # the host gates plugin engines on this switch
```

Optional keys: `clm.reserve_tokens`, `clm.pressure_thresholds`, `clm.aux_enabled`,
`clm.aux_timeout_seconds`, `clm.archive_redact`, `clm.retention_sessions`,
`clm.max_retrieve_bytes`, `clm.max_rewrite_bytes`, `clm.max_aux_spans`,
`clm.max_operations`. The engine does not choose providers or models and never writes
provider credentials.

### Roll back

The engine is request-only: it never rewrites the persisted transcript, so disabling it
restores the host's built-in compressor on the next turn without replaying anything.

```yaml
context:
  engine: compressor   # or delete the key; "compressor" is the host default
```

Then remove the plugin directory if it should not load at all:

```bash
rm -rf "$HERMES_HOME/plugins/hermes-clm"
```

The per-session archive lives under `$HERMES_HOME/clm/sessions/<sid>/`. It is inert when
the engine is off; delete those directories to reclaim the space (that is a deletion path
for archived secrets, so do it deliberately). Restart Hermes after changing
`context.engine`; the plugin is selected at agent initialisation.

## Use

```bash
hermes-clm version          # package version
hermes-clm doctor           # JSON capability report; never imports Hermes
hermes-clm bench            # deterministic offline A/B/C/D benchmark
```

`hermes-clm bench` requires a source checkout: the fixtures and harness are deliberately not
shipped in the wheel. From a wheel-only install it exits nonzero with a clear message.

Hermes integration is configured through the host's context-engine slot, not by replacing
Hermes:

```yaml
# profile config, once the plugin adapter is installed
context:
  engine: hermes-clm
```

The engine requires `compression.enabled: true` because the host gates plugin engines on
that switch. It does not choose providers or models, and it never writes provider
credentials.

## Offline benchmarks

`python -m benchmarks` (or `hermes-clm bench`) runs four reference strategies over seven
exact fixtures. Committed totals from the deterministic run in
[`benchmarks/results/`](benchmarks/results/), on CPython 3.12 and byte-identical on 3.11
and 3.14:

| Key | Strategy | Requests | Prompt tokens | Sim cost (USD) | Edits | Facts retained | Fixtures with all facts |
|---|---|---:|---:|---:|---:|---:|---:|
| A | Default compressor, modelled | 56 | 69,761 | 0.0894 | 4 | 17/20 | 4/7 |
| B | Microcompaction, modelled | 56 | 61,593 | 0.0932 | 8 | 15/20 | 2/7 |
| C | CLM model-directed edits | 56 | 59,103 | 0.0865 | 9 | 20/20 | 7/7 |
| D | CLM plus a local mock auxiliary proposal | 56 | 59,763 | 0.1013 | 9 | 20/20 | 7/7 |

What this does and does not say:

- **It is a simulator, not the engine.** A and B are policy models of a threshold
  summariser and microcompaction; C and D are reference CLM policies, not
  `hermes_clm.engine`. No Hermes process, provider or model runs.
- **C and D pass their preservation checks by construction.** The fixtures declare
  `FACT[...]` lines and the CLM policies carry them forward explicitly. The run proves the
  accounting distinguishes policies; it is not a task-accuracy or efficacy result.
- **B is not cheaper despite fewer prompt tokens.** Its repeated tool-row rewrites break
  the prefix cache more often, so illustrative cost is above A. D's auxiliary proposal
  costs more than it saves here, because aux input tokens are counted.
- Token counts use a fixed four-characters-per-token estimator, not a provider tokenizer;
  cost uses illustrative list prices and latency uses synthetic constants. Neither is a
  bill or a timing measurement.

Fixtures cover a long coding session, interruption and restart, conflicting updates, a huge
tool output, prompt injection, archive recovery and session handoff. See
[`docs/benchmarking.md`](docs/benchmarking.md) for the method, the per-fixture table and the
cost-gated procedure that any future live run must follow.

## Limits, honestly

- The plugin cannot defend against code running as the same user; a shell can read the
  store. File permissions, redaction at write and checksums are corruption and authority
  controls, not confidentiality against same-UID code.
- An unanchored hash chain detects corruption and interrupted writes; it does not make
  tampering impossible.
- Injected text can still persuade a model. The guarantee is that injected text never gains
  operator or system authority, not that injection never happens.
- Hermes currently offers no supported mid-loop transcript commit; the engine projects
  request copies instead. Child-to-parent archive retrieval is disabled until the host
  supplies lineage.
- Benchmark gains are not evidence of real task accuracy, and CLM-style benefits are
  model-conditional.

The full list is in [`docs/limitations.md`](docs/limitations.md).

## Prior art and licensing

This project re-implements ideas from *Context Language Models* (Shao et al.,
[arXiv:2609.37725](https://arxiv.org/abs/2609.37725)) and studies the authors' reference
release at commit `483198229ab7b90c0eed4651306ab3b3505aaf50`. That release is
**CC BY-NC 4.0**; no code, prompt text, YAML templates or documentation from it is copied or
vendored here. The implementation is independent, with its own protocol, prompt wording,
budget state machine and recovery layer. See
[`docs/research/README.md`](docs/research/README.md) for the full attribution and the
review notes.

Source-available under the [Mutualist License v1.2](LICENSE) (`LicenseRef-MutuaL-1.2`).
The license text travels in the wheel and sdist. There is no `License ::` classifier because
MutuaL is not an SPDX-listed OSI license.

## Contributing and security

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development setup, gates, commit and release rules.
- [`SECURITY.md`](SECURITY.md) — private reporting and scope.
- [`CHANGELOG.md`](CHANGELOG.md) — release notes, generated from Conventional Commits.
