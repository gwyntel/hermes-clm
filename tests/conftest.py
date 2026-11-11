"""Root pytest configuration for hermes-clm.

Import-safe from a clean checkout: ``src/`` and shared test helpers are added to
``sys.path`` so the src-layout package and fixtures import without an editable install.
Shared fixtures live here because ``tests/security`` and ``tests/fuzz`` consume the same
throwaway HERMES_HOME and store fixtures.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
SECURITY = TESTS / "security"
MUTATION_FIXTURE = os.environ.get("CLM_MUTATION_FIXTURE") == "1"
for candidate in (TESTS, SECURITY) if MUTATION_FIXTURE else (SRC, TESTS, SECURITY):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def pytest_configure(config):  # noqa: D401 - pytest hook
    """Fail fast if the package is not importable from src; register suite markers."""
    import importlib

    config.addinivalue_line("markers", "security: security/adversarial suite (plan D)")
    config.addinivalue_line("markers", "fuzz: bounded deterministic property fuzz")
    config.addinivalue_line("markers", "mutation: defect-injection harness self-proof")
    config.addinivalue_line(
        "markers", "integration: needs another delegate's branch (engine/tools)"
    )
    config.addinivalue_line(
        "markers", "t(*ids): design-review acceptance test ids covered (T1..T49)"
    )
    if not MUTATION_FIXTURE:
        importlib.import_module("hermes_clm.identity")


@pytest.fixture
def home(tmp_path):
    """A throwaway HERMES_HOME. Never the operator's real one."""
    h = tmp_path / "hermes-home"
    h.mkdir(mode=0o700)
    return h


@pytest.fixture
def other_home(tmp_path):
    h = tmp_path / "hermes-home-other"
    h.mkdir(mode=0o700)
    return h


@pytest.fixture
def sid():
    return "sess-sec-0001"


@pytest.fixture
def store(home, sid):
    from _support import contract as C

    s = C.for_session(home, sid)
    try:
        yield s
    finally:
        C.close(s)


@pytest.fixture
def base_messages():
    """Canonical conversation shape used by most tests: system, operator, model."""
    return [
        {"role": "system", "content": "HOST SYSTEM PROMPT: be careful."},
        {"role": "user", "content": "operator constraint: never delete the audits table"},
        {"role": "assistant", "content": "acknowledged"},
    ]


@pytest.fixture
def audit_tree():
    """Snapshot every path, mode, size, mtime and digest under a directory."""

    def snapshot(root):
        out = {}
        root = str(root)
        for dirpath, dirnames, filenames in os.walk(root):
            for name in sorted(dirnames + filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                st = os.lstat(full)
                digest = ""
                if stat.S_ISREG(st.st_mode):
                    import hashlib

                    with open(full, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                out[rel] = (
                    stat.S_IMODE(st.st_mode),
                    st.st_mode & stat.S_IFMT(st.st_mode),
                    st.st_size,
                    st.st_mtime_ns,
                    digest,
                )
        return out

    return snapshot
