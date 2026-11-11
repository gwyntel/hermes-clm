# Security and adversarial testing (`test/security`, `tests/fuzz`)

Owner: security/concurrency delegate (plan D). Scope of this branch: `tests/security/**`,
`tests/fuzz/**`, this document. No production files.

Sources of truth, in order: `docs/integration-contract.md`, `docs/architecture.md`,
`docs/adr/0001-request-projection.md`, then the design review
(`security-design-review.md`, invariants I1–I46, acceptance tests T1–T49).

## 1. Status

> **Integrated and green (local).** On the integration tree the full suite reports
> **811 passed, 3 skipped, 0 failed, 0 xpassed** with `CLM_HERMES_CHECKOUT` pointing at a
> real Hermes checkout (this count includes the system-prompt-section tests); without it
> the same tree is 782 passed / 32 skipped. The three
> remaining skips are expected: two media probes do not apply to inline archives, and one
> tool-schema probe has no module-level JSON schema object. `tests/security` and
> `tests/fuzz` all pass, including the 16-test mutation harness: a correct fixture baseline
> is green and every injected defect turns its designated probe red.
>
> What is proven locally: all 49 acceptance tests execute against production code; I1–I46
> are exercised where the plugin can enforce them; the assertions bite (mutation harness);
> and the seeded fuzz properties hold. What is **not** proven: remote CI, a clean-clone
> install, or a live Hermes multi-tool-call run. Those are release gates, not claims.

The design predates the implementation, so the original table below is retained as
history only. It is superseded by the current result above.

`src/hermes_clm` did not exist when this suite was written and had not landed on
`feat/store`, `feat/engine`, `feat/tools` or `feat/delivery`. Every test that
needs the implementation is therefore **written against the contract and not executed
against production code**. That is a skip, not a pass:

| Result | Count | What it is |
|---|---|---|
| passed (no implementation present) | 19 | 4 traceability gates + 15 harness/defect tests |
| skipped, one reason, module level | 18 | 14 security modules + 4 fuzz modules waiting for `src/hermes_clm` |
| failed | 0 | — |
| **executed against production code** | **0 of 49** | no implementation to execute against |

What *is* proven today, and is not a claim about production:

1. The suite covers all 49 acceptance tests (static gate, `test_00_traceability.py`).
2. The assertions bite: a correct fixture implementation of the contracted API passes the
   twelve designated probe tests, and each of twelve single-defect mutations of that
   fixture turns its probe red (§5). That is the anti-"fake green" evidence.
3. Fuzz properties hold against the fixture, so the properties are satisfiable and the
   generators are not vacuously empty.

What is *not* proven: that any real implementation satisfies I1–I46. The moment
`src/hermes_clm` lands on an integration branch, `pytest tests/security tests/fuzz` runs
the same file set with no edits; any red is a finding for the owning delegate, not a test
bug to be papered over.

Run it:

```bash
python -m pytest tests/security tests/fuzz -q          # honest report, skips included
python -m pytest tests/ -q -rs                          # include skip reasons
python -m pytest tests/security/mutation -q             # defect-injection harness
CLM_FUZZ_ITERATIONS=2000 python -m pytest tests/fuzz -q # longer deterministic sweep
```

Requires Python ≥ 3.11 and pytest. No network, no model calls, no writes outside `tmp_path`
(every fixture is a throwaway `HERMES_HOME`).

## 2. Layout

