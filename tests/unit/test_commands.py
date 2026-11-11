"""Operator command surface: status, manual edit (stage+commit), unpin."""

from __future__ import annotations

from clm_fakes import FakeService

from hermes_clm.commands import OPERATOR_ORIGIN, USAGE, handle, parse


def run(raw: str, service=None):
    return handle(raw, resolve_service=lambda session_id: service)


def test_parse_splits_subcommand():
    assert parse("") == ("help", [])
    assert parse("   ") == ("help", [])
    assert parse("status") == ("status", [])
    assert parse("edit drop m-1 m-2") == ("edit", ["drop", "m-1", "m-2"])
    assert parse("STATUS") == ("status", [])


def test_help_when_empty():
    text = run("")
    assert text == USAGE
    assert "/clm status" in text


def test_help_variants():
    for raw in ("help", "?", "-h", "--help"):
        assert run(raw) == USAGE


def test_unknown_subcommand_explains_itself():
    text = run("frobnicate")
    assert "unknown subcommand" in text
    assert "/clm help" in text


def test_status_without_a_store():
    text = run("status", service=None)
    assert "no CLM store" in text


def test_status_reports_revision_counts_budget_and_aux():
    service = FakeService(revision=12)
    service.status_result = {
        "ok": True,
        "revision": 12,
        "head_hash": "0123456789abcdef",
        "active": 30,
        "offloaded": 4,
        "pinned": 2,
        "estimated_tokens": 96000,
        "context_length": 128000,
        "reserve_tokens": 2048,
        "pressure": 0.75,
        "store_health": "ok",
        "last_error": None,
        "aux": {"task": "compression", "enabled": True, "calls": 2, "failures": 0, "latency_ms_total": 900},
    }
    text = run("status", service=service)
    assert "revision 12" in text
    assert "head 0123456789ab" in text
    assert "30 active" in text and "4 offloaded" in text and "2 pinned" in text
    assert "96000 / 128000 tokens (75%)" in text
    assert "reserve 2048" in text and "pressure 0.75" in text
    assert "store: ok" in text
    assert "aux: compression enabled" in text and "avg 450ms" in text
    assert "last error: none" in text


def test_status_surfaces_a_store_error():
    service = FakeService()
    service.status_result = {"ok": False, "errors": [{"code": "corrupt", "message": "index rebuild failed"}]}
    text = run("status", service=service)
    assert "corrupt" in text and "index rebuild failed" in text


def test_status_rejects_arguments():
    text = run("status now", service=FakeService())
    assert "status takes no arguments" in text


# --------------------------------------------------------------------------- #
# Manual edit
# --------------------------------------------------------------------------- #


def test_edit_drop_stages_and_commits_as_the_operator():
    service = FakeService(revision=7)
    text = run("edit drop m-0001 m-0002", service=service)
    assert "drop applied to m-0001, m-0002" in text
    assert service.names() == ["status", "stage", "commit"]

    operations = service.args_for("stage")[0]
    assert operations == [{"kind": "drop", "span_ids": ["m-0001", "m-0002"]}]
    stage_kwargs = service.kwargs_for("stage")
    assert stage_kwargs["expected_revision"] == 7
    assert stage_kwargs["actor_origin"] == OPERATOR_ORIGIN
    commit_kwargs = service.kwargs_for("commit")
    assert commit_kwargs["expected_revision"] == 7
    assert commit_kwargs["txn_id"] == stage_kwargs["txn_id"]


def test_edit_rewrite_joins_the_replacement_text():
    service = FakeService(revision=2)
    text = run("edit rewrite m-0001 shorter version here", service=service)
    assert "rewrite applied to m-0001" in text
    assert service.args_for("stage")[0] == [
        {"kind": "rewrite", "span_ids": ["m-0001"], "replacement": "shorter version here"}
    ]


def test_edit_rewrite_requires_text():
    text = run("edit rewrite m-0001", service=FakeService())
    assert "usage" in text.lower()


def test_edit_requires_kind_and_span():
    assert "usage" in run("edit", service=FakeService()).lower()
    assert "usage" in run("edit drop", service=FakeService()).lower()


def test_edit_rejects_unknown_kinds():
    text = run("edit summarize m-0001", service=FakeService())
    assert "unknown edit kind" in text
    assert "drop" in text and "rewrite" in text


def test_edit_rejects_malformed_span_ids():
    service = FakeService()
    text = run("edit drop ../etc/passwd", service=service)
    assert "malformed span ID" in text
    assert service.names() == []


def test_edit_stage_failure_does_not_commit():
    service = FakeService(revision=3)
    service.stage_result = {
        "ok": False,
        "errors": [{"code": "stale_revision", "message": "revision moved to 9"}],
    }
    text = run("edit drop m-0001", service=service)
    assert "stale_revision" in text
    assert "commit" not in service.names()


def test_edit_commit_failure_is_reported():
    service = FakeService(revision=3)
    service.commit_result = {"ok": False, "errors": [{"code": "stale_revision", "message": "moved"}]}
    text = run("edit drop m-0001", service=service)
    assert "commit failed" in text.lower() or "stale_revision" in text


def test_edit_reports_the_new_revision():
    service = FakeService(revision=4)
    text = run("edit keep m-0001", service=service)
    assert "revision 5" in text


# --------------------------------------------------------------------------- #
# Unpin
# --------------------------------------------------------------------------- #


def test_unpin_is_operator_only_and_reports_the_new_revision():
    service = FakeService(revision=8)
    text = run("unpin m-0001", service=service)
    assert "unpinned m-0001" in text and "revision 9" in text
    span_ids = service.args_for("unpin")[0]
    assert span_ids == ["m-0001"]
    kwargs = service.kwargs_for("unpin")
    assert kwargs["actor_origin"] == OPERATOR_ORIGIN
    assert kwargs["expected_revision"] == 8


def test_unpin_requires_span_ids():
    text = run("unpin", service=FakeService())
    assert "usage" in text.lower()


def test_unpin_reports_a_refusal():
    service = FakeService()

    def refuse(*args, **kwargs):
        return {"ok": False, "errors": [{"code": "not_pinned", "message": "span is not pinned"}]}

    service.unpin = refuse  # type: ignore[assignment]
    text = run("unpin m-0001", service=service)
    assert "not_pinned" in text


def test_unpin_without_store_support_is_explained():
    service = FakeService()
    service.unpin = None  # type: ignore[assignment]
    text = run("unpin m-0001", service=service)
    assert "does not expose operator unpin" in text


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #


def test_command_never_raises_when_the_service_explodes():
    service = FakeService()
    service.raise_on.add("status")
    text = run("status", service=service)
    assert isinstance(text, str)
    assert "RuntimeError" in text


def test_command_never_raises_when_the_resolver_explodes():
    def resolver(session_id):
        raise RuntimeError("no engine bound")

    text = handle("status", resolve_service=resolver)
    assert "no CLM store" in text
