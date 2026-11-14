"""Real-engine acceptance for the model-ergonomics paths.

Everything here runs the shipped ``CLMEngine`` against a real ``ContextStore``
under a throwaway home, with a scripted caller standing in for the host. There is
no provider, no model and no benchmark simulator, so these are
**real-engine-offline** checks: they prove that the shipped code paths project,
commit, refuse and report as specified. They prove nothing about model behaviour.

Each behaviour was already driven RED by a unit test in Tasks 2-10; this module is
the end-to-end proof across the whole stack (engine -> store -> projector -> tool
dispatch), not a replacement for those focused tests.
"""

from __future__ import annotations

import json
import re

import pytest

from _clm_fakes import assemble, assistant_call, message, tool_result
from hermes_clm import budget as _budget
from hermes_clm import notice as _notice
from hermes_clm.engine import CLMEngine, EngineConfig

pytestmark = pytest.mark.integration

SYSTEM_PROMPT = "You are Hermes. Follow the operator's instructions."

#: Fields whose value is a wall clock or a process-local identifier, and may
#: therefore differ between two otherwise identical runs.
_VOLATILE_KEYS = frozenset({"at", "created_at", "generated_at", "session_id", "store_root_session"})

STUB_RE = re.compile(r"\[clm\.offloaded span=(?P<span>sp_[0-9a-f]+)")


# --------------------------------------------------------------------------- host


class ScriptedHost:
    """Minimal host: build the request, run the engine hook, keep history raw."""

    def __init__(self, engine: CLMEngine, history: list[dict]) -> None:
        self.engine = engine
        self.history = history

    def request(self) -> list[dict]:
        return assemble(self.history, system=SYSTEM_PROMPT)

    def iterate(self, *, budget_tokens: int = 128_000):
        """One provider-bound iteration: assemble, then let the engine select."""
        request = self.request()
        incoming = next((row for row in reversed(request) if row["role"] == "user"), None)
        selected = self.engine.select_context(
            request,
            conversation_messages=list(self.history),
            incoming_message=incoming,
            budget_tokens=budget_tokens,
        )
        return request, selected


def real_engine(tmp_path, *, session_id: str = "sess-ergo", context_length: int = 64_000):
    engine = CLMEngine(
        config=EngineConfig(max_rewrite_bytes=4096),
        hermes_home=str(tmp_path / "home"),
    )
    engine.on_session_start(session_id, context_length=context_length)
    assert engine.store is not None, "real store did not open"
    return engine


