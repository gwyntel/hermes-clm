# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/); Release Please
generates release notes from those commits.

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
