from __future__ import annotations

from _clm_fakes import StoreFactory, assemble, coding_turns, snapshot_for
from hermes_clm.engine import CLMEngine


def observed_engine(tmp_path):
    history = coding_turns()
    engine = CLMEngine(
        hermes_home=str(tmp_path),
        store_factory=StoreFactory(snapshot_for(history, [], revision=1)),
    )
    engine.on_session_start("pressure")
    engine.select_context(assemble(history), conversation_messages=history)
    return engine


def test_compactable_is_request_scoped_and_pressure_has_no_stale_cache(tmp_path):
    engine = observed_engine(tmp_path)
    compactable = engine.compactable()
    assert compactable["known"] is True
    assert compactable["basis"] == "request"
    assert compactable["tokens"] >= 0
    hint = engine.pressure_hint()
    assert set(hint) == {"scope", "tier", "headroom_tokens", "overflow_tokens"}
    assert hint["scope"] == "request"


def test_no_observed_request_is_unknown_not_zero(tmp_path):
    engine = CLMEngine(hermes_home=str(tmp_path), store_factory=StoreFactory())
    engine.on_session_start("pressure-unobserved")
    assert engine.compactable() == {"known": False, "reason": "no_request_observed"}
