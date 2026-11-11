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
bandit -q -r src benchmarks
pip freeze --exclude-editable | grep -viE '^(pip|setuptools|hermes-clm)(==| @ )' > /tmp/audit-reqs.txt
pip-audit --strict --no-deps --disable-pip -r /tmp/audit-reqs.txt
python -m build
python -m twine check dist/*
python tests/packaging/verify_wheel.py dist
```

`pytest` covers the packaging contract, import isolation and the deterministic benchmark
harness. The full engine/store/tool suites arrive with their integration branches.

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
   `pyproject.toml` and `src/hermes_clm/_version.py`.
3. Merging the release PR creates the tag and GitHub release, and runs the build.
4. PyPI publishing uses OIDC trusted publishing through the `pypi` environment. It is
   disabled until the PyPI pending publisher exists (see `docs/limitations.md`); the
   GitHub release and attached artifacts do not depend on it.

Do not hand-edit the version or changelog for a release that Release Please owns.

## Security

Do not open a public issue for a vulnerability. Follow [`SECURITY.md`](SECURITY.md).
