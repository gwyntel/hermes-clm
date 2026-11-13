"""Durable session lineage: the child of a compression rotation keeps its archive.

The host compacts in place, commits, and then rotates the session id
(``_notify_context_engine_compression_complete``). A gateway that never saw the
parent — a cold ``session.mutate(compress)`` engine, or the next process — must
still resolve the child id to the archive that holds the conversation's spans,
pins and revisions. That relation is a record on disk, and everything about it is
untrusted: it decides *which* archive a session id addresses.

Each test here is a property of the record layer, not of the engine: the engine
tests drive the rotation that writes it.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from hermes_clm.errors import (
    ConflictError,
    CorruptStoreError,
    NotFoundError,
    PathSafetyError,
    ValidationError,
)
from hermes_clm.models import EditKind, EditOperation
from hermes_clm.store import (
    LINEAGE_MAX_DEPTH,
    ContextStore,
    lineage_path,
    record_session_lineage,
    resolve_session_lineage,
    session_dir,
    session_key,
)

PARENT = "sess-parent-rotation"
CHILD = "sess-child-rotation"


def _seed(home, session_id: str = PARENT, *, pin: bool = True) -> dict:
    """A store with one span (and optionally one pin); returns its identity."""

    store = ContextStore.for_session(home, session_id)
    try:
        store.ingest(
            [
                {"message_uid": "u-1", "role": "assistant", "content": "parent archive payload"},
                {"message_uid": "u-2", "role": "assistant", "content": "second span"},
            ]
        )
        snapshot = store.snapshot()
        span_id = next(iter(snapshot.spans))
        if pin:
            store.stage(
                [EditOperation(kind=EditKind.PIN, span_ids=(span_id,))],
                expected_revision=snapshot.revision,
                txn_id="txn-pin",
                actor_origin="agent",
            )
            store.commit(txn_id="txn-pin", expected_revision=snapshot.revision)
        snapshot = store.snapshot()
        return {
            "db_id": store.db_id,
            "directory": store.directory,
            "spans": set(snapshot.spans),
            "pins": set(snapshot.pins),
            "revision": snapshot.revision,
            "span_id": span_id,
        }
    finally:
        store.close()


def _write_record(home, child: str, parent: str, **overrides) -> None:
    """Write a well-formed record directly, bypassing the API's guards."""

    path = lineage_path(home, child)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "format": 1,
        "child_key": session_key(child),
        "parent_key": session_key(parent),
        "parent_session_id": parent,
        "boundary": "compression",
        "created_at": "2026-01-01T00:00:00Z",
        "engine_version": "test",
    }
    payload.update(overrides)
    raw = json.dumps(payload).encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# The relation itself
# --------------------------------------------------------------------------- #


def test_child_resolves_the_archive_the_parent_owns(home):
    parent = _seed(home)
    record_session_lineage(home, CHILD, PARENT)

    with ContextStore.for_session(home, CHILD) as child:
        assert child.session_id == PARENT, "the archive is filed under its owner"
        assert child.db_id == parent["db_id"]
        assert child.directory == parent["directory"]
        assert set(child.snapshot().spans) == parent["spans"]
        assert set(child.snapshot().pins) == parent["pins"]


def test_parent_still_resolves_after_the_alias_exists(home):
    parent = _seed(home)
    record_session_lineage(home, CHILD, PARENT)

    with ContextStore.for_session(home, PARENT) as reopened:
        assert reopened.session_id == PARENT
        assert reopened.db_id == parent["db_id"]
        assert set(reopened.snapshot().spans) == parent["spans"]


def test_the_open_handle_is_not_replaced_when_the_alias_is_recorded(home):
    """Recording is a store-layer write: the caller's handle keeps working."""

    store = ContextStore.for_session(home, PARENT)
    try:
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "live payload"}])
        before = store.db_id
        record_session_lineage(home, CHILD, PARENT)
        assert store.db_id == before
        assert set(store.snapshot().spans)
        store.ingest([{"message_uid": "u-2", "role": "assistant", "content": "after alias"}])
        assert len(store.snapshot().spans) == 2
    finally:
        store.close()


def test_a_cold_read_only_child_resolves_the_archive(home):
    parent = _seed(home)
    record_session_lineage(home, CHILD, PARENT)

    with ContextStore.for_session(home, CHILD, read_only=True) as child:
        assert child.read_only is True
        assert child.db_id == parent["db_id"]
        assert set(child.snapshot().spans) == parent["spans"]


def test_read_only_child_without_an_archive_is_store_not_found(home):
    """A record whose archive is gone must not silently become a fresh store."""

    _seed(home)
    record_session_lineage(home, CHILD, PARENT)
    ContextStore.purge_session(home, PARENT)

    with pytest.raises(NotFoundError) as excinfo:
        ContextStore.for_session(home, CHILD, read_only=True)
    assert excinfo.value.code in {"store_not_found", "not_found"}


