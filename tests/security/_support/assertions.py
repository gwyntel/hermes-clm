"""Assertion helpers with one property in common: a refusal is never silently a pass."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Callable, Iterable

from _support import contract as C


def expect_refusal(fn: Callable[..., Any], *args, **kwargs) -> str:
    """Call `fn`; require that it refuses. Returns the refusal text.

    Accepts either an exception (any Exception, including sqlite errors) or a
    result object whose `ok` is false. An AssertionError from the call itself
    propagates -- it means the test's own setup was wrong, not that the
    implementation refused.
    """
    try:
        value = fn(*args, **kwargs)
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 - refusal shape is implementation-defined
        return f"{type(exc).__name__}: {exc}"
    if hasattr(value, "ok") or isinstance(value, dict):
        if not C.res_ok(value):
            errors = C.res_errors(value)
            return "; ".join(errors) if errors else repr(value)
    raise AssertionError(f"expected a refusal, got {value!r}")


def assert_strings_absent(obj: Any, needles: Iterable[str], *, where: str) -> None:
    haystack = C.flatten_text(obj)
    for needle in needles:
        assert needle not in haystack, f"{where}: leaked {needle!r}"


def assert_bytes_absent(paths: Iterable[str], needles: Iterable[bytes], *, where: str) -> None:
    blobs = []
    for path in paths:
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                blobs.append((path, fh.read()))
    for needle in needles:
        for path, blob in blobs:
            assert needle not in blob, f"{where}: {needle!r} found verbatim in {path}"


def assert_mode(path, expected: int, *, what: str) -> None:
    actual = os.stat(path).st_mode & 0o777
    assert actual == expected, f"{what} {path} mode {oct(actual)} != {oct(expected)}"


def subprocess_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    path = [p for p in sys.path if p and os.path.isdir(p)]
    env["PYTHONPATH"] = os.pathsep.join(path)
    env.pop("PYTHONWARNINGS", None)
    if extra:
        env.update(extra)
    return env


def run_child(code: str, *, timeout: int = 90, env_extra: dict | None = None) -> dict:
    """Run a snippet in a fresh interpreter and parse its last JSON line."""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env(env_extra),
    )
    out = proc.stdout.strip()
    if not out:
        raise AssertionError(
            f"child produced no stdout (rc={proc.returncode})\nSTDERR:\n{proc.stderr[-4000:]}"
        )
    line = out.splitlines()[-1]
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"child did not emit JSON (rc={proc.returncode}): {line!r}\n"
            f"STDERR:\n{proc.stderr[-4000:]}"
        ) from exc
    payload.setdefault("_rc", proc.returncode)
    payload.setdefault("_stderr", proc.stderr[-4000:])
    return payload


def disk_signature(hermes_home, session_id) -> dict:
    """(relpath -> (mode, size, mtime_ns, sha256)) for the whole session dir."""
    import stat as stat_mod

    directory = C.session_dir(hermes_home, session_id)
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(directory)):
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            digest = ""
            if stat_mod.S_ISREG(st.st_mode):
                with open(full, "rb") as fh:
                    digest = C.sha256_bytes(fh.read())
            out[os.path.relpath(full, str(directory))] = (
                stat_mod.S_IMODE(st.st_mode),
                st.st_size,
                st.st_mtime_ns,
                digest,
            )
    return out


def chain_health(store) -> dict:
    """Best-effort view of integrity reporting exposed by status()/verify_chain()."""
    out = {"verify_chain": None, "status_keys": [], "raw": None}
    fn = getattr(store, "verify_chain", None)
    if callable(fn):
        try:
            out["verify_chain"] = fn()
        except Exception as exc:  # noqa: BLE001
            out["verify_chain"] = f"{type(exc).__name__}: {exc}"
    try:
        st = C.status(store)
    except Exception as exc:  # noqa: BLE001
        out["raw"] = f"{type(exc).__name__}: {exc}"
        return out
    out["raw"] = st
    if isinstance(st, dict):
        out["status_keys"] = sorted(st)
    return out


def msg(role: str, content: Any, **extra) -> dict:
    base = {"role": role, "content": content}
    base.update(extra)
    return base


def tool_call(call_id: str, name: str = "bash") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }
