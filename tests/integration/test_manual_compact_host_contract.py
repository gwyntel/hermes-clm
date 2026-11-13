"""The gateway/CLI ``/compact`` contract, against a real store.

Mirrors ``gateway/session_mutation_compress.py`` (desktop app + ``-z`` CLI both
reach compaction through the gateway, never through a local compressor):

* ``_engine_compress`` calls ``engine.compress(head, current_tokens=…,
  focus_topic=…, force=True, memory_context=…)`` and treats *identity* as "no
  changes from compression" — a no-op success the operator cannot see through;
* on success it calls ``engine.on_session_start(new_id,
  boundary_reason="compression", old_session_id=old_id)``, rotating the id with no
  reset in between;
* a cold session gets a one-off engine bound with
  ``bind_session_state(session_db, session_id)``.

These are the paths that produced "no store for this session" in a live profile,
so they are exercised here with the real ``ContextStore`` and a throwaway home.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_clm.engine import CLMEngine
from hermes_clm.store import ContextStore, lineage_path, resolve_session_lineage

pytestmark = pytest.mark.integration

HERMES_CHECKOUT = os.environ.get("CLM_HERMES_CHECKOUT", "").strip()

TOOLS = ("shell_exec", "read_file", "web_search", "grep")


def _rough_tokens(messages) -> int:
    """The gateway's own cheap estimator (chars/4), reproduced locally."""

    total = 0
    for row in messages:
        content = row.get("content")
        if isinstance(content, str):
            total += len(content)
        for call in row.get("tool_calls") or ():
            total += len(str(call))
    return total // 4


def _gateway_history(turns: int = 5, tool_chars: int = 12_000) -> list[dict]:
    """What ``get_messages_as_conversation`` hands the engine: uids, tool calls."""

    rows = [{"role": "system", "content": "SYSTEM PROMPT", "message_uid": "uid-sys"}]
    for i in range(turns):
        rows.append({"role": "user", "content": f"task {i}", "message_uid": f"uid-u{i}"})
        rows.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": TOOLS[i % len(TOOLS)], "arguments": "{}"},
                    }
                ],
                "message_uid": f"uid-a{i}",
            }
        )
        rows.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": f"RESULT {i}\n" + "Z" * tool_chars,
                "message_uid": f"uid-t{i}",
            }
        )
    return rows


def _stubs(messages) -> int:
    return sum(1 for row in messages if "[clm.offloaded" in str(row.get("content")))


def _engine(home, session_id: str, *, context_length: int = 120_000) -> CLMEngine:
    engine = CLMEngine(config={"clm.hermes_home": str(home), "clm.aux_enabled": False})
    engine.on_session_start(session_id, hermes_home=str(home), context_length=context_length)
    return engine


def test_gateway_compaction_returns_a_real_reduction_not_an_identity(home):
    """The acceptance criterion: fewer tokens, from persisted state, via /compact."""

    head = _gateway_history()
    engine = _engine(home, "sess-gateway")
    current_tokens = _rough_tokens(head)

    compressed = engine.compress(
        [dict(row) for row in head],
        current_tokens=current_tokens,
        focus_topic=None,
        force=True,
        memory_context="",
    )

    assert compressed != head, "the gateway would report 'no changes from compression'"
    assert _rough_tokens(compressed) < current_tokens
    assert _stubs(compressed) >= 1
    # Persisted, not a local-only rewrite: a fresh read of the store proves it.
    revision = engine.store.status()["revision"]
    assert revision >= 2
    replay = engine.select_context(
        [dict(row) for row in head], conversation_messages=[dict(row) for row in head]
    )
    assert _stubs(replay) >= 1
    assert engine.store.status()["revision"] == revision


def test_gateway_rotation_after_compaction_keeps_the_compaction(home):
    """Post-commit id rotation must not resurrect the compacted transcript."""

    head = _gateway_history()
    engine = _engine(home, "sess-old")
    engine.compress([dict(row) for row in head], force=True)
    assert engine._last_compress_aborted is False

    # Exact host notification (agent/conversation_compression.py post-commit).
    engine.on_session_start("sess-new", boundary_reason="compression", old_session_id="sess-old")

    assert engine.store is not None
    replay = engine.select_context(
        [dict(row) for row in head], conversation_messages=[dict(row) for row in head]
    )
    assert _stubs(replay) >= 1, "the child session lost the parent's committed offloads"
    assert _rough_tokens(replay) < _rough_tokens(head)


def test_cold_gateway_engine_bound_by_session_id_can_compact(home):
    """A one-off engine for a cold session has no ``on_session_start`` first."""

    engine = CLMEngine(config={"clm.hermes_home": str(home), "clm.aux_enabled": False})
    engine.bind_session_state(None, session_id="sess-cold-gateway")
    head = _gateway_history()

    compressed = engine.compress([dict(row) for row in head], force=True)

    assert engine._last_compress_aborted is False, engine._last_summary_error
    assert _rough_tokens(compressed) < _rough_tokens(head)


