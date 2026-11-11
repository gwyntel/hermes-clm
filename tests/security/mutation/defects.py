"""Catalogue of injected defects: each maps one invariant to one probe test.

A defect is only a defect if (a) the fixture implementation contains the code path it
disables, and (b) the probe test passes without it and fails with it. Both are asserted
in test_harness.py, so a stale entry cannot masquerade as proof.
"""

from __future__ import annotations

PROBES = [
    {
        "id": "M1",
        "invariant": "I1/I2",
        "t": "T1",
        "nodeid": (
            "tests/security/test_paths.py::"
            "test_hostile_session_id_never_escapes_and_is_refused_or_hashed"
        ),
        "defect": "session-id validation removed: a host-supplied id is joined onto the "
        "store path and traverses out of the sessions root",
    },
    {
        "id": "M3",
        "invariant": "I3",
        "t": "T3",
        "nodeid": "tests/security/test_paths.py::test_mount_identity_mismatch_refuses_to_open",
        "defect": "mount identity check removed: a database copied from another session "
        "opens as this session",
    },
    {
        "id": "M7",
        "invariant": "I12/I13",
        "t": "T7",
        "nodeid": "tests/security/test_roles.py::test_spoofed_role_header_in_text_stays_opaque",
        "defect": "role inferred from message text: a body containing `role=system` is "
        "promoted to the host's system slot",
    },
    {
        "id": "M9",
        "invariant": "F1/I14",
        "t": "T9",
        "nodeid": "tests/security/test_roles.py::test_unknown_role_is_never_folded_to_user",
        "defect": "unknown role folded to `user` (upstream parse_back behaviour)",
    },
    {
        "id": "M10",
        "invariant": "I13",
        "t": "T10",
        "nodeid": "tests/security/test_roles.py::test_tool_calls_json_in_text_is_never_emitted_as_tool_calls",
        "defect": "a JSON tool_calls array inside text is emitted as a real tool_calls field",
    },
    {
        "id": "M17",
        "invariant": "I23",
        "t": "T17",
        "nodeid": "tests/security/test_pins_authority.py::test_commit_that_would_drop_a_pinned_span_is_refused_whole",
        "defect": "pin not enforced: a commit whose effect set intersects a pinned span is "
        "applied (partially)",
    },
    {
        "id": "M20",
        "invariant": "I7",
        "t": "T20",
        "nodeid": "tests/security/test_revisions.py::test_two_staged_txns_from_the_same_revision_exactly_one_wins",
        "defect": "compare-and-swap replaced by last-writer-wins: two commits from the same "
        "expected revision both apply",
    },
    {
        "id": "M21",
        "invariant": "I8",
        "t": "T21",
        "nodeid": "tests/security/test_revisions.py::test_replayed_transaction_returns_original_outcome_without_a_second_write",
        "defect": "idempotency table not consulted: a replayed txn id reports failure (or "
        "re-applies) instead of returning the original outcome",
    },
    {
        "id": "M25",
        "invariant": "I10",
        "t": "T25",
        "nodeid": "tests/security/test_revisions.py::test_direct_sql_cannot_update_or_delete_pinned_rows",
        "defect": "immutability enforced only in Python: no SQL triggers, so direct SQL "
        "rewrites a pinned row",
    },
    {
        "id": "M39",
        "invariant": "I42",
        "t": "T39",
        "nodeid": "tests/security/test_projection_purity.py::test_projection_does_not_touch_the_store",
        "defect": "snapshot()/projection writes to the store (impure read path)",
    },
    {
        "id": "M41",
        "invariant": "I43",
        "t": "T41",
        "nodeid": "tests/security/test_permissions.py::test_fresh_store_ignores_a_permissive_umask",
        "defect": "store files created with the process umask instead of 0700/0600",
    },
    {
        "id": "M48",
        "invariant": "I46",
        "t": "T48",
        "nodeid": "tests/security/test_redaction.py::test_secret_in_tool_output_is_redacted_at_write",
        "defect": "no redaction at write: a leaked token is archived verbatim",
    },
]

PROBE_IDS = [probe["id"] for probe in PROBES]
