# Security policy

## Supported versions

`hermes-clm` is pre-release. Security fixes land on `main` and in the newest tagged
release; older pre-release tags are not maintained.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on
[`gwyntel/hermes-clm`](https://github.com/gwyntel/hermes-clm/security/advisories/new).
Do not open a public issue. Include a minimal reproduction, the affected version, and the
impact you believe it has. Acknowledgement targets are best-effort; this is a small project.

## Scope and threat model

The design assumes two adversaries:

- **A model acting on injected or self-authored text.** It has no filesystem intent but can
  read and write through the agent's tools. The plugin's job is to ensure such text never
  gains operator or system authority, never changes pins, and never authorises a
  side-effecting tool.
- **Code running as the same user.** A shell can read and rewrite the store. No in-process
  plugin can prevent that, and at-rest encryption is not a control because the key would
  live in the same process.

The plugin provides: path and session scoping, permissions (`0700`/`0600`), redaction at
ingest, append-only archive rows, SQL triggers that refuse updates to protected rows,
compare-and-swap revisions, transaction idempotency, proposal hashes bound to a base
revision, and per-record checksums plus a hash chain.

It does **not** provide confidentiality against same-UID code, immunity to prompt
injection, a tamper-proof anchor outside the writable set, or child-to-parent archive
access until Hermes supplies session lineage. See [`docs/limitations.md`](docs/limitations.md)
for the complete list.

## Handling benchmark and session data

Never attach a real session archive, a tool output containing a secret, or a live provider
key to an issue or pull request. The archive is designed to redact at write, but a
reproduction should use synthetic fixtures from `benchmarks/fixtures/`.
