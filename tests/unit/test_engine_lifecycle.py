"""Engine lifecycle, clone, model switch, usage accounting, config and degradation."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from _clm_fakes import (
    StoreFactory,
    assemble,
    coding_turns,
    make_store,
    snapshot_for,
)
from hermes_clm import budget as budget_module
from hermes_clm.engine import (
    CLMEngine,
    EngineConfig,
    OperationSpec,
    ToolService,
    default_store_factory,
)


def engine_for(snapshot=None, *, supports_commit: bool = True, **kwargs):
    factory = StoreFactory(snapshot, supports_commit=supports_commit)
    engine = CLMEngine(store_factory=factory, hermes_home="/tmp/hermes-home", **kwargs)
    return engine, factory


def started_engine(
    snapshot=None, *, supports_commit: bool = True, session_id: str = "sess-1", **kwargs
):
    engine, factory = engine_for(snapshot, supports_commit=supports_commit, **kwargs)
    engine.on_session_start(session_id, hermes_home="/tmp/hermes-home")
    return engine, factory


def test_package_imports_and_runs_without_hermes():
    """The runtime package must import with the Hermes package blocked."""
    script = textwrap.dedent(
        """
        import sys

        class BlockHermes:
            def find_module(self, name, path=None):
                return self if name == "agent" or name.startswith("agent.") else None

            def find_spec(self, name, path=None, target=None):
                if name == "agent" or name.startswith("agent."):
                    raise ImportError("Hermes is not installed")
                return None

        sys.meta_path.insert(0, BlockHermes())
        sys.path.insert(0, "src")
        from hermes_clm import budget, engine, identity, projection
        from hermes_clm.engine import CLMEngine

        assert "agent" not in sys.modules, sorted(sys.modules)
        engine_instance = CLMEngine()
        assert engine_instance.name == "hermes-clm"
        assert engine_instance.select_context([{"role": "user", "content": "x"}]) is None
        assert engine_instance.should_compress() is False
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd="."
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_engine_subclasses_the_real_host_abc_when_a_checkout_is_available(monkeypatch, tmp_path):
    """Cross-check against the real ABC: set CLM_HERMES_CHECKOUT to a Hermes tree.

    Skipped in normal runs because this repository must not depend on a Hermes
    checkout; run it locally before claiming host compatibility.
    """
    import os

    checkout = os.environ.get("CLM_HERMES_CHECKOUT")
    if not checkout or not os.path.isdir(os.path.join(checkout, "agent")):
        pytest.skip("set CLM_HERMES_CHECKOUT to a Hermes checkout to run this cross-check")

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {checkout!r})
        sys.path.insert(0, "src")
        from agent.context_engine import ContextEngine
        import hermes_clm.engine as engine_module

        assert engine_module._EngineBase is ContextEngine, "engine did not pick up the host ABC"
        engine = engine_module.CLMEngine()
        assert isinstance(engine, ContextEngine)
        assert engine.select_context([{{"role": "user", "content": "x"}}]) is None
        # Same comparison the host's _engine_overrides_hook() performs, without
        # importing conversation_loop (which needs the whole host dependency set).
        assert engine.select_context.__func__ is not ContextEngine.select_context
        assert engine.on_turn_complete.__func__ is ContextEngine.on_turn_complete
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd="."
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_importing_the_package_does_not_import_hermes():
    for name in (
        "hermes_clm.engine",
        "hermes_clm.projection",
        "hermes_clm.identity",
        "hermes_clm.budget",
    ):
        __import__(name)
    assert "agent" not in sys.modules


