"""Offline benchmark harness tests: determinism, isolation and report shape."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.runner import run_all, write_reports  # noqa: E402

EXPECTED_FIXTURES = {
    "long-coding",
    "interruption",
    "conflicting-updates",
    "huge-output",
    "injection",
    "recovery",
    "handoff",
}


def test_all_fixtures_and_strategies_run() -> None:
    report = run_all()
    assert {fixture["id"] for fixture in report["fixtures"]} == EXPECTED_FIXTURES
    assert {row["strategy"] for row in report["results"]} == {"A", "B", "C", "D"}
    assert len(report["results"]) == len(EXPECTED_FIXTURES) * 4
    assert report["evidence_class"] == "offline-simulation"
    assert report["schema_version"] == 1


def test_results_are_byte_deterministic() -> None:
    first = json.dumps(run_all(), sort_keys=True)
    second = json.dumps(run_all(), sort_keys=True)
    assert first == second


def test_no_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("benchmark attempted network access")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    report = run_all()
    assert report["results"]


def test_c_and_d_check_every_fixture() -> None:
    report = run_all()
    for strategy in ("C", "D"):
        rows = [row for row in report["results"] if row["strategy"] == strategy]
        assert all(row["facts_retained"] == row["facts_total"] for row in rows)
        assert all(row["checks_passed"] for row in rows)


def test_a_and_b_do_not_archive_offloaded_bytes() -> None:
    report = run_all()
    for strategy in ("A", "B"):
        rows = [
            row
            for row in report["results"]
            if row["strategy"] == strategy and row["fixture"] in {"huge-output", "recovery"}
        ]
        assert rows
        assert all(not row["retrieval_exact"] for row in rows)
        assert all(row["archive_entries"] == 0 for row in rows)


def test_recovery_checks_are_meaningful() -> None:
    report = run_all()
    rows = {(row["fixture"], row["strategy"]): row for row in report["results"]}
    assert rows[("recovery", "C")]["retrieval_exact"] is True
    assert rows[("recovery", "A")]["retrieval_exact"] is False
    assert rows[("interruption", "C")]["recovery_clean"] is True


def test_injection_is_never_elevated() -> None:
    report = run_all()
    rows = [row for row in report["results"] if row["fixture"] == "injection"]
    assert all(row["injection_contained"] for row in rows)
    assert all(row["system_preserved"] for row in rows)


def test_aux_strategy_is_a_local_mock() -> None:
    report = run_all()
    rows = [row for row in report["results"] if row["strategy"] == "D"]
    assert any(row["aux_calls"] > 0 for row in rows)
    assert all(row["aux_tokens"] >= 0 for row in rows)


def test_reports_are_written(tmp_path: Path) -> None:
    report = run_all(strategy_names=["A", "C"])
    json_path = tmp_path / "nested" / "report.json"
    markdown_path = tmp_path / "nested" / "report.md"
    write_reports(report, json_path=json_path, markdown_path=markdown_path)
    assert json.loads(json_path.read_text())["results"]
    body = markdown_path.read_text()
    assert "offline-simulation" in body
    assert "A — Default compressor" in body