```text
tests/conftest.py                     shared fixtures: throwaway HERMES_HOME, store, base conversation
tests/security/
  _support/contract.py                the ONLY place that names a hermes_clm symbol (§3)
  _support/adversarial.py             hostile corpora: session ids, roles, spoof bodies, secrets
  _support/assertions.py              expect_refusal, leakage scans, subprocess runner, disk signatures
  test_00_traceability.py             static gate: T1–T49 coverage, no orphans, doc drift
  test_paths.py                       T1 T2 T3 T5 T6
  test_media.py                       T4 T12
  test_roles.py                       T7 T8 T9 T10 T11
  test_injection.py                   T13 T14 T15
  test_pins_authority.py              T16 T17 T18 T19
  test_revisions.py                   T20 T21 T25 T26 T27
  test_recovery.py                    T22 T23 T24 T28 T29 T30 T31 T32 T33
  test_concurrency.py                 T34 T35 T36 T38
  test_projection_purity.py           T39 T40
  test_permissions.py                 T41 T42 T43
  test_proposals.py                   T44 T45 T46 T47
  test_redaction.py                   T48 T49
  test_agent_tool_authority.py        T6 T16 at the tool boundary
  integration/test_engine_security.py T15 T33 T37 (needs feat/engine)
  mutation/                           defect-injection harness (§5)
tests/fuzz/                           bounded, seeded property fuzz (§6)
```

## 3. Contract adapter

`tests/security/_support/contract.py` is the single adaptation layer. It imports only the
names in the integration contract and binds calls by inspecting the callee signature, so an
implementation delegate may use keyword names like `operations`/`ops`,
`request_messages`/`messages`, or `project`/`project_messages` without breaking the suite.
If a delegate chooses a *different* documented entry point, that file is the only edit —
and the traceability gate will still pass, so the change must be reviewed, not assumed.

Non-negotiables the adapter encodes:

- `ContextStore.for_session(hermes_home, session_id, read_only=False)` is the only way a
  store is opened; no test constructs paths itself.
- `store.stage(operations, expected_revision=…, txn_id=…, actor_origin=…)`,
  `commit(txn_id=…, expected_revision=…)`, `create_proposal`/`accept_proposal`,
  `retrieve/inspect/search/status/snapshot` are the only mutating/reading surfaces used.
- `hermes_clm.projection` must expose one of `project`, `project_messages`,
  `project_request`, `project_view`, `apply_projection`. A declined projection is
  `None` (or a result whose `messages` is `None`).
- Agent **pin, commit and revert are permitted** (plan D; ADR decision 5). The suite tests
  that pinning cannot elevate origin/authority, that no unpin verb is reachable from an
  agent tool, and that a commit intersecting a pin is refused whole. It does *not* assert
  that pinning is impossible — that prohibition in the review conflicts with the plan and
  is deliberately not implemented here.

`docs/integration-contract.md` does not currently name the projection entry point, the
database filename, the archive's media layout, or a retention/purge call. The suite
tolerates the first two (globbing, candidate names) and reports the last two as blocked
requirements in §7.

## 4. Coverage map (T1–T49)

Every row is enforced by `test_00_traceability.py`: the id must appear here *and* on at
least one `@pytest.mark.t("Tn")` decorator. "Gate" says what the test needs before it can
execute: `store` = `feat/store`, `engine` = `feat/engine`, `tools` = `feat/tools`.

