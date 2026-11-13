"""Session rotation: ``/compact`` renames the session without losing its archive.

The host compacts in place and then rotates the session id, telling the engine
through ``on_session_start(new_id, boundary_reason="compression",
old_session_id=old_id)`` — with no ``on_session_end``/``on_session_reset`` in
between (``agent/conversation_compression.py`` post-commit notification). An
engine that treats any new id as a new conversation opens an empty store for the
child, so the very next ``select_context`` sees no revisions and the transcript
the operator just compacted comes back whole: the live "no store after /compact"
symptom.

Two properties are pinned here:

* the **live** adoption keeps the open handle (same store, same revision lineage);
* the relation is written to disk, so a **cold** engine for the child — the
  gateway builds a one-off engine per compression, then closes it — resolves the
  same archive, pins included.

Adoption is gated on the compression boundary and on this engine serving the
stated parent: ``/new``, ``/resume`` and ``/branch`` must never inherit an
archive, and a stale notification naming someone else must not either.
"""

from __future__ import annotations

from hermes_clm import engine as engine_module
from hermes_clm import store as store_module
from hermes_clm.engine import CLMEngine
from hermes_clm.models import EditKind, EditOperation
from hermes_clm.store import lineage_path, resolve_session_lineage


def _transcript(turns: int = 4, tool_chars: int = 900) -> list[dict]:
    rows = [{"role": "system", "content": "sys", "message_uid": "uid-sys"}]
    for i in range(turns):
        rows.append({"role": "user", "content": f"step {i}", "message_uid": f"uid-u{i}"})
        rows.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "shell_exec", "arguments": "{}"},
                    }
                ],
                "message_uid": f"uid-a{i}",
            }
        )
        rows.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": f"OUTPUT {i}\n" + "Y" * tool_chars,
                "message_uid": f"uid-t{i}",
            }
        )
    return rows


def _engine(home, session_id: str, *, context_length: int = 60_000) -> CLMEngine:
    engine = CLMEngine(config={"clm.hermes_home": str(home)})
    engine.on_session_start(session_id, hermes_home=str(home), context_length=context_length)
    return engine


def _stub_count(messages) -> int:
    return sum(1 for row in (messages or []) if "[clm.offloaded" in str(row.get("content")))


def _compact(engine: CLMEngine, rows: list[dict]) -> list[dict]:
    out = engine.compress([dict(row) for row in rows], force=True)
    assert engine._last_compress_aborted is False, engine._last_summary_error
    assert _stub_count(out) > 0
    return out


def _pin_one(engine: CLMEngine) -> str:
    """Pin one span in the engine's store (the state a cold child must inherit)."""

    store = engine.store
    snapshot = store.snapshot()
    span_id = next(iter(snapshot.spans))
    store.stage(
        [EditOperation(kind=EditKind.PIN, span_ids=(span_id,))],
        expected_revision=snapshot.revision,
        txn_id="txn-pin",
        actor_origin="agent",
    )
    store.commit(txn_id="txn-pin", expected_revision=snapshot.revision)
    return span_id


# --------------------------------------------------------------------------- #
# Live adoption
# --------------------------------------------------------------------------- #


def test_compaction_rotation_adopts_the_store_it_just_compacted(home):
    """Same store, same revision lineage, new id — and the projection survives."""

    original = _transcript()
    engine = _engine(home, "sess-parent")
    compacted = _compact(engine, original)
    revision_before = engine.store.status()["revision"]
    store_before = engine.store

    engine.on_session_start(
        "sess-child", boundary_reason="compression", old_session_id="sess-parent"
    )

    assert engine.session_id == "sess-child"
    assert engine.store is store_before, "the child id must not open a second store"
    assert engine.store.status()["revision"] == revision_before
    # The host keeps serving the *original* history rows through the child engine;
    # the committed offloads must still apply or the compaction is undone.
    reprojected = engine.select_context(
        [dict(row) for row in original], conversation_messages=[dict(row) for row in original]
    )
    assert _stub_count(reprojected) > 0
    assert _stub_count(compacted) > 0


def test_rotation_is_receipted_with_the_lineage_outcome(home):
    engine = _engine(home, "sess-parent")
    _compact(engine, _transcript())

    engine.on_session_start(
        "sess-child", boundary_reason="compression", old_session_id="sess-parent"
    )

    receipts = engine.get_status().get("receipts") or engine._last_receipts
    rotations = [r for r in receipts if r.get("stage") == "session_rotation"]
    assert rotations, "an adopted rotation must be receipted"
    assert rotations[-1]["from"] == "sess-parent"
    assert rotations[-1]["to"] == "sess-child"
    assert rotations[-1]["boundary"] == "compression"
    assert rotations[-1]["lineage"] in {"written", "present"}
    assert engine.get_status()["last_lineage_error"] is None


