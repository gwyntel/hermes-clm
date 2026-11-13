"""Store attachment: a store always lives under an absolute HERMES_HOME.

Regression class: "``/compact`` says no store".  ``Path("")`` stringifies to
``"."``, so ``sessions_root``'s old ``if not str(home)`` guard never fired and an
engine that had not been handed a home yet created ``./clm/sessions/<key>/``
beside whatever directory the process was running in.  A store outside
``HERMES_HOME`` is invisible to the next process (which then reports ``no store
for this session``), is shared across profiles by a relative path, and lets a
projection write into a checkout.
"""

from __future__ import annotations

import pytest

from hermes_clm.engine import CLMEngine
from hermes_clm.errors import ValidationError
from hermes_clm.store import ContextStore, sessions_root


@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "clm", "relative/home"])
def test_sessions_root_refuses_a_home_that_is_not_absolute(bad):
    with pytest.raises(ValidationError) as excinfo:
        sessions_root(bad)
    assert excinfo.value.code == "invalid_home"
    assert "absolute" in excinfo.value.message or "required" in excinfo.value.message


def test_sessions_root_accepts_an_absolute_home(home):
    assert sessions_root(home) == home / "clm" / "sessions"
    assert sessions_root(str(home)) == home / "clm" / "sessions"


def test_for_session_with_a_blank_home_writes_nothing_beside_the_process(tmp_path, monkeypatch):
    """The real store refuses *before* creating a directory in the CWD."""

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    with pytest.raises(ValidationError) as excinfo:
        ContextStore.for_session("", "sess-1")

    assert excinfo.value.code == "invalid_home"
    assert not (workdir / "clm").exists()
    assert list(workdir.iterdir()) == []


def test_engine_without_a_home_refuses_and_names_the_cause(tmp_path, monkeypatch):
    """No home anywhere: detached, typed, and no store beside the process."""

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    # A host-less environment: no hermes_constants to resolve a home from.
    monkeypatch.setitem(__import__("sys").modules, "hermes_constants", None)

    engine = CLMEngine(config={})
    engine.on_session_start("sess-no-home")

    assert engine.store is None
    assert engine.get_status()["store"] == "detached"
    assert (engine.get_status()["store_error"] or {}).get("code") == "invalid_home"
    assert not (workdir / "clm").exists()

    result = engine.compress([{"role": "user", "content": "hi", "message_uid": "u1"}], force=True)
    assert result == [{"role": "user", "content": "hi", "message_uid": "u1"}]
    assert engine._last_compress_aborted is True
    # The operator-facing reason keeps the store's own code instead of "no store".
    assert "invalid_home" in (engine._last_summary_error or "")


def test_engine_opens_under_the_home_it_is_given(home):
    engine = CLMEngine(config={})
    engine.on_session_start("sess-homed", hermes_home=str(home))

    assert engine.store is not None
    assert engine.store.directory.parent == home / "clm" / "sessions"
    assert engine.get_status()["store_error"] is None


def test_two_homes_for_the_same_session_id_are_two_isolated_stores(home, other_home):
    """Cross-profile isolation: the session id alone never addresses an archive."""

    first = CLMEngine(config={})
    first.on_session_start("sess-shared-name", hermes_home=str(home))
    assert first.store is not None
    first.store.ingest([{"role": "user", "content": "profile A only", "message_uid": "uid-a"}])

    second = CLMEngine(config={})
    second.on_session_start("sess-shared-name", hermes_home=str(other_home))
    assert second.store is not None
    assert second.store.directory != first.store.directory
    first_store_dir, second_store_dir = first.store.directory, second.store.directory
    # The second profile's store is its own: the archive is not addressable by
    # session id alone.
    assert first.store.snapshot().spans
    assert second.store.snapshot().spans == {}

    first._close_store()
    second._close_store()
    assert str(first_store_dir).startswith(str(home))
    assert str(second_store_dir).startswith(str(other_home))
    assert str(first_store_dir) != str(second_store_dir)