def test_second_engine_cannot_open_a_second_writer_for_the_same_store(home):
    owner = _engine(home, "sess-lock")
    intruder = CLMEngine(config={"clm.hermes_home": str(home), "clm.aux_enabled": False})
    intruder.on_session_start("sess-lock", hermes_home=str(home))

    if intruder.store is None:
        assert (intruder.get_status()["store_error"] or {}).get("code")
        out = intruder.compress(_gateway_history(), force=True)
        assert intruder._last_compress_aborted is True
        assert "[clm.offloaded" not in str(out), "a refused engine must not rewrite the transcript"
    assert owner.store.status()["ok"] is True


def test_store_read_only_engine_reports_typed_refusal(home):
    """Read-only attach (recovery/inspection) must refuse `/compact`, not fake it."""

    seeded = ContextStore.for_session(str(home), "sess-readonly")
    seeded.ingest(_gateway_history(turns=2))
    seeded.close()

    reader = ContextStore.for_session(str(home), "sess-readonly", read_only=True)
    try:
        engine = CLMEngine(config={"clm.hermes_home": str(home), "clm.aux_enabled": False})
        engine.on_session_start("sess-readonly", hermes_home=str(home))
        engine._store = reader
        engine._store_session = "sess-readonly"
        engine._store_home = str(home)
        head = _gateway_history(turns=2)
        out = engine.compress([dict(row) for row in head], force=True)
        assert engine._last_compress_aborted is True
        assert (engine._last_summary_error or "").startswith("store_read_only")
        assert out == head, "a read-only store must not rewrite the transcript"
    finally:
        reader.close()


# --------------------------------------------------------------------------- #
# The real host notification, in the host's own interpreter
# --------------------------------------------------------------------------- #
#
# ``_notify_context_engine_compression_complete`` lives in ``agent/`` and pulls
# the host's import graph, so it runs in a subprocess: the test process stays free
# of Hermes modules (a sibling test asserts that), and the probe is closer to the
# real gateway, where the notification and the engine share one cold process.

PROBE = r"""
import json, os, sys
sys.path.insert(0, os.environ["CLM_HERMES_CHECKOUT"])
sys.path.insert(0, os.environ["CLM_REPO_ROOT"])

from agent.conversation_compression import (
    _notify_context_engine_compression_complete as notify,
)
from hermes_clm.engine import CLMEngine

HOME = os.environ["CLM_PROBE_HOME"]
MODE = os.environ["CLM_PROBE_MODE"]


class Agent:
    platform = "cli"
    _gateway_session_key = None

    def __init__(self, engine):
        self.context_compressor = engine


def transcript(turns=5, chars=12000):
    rows = [{"role": "system", "content": "SYSTEM PROMPT", "message_uid": "uid-sys"}]
    for i in range(turns):
        rows.append({"role": "user", "content": f"task {i}", "message_uid": f"uid-u{i}"})
        rows.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"call_{i}", "type": "function",
                     "function": {"name": "shell_exec", "arguments": "{}"}}
                ],
                "message_uid": f"uid-a{i}",
            }
        )
        rows.append(
            {"role": "tool", "tool_call_id": f"call_{i}",
             "content": f"RESULT {i}\n" + "Z" * chars, "message_uid": f"uid-t{i}"}
        )
    return rows


def cold_engine(session_id):
    # Exactly how gateway/session_mutation_compress.py builds its one-off.
    engine = CLMEngine(config={"clm.hermes_home": HOME, "clm.aux_enabled": False})
    engine.bind_session_state(None, session_id=session_id)
    return engine


def stubs(messages):
    return sum(1 for row in messages if "[clm.offloaded" in str(row.get("content")))


def pin_one(engine):
    snapshot = engine.store.snapshot()
    span_id = sorted(snapshot.spans)[0]
    from hermes_clm.models import EditKind, EditOperation

    engine.store.stage(
        [EditOperation(kind=EditKind.PIN, span_ids=(span_id,))],
        expected_revision=snapshot.revision,
        txn_id="probe-pin",
        actor_origin="agent",
    )
    engine.store.commit(txn_id="probe-pin", expected_revision=snapshot.revision)
    return span_id


head = transcript()
out = {"mode": MODE, "checkout": os.environ["CLM_HERMES_CHECKOUT"]}

if MODE == "adopt":
    engine = cold_engine("sess-host-old")
    engine.compress([dict(row) for row in head], force=True)
    store = engine.store
    out["aborted"] = engine._last_compress_aborted
    out["summary_error"] = engine._last_summary_error
    out["delivered"] = notify(
        Agent(engine), new_session_id="sess-host-new", old_session_id="sess-host-old"
    )
    out["session_id"] = engine.session_id
    out["same_store"] = engine.store is store
elif MODE == "cold":
    engine = cold_engine("sess-cold-parent")
    engine.compress([dict(row) for row in head], force=True)
    out["stubs"] = stubs(engine.select_context([dict(r) for r in head],
                                               conversation_messages=[dict(r) for r in head]) or head)
    out["pinned"] = pin_one(engine)
    out["db_id"] = engine.store.db_id
    out["revision"] = engine.store.status()["revision"]
    out["delivered"] = notify(
        Agent(engine), new_session_id="sess-cold-child", old_session_id="sess-cold-parent"
    )
    engine.on_session_end("sess-cold-child", [])

    child = cold_engine("sess-cold-child")
    out["child_db_id"] = child.store.db_id if child.store else None
    out["child_revision"] = child.store.status()["revision"] if child.store else None
    out["child_pins"] = sorted(child.store.snapshot().pins) if child.store else None
    replayed = child.select_context([dict(r) for r in head],
                                    conversation_messages=[dict(r) for r in head])
    out["replay_stubs"] = stubs(replayed or head)
    out["replay_shrank"] = bool(replayed) and len(str(replayed)) < len(str(head))
elif MODE == "repeat":
    first = cold_engine("sess-rep-0")
    first.compress([dict(row) for row in head], force=True)
    out["db_id"] = first.store.db_id
    out["rot1"] = notify(Agent(first), new_session_id="sess-rep-1", old_session_id="sess-rep-0")
    first.on_session_end("sess-rep-1", [])

    second = cold_engine("sess-rep-1")
    out["second_db_id"] = second.store.db_id if second.store else None
    second.compress([dict(row) for row in head], force=True)
    out["rot2"] = notify(Agent(second), new_session_id="sess-rep-2", old_session_id="sess-rep-1")
    second.on_session_end("sess-rep-2", [])

    third = cold_engine("sess-rep-2")
    out["third_db_id"] = third.store.db_id if third.store else None

print(json.dumps(out))
"""