def test_the_rotation_is_durable_for_a_cold_child(home):
    """The gateway closes its one-off engine; the next one must find the archive."""

    engine = _engine(home, "sess-parent")
    _compact(engine, _transcript())
    pinned = _pin_one(engine)
    revision = engine.store.status()["revision"]
    db_id = engine.store.db_id
    engine.on_session_start(
        "sess-child", boundary_reason="compression", old_session_id="sess-parent"
    )
    engine.on_session_end("sess-child", _transcript())

    assert engine.store is None, "the host closed the engine it compacted with"
    assert resolve_session_lineage(home, "sess-child") == "sess-parent"

    # A fresh engine for the child id: cold start, no parent in memory anywhere.
    resumed = _engine(home, "sess-child")
    assert resumed.store is not None
    assert resumed.store.db_id == db_id, "the child opened a different archive"
    assert resumed.store.status()["revision"] == revision
    assert pinned in set(resumed.store.snapshot().pins)
    assert (
        _stub_count(
            resumed.select_context(
                [dict(row) for row in _transcript()],
                conversation_messages=[dict(row) for row in _transcript()],
            )
        )
        > 0
    ), "the child lost the parent's committed offloads"


def test_repeated_rotation_keeps_one_archive_and_flattens_the_record(home):
    engine = _engine(home, "sess-gen0")
    _compact(engine, _transcript())
    store = engine.store
    revision = store.status()["revision"]

    engine.on_session_start("sess-gen1", boundary_reason="compression", old_session_id="sess-gen0")
    engine.on_session_start("sess-gen2", boundary_reason="compression", old_session_id="sess-gen1")

    assert engine.store is store, "a rotation must never reopen the store"
    assert engine.store.status()["revision"] == revision
    # Both records exist and both resolve to the one archive.
    assert resolve_session_lineage(home, "sess-gen1") == "sess-gen0"
    assert resolve_session_lineage(home, "sess-gen2") == "sess-gen0"

    engine.on_session_end("sess-gen2", _transcript())
    cold = _engine(home, "sess-gen2")
    assert cold.store.db_id == store.db_id


# --------------------------------------------------------------------------- #
# The gate: only a compressed parent is adopted
# --------------------------------------------------------------------------- #


def test_a_rotation_without_the_compression_boundary_is_a_new_conversation(home):
    engine = _engine(home, "sess-parent")
    _compact(engine, _transcript())
    first_store = engine.store

    engine.on_session_start("sess-other", boundary_reason="new", old_session_id="sess-parent")

    assert engine.store is not first_store
    assert engine.store.session_id == "sess-other"
    assert engine.store.snapshot().spans == {}
    assert not lineage_path(home, "sess-other").exists()


def test_a_missing_boundary_is_not_adopted(home):
    engine = _engine(home, "sess-parent")
    _compact(engine, _transcript())

    engine.on_session_start("sess-quiet", old_session_id="sess-parent")

    assert engine.store.session_id == "sess-quiet"
    assert not lineage_path(home, "sess-quiet").exists()


def test_from_equals_to_records_nothing(home):
    """``previous_target_session_id`` can name the same id; there is no rotation."""

    engine = _engine(home, "sess-same")
    _compact(engine, _transcript())
    store = engine.store

    engine.on_session_start("sess-same", boundary_reason="compression", old_session_id="sess-same")

    assert engine.store is store
    assert not lineage_path(home, "sess-same").exists()
    stages = [r.get("stage") for r in engine.get_status().get("receipts") or []]
    assert stages.count("session_rotation") == 0, "no rotation happened, so no receipt"


def test_rotation_from_an_unrelated_session_never_steals_its_store(home):
    """A stale notification naming someone else's id must not adopt this store."""

    engine = _engine(home, "sess-parent")
    _compact(engine, _transcript())

    engine.on_session_start(
        "sess-intruder", boundary_reason="compression", old_session_id="sess-someone-else"
    )

    assert engine.session_id == "sess-intruder"
    assert engine.store is not None
    assert engine.store.session_id == "sess-intruder"
    assert engine.store.snapshot().spans == {}
    assert not lineage_path(home, "sess-intruder").exists()


def test_a_rotation_after_a_reset_is_not_adopted(home):
    """``on_session_reset`` releases the handle, so there is nothing to inherit."""

    engine = _engine(home, "sess-parent")
    _compact(engine, _transcript())
    engine.on_session_reset()

    engine.on_session_start(
        "sess-after-reset", boundary_reason="compression", old_session_id="sess-parent"
    )

    assert engine.store.session_id == "sess-after-reset"
    assert engine.store.snapshot().spans == {}
    assert not lineage_path(home, "sess-after-reset").exists()


