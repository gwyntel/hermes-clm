# Research notes

Original analysis produced during the design of `hermes-clm`, sanitised for publication.
Local paths, hostnames, profile names, account identifiers and access-control findings were
removed. The technical claims and known limitations were retained.

| Note | What it covers |
|---|---|
| [`context-language-models-paper.md`](context-language-models-paper.md) | Full reading of *Context Language Models* (arXiv:2609.37725), with measured-claim, implementation-fact and inference labels. |
| [`reference-implementation.md`](reference-implementation.md) | Licence and behaviour review of the authors' reference release, and the independent-implementation boundary. |
| [`hermes-host-api.md`](hermes-host-api.md) | Verified Hermes context-engine API, turn-loop call sites, tool dispatch, usage and lifecycle facts. |
| [`projection-review.md`](projection-review.md) | Adversarial review of the request-projection design: alignment, system/memory preservation, overflow liveness, persistence and isolation. |
| [`security-design-review.md`](security-design-review.md) | Threat model, invariants and acceptance tests T1–T49 for the store and projection. |
| [`repo-conventions.md`](repo-conventions.md) | Packaging, licence, CI and release conventions for a public Python package. |

## Attribution

The design re-implements ideas from:

> Rulin Shao et al. “Context Language Models.” arXiv:2609.37725,
> 2026-09-29. <https://arxiv.org/abs/2609.37725>

Reference release studied: `facebookresearch/context-language-models` at commit
`483198229ab7b90c0eed4651306ab3b3505aaf50` (2026-10-13).

That release is licensed **CC BY-NC 4.0**. No source file, prompt string, YAML template or
documentation text from it is copied into this project, and it is not vendored, installed as
a dependency, or present in CI. The implementation uses an independent protocol, prompt
wording, budget state machine and recovery layer. The notes above describe behaviour and
licence facts; they are not a port.

Host API facts were verified against Hermes Agent v0.23.0 (`2bd378ed`) and an upstream
`main` snapshot (`2fd5066f`). Hermes is a separate project and is not affiliated with this
one.

## Licence of these notes

These notes are original analysis by the project authors and are covered by the repository's
Mutualist License v1.2 (`LicenseRef-MutuaL-1.2`). Quoted facts from the paper and upstream
repositories remain attributable to their authors under their own licences.
