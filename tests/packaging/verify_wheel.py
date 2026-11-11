#!/usr/bin/env python3
"""Verify built artifacts without installing Hermes or trusting the build log.

Usage: python tests/packaging/verify_wheel.py dist

The checks are deliberately explicit about *every* module under ``src/hermes_clm`` so a
merge that adds a file the packaging config does not discover fails here instead of at
runtime. Both the wheel and the sdist are checked; the sdist additionally carries the
Hermes plugin scaffold (``plugin.yaml`` + the root ``__init__.py`` shim), docs, benchmarks
and tests.
"""

from __future__ import annotations

import email
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}")
    raise SystemExit(1)


def package_modules() -> set[str]:
    """Every importable source file plus the typing marker, as wheel-relative names."""

    modules = {f"hermes_clm/{path.name}" for path in sorted((SRC / "hermes_clm").glob("*.py"))}
    modules.add("hermes_clm/py.typed")
    return modules


def plugin_version() -> str:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    if match is None:
        fail("plugin.yaml has no version line")
    return match.group(1)


def check_wheel(path: Path, expected_version: str) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        expected = package_modules()
        missing = expected - names
        if missing:
            fail(f"wheel missing package files: {sorted(missing)}")
        extra = {
            name for name in names if name.startswith("hermes_clm/") and not name.endswith("/")
        } - expected
        if extra:
            fail(f"wheel carries unexpected package files: {sorted(extra)}")
        if any(name.startswith(("tests/", "benchmarks/", "docs/")) for name in names):
            fail("wheel contains development-only trees")
        if "__init__.py" in names:
            fail("wheel must not ship the repository-root plugin shim")
        licenses = [name for name in names if ".dist-info/licenses/LICENSE" in name]
        if not licenses:
            fail("wheel does not carry the license file")
        metadata_name = next((name for name in names if name.endswith(".dist-info/METADATA")), None)
        if metadata_name is None:
            fail("wheel has no METADATA")
        metadata = email.message_from_bytes(wheel.read(metadata_name))
        if metadata["Name"] != "hermes-clm":
            fail(f"unexpected wheel name: {metadata['Name']}")
        if metadata["License-Expression"] != "LicenseRef-MutuaL-1.2":
            fail(f"unexpected license expression: {metadata['License-Expression']}")
        if metadata["Requires-Python"] != ">=3.11":
            fail(f"unexpected Requires-Python: {metadata['Requires-Python']}")
        runtime = [
            value for value in metadata.get_all("Requires-Dist", []) if "extra ==" not in value
        ]
        if runtime:
            fail(f"runtime dependencies must be empty: {runtime}")
        if metadata.get("Version") != expected_version:
            fail(f"unexpected version: {metadata.get('Version')} (expected {expected_version})")
        print(f"wheel ok: {path.name} ({len(names)} files, {len(expected)} package files)")


def check_sdist(path: Path, expected_version: str) -> None:
    with tarfile.open(path) as sdist:
        names = set(sdist.getnames())
    suffixes = {name.rsplit("/", 1)[-1] for name in names}
    for required in {"LICENSE", "README.md", "pyproject.toml", "MANIFEST.in", "plugin.yaml"}:
        if required not in suffixes:
            fail(f"sdist missing {required}")
    if not any(name.endswith("/__init__.py") and name.count("/") == 1 for name in names):
        fail("sdist missing the root plugin entry-point __init__.py")
    for module in sorted(package_modules()):
        if not any(name.endswith(f"src/{module}") for name in names):
            fail(f"sdist missing src/{module}")
    for required in (
        "benchmarks/runner.py",
        "benchmarks/fixtures/long-coding.json",
        "docs/benchmarking.md",
        "docs/limitations.md",
        "tests/packaging/test_packaging.py",
    ):
        if not any(name.endswith(required) for name in names):
            fail(f"sdist missing {required}")
    if any("__pycache__" in name or "/.venv/" in name or "/dist/" in name for name in names):
        fail("sdist contains local build or cache artifacts")
    if plugin_version() != expected_version:
        fail(f"plugin.yaml version {plugin_version()} disagrees with {expected_version}")
    print(f"sdist ok: {path.name} ({len(names)} entries)")


def main(argv: list[str]) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "dist")
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"expected exactly one wheel and one sdist in {dist}, got {wheels} {sdists}")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    check_wheel(wheels[0], project["version"])
    check_sdist(sdists[0], project["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