def test_a_rotation_is_adopted_before_the_parent_store_is_opened(home):
    """A competing process adopts the continuation of a session it has not touched.

    ``agent/conversation_compression.py`` recovers a compression child row and
    calls the same hook with ``boundary_reason="compression"``. That process may
    not have opened the parent's store yet this turn; refusing to adopt there is
    how a continuation ended up with an empty archive.
    """

    engine = _engine(home, "sess-early")
    _compact(engine, _transcript())
    db_id = engine.store.db_id
    engine._close_store()  # handle released, session id still ours (lazy reopen)
    assert engine.store is None

    engine.on_session_start(
        "sess-early-child", boundary_reason="compression", old_session_id="sess-early"
    )

    assert engine.store is not None, "the child id must reopen the parent's archive"
    assert engine.store.db_id == db_id
    assert resolve_session_lineage(home, "sess-early-child") == "sess-early"


def test_a_rotation_with_a_busy_store_still_records_the_lineage(home):
    """Another writer holds the archive: the relation must outlive this process."""

    owner = _engine(home, "sess-busy-parent")
    _compact(owner, _transcript())
    db_id = owner.store.db_id

    latecomer = CLMEngine(config={"clm.hermes_home": str(home)})
    latecomer.on_session_start("sess-busy-parent", hermes_home=str(home))
    assert latecomer.store is None, "the probe needs a refused attach"
    assert (latecomer.get_status()["store_error"] or {}).get("code")

    latecomer.on_session_start(
        "sess-busy-child", boundary_reason="compression", old_session_id="sess-busy-parent"
    )

    assert latecomer.session_id == "sess-busy-child"
    # The live engine could not write, but the child's relation is on disk: the
    # next process resolves it instead of starting an empty archive.
    assert resolve_session_lineage(home, "sess-busy-child") == "sess-busy-parent"
    owner.on_session_end("sess-busy-parent", [])
    cold = _engine(home, "sess-busy-child")
    assert cold.store.db_id == db_id


# --------------------------------------------------------------------------- #
# Bind / resume / new-session semantics
# --------------------------------------------------------------------------- #


def test_bind_session_state_adopts_the_id_the_gateway_built_the_engine_for(home):
    """Cold ``session.mutate(compress)`` binds ``(session_db, session_id)``."""

    # The gateway builds this engine with the profile's config, so the home comes
    # from config (or the host's own get_hermes_home()), never from the bind call.
    engine = CLMEngine(config={"clm.hermes_home": str(home)})
    engine.bind_session_state(None, session_id="sess-cold")

    assert engine.session_id == "sess-cold"
    assert engine.store is not None
    out = _compact(engine, _transcript())
    assert _stub_count(out) > 0


def test_bind_session_state_cannot_rebind_a_bound_engine(home):
    engine = _engine(home, "sess-mine")
    engine.bind_session_state(None, session_id="sess-theirs")
    assert engine.session_id == "sess-mine"


def test_new_session_gets_its_own_store_and_the_old_one_is_closed(home):
    """``/new`` semantics: reset, then a different id — a separate archive."""

    original = _transcript()
    engine = _engine(home, "sess-one")
    _compact(engine, original)
    first_store = engine.store

    engine.on_session_reset()
    engine.on_session_start("sess-two", hermes_home=str(home))

    assert first_store.closed is True, "the previous writer must be released"
    assert engine.store is not None and engine.store is not first_store
    assert engine.store.session_id == "sess-two"
    # Nothing of the first session's archive is addressable from the second.
    assert engine.store.snapshot().spans == {}
    assert resolve_session_lineage(home, "sess-two") is None


def test_a_cold_resume_of_an_unknown_id_starts_its_own_lineage(home):
    """No rotation was recorded for this id, so it is its own conversation."""

    parent = _engine(home, "sess-parent")
    _compact(parent, _transcript())
    parent._close_store()

    resumed = _engine(home, "sess-unrelated")
    assert resumed.store is not None
    assert resumed.store.session_id == "sess-unrelated"
    assert resumed.store.snapshot().spans == {}
    assert resolve_session_lineage(home, "sess-unrelated") is None


# --------------------------------------------------------------------------- #
# Diagnostics and contention
# --------------------------------------------------------------------------- #


