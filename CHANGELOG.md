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

[Unreleased]: https://github.com/gwyntel/hermes-clm/commits/main
