# Contributing to hermes-clm

Bug reports, design critiques and pull requests are welcome. This repository is small on
purpose: keep changes narrow, testable and reversible.

## Development setup

```bash
git clone https://github.com/gwyntel/hermes-clm
cd hermes-clm
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Hermes is **not** installed for development by default. The package must import and its
CLI must run without it; host adapters use guarded, lazy imports.

## Gates

Run the same gates CI runs before opening a pull request:

```bash
ruff check .
ruff format --check .
mypy src
pytest -m "not slow" --cov --cov-report=term-missing
pytest tests/security/mutation -q
bandit -q -ll -r src benchmarks -x tests
pip freeze --exclude-editable | grep -viE '^(pip|setuptools|hermes-clm)(==| @ )' > /tmp/audit-reqs.txt
pip-audit --strict --no-deps --disable-pip -r /tmp/audit-reqs.txt
python -m build
python -m twine check dist/*
python tests/packaging/verify_wheel.py dist
```

`pytest` covers unit, integration, packaging, security, fuzz and the benchmark harness; the
mutation harness is a separate command because it re-runs probes in subprocesses. Coverage
has an 85% floor in `pyproject.toml`. `ruff check .` currently reports findings in
`tests/security/**` and one unused import in `src/hermes_clm/archive.py`; see
[`docs/limitations.md`](docs/limitations.md#integration-status) before assuming a red gate
is yours.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
  `docs:`, `test:`, `build:`, `ci:`, `chore:`, `refactor:`, `perf:`.
- PR titles are checked against the same convention because Release Please reads them.
- One logical change per commit. Add tests with behaviour changes; a known-bad test that
  goes red without the fix is better than a coverage number.
- Do not force-push a branch under review.
- Do not include credentials, session archives, model prompts containing private data, or
  benchmark output from a live paid run.

## Offline by default

CI never calls a model or a provider. The benchmark harness is deterministic and offline.
A live benchmark requires a separate, explicit approval listing the model, provider alias
classification, call count, token ceiling, key source and estimated cost. Never put a key
in the repository or in CI secrets for a benchmark.

## Releases

Releases are automated:

1. Conventional commits land on `main` through a pull request.
2. Release Please opens or updates a release PR with `CHANGELOG.md` and version bumps for
   `pyproject.toml`, `src/hermes_clm/_version.py`, and `plugin.yaml`.
3. Merging the release PR updates `main`. Until the upstream single-package component
   mismatch ([release-please-action#1205](https://github.com/googleapis/release-please-action/issues/1205))
   is fixed, create the tag and GitHub release at the release merge commit and change the
   release PR label from `autorelease: pending` to `autorelease: tagged`; otherwise the
   next run aborts on an untagged merged release PR.
4. The GitHub release runs the build and attaches the distributions. PyPI publishing uses
   OIDC trusted publishing through the `pypi` environment. It is disabled until the PyPI
   pending publisher exists (see `docs/limitations.md`); the GitHub release and attached
   artifacts do not depend on it.

Do not hand-edit the version or changelog for a release that Release Please owns.

## Security

Do not open a public issue for a vulnerability. Follow [`SECURITY.md`](SECURITY.md).