def test_default_store_factory_is_lazy_and_import_error_is_contained(monkeypatch):
    module = sys.modules[default_store_factory.__module__]
    source = open(str(module.__file__), encoding="utf-8").read()
    assert "importlib.import_module" in source
    # The factory imports the store per call, so simulate the delivery unit being absent.
    # Relying on the real store being missing made this pass only before the merge.
    real_import = module.importlib.import_module

    def missing(name, package=None):
        if name == ".store":
            raise ModuleNotFoundError("No module named 'hermes_clm.store'", name="hermes_clm.store")
        return real_import(name, package)

    monkeypatch.setattr(module.importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        default_store_factory("/tmp/home", "sess")


def test_store_is_not_opened_before_a_session_starts():
    engine, factory = engine_for()
    assert factory.opened == []
    assert engine.store is None


def test_on_session_start_opens_exactly_one_store():
    engine, factory = started_engine()
    assert factory.opened == [
        {"hermes_home": "/tmp/hermes-home", "session_id": "sess-1", "read_only": False}
    ]
    engine.on_session_start("sess-1", hermes_home="/tmp/hermes-home")
    assert len(factory.opened) == 1


def test_on_session_start_records_the_hosts_context_length():
    engine, _ = started_engine()
    engine.on_session_start("sess-1", context_length=200_000)
    assert engine.context_length == 200_000
    assert engine.threshold_tokens == int(200_000 * engine.threshold_percent)


def test_select_context_without_history_is_identity_and_opens_no_store():
    engine, factory = engine_for()
    engine.on_session_start("sess-1")
    request = [{"role": "user", "content": "hi"}]
    assert engine.select_context(request, conversation_messages=[]) is None
    assert factory.store.ingest_calls == []


def test_select_context_returns_none_when_nothing_is_committed():
    history = coding_turns()
    engine, factory = started_engine(snapshot_for(history, [], revision=0))
    request = assemble(history)
    assert engine.select_context(request, conversation_messages=history) is None
    assert factory.store.ingest_calls  # write-through archive still ran


def test_select_context_ingests_history_without_mutating_it():
    history = coding_turns()
    engine, factory = started_engine(snapshot_for(history, [], revision=0))
    request = assemble(history)
    engine.select_context(request, conversation_messages=history)
    assert factory.store.ingest_calls[0] == history
    assert history[0]["message_uid"] == "u1"


def test_engine_implements_the_full_host_engine_surface():
    """The ABC is complete: every hook the host reads exists and is callable."""
    engine, _ = started_engine()
    surface = [
        "select_context",
        "compress",
        "update_from_response",
        "should_compress",
        "should_compress_info",
        "should_compress_preflight",
        "should_defer_preflight_to_real_usage",
        "prune_tool_results_only",
        "has_content_to_compress",
        "on_turn_complete",
        "get_automatic_compaction_status_message",
        "on_session_start",
        "on_session_end",
        "on_session_reset",
        "get_tool_schemas",
        "handle_tool_call",
        "get_status",
        "clone_for_agent",
        "update_model",
        "name",
    ]
    for attribute in surface:
        assert hasattr(engine, attribute), attribute
        value = getattr(engine, attribute)
        assert value if attribute == "name" else callable(value), attribute
    # Abstract members are overridden here, not inherited from a stand-in base.
    for abstract in ("update_from_response", "should_compress", "compress"):
        assert abstract in CLMEngine.__dict__ or abstract in type(engine).__dict__
    assert engine.name == "hermes-clm"
    assert engine.emit_automatic_compaction_status is False


def test_should_compress_and_preflight_stay_disabled():
    engine, _ = started_engine()
    assert engine.should_compress() is False
    assert engine.should_compress(999_999) is False
    assert engine.should_compress_info() == (False, None)
    assert engine.should_compress_preflight([{"role": "user", "content": "x"}]) is False
    assert engine.should_defer_preflight_to_real_usage(10) is False
    messages = [{"role": "user", "content": "x"}]
    assert engine.prune_tool_results_only(messages) == (messages, 0)


def test_compress_is_identity_by_default():
    engine, factory = started_engine(
        snapshot_for(coding_turns(), [OperationSpec(kind="drop", span_ids=("a1", "t1"))])
    )
    messages = coding_turns()
    result = engine.compress(messages)
    assert result == messages
    assert result is not messages
    assert all(
        produced is not original for produced, original in zip(result, messages, strict=True)
    )
    assert factory.store.ingest_calls == []
    assert engine.compression_count == 0


def test_clone_for_agent_does_not_share_the_store_or_copy_sqlite_state():
    engine, factory = started_engine()
    engine.context_length = 128_000
    engine.threshold_tokens = 96_000
    engine.last_prompt_tokens = 4321
    engine.compression_count = 3

    clone = engine.clone_for_agent()

    assert clone is not engine
    assert clone.store is None
    assert clone.context_length == 128_000
    assert clone.threshold_tokens == 96_000
    assert clone.last_prompt_tokens == 4321
    assert clone.calibration is not engine.calibration
    assert clone.name == engine.name
    assert factory.opened == [factory.opened[0]]  # no extra store opened by cloning


def test_clone_starts_a_separate_store_for_its_own_session():
    engine, factory = started_engine()
    clone = engine.clone_for_agent()
    clone.on_session_start("child-session")
    assert [entry["session_id"] for entry in factory.opened] == ["sess-1", "child-session"]
    assert clone.store is not engine.store


def test_update_model_recomputes_thresholds_without_hermes(monkeypatch):
    engine, _ = started_engine()
    engine.model_thresholds = {"claude": 0.9, "claude-opus": 0.5}
    engine.update_model("claude-opus-4", 200_000, provider="p")
    assert engine.context_length == 200_000
    assert engine.threshold_percent == 0.5  # longest match wins
    assert engine.threshold_tokens == 100_000

    engine.update_model("claude-haiku", 100_000)
    assert engine.threshold_percent == 0.9

    engine.update_model("other-model", 50_000)
    assert engine.threshold_percent == engine._config_threshold_percent

    engine.update_model("claude-opus-4", 10_000)
    assert engine.context_length == 10_000
    assert engine.threshold_tokens == 5_000


def test_update_from_response_tracks_usage_and_calibrates():
    engine, _ = started_engine()
    engine._last_estimate = 1000
    engine.update_from_response(
        {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "cache_read_tokens": 900,
            "cache_write_tokens": 100,
        }
    )
    assert engine.last_prompt_tokens == 1200
    assert engine.last_completion_tokens == 300
    assert engine.last_total_tokens == 1500
    assert engine.last_cache_read_tokens == 900
    assert engine.last_cache_write_tokens == 100
    assert engine.calibration.samples == 1
    assert engine.calibration.ratio != 1.0


def test_update_from_response_ignores_a_missing_usage_sample():
    engine, _ = started_engine()
    engine.update_from_response({})
    assert engine.last_prompt_tokens == 0
    assert engine.calibration.samples == 0
    engine.update_from_response(None)  # type: ignore[arg-type]
    assert engine.last_prompt_tokens == 0


def test_usage_receipts_are_bounded():
    engine, _ = started_engine()
    for index in range(50):
        engine.update_from_response({"prompt_tokens": 10 + index})
    assert len(engine._usage_receipts) == 20
    assert engine._usage_receipts[-1]["prompt_tokens"] == 59


def test_get_status_reports_revision_budget_and_no_paths():
    history = coding_turns()
    engine, _ = started_engine(
        snapshot_for(history, [OperationSpec(kind="drop", span_ids=("a1", "t1"))], revision=4)
    )
    engine.context_length = 100_000
    engine.select_context(assemble(history), conversation_messages=history)

    status = engine.get_status()
    assert status["engine"] == "hermes-clm"
    assert status["revision"] == 4
    assert status["store"] == "attached"
    assert status["budget"]["context_length"] == 100_000
    assert status["config"]["reserve_tokens"] == budget_module.DEFAULT_RESERVE_TOKENS
    assert "model" not in json.dumps(status["config"])
    assert "/tmp/hermes-home" not in json.dumps(status)
    assert "last_prompt_tokens" in status and "usage_percent" in status


def test_on_session_end_ingests_and_closes():
    history = coding_turns()
    engine, factory = started_engine()
    engine.on_session_end("sess-1", history)
    assert factory.store.ingest_calls[-1] == history
    assert factory.store.closed is True
    assert engine.store is None


def test_on_session_reset_clears_counters_and_reopens_lazily():
    engine, factory = started_engine()
    engine.last_prompt_tokens = 10
    engine.compression_count = 2
    engine.on_session_reset()
    assert engine.last_prompt_tokens == 0
    assert engine.compression_count == 0
    assert engine.store is None
    # The session id survives so the next request reopens the same durable session.
    engine.select_context(assemble(coding_turns()), conversation_messages=coding_turns())
    assert len(factory.opened) == 2


def test_unknown_tool_call_returns_json_error():
    engine, _ = started_engine()
    payload = json.loads(engine.handle_tool_call("clm_missing", {}))
    assert "error" in payload


def test_tool_service_refuses_without_a_store():
    engine = CLMEngine(store_factory=StoreFactory())
    service = engine.tool_service()
    assert isinstance(service, ToolService)
    with pytest.raises(RuntimeError):
        service.inspect()


def test_tool_service_delegates_to_the_session_store():
    history = coding_turns()
    engine, factory = started_engine(snapshot_for(history))
    service = engine.tool_service()
    assert service.inspect(limit=5)["limit"] == 5
    assert service.retrieve("u1")["found"] is True
    assert service.search("files", limit=2)["limit"] == 2
    assert service.status()["engine"] == "hermes-clm"


def test_config_defaults_match_the_integration_contract():
    config = EngineConfig()
    assert config.reserve_tokens == 2048
    assert config.pressure_thresholds == (0.25, 0.5, 0.75)
    assert config.aux_enabled is True
    assert config.aux_timeout_seconds == 45.0
    assert config.archive_redact is True
    assert config.retention_sessions == 32


def test_config_reads_clm_prefixed_keys():
    config = EngineConfig.from_mapping(
        {
            "clm.reserve_tokens": 512,
            "clm.pressure_thresholds": [0.5, 0.9],
            "clm.aux_enabled": False,
            "clm.aux_timeout_seconds": 12.5,
            "clm.max_rewrite_bytes": 99,
        }
    )
    assert config.reserve_tokens == 512
    assert config.pressure_thresholds == (0.5, 0.9)
    assert config.aux_enabled is False
    assert config.aux_timeout_seconds == 12.5
    assert config.max_rewrite_bytes == 99
    assert "model" not in config.to_dict()


def test_config_rejects_model_or_provider_keys():
    for key in ("clm.model", "clm.provider", "model", "clm.base_url", "clm.api_key"):
        with pytest.raises(ValueError):
            EngineConfig.from_mapping({key: "x"})


def test_engine_degrades_when_the_store_factory_raises():
    def exploding_factory(hermes_home, session_id, *, read_only=False):
        raise OSError("disk on fire")

    engine = CLMEngine(store_factory=exploding_factory)
    engine.on_session_start("sess-1")
    assert engine.store is None
    assert (
        engine.select_context(assemble(coding_turns()), conversation_messages=coding_turns())
        is None
    )
    assert "OSError" in engine.get_status()["last_error"]


def test_store_snapshot_corruption_degrades_to_identity():
    history = coding_turns()
    store = make_store(snapshot_for(history))
    store.snapshot = lambda: None  # type: ignore[method-assign]  # structural corruption
    engine = CLMEngine(store_factory=lambda home, session, read_only=False: store)
    engine.on_session_start("sess-1")

    assert engine.select_context(assemble(history), conversation_messages=history) is None
    assert engine.get_status()["last_error"] == "snapshot_unavailable"