def coding_history(turns: int = 3, tool_chars: int = 6_000) -> list[dict]:
    """Prior turns of tool work, then a live turn whose newest result is host-owned."""

    rows: list[dict] = [message("user", "start the job", "u0")]
    for index in range(turns):
        rows.append(assistant_call(f"a{index}", f"call_{index}"))
        rows.append(
            tool_result(f"t{index}", f"call_{index}", f"OUTPUT {index}\n" * (tool_chars // 9))
        )
        rows.append(message("assistant", f"step {index} done", f"s{index}"))
        rows.append(message("user", f"continue {index}", f"u{index + 1}"))
    rows.append(assistant_call("a-live", "call_live"))
    rows.append(tool_result("t-live", "call_live", "NEWEST TOOL OUTPUT"))
    return rows


def prose_history() -> list[dict]:
    """Settled assistant prose behind a live tool turn."""

    rows: list[dict] = [message("user", "explain the failure modes", "pu1")]
    rows.append(message("assistant", "FIRST LONG NOTES\n" + "detail line\n" * 200, "p1"))
    rows.append(message("user", "and the second one?", "pu2"))
    rows.append(message("assistant", "SECOND LONG NOTES\n" + "more detail\n" * 200, "p2"))
    rows.append(assistant_call("pa", "call_p"))
    rows.append(tool_result("pt", "call_p", "LIVE TOOL OUTPUT"))
    return rows


def call(engine: CLMEngine, name: str, args: dict, history: list[dict]) -> dict:
    return json.loads(engine.handle_tool_call(name, args, messages=list(history)))


def spans_by_uid(engine: CLMEngine, history: list[dict], **kwargs) -> dict[str, dict]:
    page = call(engine, "clm_inspect", {"limit": 100, **kwargs}, history)
    assert page["ok"] is True, page
    return {item["uid"]: item for item in page["items"]}


def sized_history(tokens: int) -> list[dict]:
    """One user row whose ``chars/4`` estimate is exactly ``tokens``.

    ``estimate_message_tokens`` is ``MESSAGE_OVERHEAD_TOKENS + ceil(len / 4)``, and
    the payload length is a multiple of four characters, so this is exact rather
    than approximate: the ratio under test cannot drift with calibration.
    """

    payload_chars = (tokens - _budget.MESSAGE_OVERHEAD_TOKENS) * _budget.CHARS_PER_TOKEN
    assert payload_chars > 0
    return [message("user", "x" * payload_chars, "u-sized")]


def scrub(value):
    """Drop wall-clock and process-local fields, recursively."""

    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if key not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


# ------------------------------------------------------- 1. pressure tiers


def test_pressure_tiers_follow_the_raw_context_length(tmp_path):
    """Tiers bucket against ``context_length``, not the reserve-shrunk window.

    The window under test is a 32k offline engine window (the host minimum stays
    64,000). At 24% of 32k the request is 7,680 tokens; measured against the usable
    window (32,000 - 2,048) the same projection reads 25.6% and would be
    mislabelled ``elevated``.
    """

    engine = real_engine(tmp_path, context_length=64_000)
    try:
        engine.update_model("scripted", 32_000)
        expected = {
            0.24: "normal",
            0.25: "elevated",
            0.50: "high",
            0.75: "critical",
            0.98: "critical",
        }
        for ratio, tier in expected.items():
            tokens = int(ratio * 32_000)
            state = engine.budget_state(sized_history(tokens))
            assert state["context_length"] == 32_000
            assert state["estimated_tokens"] == tokens
            assert state["ratio"] == pytest.approx(ratio, abs=1e-6)
            assert state["tier"] == tier, (ratio, state)

        # The discriminating case: 0.24 would read "elevated" against usable_limit.
        state = engine.budget_state(sized_history(int(0.24 * 32_000)))
        assert state["estimated_tokens"] / state["usable_limit"] >= 0.25
        assert state["tier"] == "normal"
    finally:
        engine.on_session_end("sess-ergo", [])


# ------------------------------------- 2. nothing compacts automatically


def test_no_ratio_ever_triggers_automatic_compression(tmp_path):
    """``should_compress*`` stay false and plain ``compress`` writes nothing."""

    engine = real_engine(tmp_path, context_length=64_000)
    try:
        engine.update_model("scripted", 32_000)
        for ratio in (0.0, 0.25, 0.5, 0.75, 0.98, 1.0):
            tokens = max(_budget.MESSAGE_OVERHEAD_TOKENS + 1, int(ratio * 32_000))
            history = sized_history(tokens)
            assert engine.should_compress() is False
            assert engine.should_compress(tokens) is False
            assert engine.should_compress_info(tokens) == (False, None)
            assert engine.should_compress_preflight(history) is False

            before_revision = engine.store.status()["revision"]
            out = engine.compress([dict(row) for row in history])
            assert out == history, "plain compress must be identity"
            assert engine.store.status()["revision"] == before_revision
            assert engine.store.status()["staged_transactions"] == []
    finally:
        engine.on_session_end("sess-ergo", [])


# ------------------------------- 3. manual vs overflow are distinct paths


def test_manual_and_overflow_compaction_are_separately_identified(tmp_path):
    """``/compact`` commits as operator work; overflow commits as system work."""

    engine = real_engine(tmp_path)
    try:
        history = coding_history()
        host = ScriptedHost(engine, history)
        host.iterate()  # observe one request so candidates and previews exist

        manual = engine.compress([dict(row) for row in history], force=True)
        assert engine._last_compress_aborted is False, engine._last_summary_error
        assert manual != history, "manual compaction must actually reduce something"

        summary = call(engine, "clm_status", {}, history)
        assert summary["last_compaction"]["actor"] == "operator"
        assert summary["last_compaction"]["spans_offloaded"] >= 1

        # The notice rides on the *projection*, never on the compress return value:
        # it is byte-stable per revision so the cached prefix survives.
        _, selected = host.iterate()
        assert selected is not None
        notices = [row for row in selected if _notice.is_notice_row(row)]
        assert len(notices) == 1, selected
        assert selected[1] is notices[0]
        assert "actor=operator" in notices[0]["content"]

        # Overflow is a different actor, on its own session and its own window.
        overflow = real_engine(tmp_path, session_id="sess-overflow")
        try:
            overflow.update_model("scripted", 32_000)
            big = coding_history(turns=4, tool_chars=40_000)
            overflow_host = ScriptedHost(overflow, big)
            overflow_host.iterate(budget_tokens=32_000)
            state = overflow.budget_state(big)
            assert state["fits"] is False, state
            assert state["tier"] == "critical"

            out = overflow.compress([dict(row) for row in big], bypass_cooldown=True)
            assert overflow._last_compress_aborted is False, overflow._last_summary_error
            assert out is not None
            overflow_summary = call(overflow, "clm_status", {}, big)
            assert overflow_summary["last_compaction"]["actor"] == "system"
            assert overflow_summary["last_compaction"]["spans_offloaded"] >= 1
        finally:
            overflow.on_session_end("sess-overflow", [])
    finally:
        engine.on_session_end("sess-ergo", [])


# ---------------------------------- 4. headroom and compactable reporting


def test_summary_reports_headroom_and_compactable_without_faking_zero(tmp_path):
    engine = real_engine(tmp_path)
    try:
        history = coding_history()
        host = ScriptedHost(engine, history)
        host.iterate()

        summary = call(engine, "clm_status", {}, history)
        assert summary["ok"] is True
        assert summary["tokens"]["scope"] == "request"
        estimated = summary["tokens"]["calibrated"]
        assert isinstance(summary["headroom_tokens"], int)
        assert summary["headroom_tokens"] == max(0, (summary["context_length"] - 2048) - estimated)
        assert summary["overflow_tokens"] == 0
        assert isinstance(summary["compactable_tokens"], int)
        assert summary["compactable_tokens"] > 0
        assert summary["compactable_units"] >= 1
        assert "compactable_unknown" not in summary
        # The store-wide aggregate is a different scope, not this request's total.
        assert "all_spans_est_tokens" not in summary

        # Attached but no request observed yet: unknown, never zero. No context
        # length has been reported either, so headroom is unknown rather than 0.
        cold = real_engine(tmp_path, session_id="sess-cold", context_length=0)
        try:
            cold_status = call(cold, "clm_status", {}, history)
            assert cold_status["compactable_tokens"] is None
            assert cold_status["compactable_unknown"] == "no_request_observed"
            assert cold_status["headroom_tokens"] is None  # no context length, not 0
            assert cold_status["tokens"]["calibrated"] == 0
        finally:
            cold.on_session_end("sess-cold", [])

        # No store at all is a typed refusal, not a report full of zeros.
        detached = CLMEngine(config=EngineConfig(), hermes_home=str(tmp_path / "detached"))
        refused = call(detached, "clm_status", {}, history)
        assert refused["ok"] is False
        assert refused["errors"][0]["code"] == "no_session"
        assert "compactable_tokens" not in refused
    finally:
        engine.on_session_end("sess-ergo", [])


# -------------------------------- 5. stale rows after a host retry


def test_a_host_retry_leaves_old_rows_stale_and_the_preview_says_so(tmp_path):
    """Stale rows are hidden by default and an edit aimed at one cannot reach the request.

    The store has no notion of request alignment (``store.py`` never aligns rows),
    so it books the operation against span state; the **request-level preview** is
    what must report the batch as inert. Both halves are asserted here: the
    response must not claim a saving it cannot deliver.
    """

    engine = real_engine(tmp_path)
    try:
        history = coding_history(turns=2, tool_chars=200)
        host = ScriptedHost(engine, history)
        host.iterate()

        # The host retries with a shorter transcript: earlier rows stay archived
        # but are no longer supplied.
        retry = history[-4:]
        engine.select_context(assemble(retry, system=SYSTEM_PROMPT), conversation_messages=retry)

        default_page = call(engine, "clm_inspect", {"limit": 100}, retry)
        default_uids = {item["uid"] for item in default_page["items"]}
        assert "s0" not in default_uids, "absent rows are omitted by default"
        assert default_page["omitted_absent"] >= 1

        by_uid = spans_by_uid(engine, retry, include_stale=True)
        assert by_uid["s0"]["status"] == "stale"
        assert by_uid["s0"]["present"] is False
        assert by_uid["s0"]["protected"] is False

        status = call(engine, "clm_status", {}, retry)
        staged = call(
            engine,
            "clm_stage",
            {
                "operations": [{"kind": "drop", "span_ids": [by_uid["s0"]["span_id"]]}],
                "expected_revision": status["revision"],
                "txn_id": "txn-stale",
            },
            retry,
        )
        assert staged["ok"] is True, staged
        projected = staged["projected"]
        assert projected["available"] is True
        assert projected["inert"] == 1
        assert projected["applied"] == 0
        assert projected["saved"] == 0
        # The store's own receipt is bookkeeping against span state, not a claim
        # that the request changed.
        assert staged["applied"][0]["effect"] == "applied"
        assert staged["inert"] == []
    finally:
        engine.on_session_end("sess-ergo", [])


# ------------------------------------------------ 6. fast commit path


def test_fast_commit_advances_one_revision_and_an_unmet_minimum_writes_nothing(tmp_path):
    engine = real_engine(tmp_path)
    try:
        history = coding_history()
        host = ScriptedHost(engine, history)
        host.iterate()
        spans = spans_by_uid(engine, history)
        status = call(engine, "clm_status", {}, history)
        # A tool group is atomic: the assistant call row and its result go together.
        group = [spans["a0"]["span_id"], spans["t0"]["span_id"]]

        refused = call(
            engine,
            "clm_stage",
            {
                "operations": [{"kind": "offload", "span_ids": group}],
                "expected_revision": status["revision"],
                "txn_id": "txn-min",
                "commit": True,
                "min_saved_tokens": 10_000_000,
            },
            history,
        )
        assert refused["ok"] is False
        assert refused["errors"][0]["code"] == "insufficient_saving"
        after_refusal = call(engine, "clm_status", {}, history)
        assert after_refusal["revision"] == status["revision"]
        assert not after_refusal.get("staged"), "a refused fast commit must not stage"

        applied = call(
            engine,
            "clm_stage",
            {
                "operations": [{"kind": "offload", "span_ids": group}],
                "expected_revision": status["revision"],
                "txn_id": "txn-fast",
                "commit": True,
            },
            history,
        )
        assert applied["ok"] is True, applied
        assert applied["committed"] is True and applied["atomic"] is True
        assert applied["revision"] == status["revision"] + 1

        _, selected = host.iterate()
        assert selected is not None
        stubbed = [
            row
            for row in selected
            if isinstance(row.get("content"), str) and row["content"].startswith("[clm.offloaded")
        ]
        assert len(stubbed) == 2, selected  # the assistant row and its result
    finally:
        engine.on_session_end("sess-ergo", [])


# ------------------------------------------------- 7. literal patch


def test_a_literal_patch_reaches_the_request_and_the_archive_stays_byte_exact(tmp_path):
    engine = real_engine(tmp_path)
    try:
        original = "deploy report\nSTATUS: failing\nrollback ready"
        patched_text = "deploy report\nSTATUS: passing\nrollback ready"
        history = [
            message("user", "what does the report say", "u1"),
            assistant_call("a1", "call_1"),
            tool_result("t1", "call_1", original),
            message("assistant", "It reports a failure.", "a2"),
            message("user", "fix it", "u2"),
            assistant_call("a3", "call_2"),
            tool_result("t2", "call_2", "NEWEST TOOL OUTPUT"),
        ]
        host = ScriptedHost(engine, history)
        host.iterate()
        spans = spans_by_uid(engine, history)
        status = call(engine, "clm_status", {}, history)
        operation = {
            "kind": "patch",
            "span_ids": [spans["t1"]["span_id"]],
            "old": "STATUS: failing",
            "new": "STATUS: passing",
        }

        staged = call(
            engine,
            "clm_stage",
            {
                "operations": [operation],
                "expected_revision": status["revision"],
                "txn_id": "txn-patch",
            },
            history,
        )
        assert staged["ok"] is True, staged
        # Resolved into a whole-span rewrite before storage, with a digest receipt.
        assert staged["staged_operations"][0]["kind"] == "rewrite"
        assert staged["staged_operations"][0]["via"] == "patch"
        assert staged["patched"][0]["kind"] == "patch"

        # The fast path is refused for patches: replay cannot re-resolve live text.
        fast = call(
            engine,
            "clm_stage",
            {
                "operations": [operation],
                "expected_revision": status["revision"],
                "txn_id": "txn-patch-fast",
                "commit": True,
            },
            history,
        )
        assert fast["ok"] is False
        assert fast["errors"][0]["code"] == "invalid_field"

        committed = call(
            engine,
            "clm_commit",
            {"txn_id": "txn-patch", "expected_revision": status["revision"]},
            history,
        )
        assert committed["ok"] is True, committed

        _, selected = host.iterate()
        assert selected is not None
        contents = [row.get("content") for row in selected]
        assert patched_text in contents
        assert original not in contents

        # The archive is untouched: the original bytes are still recoverable.
        archived = call(engine, "clm_retrieve", {"span_id": spans["t1"]["span_id"]}, history)
        assert archived["ok"] is True, archived
        assert archived["message"]["content"] == original
        assert archived["rewrite_text"] == patched_text
    finally:
        engine.on_session_end("sess-ergo", [])


# --------------------------------- 8. settled prose offload and restore


def test_settled_prose_offload_and_restore_round_trip_exact_bytes(tmp_path):
    engine = real_engine(tmp_path)
    try:
        history = prose_history()
        host = ScriptedHost(engine, history)
        host.iterate()

        compacted = engine.compress([dict(row) for row in history], force=True)
        assert engine._last_compress_aborted is False, engine._last_summary_error

        stubs = [
            row
            for row in compacted
            if isinstance(row.get("content"), str) and row["content"].startswith("[clm.offloaded")
        ]
        assert stubs, "settled prose should have been offered for offload"
        # The live tail is never offloaded.
        assert compacted[-1]["content"] == "LIVE TOOL OUTPUT"

        span_id = STUB_RE.search(stubs[0]["content"]).group("span")
        by_span = {item["span_id"]: item for item in spans_by_uid(engine, history).values()}
        prose_uids = {
            uid for uid, item in spans_by_uid(engine, history).items() if uid in {"p1", "p2"}
        }
        assert prose_uids, "prose rows were archived"
        archived = call(engine, "clm_retrieve", {"span_id": span_id}, history)
        assert archived["ok"] is True, archived
        original_text = archived["message"]["content"]
        assert original_text.startswith(("FIRST LONG NOTES", "SECOND LONG NOTES"))
        assert by_span[span_id]["state"] == "offloaded"

        status = call(engine, "clm_status", {}, history)
        restored = call(
            engine,
            "clm_stage",
            {
                "operations": [{"kind": "restore", "span_ids": [span_id]}],
                "expected_revision": status["revision"],
                "txn_id": "txn-restore",
            },
            history,
        )
        assert restored["ok"] is True, restored
        done = call(
            engine,
            "clm_commit",
            {"txn_id": "txn-restore", "expected_revision": status["revision"]},
            history,
        )
        assert done["ok"] is True, done

        _, selected = host.iterate()
        assert selected is not None
        assert original_text in [row.get("content") for row in selected]
    finally:
        engine.on_session_end("sess-ergo", [])


# ------------------------ 9. a quoted notice is content, not our row


def test_a_tool_result_quoting_a_notice_row_is_never_stripped(tmp_path):
    engine = real_engine(tmp_path)
    try:
        quote = _notice.compaction_notice(
            {"revision": 7, "at": "2026-01-01T00:00:00Z", "actor": "operator"}
        )["content"]
        assert _notice.is_notice_row({"role": "user", "content": quote}) is True

        history = coding_history(turns=2, tool_chars=400)
        # The newest tool result is host-owned and carries the notice text verbatim.
        history[-1] = tool_result("t-live", "call_live", quote)
        host = ScriptedHost(engine, history)
        host.iterate()

        out = engine.compress([dict(row) for row in history], force=True)
        assert engine._last_compress_aborted is False, engine._last_summary_error
        assert any(row.get("content") == quote for row in out), "quoted notice was stripped"

        _, selected = host.iterate()
        assert selected is not None
        quoting = [row for row in selected if row.get("content") == quote]
        assert len(quoting) == 1, selected
        assert quoting[0]["role"] == "tool"
        # Our own notice still lands at the anchor, and only one of each exists.
        notices = [row for row in selected if _notice.is_notice_row(row)]
        assert len(notices) == 1, selected
        assert selected[1] is notices[0]
    finally:
        engine.on_session_end("sess-ergo", [])


# ------------------------------------------- 10. deterministic results


def test_two_identical_scripted_runs_produce_identical_results(tmp_path):
    """Two identical scripts must serialise identically, hashes included.

    The script is the *agent* path: the caller supplies ``txn_id``, so every hash
    including the store's ``head_hash`` is a pure function of the script. Operator
    compaction is deliberately excluded: ``_manual`` mints ``manual-<uuid4>``, so
    its txn id — and every hash chained through it — differs by design between
    runs. That is randomness, not drift, and scrubbing it silently would hide a
    real comparison; scripted edits keep the strongest field in the comparison.
    """

    def run(root):
        engine = real_engine(root, session_id="sess-determinism")
        try:
            history = coding_history(turns=2, tool_chars=900)
            host = ScriptedHost(engine, history)
            host.iterate()
            spans = spans_by_uid(engine, history)
            status = call(engine, "clm_status", {}, history)
            staged = call(
                engine,
                "clm_stage",
                {
                    "operations": [
                        {
                            "kind": "offload",
                            "span_ids": [spans["a0"]["span_id"], spans["t0"]["span_id"]],
                        }
                    ],
                    "expected_revision": status["revision"],
                    "txn_id": "txn-script-1",
                },
                history,
            )
            committed = call(
                engine,
                "clm_commit",
                {"txn_id": "txn-script-1", "expected_revision": status["revision"]},
                history,
            )
            after = call(engine, "clm_status", {}, history)
            detail = call(engine, "clm_status", {"detail": "full"}, history)
            _, selected = host.iterate()
            return {
                "staged": staged,
                "committed": committed,
                "status": after,
                "detail": scrub(detail),
                "projection": scrub(selected),
            }
        finally:
            engine.on_session_end("sess-determinism", [])

    first = run(tmp_path / "one")
    second = run(tmp_path / "two")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # The strongest field in the comparison really is present and identical.
    assert first["detail"]["head_hash"] == second["detail"]["head_hash"]
    assert first["status"]["revision"] == 2
