"""Packaging-level tests: metadata, import isolation and the CLI.

These tests deliberately avoid importing Hermes. The subprocess test installs a
meta-path blocker so an accidental host import fails loudly instead of passing on a
machine where Hermes happens to be installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import hermes_clm  # noqa: E402
from hermes_clm import cli  # noqa: E402
from hermes_clm._version import __version__ as module_version  # noqa: E402


def test_metadata_contract() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "hermes-clm"
    assert project["requires-python"] == ">=3.11"
    assert project["license"] == "LicenseRef-MutuaL-1.2"
    assert project["license-files"] == ["LICENSE"]
    assert project["dependencies"] == []
    assert project["scripts"]["hermes-clm"] == "hermes_clm.cli:main"
    assert "License ::" not in " ".join(project["classifiers"])
    assert {
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
    }.issubset(set(project["classifiers"]))


def test_versions_agree() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == module_version == hermes_clm.__version__


def test_license_text_is_filled_in() -> None:
    text = (REPO_ROOT / "LICENSE").read_text()
    assert text.startswith("# The Mutualist License v1.2")
    assert "**Name of this software:** hermes-clm" in text
    assert "https://github.com/gwyntel/hermes-clm" in text
    assert "**Original copyright holder(s):** GwynTel" in text
    assert "(name)" not in text


def test_import_without_hermes() -> None:
    code = """
import importlib.abc
import sys

BLOCKED = {"hermes", "hermes_cli", "agent", "run_agent"}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED:
            raise ImportError("Hermes import blocked by isolation test: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
import hermes_clm
import hermes_clm.cli
import hermes_clm.__main__
assert hermes_clm.__version__
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"hermes-clm {module_version}"


def test_cli_doctor_reports_capabilities(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["doctor", "--benchmarks-dir", str(REPO_ROOT / "benchmarks")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["package"] == "hermes-clm"
    assert report["python_ok"] is True
    assert report["benchmarks_available"] is True


def test_cli_bench_writes_reports(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    assert (
        cli.main(
            [
                "bench",
                "--benchmarks-dir",
                str(REPO_ROOT / "benchmarks"),
                "--strategies",
                "A,C",
                "--json",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads(out.read_text())
    assert {row["strategy"] for row in report["results"]} == {"A", "C"}
    assert report["evidence_class"] == "offline-simulation"


def test_cli_bench_fails_clearly_outside_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["bench", "--benchmarks-dir", str(tmp_path)]) == 2
    assert "not installed with the wheel" in capsys.readouterr().err


def test_benchmark_discovery_walks_up_from_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    found = cli._find_benchmarks_dir(None)
    assert found == REPO_ROOT / "benchmarks"


def test_cli_bench_stdout_and_out_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out.json"
    assert (
        cli.main(
            [
                "bench",
                "--benchmarks-dir",
                str(REPO_ROOT / "benchmarks"),
                "--strategies",
                "A",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert json.loads(out.read_text())["results"]
    assert (
        cli.main(
            [
                "bench",
                "--benchmarks-dir",
                str(REPO_ROOT / "benchmarks"),
                "--strategies",
                "A",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["results"]


def test_cli_bench_rejects_unknown_strategy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(
            [
                "bench",
                "--benchmarks-dir",
                str(REPO_ROOT / "benchmarks"),
                "--strategies",
                "Z",
            ]
        )
        == 2
    )
    assert "unknown strategy" in capsys.readouterr().err


def test_python_dash_m_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "hermes_clm", "version"],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("hermes-clm ")