| # | Invariant | Test | Gate |
|---|---|---|---|
| T1 | I1, I2 | `test_paths.py::test_bad_session_id_refused_before_any_filesystem_use`, `::test_host_shaped_session_ids_accepted_and_confined`, `fuzz/test_fuzz_session_ids.py` | store |
| T2 | I2 | `test_paths.py::test_session_directory_that_is_a_symlink_is_refused` | store |
| T3 | I3 | `test_paths.py::test_database_swapped_for_symlink_to_other_session_is_refused`, `::test_mount_identity_mismatch_refuses_to_open` | store |
| T4 | I5 | `test_media.py::test_media_blob_replaced_by_symlink_to_secret_is_not_read` | store |
| T5 | I4 | `test_paths.py::test_active_json_cannot_relocate_the_store_root`, `::test_stored_content_cannot_relocate_the_store_root` | store |
| T6 | I1, I40 | `test_paths.py::test_cross_session_retrieve_is_refused`, `::test_read_entry_points_have_no_session_or_path_selector`, `test_agent_tool_authority.py::test_no_tool_accepts_a_session_or_path_selector` | store, tools |
| T7 | I12, I13 | `test_roles.py::test_spoofed_role_header_in_text_stays_opaque`, `fuzz/test_fuzz_projection.py::test_random_conversations_never_gain_authority` | store |
| T8 | I14 | `test_roles.py::test_rogue_system_row_outside_pinned_prefix_is_refused` | store |
| T9 | F1, I14 | `test_roles.py::test_unknown_role_is_never_folded_to_user` | store |
| T10 | I13 | `test_roles.py::test_tool_calls_json_in_text_is_never_emitted_as_tool_calls` | store |
| T11 | I15 | `test_roles.py::test_incomplete_tool_group_is_dropped_whole`, `::test_complete_tool_group_survives` | store |
| T12 | I5, I16 | `test_media.py::test_image_part_hash_mismatch_is_refused_not_substituted`, `::test_aux_proposal_cannot_produce_a_multimodal_part` | store |
| T13 | I17, I19 | `test_injection.py::test_model_injection_persists_as_labelled_data_with_no_policy_effect`, `::test_injection_cannot_disable_redaction_for_later_ingest` | store |
| T14 | I18, I29 | `test_injection.py::test_accepted_aux_text_renders_untrusted_and_after_operator_messages` | store |
| T15 | I19 | `test_injection.py::test_store_text_claiming_policy_change_is_inert`, `integration/test_engine_security.py::test_policy_text_in_history_never_changes_engine_mode` | store, engine |
| T16 | I21 | `test_pins_authority.py::test_agent_pin_is_allowed_but_confers_no_authority`, `::test_agent_commit_and_revert_are_permitted`, `::test_unpin_is_not_an_operation_kind`, `::test_no_agent_tool_can_unpin_or_move_a_pin`, `test_agent_tool_authority.py::test_closed_operation_kinds_are_pin_inclusive_and_unpin_free` | store, tools |
| T17 | I23 | `test_pins_authority.py::test_commit_that_would_drop_a_pinned_span_is_refused_whole` | store |
| T18 | I22 | `test_pins_authority.py::test_pin_ids_are_content_addressed_and_not_reused` | store |
| T19 | I9, I23 | `test_pins_authority.py::test_revert_across_a_pin_is_refused_and_archive_keeps_both_sides` | store |
| T20 | I7 | `test_revisions.py::test_two_staged_txns_from_the_same_revision_exactly_one_wins` | store |
| T21 | I8 | `test_revisions.py::test_replayed_transaction_returns_original_outcome_without_a_second_write` | store |
| T22 | I8 | `test_recovery.py::test_txn_staged_by_a_crashed_process_is_never_applied` | store |
| T23 | I30 | `test_recovery.py::test_stale_active_json_is_discarded_not_replayed` | store |
| T24 | I30 | `test_recovery.py::test_foreign_active_json_is_discarded` | store |
| T25 | I10 | `test_revisions.py::test_direct_sql_cannot_update_or_delete_pinned_rows` | store |
| T26 | I11, I32 | `test_revisions.py::test_single_byte_payload_flip_tombstones_only_that_span` | store |
| T27 | §5 limit | `test_revisions.py::test_consistent_chain_rewrite_is_undetected_without_an_anchor` | store |
| T28 | I34 | `test_recovery.py::test_sigkill_during_commit_leaves_a_consistent_head` | store |
| T29 | I30, I35 | `test_recovery.py::test_interrupted_index_write_is_rebuilt_from_the_db` | store |
| T30 | I30 | `test_recovery.py::test_garbage_active_json_is_rebuilt`, `fuzz/test_fuzz_active_json.py` | store |
| T31 | I31 | `test_recovery.py::test_failed_quick_check_is_reported_and_never_repaired` | store |
| T32 | I32 | `test_recovery.py::test_one_unreadable_payload_tombstones_and_the_rest_projects` | store |
| T33 | I33, F3 | `test_recovery.py::test_unreadable_structure_yields_no_projection`, `integration/test_engine_security.py::test_structural_corruption_degrades_to_none_not_a_guess` | store, engine |
| T34 | I39 | `test_concurrency.py::test_second_opener_is_read_only_and_its_commits_are_rejected` | store |
| T35 | I40 | `test_concurrency.py::test_sibling_and_parent_sessions_are_unreachable` | store |
| T36 | I38 | `test_concurrency.py::test_long_write_transaction_elsewhere_yields_a_bounded_refusal_not_corruption` | store |
| T37 | I37 | `integration/test_engine_security.py::test_late_or_repeated_commit_lands_at_most_once` | engine |
| T38 | I23 | `test_concurrency.py::test_pin_created_between_stage_and_commit_blocks_the_commit` | store |
| T39 | I42 | `test_projection_purity.py::test_projection_does_not_touch_the_store`, `::test_projection_returns_a_deep_copy_not_internal_state` | store |
| T40 | I42 | `test_projection_purity.py::test_host_mutation_of_projection_cannot_reach_the_store` | store |
| T41 | I43 | `test_permissions.py::test_fresh_store_ignores_a_permissive_umask` | store |
| T42 | I43 | `test_permissions.py::test_loose_existing_modes_are_tightened_or_refused` | store |
| T43 | I43 | `test_permissions.py::test_hardlinked_database_is_refused` | store |
| T44 | I26 | `test_proposals.py::test_proposal_byte_change_after_display_is_rejected` | store |
| T45 | I26, I27 | `test_proposals.py::test_proposal_with_stale_base_revision_is_rejected` | store |
| T46 | I27 | `test_proposals.py::test_proposal_cannot_reference_foreign_or_pinned_spans` | store |
| T47 | I28 | `test_proposals.py::test_unknown_proposal_fields_and_op_kinds_are_rejected`, `fuzz/test_fuzz_ops.py` | store |
| T48 | I46 | `test_redaction.py::test_secret_in_tool_output_is_redacted_at_write`, `::test_redaction_is_origin_independent` | store |
| T49 | I46 | `test_redaction.py::test_retention_expires_unpinned_sessions_and_keeps_pinned` | store |

