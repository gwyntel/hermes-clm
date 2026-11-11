# hermes-clm — model-directed context management for Hermes Agent

**Give the agent control of its own context, without giving up the raw transcript, the
operator's constraints, or the audit trail.**

`hermes-clm` is a context engine for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
It archives each session in a per-session SQLite store, lets the model inspect, stage,
commit, offload and restore individual spans through explicit tools, and projects the
committed revision onto each provider request as a pure function. Raw history is never
rewritten, tool-call groups stay atomic, and system / user / pinned rows pass through
byte-identically.

> **Status: pre-release.** The package metadata, CLI, benchmark harness and documentation
> are complete. The engine, store, tools and security suites land on separate integration
> branches before the first release candidate. Public APIs may change before `v0.1.0`.

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

From a checkout:

```bash
git clone https://github.com/gwyntel/hermes-clm
cd hermes-clm
python -m pip install .
```

Development install with test and build tooling:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Once a release exists, `pip install hermes-clm` will work. There is no PyPI project or
pending publisher yet; see [`docs/limitations.md`](docs/limitations.md).

## Use

```bash
hermes-clm version          # package version
hermes-clm doctor           # JSON capability report; never imports Hermes
hermes-clm bench            # deterministic offline A/B/C/D benchmark
```

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
exact fixtures:

| Key | Strategy |
|---|---|
| A | Default compressor, modelled |
| B | Microcompaction, modelled |
| C | CLM model-directed edits |
| D | CLM plus a local mock auxiliary proposal |

Fixtures cover a long coding session, interruption and restart, conflicting updates, a huge
tool output, prompt injection, archive recovery and session handoff. Metrics include prompt
tokens, prefix-cache reuse, illustrative cost, synthetic latency, edits, archive integrity,
fact retention, injection containment and handoff fidelity.

The harness makes **no network calls and no model calls**. Token counts use a fixed
estimator; cost and latency use published constants. Results demonstrate harness behaviour
and the accounting, not task accuracy. See [`docs/benchmarking.md`](docs/benchmarking.md)
for the method and the committed run, and [`benchmarks/results/`](benchmarks/results/) for
the raw JSON.

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