def test_resolve_reports_none_for_a_session_with_its_own_store(home):
    _seed(home, "sess-solo")
    assert resolve_session_lineage(home, "sess-solo") is None


# --------------------------------------------------------------------------- #
# The record file: owner-only, atomic, never partial
# --------------------------------------------------------------------------- #


def test_record_is_owner_only_and_leaves_no_temporaries(home):
    _seed(home)
    record_session_lineage(home, CHILD, PARENT)

    path = lineage_path(home, CHILD)
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(path.parent).st_mode) == 0o700
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


def test_a_repeat_is_idempotent_but_a_different_parent_is_refused(home):
    _seed(home)
    other = _seed(home, "sess-other-archive")

    first = record_session_lineage(home, CHILD, PARENT)
    second = record_session_lineage(home, CHILD, PARENT)
    assert first["written"] is True
    assert second["written"] is False, "a repeat must not rewrite the record"

    assert resolve_session_lineage(home, CHILD) == PARENT
    with pytest.raises(ConflictError) as excinfo:
        record_session_lineage(home, CHILD, "sess-other-archive")
    assert excinfo.value.code == "lineage_conflict"
    assert resolve_session_lineage(home, CHILD) == PARENT, "a refusal must not repoint"
    assert other["db_id"] != _seed(home, PARENT)["db_id"]


def test_a_session_that_already_owns_a_store_is_never_aliased(home):
    """Collision: the child id has its own archive; aliasing it would hide it."""

    _seed(home)
    _seed(home, CHILD, pin=False)

    with pytest.raises(ConflictError) as excinfo:
        record_session_lineage(home, CHILD, PARENT)
    assert excinfo.value.code == "lineage_conflict"
    assert resolve_session_lineage(home, CHILD) is None

    with ContextStore.for_session(home, CHILD) as own:
        assert own.session_id == CHILD
        assert {span.message.get("content") for span in own.snapshot().spans.values()}


# --------------------------------------------------------------------------- #
# Only a verified compression rotation may alias
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("boundary", ["new", "resume", "branch", "", "compression "])
def test_only_the_compression_boundary_may_record_lineage(home, boundary):
    _seed(home)
    with pytest.raises(ValidationError) as excinfo:
        record_session_lineage(home, CHILD, PARENT, boundary=boundary)
    assert excinfo.value.code == "lineage_boundary"
    assert not lineage_path(home, CHILD).exists()


def test_a_self_alias_is_refused(home):
    _seed(home)
    with pytest.raises(ValidationError) as excinfo:
        record_session_lineage(home, PARENT, PARENT)
    assert excinfo.value.code == "lineage_self"


def test_aliasing_a_session_without_an_archive_is_refused(home):
    with pytest.raises(NotFoundError) as excinfo:
        record_session_lineage(home, CHILD, PARENT)
    assert excinfo.value.code == "lineage_parent_missing"
    assert not lineage_path(home, CHILD).exists()


# --------------------------------------------------------------------------- #
# Hostile records: traversal, symlink, permissions, corruption, cycles
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "/etc/passwd", "a\x00b", "x" * 300])
def test_a_record_naming_a_traversing_parent_is_refused(home, hostile):
    _seed(home)
    _write_record(home, CHILD, PARENT, parent_session_id=hostile, parent_key=session_key(PARENT))

    with pytest.raises((CorruptStoreError, ValidationError)) as excinfo:
        resolve_session_lineage(home, CHILD)
    assert getattr(excinfo.value, "code", "") in {"corrupt_lineage", "invalid_session_id"}


def test_a_record_filed_under_another_key_is_refused(home):
    _seed(home)
    _write_record(home, CHILD, PARENT, child_key=session_key("sess-somebody-else"))

    with pytest.raises(CorruptStoreError) as excinfo:
        resolve_session_lineage(home, CHILD)
    assert excinfo.value.code == "corrupt_lineage"


def test_a_symlinked_record_is_refused_and_not_followed(home, tmp_path):
    _seed(home)
    record_session_lineage(home, CHILD, PARENT)
    real = lineage_path(home, CHILD)
    hijack = lineage_path(home, "sess-hijacker")
    hijack.parent.mkdir(mode=0o700, exist_ok=True)
    hijack.symlink_to(real)

    with pytest.raises(PathSafetyError) as excinfo:
        resolve_session_lineage(home, "sess-hijacker")
    assert excinfo.value.code == "symlink_refused"


def test_a_group_readable_record_is_refused(home):
    _seed(home)
    record_session_lineage(home, CHILD, PARENT)
    path = lineage_path(home, CHILD)
    os.chmod(path, 0o644)

    with pytest.raises(PathSafetyError) as excinfo:
        resolve_session_lineage(home, CHILD)
    assert excinfo.value.code == "insecure_permissions"