## 5. Known-bad mutation harness (`tests/security/mutation`)

There is no production code on this branch, so "does the suite catch defects?" cannot be
answered by mutating production source. Instead the harness ships a *correct* fixture
implementation of exactly the contract subset the probes use
(`mutation/fixture_impl/hermes_clm`, never installed, never importable in a normal run) and
a switch that injects one defect at a time (`CLM_FIXTURE_DEFECT`).

For each probe the harness runs the probe test in a subprocess twice:

- `CLM_FIXTURE_DEFECT=none` → the real security test must **pass**;
- `CLM_FIXTURE_DEFECT=Mx` → the same test must **fail**, and the failure summary must name
  that exact node id (a collection error is not accepted as evidence).

Both directions run for all twelve probes, so a probe that is unsatisfiable, or a defect
that changes nothing, fails the harness itself.

| Defect | Invariant | Probe |
|---|---|---|
| M1 session-id validation removed | I1, I2 | T1 traversal refusal |
| M3 mount identity check removed | I3 | T3 copied-database refusal |
| M7 role inferred from text | I12, I13 | T7 spoofed `role=system` body |
| M9 unknown role folded to `user` | F1, I14 | T9 unknown-role quarantine |
| M10 `tool_calls` synthesised from text | I13 | T10 text tool-call injection |
| M17 pin not enforced at commit | I23 | T17 pinned-span drop |
| M20 last-writer-wins instead of CAS | I7 | T20 two commits from one revision |
| M21 idempotency table ignored | I8 | T21 replayed `txn_id` |
| M25 no SQL triggers | I10 | T25 direct `UPDATE`/`DELETE` |
| M39 snapshot writes to the store | I42 | T39 projection purity |
| M41 umask modes instead of 0700/0600 | I43 | T41 fresh-store permissions |
| M48 no redaction at write | I46 | T48 secret in tool output |

