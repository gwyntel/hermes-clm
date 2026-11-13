"""Engine lifecycle, clone, model switch, usage accounting, config and degradation."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from _clm_fakes import (
    FakeOperation,
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


def test_default_store_factory_is_lazy_and_contains_errors(tmp_path):
    """The factory imports the store on call (not at import) and real errors stay contained."""
    # 1. Lazy: importing the engine does not import the store package.
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, "src")
        import hermes_clm.engine  # noqa: F401
        assert "hermes_clm.store" not in sys.modules, sorted(sys.modules)
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd="."
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout

    # 2. Real open: the store exists on this branch, so opening must succeed.
    store = default_store_factory(str(tmp_path / "home"), "sess-lazy")
    try:
        assert store.session_id == "sess-lazy"
        assert store.directory.is_dir()
        assert store.status()["ok"] is True
    finally:
        store.close()

    # 3. Error containment: a bad home raises from the store, and the engine
    #    absorbs it into detached mode instead of breaking the turn.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    from hermes_clm.errors import CLMError

    with pytest.raises((CLMError, OSError)):
        default_store_factory(str(blocker), "sess-unopenable")

    engine = CLMEngine(hermes_home=str(blocker), store_factory=default_store_factory)
    engine.on_session_start("sess-unopenable")
    assert engine.store is None
    assert engine.get_status()["last_error"]
    assert (
        engine.select_context(assemble(coding_turns()), conversation_messages=coding_turns())
        is None
    )


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


# ------------------------------------------------------------------ config seam


def test_engine_accepts_the_plugins_flat_clm_mapping():
    """``plugin.build_engine`` passes ``dict(read_clm_config())``: flat ``clm.*`` keys."""
    raw = {
        "clm.reserve_tokens": 512,
        "clm.pressure_thresholds": [0.5, 0.9],
        "clm.aux_enabled": False,
        "clm.aux_timeout_seconds": 12.5,
        "clm.max_rewrite_bytes": 99,
        "clm.max_aux_spans": 3,
        "clm.max_operations": 2,
    }
    engine = CLMEngine(config=raw, store_factory=StoreFactory())

    assert isinstance(engine.config, EngineConfig)
    assert engine.config.reserve_tokens == 512
    assert engine.config.pressure_thresholds == (0.5, 0.9)
    assert engine.config.aux_enabled is False
    assert engine.config.max_rewrite_bytes == 99
    # The raw mapping survives for ToolLimits, including keys EngineConfig lacks.
    assert engine._tool_limits.max_rewrite_bytes == 99
    assert engine._tool_limits.max_aux_spans == 3
    assert engine._tool_limits.max_operations == 2
    # And the engine is usable: the aux compressor is built from that config,
    # with the compression task only and no provider surface of its own.
    assert engine.aux.enabled is False
    metrics = engine.aux.metrics()
    assert metrics["task"] == "compression"
    assert metrics["timeout_seconds"] == 12.5
    assert not any(key in json.dumps(metrics) for key in ("model", "provider", "base_url"))
    assert engine.get_tool_schemas()
    assert engine.get_status()["config"]["reserve_tokens"] == 512


def test_engine_rejects_model_or_provider_config():
    with pytest.raises(ValueError):
        CLMEngine(config={"clm.model": "gpt"}, store_factory=StoreFactory())


def test_engine_accepts_an_engine_config_object_unchanged():
    config = EngineConfig(reserve_tokens=777)
    engine = CLMEngine(config=config, store_factory=StoreFactory())
    assert engine.config is config
    assert engine._tool_limits.max_rewrite_bytes == config.max_rewrite_bytes


# --------------------------------------------------------- default store config


def test_default_store_factory_applies_engine_config_to_the_store(tmp_path):
    config = EngineConfig(
        max_retrieve_bytes=1234,
        max_rewrite_bytes=4321,
        max_aux_spans=3,
        archive_redact=False,
        retention_sessions=5,
    )
    store = default_store_factory(str(tmp_path / "home"), "sess-config", config=config)
    try:
        status = store.status()
        assert status["limits"]["max_retrieve_bytes"] == 1234
        assert status["limits"]["max_rewrite_bytes"] == 4321
        assert status["limits"]["max_aux_spans"] == 3
        # Redaction is a store-side decision, so the engine's flag must reach it.
        assert status["redaction"]["enabled"] is False
    finally:
        store.close()


def test_engine_with_the_real_factory_opens_a_store_with_its_limits(tmp_path):
    engine = CLMEngine(
        config={"clm.max_retrieve_bytes": 2048, "clm.archive_redact": False},
        hermes_home=str(tmp_path / "home"),
    )
    engine.on_session_start("sess-real")

    assert engine.store is not None
    try:
        status = engine.tool_service().status()
        assert status["limits"]["max_retrieve_bytes"] == 2048
        assert status["redaction"]["enabled"] is False
        assert status["revision"] >= 1  # a fresh store starts at revision 1
    finally:
        engine.on_session_end("sess-real", [])


def test_custom_store_factories_keep_the_three_argument_protocol():
    factory = StoreFactory()
    engine = CLMEngine(store_factory=factory, hermes_home="/tmp/hermes-home")
    engine.on_session_start("sess-custom")
    assert factory.opened == [
        {"hermes_home": "/tmp/hermes-home", "session_id": "sess-custom", "read_only": False}
    ]


# --------------------------------------------------------------- service scope


def test_service_for_session_is_scoped_to_this_engine():
    engine, factory = started_engine()
    service = engine.service_for_session()
    assert isinstance(service, ToolService)
    assert service.revision == factory.store.status()["revision"]

    assert engine.service_for_session("sess-1") is not None
    # Another session never resolves: no cross-session reads, ever.
    assert engine.service_for_session("sess-other") is None
    assert [entry["session_id"] for entry in factory.opened] == ["sess-1"]


def test_service_for_session_attaches_the_store_lazily():
    engine, factory = engine_for()
    assert engine.store is None
    assert factory.opened == []

    service = engine.service_for_session("sess-tools")

    assert service is not None
    assert engine.session_id == "sess-tools"
    assert factory.opened[0]["session_id"] == "sess-tools"
    # A second, different session is refused even though the engine is attached.
    assert engine.service_for_session("sess-other") is None


def test_service_for_session_returns_none_when_the_store_cannot_open():
    def exploding_factory(hermes_home, session_id, *, read_only=False):
        raise OSError("disk on fire")

    engine = CLMEngine(store_factory=exploding_factory, session_id="sess-bad")
    assert engine.service_for_session() is None
    assert "OSError" in engine.get_status()["last_error"]


# ------------------------------------------------------------ tool service API


class _RecordingStore:
    """Store stand-in exposing the proposal/operator surface ToolService delegates to."""

    def __init__(self, revision: int = 3) -> None:
        self.revision = revision
        self.calls: list[tuple[str, dict]] = []
        self.proposal = {
            "proposal_id": "prop_1",
            "proposal_sha256": "a" * 64,
            "base_revision": revision,
            "origin": "aux",
            "operations": [{"kind": "rewrite", "span_ids": ["t1"], "replacement": "short"}],
            "span_ids": ["t1"],
            "created_revision": revision,
        }
        self.stage_result = {"ok": True, "revision": revision, "errors": [], "receipts": []}

    def status(self):
        return {"ok": True, "revision": self.revision}

    def create_proposal(self, operations, *, base_revision):
        self.calls.append(("create_proposal", {"operations": operations, "base": base_revision}))
        return dict(self.proposal, base_revision=base_revision)

    def accept_proposal(self, **kwargs):
        self.calls.append(("accept_proposal", dict(kwargs)))
        return dict(self.stage_result)

    def operator_unpin_many(self, span_ids, *, expected_revision, txn_id):
        self.calls.append(
            (
                "operator_unpin_many",
                {
                    "span_ids": list(span_ids),
                    "expected_revision": expected_revision,
                    "txn_id": txn_id,
                },
            )
        )
        self.revision = expected_revision + 1
        return {
            "ok": True,
            "revision": self.revision,
            "applied": [
                {"span_id": span_id, "kind": "unpin", "effect": "applied"} for span_id in span_ids
            ],
            "errors": [],
        }


def _service_with(store):
    engine = CLMEngine(store_factory=lambda home, session, read_only=False: store)
    engine.on_session_start("sess-props")
    return engine.tool_service(), store


def test_service_revision_reads_the_store_not_the_last_projection():
    history = coding_turns()
    engine, factory = started_engine(snapshot_for(history, [], revision=4))
    engine.select_context(assemble(history), conversation_messages=history)
    assert engine.get_status()["revision"] == 4  # cached projection revision

    # The store moves on; the tool surface must report the live head.
    factory.store._snapshot.revision = 7

    assert engine.tool_service().revision == 7
    assert engine.tool_service().status()["revision"] == 7


def test_service_revision_is_zero_without_a_store():
    engine = CLMEngine(store_factory=StoreFactory())
    assert engine.tool_service().revision == 0


def test_service_delegates_proposal_creation_and_acceptance():
    service, store = _service_with(_RecordingStore(revision=3))
    operations = [{"kind": "rewrite", "span_ids": ["t1"], "replacement": "short"}]

    proposal = service.create_proposal(operations, base_revision=3)

    assert proposal["proposal_id"] == "prop_1"
    assert proposal["base_revision"] == 3
    assert store.calls[0][1]["base"] == 3
    # The store owns proposal identity; its hash is surfaced unchanged.
    assert proposal["proposal_sha256"] == "a" * 64
    assert "store_proposal_sha256" not in proposal
    # The store defaults a missing actor to aux; the engine adds no fields.
    assert store.calls[0][1]["operations"] == operations

    accepted = service.accept_proposal(
        proposal_id="prop_1", proposal_sha256="a" * 64, base_revision=3, txn_id="txn-acc"
    )
    assert accepted["ok"] is True
    assert store.calls[1][0] == "accept_proposal"
    assert store.calls[1][1]["txn_id"] == "txn-acc"
    assert store.calls[1][1]["proposal_sha256"] == "a" * 64


def test_service_create_proposal_defaults_to_the_live_revision():
    service, store = _service_with(_RecordingStore(revision=5))
    service.create_proposal([{"kind": "rewrite", "span_ids": ["t1"], "replacement": "x"}])
    assert store.calls[0][1]["base"] == 5


def test_service_unpin_is_operator_only_and_one_atomic_revision():
    service, store = _service_with(_RecordingStore(revision=2))

    refused = service.unpin(["t1"], expected_revision=2, txn_id="txn-a", actor_origin="agent")
    assert refused["ok"] is False
    assert refused["errors"][0]["code"] == "pin_authority"
    assert store.calls == []

    applied = service.unpin(
        ["t1", "t2"], expected_revision=2, txn_id="txn-op", actor_origin="operator"
    )
    assert applied["ok"] is True
    assert applied["revision"] == 3, "a batch unpin is exactly one revision"
    # One store call, one txn id: a mid-batch refusal cannot leave a partial unpin.
    assert store.calls == [
        (
            "operator_unpin_many",
            {"span_ids": ["t1", "t2"], "expected_revision": 2, "txn_id": "txn-op"},
        )
    ]
    assert [receipt["span_id"] for receipt in applied["applied"]] == ["t1", "t2"]


def test_select_context_refuses_a_truncated_operation_log():
    history = coding_turns()
    snapshot = snapshot_for(history, [FakeOperation(kind="drop", span_ids=("t1",))])
    snapshot.operations_truncated = True
    engine, _factory = started_engine(snapshot)

    assert engine.select_context(assemble(history), conversation_messages=history) is None
    assert engine._last_error == "operations_truncated"


def test_handle_tool_call_rejects_non_mapping_arguments():
    engine, _factory = started_engine()

    for args in (["not", "a", "mapping"], 7, '"status"', "{bad json"):
        payload = json.loads(engine.handle_tool_call("clm_status", args))
        assert payload["ok"] is False
        assert payload["errors"][0]["code"] == "invalid_arguments"


# ---------------------------------------------------------------- tool dispatch


def test_engine_advertises_the_clm_tools():
    engine, _ = started_engine()
    names = {schema["name"] for schema in engine.get_tool_schemas()}
    assert {
        "clm_status",
        "clm_inspect",
        "clm_stage",
        "clm_commit",
        "clm_discard",
        "clm_revert",
        "clm_search",
        "clm_retrieve",
        "clm_pin",
        "clm_boundary",
        "clm_propose_compression",
        "clm_accept_proposal",
    } <= names
    for schema in engine.get_tool_schemas():
        assert "parameters" in schema and "description" in schema
        assert not any(key in json.dumps(schema) for key in ("api_key", "base_url"))


def test_tool_dispatch_status_reads_the_live_store_revision():
    history = coding_turns()
    engine, factory = started_engine(snapshot_for(history, [], revision=4))
    factory.store._snapshot.revision = 6

    payload = json.loads(engine.handle_tool_call("clm_status", {"detail": "full"}))

    assert payload["ok"] is True
    assert payload["revision"] == 6
    assert payload["engine"] == "hermes-clm"
    assert "aux" in payload


def test_tool_dispatch_stage_and_commit_go_through_the_scoped_service():
    history = coding_turns()
    engine, factory = started_engine(snapshot_for(history, [], revision=1))

    staged = json.loads(
        engine.handle_tool_call(
            "clm_stage",
            {
                "operations": [{"kind": "drop", "span_ids": ["a1", "t1"]}],
                "expected_revision": 1,
                "txn_id": "txn-tool",
            },
            messages=history,
        )
    )
    assert staged["ok"] is True
    assert staged["txn_id"] == "txn-tool"
    committed = json.loads(
        engine.handle_tool_call("clm_commit", {"txn_id": "txn-tool", "expected_revision": 1})
    )

    assert committed["ok"] is True
    assert factory.store.commits == [{"txn_id": "txn-tool", "expected_revision": 1}]
    assert factory.store.staged[0][3] == "agent"
    # messages=... is archived before dispatch so a mid-turn call sees its rows.
    assert factory.store.ingest_calls[0] == history


def test_tool_dispatch_never_crosses_sessions():
    engine, factory = started_engine(snapshot_for(coding_turns()))
    payload = json.loads(engine.handle_tool_call("clm_status", {}, session_id="sess-other"))
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "no_session"
    assert [entry["session_id"] for entry in factory.opened] == ["sess-1"]


def test_tool_dispatch_returns_json_errors_and_never_raises():
    engine, _ = started_engine()
    unknown = json.loads(engine.handle_tool_call("clm_nope", {}))
    assert unknown["ok"] is False and "error" in unknown

    # A handler that explodes is still a JSON reply, not an exception.
    class Exploding:
        name = "clm_status"

        @property
        def schema(self):
            return {"name": self.name, "description": "x", "parameters": {}}

        def handler(self, *_args, **_kwargs):
            raise RuntimeError("boom at /private/store.sqlite3")

    engine._tool_specs = [Exploding()]
    broken = json.loads(engine.handle_tool_call("clm_status", {}))
    assert broken["ok"] is False
    assert "RuntimeError" in broken["error"]

    # Bad argument shapes are reported, not raised.
    engine._tool_specs = None
    assert json.loads(engine.handle_tool_call("clm_stage", "not-json"))["ok"] is False
    assert json.loads(engine.handle_tool_call("clm_stage", {"operations": "x"}))["ok"] is False


def test_clone_shares_the_aux_compressor_and_config_without_deepcopy():
    engine, _ = started_engine()
    clone = engine.clone_for_agent()
    assert clone.aux is engine.aux
    assert clone.config is engine.config
    assert clone._store_factory is engine._store_factory
    assert clone._raw_config == engine._raw_config
    assert clone._raw_config is not engine._raw_config  # a copy, not shared mutation
    assert clone.store is None
