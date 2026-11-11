#!/usr/bin/env python3
"""Verify built artifacts without installing Hermes or trusting the build log.

Usage: python tests/packaging/verify_wheel.py dist
"""

from __future__ import annotations

import email
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path


def fail(message: str) -> None:
    print(f"ERROR: {message}")
    raise SystemExit(1)


def check_wheel(path: Path, expected_version: str) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
        required = {
            "hermes_clm/__init__.py",
            "hermes_clm/_version.py",
            "hermes_clm/cli.py",
            "hermes_clm/__main__.py",
            "hermes_clm/py.typed",
        }
        missing = required - names
        if missing:
            fail(f"wheel missing files: {sorted(missing)}")
        if any(name.startswith(("tests/", "benchmarks/")) for name in names):
            fail("wheel contains development-only trees")
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
        print(f"wheel ok: {path.name} ({len(names)} files)")


def check_sdist(path: Path) -> None:
    with tarfile.open(path) as sdist:
        names = set(sdist.getnames())
    suffixes = {name.rsplit("/", 1)[-1] for name in names}
    for required in {"LICENSE", "README.md", "pyproject.toml"}:
        if required not in suffixes:
            fail(f"sdist missing {required}")
    if not any(name.endswith("src/hermes_clm/py.typed") for name in names):
        fail("sdist missing py.typed")
    print(f"sdist ok: {path.name}")


def main(argv: list[str]) -> int:
    dist = Path(argv[1] if len(argv) > 1 else "dist")
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"expected exactly one wheel and one sdist in {dist}, got {wheels} {sdists}")
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    check_wheel(wheels[0], project["version"])
    check_sdist(sdists[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