Two extra guards: `test_fixture_package_is_not_on_the_normal_import_path` (a green run can
never be produced by the fixture shadowing the real package) and
`test_every_defect_has_a_code_path_in_the_fixture` (a catalogued defect that disables
nothing would make its red run meaningless).

What the harness does **not** prove: anything about `hermes_clm` itself. It proves the
tests discriminate. The mutation of *production* source becomes possible once `src/`
exists; the catalog and probe node ids carry over unchanged.

The fixture implements only the subset the twelve probes touch, so running the *whole*
suite against it is expected to show failures outside that subset (recovery tombstones,
proposal hashes, abandoned transactions). That is a fixture limitation, not a suite
finding; only `tests/security/mutation/test_harness.py` decides pass/fail of the harness.

The harness was itself meta-checked while being built: replacing one probe body (T20) with
a vacuous `pass` makes `test_injected_defect_turns_the_probe_red[M20]` fail with "left
… GREEN: the suite does not detect this class of defect". A neutered assertion therefore
cannot masquerade as proof of coverage.

## 6. Fuzz

Seeded (`random.Random(<constant>)`), bounded (`CLM_FUZZ_ITERATIONS`), deterministic; no
Hypothesis dependency so the suite stays dependency-light and offline.

| File | Property |
|---|---|
| `test_fuzz_session_ids.py` | no random string, accepted or refused, resolves a session directory outside `clm/sessions/`; an accepted id is never silently sanitised |
| `test_fuzz_ops.py` | unknown operation kinds are never applied or coerced; revision moves by at most one per commit; database stays `quick_check`-clean |
| `test_fuzz_projection.py` | projection returns a list or declines, never invents a role, never rewrites a system/user row, never adds `tool_calls` |
| `test_fuzz_active_json.py` | arbitrary derived-index bytes never override the DB head or change the projection |

## 7. Integration gaps resolved

The contract gaps found before integration are closed:

1. **Retention/purge.** `ContextStore.purge_expired_sessions(home, keep=..., current_session_id=...)`
   retains pinned/current stores by default, skips unreadable stores, bounds the sweep and
   supports `dry_run`. T49 executes instead of skipping.
2. **Projection entry point.** `hermes_clm.projection.project` is pinned and exercised.
3. **Integrity signal.** `snapshot(verify_hashes=True)` is the default. A corrupt payload
   becomes an explicit unreadable tombstone for that span; `status()` reports the problem;
   other spans still project.
4. **Store layout.** `HERMES_HOME/clm/sessions/<slug>-<digest>/store.sqlite3` is documented
   in `docs/store-format.md` and asserted by T2/T5/T41.
5. **Media storage.** Media is archived inline with the message payload. Blob-swap probes
   skip explicitly because no separate blob path exists; this is recorded as a limitation,
   not a pass.
6. **Second-writer semantics.** A second read-write opener is refused with
   `StoreBusyError`; read-only observation is explicit.
7. **Operation shape.** `EditOperation` carries `kind`, `span_ids`, replacement and actor;
   `expected_revision` and `txn_id` belong to `stage()`/`commit()`.

Documented limitations, asserted as limitations rather than defences:

- **T27**: an unanchored hash chain cannot detect a consistent rewrite by same-UID code
  (review §5, L10). The test asserts the limitation and tells the maintainer to update this
  document if an anchor is added. It never claims tamper-proofing.
- **T4/T12**: if media is stored inline rather than as a blob, the blob-swap attack surface
  does not exist in that form; the tests skip with an explicit reason instead of passing.
- Nothing here defends against arbitrary same-UID shell access (review L1–L3); the suite
  tests authority, permissions, redaction, CAS, idempotency and corruption detection, which
  is what the plugin can actually enforce.