def _cold_gateway_engine(home, session_id: str) -> CLMEngine:
    """Exactly how ``gateway/session_mutation_compress.py`` builds its one-off."""

    engine = CLMEngine(config={"clm.hermes_home": str(home), "clm.aux_enabled": False})
    engine.bind_session_state(None, session_id=session_id)
    return engine


def _run_probe(mode: str, home):
    """Run the host-notification probe against a throwaway home; return its JSON."""

    if (
        not HERMES_CHECKOUT
        or not (Path(HERMES_CHECKOUT) / "agent" / "conversation_compression.py").is_file()
    ):
        pytest.skip("set CLM_HERMES_CHECKOUT to a Hermes checkout to run the real host probe")
    env = dict(os.environ)
    env.update(
        CLM_HERMES_CHECKOUT=HERMES_CHECKOUT,
        CLM_REPO_ROOT=str(Path(__file__).resolve().parents[2]),
        CLM_PROBE_HOME=str(home),
        CLM_PROBE_MODE=mode,
        PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"),
    )
    proc = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, timeout=180
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, f"probe printed no JSON:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(lines[-1])


def test_the_real_host_notification_adopts_the_compressed_store(home):
    """Call the host's own post-commit callback, not a hand-written equivalent."""

    out = _run_probe("adopt", home)

    assert out["aborted"] is False, out["summary_error"]
    assert out["delivered"] is True, "the host hook must be callable and must return True"
    assert out["session_id"] == "sess-host-new"
    assert out["same_store"] is True, "the host callback rotated the id onto a new store"
    assert resolve_session_lineage(home, "sess-host-new") == "sess-host-old"


def test_cold_compress_then_host_notify_then_close_then_child_opens_the_same_archive(home):
    """The reported symptom, end to end, through the host's own notification.

    The gateway compacts with a one-off engine, the host rotates the id, the
    one-off engine is closed, and the next turn builds a fresh engine for the child
    id. That engine must find the archive the parent committed — spans, pin and
    offloads — or ``select_context`` has nothing to project and the compacted
    transcript comes back whole.
    """

    out = _run_probe("cold", home)

    assert out["delivered"] is True
    assert out["child_db_id"] == out["db_id"], "the child opened a different archive"
    assert out["child_revision"] == out["revision"]
    assert out["pinned"] in (out["child_pins"] or []), "the child lost the parent's pin"
    assert out["replay_stubs"] >= 1, "the child is store-less: the parent's offloads are gone"
    assert out["replay_shrank"] is True


def test_repeated_cold_rotations_keep_one_archive(home):
    """Two ``/compact``s in a row, each with its own one-off engine."""

    out = _run_probe("repeat", home)

    assert out["rot1"] is True and out["rot2"] is True
    assert out["second_db_id"] == out["db_id"], "the second engine opened a different archive"
    assert out["third_db_id"] == out["db_id"]
    assert resolve_session_lineage(home, "sess-rep-1") == "sess-rep-0"
    assert resolve_session_lineage(home, "sess-rep-2") == "sess-rep-0"


def test_a_new_session_does_not_inherit_the_archive(home):
    """``/new`` also calls the engine; its boundary must not record lineage."""

    engine = _cold_gateway_engine(home, "sess-before-new")
    engine.compress([dict(row) for row in _gateway_history()], force=True)

    # What the host does for /new: a fresh id and no compression boundary.
    engine.on_session_reset()
    engine.on_session_start("sess-after-new", hermes_home=str(home))

    assert engine.store.session_id == "sess-after-new"
    assert engine.store.snapshot().spans == {}
    assert not lineage_path(home, "sess-after-new").exists()
    assert resolve_session_lineage(home, "sess-after-new") is None
