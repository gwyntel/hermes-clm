# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/); Release Please
generates release notes from those commits.

## [0.3.0](https://github.com/gwyntel/hermes-clm/compare/v0.2.0...v0.3.0) (2026-11-14)


### Features

* **compaction:** offload eligible settled assistant prose ([132206f](https://github.com/gwyntel/hermes-clm/commit/132206fe5f1c32a5e0909836b98ebf8ab0ad9de9))
* **inspect:** hide stale spans without hiding archive access ([e078429](https://github.com/gwyntel/hermes-clm/commit/e0784296e11c148bff0c334f6dd9fd168e573410))
* model ergonomics — honest scope, cheap paths, safe reactivation ([207ba25](https://github.com/gwyntel/hermes-clm/commit/207ba25af1f56678e6045b07d745dc8e4aaac20e))
* **status:** expose headroom and honest savings estimates ([c3f7412](https://github.com/gwyntel/hermes-clm/commit/c3f7412df18fe3f27e9f5440e3f66bccb59e70e1))
* **store:** commit direct edits in one write transaction ([9ebea3a](https://github.com/gwyntel/hermes-clm/commit/9ebea3af0fbe2825b2dfb4d4c49d583dc1a86e2f))
* **tools:** offer checked single-call direct commits ([54487f8](https://github.com/gwyntel/hermes-clm/commit/54487f84c4933521c892630649f9d44f7c1f3f73))
* **tools:** patch literal text inside one span ([bdebc75](https://github.com/gwyntel/hermes-clm/commit/bdebc75f9ab28255af810e04e2950981e56537c1))
* **tools:** return compactable savings and pressure hints ([e3f7857](https://github.com/gwyntel/hermes-clm/commit/e3f785750204f33324ac8d34e4c89e551423b1ea))


### Bug Fixes

* **budget:** use one request-scoped token vocabulary ([fa2245c](https://github.com/gwyntel/hermes-clm/commit/fa2245cfc1812204b29f71730c2c2ac7fe451807))
* **status:** name store-wide token totals by scope ([95d5aba](https://github.com/gwyntel/hermes-clm/commit/95d5abafb87328a360701cc4a3f2f4f1fdbd01c8))
* **store:** refuse reactivations the projector would reject ([31a41a8](https://github.com/gwyntel/hermes-clm/commit/31a41a8a135ce99b7426611476f19673f0d8b0dd))


### Documentation

* correct the claims independent review disproved ([b2cfd17](https://github.com/gwyntel/hermes-clm/commit/b2cfd177a550c06908d0df9d258b455ee75c131f))
* distinguish released behavior from benchmark evidence ([da08751](https://github.com/gwyntel/hermes-clm/commit/da087513d1eafaeac75fb5c2774c87e581dae19d))
* record the post-remediation test total ([fdbb145](https://github.com/gwyntel/hermes-clm/commit/fdbb145a6ecceeeaa0edb889968065274df801dd))
* teach the cheap paths and name both reclaimable classes ([64c9c74](https://github.com/gwyntel/hermes-clm/commit/64c9c74ef5302df626904a37ab3f691cad605bee))

## [0.2.0](https://github.com/gwyntel/hermes-clm/compare/v0.1.0...v0.2.0) (2026-11-13)


### Features

* **clm:** make compaction, tokens and batch cost readable to the agent ([a632c5f](https://github.com/gwyntel/hermes-clm/commit/a632c5fe6cbd5195167e0635d4efe37fe887f547))
* improve agent compaction feedback and safety ([550a345](https://github.com/gwyntel/hermes-clm/commit/550a345e928f6ce136cb0771cef467401a26f2cd))


### Bug Fixes

* **clm:** stop dropping spans, deleting tool rows, and double-writing ops ([e420fdf](https://github.com/gwyntel/hermes-clm/commit/e420fdf23b0895e05a1c2b66047230d03ce099cc))
* **engine:** preserve stores across manual compaction ([258d5a7](https://github.com/gwyntel/hermes-clm/commit/258d5a7d21dfa2d3dccca5babaa14b5036d49c3d))
* **tools:** accept an empty cursor as the first page ([7f7d041](https://github.com/gwyntel/hermes-clm/commit/7f7d04122d694b1880e5d114910e92a8da5ce403))

## 0.1.0 (2026-11-12)


### Features

* **bench:** add deterministic offline A/B/C/D benchmark harness ([7f0a04f](https://github.com/gwyntel/hermes-clm/commit/7f0a04f8a5788a9d7816d610cfeb7f55355cf2b8))
* integrated CLM context engine, store, tools and plugin ([78785a7](https://github.com/gwyntel/hermes-clm/commit/78785a7a8e0a679170bb8fc1176fb34e7104a012))
* project committed CLM edits onto live Hermes requests ([c332ba5](https://github.com/gwyntel/hermes-clm/commit/c332ba507eb949da0d4c09881e4577b7aedc6f15))
* **store:** transactional per-session SQLite store with recovery ([d938e53](https://github.com/gwyntel/hermes-clm/commit/d938e531a823e4230962341c74d9cf18a67287fb))
* **tools:** add CLM tool surface, aux proposals and plugin registration ([27e87d8](https://github.com/gwyntel/hermes-clm/commit/27e87d8a464400171e820d973af1d75fc9ca8f39))


### Bug Fixes

* address context engine review findings ([93030db](https://github.com/gwyntel/hermes-clm/commit/93030dbc3cf1d57455fb15d742576ce7c6cff275))
* avoid dynamic SQL in operator unpin ([ebda2a4](https://github.com/gwyntel/hermes-clm/commit/ebda2a413dd92c23efd8d5f744a8c289515fec6d))
* **cli:** clarify package scope and --out help ([5e8a508](https://github.com/gwyntel/hermes-clm/commit/5e8a508a79f00125d08d00c94f5db2984cf56ee0))
* close integration security and discovery gaps ([1fa6f51](https://github.com/gwyntel/hermes-clm/commit/1fa6f51f675f5d988a8811b2775d383a5e4dcfa3))
* **delivery:** make packaging, config and CI gates honest on the integrated tree ([51bf55f](https://github.com/gwyntel/hermes-clm/commit/51bf55fec8ca2520a098d50c4f5c27639422c89f))
* **engine:** make the integrated engine meet the host and plugin seams ([350fd55](https://github.com/gwyntel/hermes-clm/commit/350fd555e90c7373d9c7fc8b5f9513306a74526f))
* **manifest:** stop declaring engine tools as plugin registrations ([381cf74](https://github.com/gwyntel/hermes-clm/commit/381cf74e10fb34776ea1deae67db056a46be5db2))
* **plugin:** decide prompt-section selection per rendered profile ([c38d08e](https://github.com/gwyntel/hermes-clm/commit/c38d08ec9c1632676407482a5c94eaaf9fb206e1))
* **store:** match aux proposal hash, boundary labels, batch unpin, retention and chain health ([40419f4](https://github.com/gwyntel/hermes-clm/commit/40419f42dbfba966dab41fc565827007546f6a51))
* **store:** serialise every read of the shared sqlite connection ([421c4da](https://github.com/gwyntel/hermes-clm/commit/421c4da84b10aacaa40992de4ad6858c0c75e4d5))
* **tools:** load as a context engine and wire the tool seam ([9f8cb1d](https://github.com/gwyntel/hermes-clm/commit/9f8cb1d634eb80a4c6b038dc9ca2a47bbdd98865))


### Documentation

* add project docs, limitations and sanitised research notes ([6ff451a](https://github.com/gwyntel/hermes-clm/commit/6ff451a58a1174bea23f9fc4bc5dea63807b7771))
* define request-projection CLM architecture ([a596d0f](https://github.com/gwyntel/hermes-clm/commit/a596d0fe7bbe672144408968805407ac2bafd98a))
* note that bench requires a source checkout ([d737767](https://github.com/gwyntel/hermes-clm/commit/d737767d092d8fa8edc37ded4e858735a88f95ce))
* record passing remote CI ([bfea9e0](https://github.com/gwyntel/hermes-clm/commit/bfea9e00e6c5379769ccfe087d8f15eb4045b947))
* remove corresponding-author email from research note ([7ef240e](https://github.com/gwyntel/hermes-clm/commit/7ef240ea4fa423623a331985ac9300b3f6f61128))
* sanitise local paths and clarify coverage limits ([883fa72](https://github.com/gwyntel/hermes-clm/commit/883fa72f88c46c2b6d5143aaa0b17193c554085e))
* specify cross-branch integration contracts ([34d6ffb](https://github.com/gwyntel/hermes-clm/commit/34d6ffbc98189c5a118c0b3f8f4e9870b58ea7bb))
* **store:** format/recovery spec + contract-signature and idempotency hardening ([5dd9282](https://github.com/gwyntel/hermes-clm/commit/5dd9282d83b9474ca6f24b11a60a2c2f8733fc73))

## [Unreleased]

### Added

- Installable `src/` package for Python 3.11–3.14 with a typed public surface,
  `LicenseRef-MutuaL-1.2` metadata and no runtime dependencies.
- `hermes-clm` console command with `version`, `doctor` and offline `bench` subcommands.
- Deterministic offline A/B/C/D benchmark harness with seven exact fixtures, token,
  cache, cost, edit, recovery and handoff metrics, and committed results.
- Documentation for benchmarking, limitations, and sanitised research notes with
  upstream attribution.
- CI across Python 3.11–3.14 for lint, format, typing, tests, security and build, with
  an honest aggregate gate.
- Release automation via Release Please, GitHub release artifacts, and a PyPI trusted
  publishing workflow gated on the `pypi` environment and a repository variable.

### Fixed

- `mypy src` no longer aborts on the repository-root plugin `__init__.py`; the config maps
  the src-layout package explicitly and ignores missing stubs for the optional Hermes host.
- The suite collects with importlib import mode, so the two same-named projection-purity
  test modules no longer collide; `pythonpath` replaces the conftest-only `sys.path` setup.
- The sdist now carries `plugin.yaml` and the root plugin entry point; `verify_wheel.py`
  checks every `src/hermes_clm` module in both artifacts and the plugin manifest version.

### Changed

- Lint gates correctness-oriented rules (`E4/E7/E9/F/I/B`); the previous select enabled
  pyupgrade/style rewrites the integrated engine does not follow.
- Coverage floor raised from 70% to 85% against a measured 86% branch result.
- Bandit gates at medium severity, with the three accepted Low findings documented.
- CI adds a separate mutation-harness job and includes it in the aggregate gate.
- README and limitations state the integrated-tree gate results, the plugin install and
  rollback path, and the simulator-only benchmark caveat with the committed totals.

[Unreleased]: https://github.com/gwyntel/hermes-clm/commits/main