def test_a_failed_lineage_write_is_reported_not_hidden(home, monkeypatch):
    """The live adoption still works; the cold child's emptiness is diagnosable."""

    engine = _engine(home, "sess-parent")
    _compact(engine, _transcript())

    def refuse(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(store_module, "record_session_lineage", refuse)

    engine.on_session_start(
        "sess-child", boundary_reason="compression", old_session_id="sess-parent"
    )

    assert engine.store is not None, "a failed lineage write must not undo the adoption"
    status = engine.get_status()
    assert status["last_lineage_error"] == "OSError"
    rotations = [r for r in status["receipts"] if r.get("stage") == "session_rotation"]
    assert rotations and rotations[-1]["lineage"] == "failed:OSError"


def test_two_engines_for_one_session_do_not_both_write(home):
    """No global writer: the second engine refuses and names the owner's lock."""

    first = _engine(home, "sess-contended")
    second = CLMEngine(config={})
    second.on_session_start("sess-contended", hermes_home=str(home))

    assert first.store is not None
    if second.store is None:
        error = second.get_status()["store_error"] or {}
        assert error.get("code") in {"store_busy", "sqlite_open_failed", "store_closed"}
        second.compress(_transcript(), force=True)
        assert second._last_compress_aborted is True
        assert error.get("code") in (second._last_summary_error or "")
    else:  # pragma: no cover - stores that allow shared readers
        assert second.store.read_only is True or second.store is not first.store
    # The owner is unaffected either way.
    assert first.store.status()["ok"] is True
    assert _stub_count(_compact(first, _transcript())) > 0


def test_a_refused_compaction_leaves_a_log_line(home, caplog):
    """An operator seeing "no changes" must find the typed reason in the log."""

    import logging

    # No injected logger: the host builds the engine without one on some paths.
    engine = CLMEngine(config={"clm.hermes_home": str(home), "clm.manual_target_ratio": 0})
    engine.on_session_start("sess-silent", hermes_home=str(home))
    with caplog.at_level(logging.WARNING, logger="hermes_clm.engine"):
        engine.compress(_transcript(), force=True)

    assert engine._last_compress_aborted is True
    assert any(
        "hermes-clm:" in record.message and "manual_no_offloadable_groups" in record.message
        for record in caplog.records
    ), "a refusal with no log line is undiagnosable"


def test_the_engine_publishes_the_session_it_serves_for_session_only_surfaces(home):
    """``/clm`` receives a session id, never the agent: it resolves through this."""

    engine = _engine(home, "sess-command")
    assert engine_module.engine_for_session("sess-command") is engine
    assert engine_module.engine_for_session("sess-other") is None
    assert engine_module.engine_for_session(None) is None

    engine.on_session_start(
        "sess-command-child", boundary_reason="compression", old_session_id="sess-command"
    )

    assert engine_module.engine_for_session("sess-command") is None
    assert engine_module.engine_for_session("sess-command-child") is engine

    engine.on_session_end("sess-command-child", [])
    assert engine_module.engine_for_session("sess-command-child") is None


# --------------------------------------------------------------------------- #
# Lifecycle conflict: a child id that already owns a store
# --------------------------------------------------------------------------- #


def test_a_rotation_never_adopts_a_parent_archive_over_the_childs_own_store(home):
    """Two archives for one conversation would silently diverge.

    The child id already exists on disk (its own session ran and compacted). A
    compression notification naming it must not point the engine at the parent's
    archive: the child's store wins, and the refusal is visible in status rather
    than discovered later as a session that lost its history.
    """

    child = _engine(home, "sess-taken")
    child_compacted = _compact(child, _transcript())
    assert _stub_count(child_compacted) > 0
    child_db = child.store.db_id
    child_revision = child.store.status()["revision"]
    child.on_session_end("sess-taken", [])

    parent = _engine(home, "sess-parent")
    _compact(parent, _transcript())
    parent_db = parent.store.db_id
    assert parent_db != child_db

    parent.on_session_start(
        "sess-taken", boundary_reason="compression", old_session_id="sess-parent"
    )

    assert parent.session_id == "sess-taken"
    assert parent.store is not None
    assert parent.store.db_id == child_db, "the child's own archive must win"
    assert parent.store.status()["revision"] == child_revision
    assert resolve_session_lineage(home, "sess-taken") is None, "no alias over an owned store"

    status = parent.get_status()
    assert "rotation_conflict" in (status["last_rotation_error"] or "")
    refused = [r for r in status["receipts"] if r.get("stage") == "session_rotation_refused"]
    assert refused, "a refused adoption must be receipted"
    assert refused[-1]["reason"] == "rotation_conflict:child_store_present"
    assert refused[-1]["from"] == "sess-parent"
    assert refused[-1]["to"] == "sess-taken"