def test_a_hard_linked_record_is_refused(home, tmp_path):
    _seed(home)
    record_session_lineage(home, CHILD, PARENT)
    path = lineage_path(home, CHILD)
    os.link(path, tmp_path / "second-name.json")

    with pytest.raises(PathSafetyError) as excinfo:
        resolve_session_lineage(home, CHILD)
    assert excinfo.value.code == "hardlink_refused"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'{"format": 99}',
        b'{"format": 1}',
        b'{"format": 1, "parent_session_id": ""}',
    ],
)
def test_a_corrupt_record_is_refused_not_ignored(home, raw):
    _seed(home)
    path = lineage_path(home, CHILD)
    path.parent.mkdir(mode=0o700, parents=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)

    with pytest.raises(CorruptStoreError) as excinfo:
        resolve_session_lineage(home, CHILD)
    assert excinfo.value.code == "corrupt_lineage"


def test_an_oversized_record_is_refused(home):
    _seed(home)
    _write_record(home, CHILD, PARENT, padding="x" * 20_000)

    with pytest.raises(CorruptStoreError) as excinfo:
        resolve_session_lineage(home, CHILD)
    assert excinfo.value.code == "corrupt_lineage"


def test_a_cycle_is_refused(home):
    _seed(home)
    _write_record(home, "sess-a", "sess-b")
    _write_record(home, "sess-b", "sess-a")

    with pytest.raises(PathSafetyError) as excinfo:
        resolve_session_lineage(home, "sess-a")
    assert excinfo.value.code == "lineage_cycle"


def test_a_self_cycle_is_refused(home):
    _seed(home)
    _write_record(home, "sess-a", "sess-a")

    with pytest.raises(PathSafetyError) as excinfo:
        resolve_session_lineage(home, "sess-a")
    assert excinfo.value.code == "lineage_cycle"


def test_an_over_long_chain_is_refused(home):
    _seed(home)
    for index in range(LINEAGE_MAX_DEPTH + 2):
        _write_record(home, f"sess-hop-{index}", f"sess-hop-{index + 1}")

    with pytest.raises(PathSafetyError) as excinfo:
        resolve_session_lineage(home, "sess-hop-0")
    assert excinfo.value.code == "lineage_depth"


def test_a_chain_resolves_transitively_to_the_archive_owner(home):
    """Records written by an older build may chain; resolution follows to the store."""

    parent = _seed(home, "sess-root")
    _write_record(home, "sess-mid", "sess-root")
    _write_record(home, "sess-leaf", "sess-mid")

    assert resolve_session_lineage(home, "sess-leaf") == "sess-root"
    with ContextStore.for_session(home, "sess-leaf") as leaf:
        assert leaf.db_id == parent["db_id"]
        assert set(leaf.snapshot().spans) == parent["spans"]


def test_the_api_flattens_a_chain_instead_of_growing_it(home):
    _seed(home, "sess-root")
    _write_record(home, "sess-mid", "sess-root")
    record_session_lineage(home, "sess-leaf", "sess-mid")

    payload = json.loads(lineage_path(home, "sess-leaf").read_bytes())
    assert payload["parent_session_id"] == "sess-root"
    assert payload["parent_key"] == session_key("sess-root")


# --------------------------------------------------------------------------- #
# Operator tooling: purge and retention follow the alias
# --------------------------------------------------------------------------- #


def test_purging_a_child_removes_the_archive_and_the_records(home):
    parent = _seed(home)
    record_session_lineage(home, CHILD, PARENT)

    result = ContextStore.purge_session(home, CHILD)

    assert result["removed"] is True
    assert result["store_key"] == session_key(PARENT)
    assert result["aliased_from"] == session_key(CHILD)
    assert not parent["directory"].exists()
    assert not lineage_path(home, CHILD).exists()
    assert resolve_session_lineage(home, CHILD) is None


def test_retention_protects_the_archive_of_a_live_child(home):
    """The live session id is the child; the store it must not lose is the parent's."""

    _seed(home)
    record_session_lineage(home, CHILD, PARENT)

    report = ContextStore.purge_expired_sessions(
        home, keep=0, current_session_id=CHILD, dry_run=True
    )

    assert report["purged"] == []
    assert [item["reason"] for item in report["skipped"]] == ["current"]


def test_a_session_id_can_never_be_its_own_ancestor(home):
    _seed(home, "sess-a")
    record_session_lineage(home, "sess-b", "sess-a")

    with pytest.raises(ConflictError) as excinfo:
        record_session_lineage(home, "sess-a", "sess-b")
    assert excinfo.value.code in {"lineage_cycle", "lineage_conflict"}


def test_the_store_directory_is_untouched_by_a_refused_record(home):
    parent = _seed(home)
    before = sorted(entry.name for entry in parent["directory"].iterdir())

    with pytest.raises(ValidationError):
        record_session_lineage(home, CHILD, PARENT, boundary="resume")

    assert sorted(entry.name for entry in parent["directory"].iterdir()) == before
    assert session_dir(home, CHILD).exists() is False
