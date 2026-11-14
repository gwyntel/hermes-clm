from __future__ import annotations

from _clm_fakes import assemble
from hermes_clm.engine import CLMEngine, default_store_factory


def make_engine(tmp_path):
    engine = CLMEngine(hermes_home=str(tmp_path), store_factory=default_store_factory)
    engine.on_session_start("sess-absence")
    return engine


def conversation(*uids):
    return [
        {"message_uid": uid, "role": "user", "content": f"message {uid}"}
        for uid in uids
    ]


def test_absence_is_unverified_without_alignment(tmp_path):
    engine = make_engine(tmp_path)
    try:
        engine.store.ingest(conversation("u1", "u2"))
        engine.store.ingest(conversation("u3"))
        page = engine.tool_service().inspect(include_stale=True)
        assert page["alignment"]["available"] is False
        assert page["alignment"]["reason"] == "no_projection"
        by_uid = {item["uid"]: item for item in page["items"]}
        assert by_uid["u1"]["status"] == "unverified"
        assert by_uid["u2"]["status"] == "unverified"
        assert by_uid["u3"]["status"] == "live"  # presence is direct store evidence
    finally:
        engine.on_session_end("sess-absence", [])


def test_absence_inside_window_and_older_leftovers_have_distinct_labels(tmp_path):
    engine = make_engine(tmp_path)
    try:
        engine.store.ingest(conversation("old-1"))
        first = conversation("u1", "u2", "u3")
        engine.select_context(assemble(first), conversation_messages=first)
        second = conversation("u1", "u3")
        engine.select_context(assemble(second), conversation_messages=second)
        page = engine.tool_service().inspect(include_stale=True)
        by_uid = {item["uid"]: item for item in page["items"]}
        assert page["alignment"]["available"] is True
        assert by_uid["old-1"]["status"] == "stale"
        assert by_uid["u2"]["status"] == "absent_now"
        assert by_uid["u1"]["status"] == "live"
    finally:
        engine.on_session_end("sess-absence", [])
