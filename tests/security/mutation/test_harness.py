"""Defect-injection harness: proof that this security suite goes red on real defects.

Why it is built this way
------------------------
The suite asserts behaviour of `hermes_clm`, which on this branch does not exist yet. A
suite that only ever skips proves nothing, and a suite that is "green" against nothing is
worse than no suite. So the harness ships a small correct implementation of the subset of
the contract the probes use (`mutation/fixture_impl`) and a switch that injects one
defect at a time (`CLM_FIXTURE_DEFECT`).

For every probe the harness proves both directions in a subprocess:

  * defect=none -> the real security test PASSES  (the assertion is satisfiable)
  * defect=Mx   -> the same test FAILS            (the assertion actually bites)

That pair is what "no fake green" means here. The fixture is never on the import path of
a normal run: it is copied into a temporary directory per run, and
`test_fixture_package_is_not_on_the_normal_import_path` guards the invariant.

When `src/hermes_clm` lands, these probes keep running against the fixture (proving the
tests), and the same tests are additionally exercised against the real implementation by
the ordinary suite -- the harness does not change.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _support.assertions import subprocess_env

from .defects import PROBE_IDS, PROBES

REPO = Path(__file__).resolve().parents[3]
SECURITY = REPO / "tests" / "security"
FIXTURE = Path(__file__).resolve().parent / "fixture_impl" / "hermes_clm"

pytestmark = pytest.mark.mutation


def _stage_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "hermes_clm"
    shutil.copytree(FIXTURE, target)
    return tmp_path


def _run(nodeid: str, defect: str, tmp_path: Path) -> subprocess.CompletedProcess:
    root = _stage_fixture(tmp_path)
    env = subprocess_env({"CLM_FIXTURE_DEFECT": defect, "CLM_MUTATION_FIXTURE": "1"})
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), str(REPO), str(SECURITY), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            nodeid,
            "-q",
            "--no-header",
            "--import-mode=importlib",
            "-o",
            f"pythonpath={root}",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _summary(proc: subprocess.CompletedProcess) -> str:
    tail = (proc.stdout or "").strip().splitlines()[-6:]
    return "\n".join(tail) + "\n--- stderr ---\n" + (proc.stderr or "")[-2000:]


def test_fixture_package_is_not_on_the_normal_import_path():
    try:
        import hermes_clm
    except Exception:
        return  # normal run: the fixture lives in tests/security/mutation/fixture_impl only
    assert "fixture_impl" not in (hermes_clm.__file__ or ""), (
        "the harness fixture shadowed the real package; a green run would be meaningless"
    )


def test_catalogue_is_internally_consistent():
    assert len(PROBE_IDS) == len(set(PROBE_IDS)), "duplicate defect ids"
    for probe in PROBES:
        assert probe["nodeid"].startswith("tests/security/")
        path, _, test_name = probe["nodeid"].partition("::")
        source = (REPO / path).read_text()
        assert f"def {test_name}(" in source, f"{probe['id']}: missing probe test {probe['nodeid']}"
        assert probe["t"] in source, f"{probe['id']}: {path} does not declare {probe['t']}"
        assert probe["defect"].strip(), f"{probe['id']}: no defect description"
        assert probe["invariant"].strip()


def test_every_defect_has_a_code_path_in_the_fixture():
    """A defect that disables nothing would make the red run meaningless."""
    source = "\n".join(p.read_text() for p in FIXTURE.rglob("*.py"))
    for probe in PROBES:
        assert f'"{probe["id"]}"' in source, (
            f'{probe["id"]} is catalogued but the fixture has no `D.active("{probe["id"]}")` path'
        )


def test_baseline_fixture_is_green_on_every_probe(tmp_path):
    failures = []
    for probe in PROBES:
        proc = _run(probe["nodeid"], "none", tmp_path / probe["id"])
        if proc.returncode != 0:
            failures.append(f"{probe['id']} -> {probe['nodeid']}\n{_summary(proc)}")
    assert not failures, (
        "the correct fixture implementation fails these probes, so the suite is asserting "
        "something the contract does not require:\n\n" + "\n\n".join(failures)
    )


@pytest.mark.parametrize("probe", PROBES, ids=PROBE_IDS)
def test_injected_defect_turns_the_probe_red(probe, tmp_path):
    proc = _run(probe["nodeid"], probe["id"], tmp_path / probe["id"])
    assert proc.returncode != 0, (
        f"defect {probe['id']} ({probe['defect']}) left {probe['nodeid']} GREEN: the suite "
        f"does not detect this class of defect.\n{_summary(proc)}"
    )
    assert f"FAILED {probe['nodeid']}" in (proc.stdout or ""), (
        f"{probe['id']}: the run was red, but {probe['nodeid']} was not the test that failed "
        f"(a collection error is not evidence):\n{_summary(proc)}"
    )