def test_rebinding_the_home_reopens_under_the_new_home(home, other_home):
    """``(session, home)`` is the key: a new home is a different store, not a rebind."""

    engine = CLMEngine(config={})
    engine.on_session_start("sess-one", hermes_home=str(home))
    first = engine.store
    assert first is not None
    first.ingest([{"role": "user", "content": "profile A only", "message_uid": "uid-a"}])

    engine.on_session_start("sess-two", hermes_home=str(other_home))

    assert engine.store is not None
    assert engine.store is not first
    assert first.closed is True, "one writer per store: the old handle is released"
    assert engine.store.directory.parent == other_home / "clm" / "sessions"
    assert engine.store.snapshot().spans == {}
    assert engine.get_status()["store_home_source"] == "on_session_start"


def _history(rows: int) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"row {index}",
            "message_uid": f"u{index}",
        }
        for index in range(rows)
    ]


def _resolutions_per_turn(engine, rows: int, calls: list[int]) -> int:
    calls.clear()
    history = _history(rows)
    for _ in range(3):
        engine.select_context(
            [dict(row) for row in history], conversation_messages=[dict(row) for row in history]
        )
    return len(calls)


def test_home_resolution_does_not_scale_with_the_transcript(home):
    """``select_context`` runs every turn: resolution is per store access, not per row."""

    engine = CLMEngine(config={"clm.hermes_home": str(home)})
    engine.on_session_start("sess-hot", hermes_home=str(home))
    assert engine.store is not None

    calls: list[int] = []
    original = engine._resolve_home

    def counted():
        calls.append(1)
        return original()

    engine._resolve_home = counted  # type: ignore[method-assign]

    small = _resolutions_per_turn(engine, 1, calls)
    large = _resolutions_per_turn(engine, 40, calls)

    assert small == large, "a 40-row turn resolved the home more often than a 1-row turn"
    assert small <= 2 * 3, (
        f"{small} reads for 3 turns: resolution is fanning out per store access pair"
    )


def test_an_unbound_engine_resolves_no_home_at_all(monkeypatch):
    """No session means no ambient home read: nothing to key a store by."""

    reads: list[int] = []

    class _Constants:
        @staticmethod
        def get_hermes_home():
            reads.append(1)
            return "/tmp/hermes-home-probe"

    monkeypatch.setitem(__import__("sys").modules, "hermes_constants", _Constants)

    engine = CLMEngine(config={})
    assert engine.service_for_session() is None
    assert reads == [], "an unbound engine must not read the ambient home"
    assert (engine.get_status()["store_error"] or {}).get("code") == "no_session"


def test_the_ambient_home_is_read_lazily_and_reused_without_churn(monkeypatch, tmp_path):
    """A gateway serving profiles has no explicit home; one call, one store open."""

    ambient = tmp_path / "ambient-home"
    ambient.mkdir(mode=0o700)
    reads: list[int] = []

    class _Constants:
        @staticmethod
        def get_hermes_home():
            reads.append(1)
            return str(ambient)

    monkeypatch.setitem(__import__("sys").modules, "hermes_constants", _Constants)

    opens: list[str] = []
    real_for_session = ContextStore.for_session

    def counting_for_session(home_path, session_id, **kwargs):
        opens.append(str(session_id))
        return real_for_session(home_path, session_id, **kwargs)

    monkeypatch.setattr(ContextStore, "for_session", staticmethod(counting_for_session))

    engine = CLMEngine(config={})
    engine.on_session_start("sess-ambient")

    small = _resolutions_per_turn(engine, 1, reads)
    large = _resolutions_per_turn(engine, 40, reads)

    assert engine.store is not None
    assert engine.get_status()["store_home_source"] == "hermes_constants"
    assert opens == ["sess-ambient"], "the same key must not reopen the store"
    assert small == large, "the ambient home is re-read per message instead of per access"
    assert small <= 2 * 3
    engine._close_store()


def test_status_reports_the_live_build(home):
    """``engine_version`` is how an operator spots a stale, long-lived host load."""

    import hermes_clm

    engine = CLMEngine(config={"clm.hermes_home": str(home)})
    engine.on_session_start("sess-version", hermes_home=str(home))

    status = engine.get_status()
    assert status["engine_version"] == hermes_clm.__version__
    assert status["engine"] == "hermes-clm"
    # Which home was used, and where it came from: a multiplexed host that hands
    # the engine the wrong profile's home must be diagnosable, not silent.
    assert status["store_home_source"] == "on_session_start"
    assert str(home) not in str(status)
    assert status["store_session"] == "sess-version"
