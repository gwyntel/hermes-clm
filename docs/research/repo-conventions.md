# Repository and release conventions for a public Python package

Derived from an internal audit of GwynTel repositories (2026-11-10). Access-control and
account findings are intentionally omitted; this note keeps only conventions that apply to
a public, packaged repository.

## Packaging

- Use a `src/` layout (`src/hermes_clm/`) with `setuptools>=77` and PEP 639 licence
  metadata. A flat layout invites accidental imports from the checkout and blurs the
  installed surface.
- `requires-python = ">=3.11"`; declare the supported minor versions in classifiers and
  exercise them in CI rather than in prose.
- Single-source the version. A static `src/hermes_clm/_version.py` annotated for Release
  Please, plus the same static value in `pyproject.toml`, gives the sdist, wheel and runtime
  one number without importing build tooling. A packaging test must fail if they disagree.
- Ship `py.typed` and declare it as package data. Inline types that are not shipped are a
  false promise.
- Keep runtime dependencies at zero where the standard library suffices. Hermes is an
  optional host and must never appear in `dependencies`.
- `[project.scripts]` is the only install-path surface. A thin `plugin.yaml` manifest, if a
  host needs one, must point at the package rather than carrying a second copy of the code.

## Licensing

- GwynTel-published work uses the Mutualist License v1.2 with the full text at root
  `LICENSE` and the `## Credit` block filled in. The holder is the org or handle, never a
  personal name.
- `license = "MutuaL-1.2"` is not a valid PEP 639 expression and fails the build.
  `license = "LicenseRef-MutuaL-1.2"` with `license-files = ["LICENSE"]` builds cleanly and
  puts the licence in `dist-info/licenses/`, satisfying the "licence travels with the copy"
  obligation.
- Do not add a `License ::` classifier for MutuaL: it is not an SPDX-listed OSI licence and
  PyPI rejects unknown classifiers.
- Add `NOTICE.md` only when third-party code or text is vendored or adapted. Name the
  upstream, its revision and what was adapted. If nothing is copied, an attribution section
  in the README is the honest artefact.

## CI shape

- `permissions: contents: read` at workflow level; elevate per job only.
- `concurrency` keyed on workflow + ref with `cancel-in-progress: true`.
- `timeout-minutes` on every job.
- Pin third-party actions deliberately. `astral-sh/setup-uv` has no floating major; pin an
  exact version. GitHub-owned actions can use a major tag while the workflow set is young,
  then move to SHAs.
- Separate jobs for lint/format, type check, test matrix, security and build. A single job
  that runs everything hides which gate failed and cannot be a required check with a stable
  name.
- One aggregate `gates` job that `needs:` every other job. Use `if: always()` and inspect
  `needs.*.result`; do **not** rely on a skipped job looking green. Branch protection then
  requires one check name.
- Test matrix: 3.11, 3.12, 3.13, 3.14 with `fail-fast: false`.
- Build job: `python -m build`, `twine check`, a wheel-content verifier, then install the
  wheel in a clean venv and run the console script. Building is not proof of installability.
- Security: `bandit` over shipped source and `pip-audit` over the runtime requirement set.
  Make the audit scope explicit; auditing the dev environment produces noise that trains
  people to ignore the gate.

## Releases

- Conventional Commits from the first commit; there is no useful changelog automation
  without a parseable history. Check PR titles too, because squash merges inherit them.
- Choose one release tool. Release Please is a good fit for a single Python package: it
  updates `CHANGELOG.md`, `pyproject.toml` and the annotated `_version.py`, opens a release
  PR, then tags and publishes on merge.
- Attach the wheel and sdist to the GitHub release. A release page without artifacts makes
  every downstream install fall back to a git URL.
- PyPI uses OIDC trusted publishing only: `id-token: write`, `environment: pypi`, and
  `pypa/gh-action-pypi-publish` with attestations. No API token, no long-lived secret.
- Trusted publishing requires a pending publisher created by an account owner before the
  first release. Until it exists, keep the publish job gated by an explicit repository
  variable and say so in the docs; the GitHub release can proceed without PyPI.
- Add a manual TestPyPI path on `workflow_dispatch` with its own `testpypi` environment so a
  release can be rehearsed without touching the real index.

## Branch protection and hygiene

- On a public repository, a ruleset on `main` works on the free plan: require a PR, require
  the aggregate CI gate, require branches to be up to date, block force-push and deletion,
  require linear history.
- Skip "require signed commits" until signing is consistent; a rule everyone bypasses is
  worse than no rule. Skip required approving reviews when there is one maintainer; use
  admin bypass rather than a fake approval.
- Dependabot for `pip` and `github-actions`, weekly, with a small open-PR limit.
- Enable Dependabot alerts, secret scanning and push protection at repository creation.
- Pin actions to SHAs once the workflow set stabilises.

## README style

- Open with the repository name and a one-line pitch, then the problem and the solution.
- Give copy-pasteable install commands. If the package is on PyPI, show `pip install`; a
  README that only documents `git clone` hides whether packaging works.
- State the licence in prose and link the full text.
- Put limits in the README, not only in a wiki page nobody reads.
