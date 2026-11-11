"""Aux proposals: immutable, hash+base pinned, never auto-committed."""

from __future__ import annotations

import unittest

import store_testkit as kit

from hermes_clm.errors import (
    NotFoundError,
    StaleProposalError,
    StoreBudgetError,
    ValidationError,
)
from hermes_clm.models import ActorOrigin, EditKind, EditOperation
from hermes_clm.store import StoreConfig


class ProposalTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def _proposal(self, span_ids=None, base=1):
        ops = [
            EditOperation(
                kind=EditKind.REWRITE,
                span_ids=(span_id,),
                replacement=f"summary of {span_id}",
                actor_origin=ActorOrigin.AUX,
                provenance={"origin": "aux", "spans": 1},
            )
            for span_id in (span_ids or [self.ids["u-a2"]])
        ]
        return self.store.create_proposal(ops, base_revision=base)

    def test_proposal_is_immutable_and_does_not_change_head(self) -> None:
        proposal = self._proposal()
        self.assertEqual(proposal.origin, "aux")
        self.assertEqual(proposal.base_revision, 1)
        self.assertEqual(len(proposal.proposal_sha256), 64)
        self.assertTrue(proposal.proposal_id.startswith("prop_"))
        self.assertEqual(self.store.snapshot().revision, 1)
        self.assertEqual(dict(self.store.snapshot().counts)["active"], 6)

    def test_proposal_id_is_deterministic(self) -> None:
        first = self._proposal()
        second = self._proposal()
        self.assertEqual(first.proposal_id, second.proposal_id)
        self.assertEqual(first.proposal_sha256, second.proposal_sha256)

    def test_accept_cannot_commit_by_itself(self) -> None:
        proposal = self._proposal()
        staged = self.store.accept_proposal(
            proposal_id=proposal.proposal_id,
            proposal_sha256=proposal.proposal_sha256,
            base_revision=proposal.base_revision,
            txn_id="txn-accept",
        )
        self.assertTrue(staged.ok)
        self.assertEqual(self.store.snapshot().revision, 1, "accept only stages")
        self.assertIsNone(self.store.snapshot().span(self.ids["u-a2"]).rewrite_text)
        committed = self.store.commit(txn_id="txn-accept", expected_revision=1)
        self.assertEqual(committed.revision, 2)
        self.assertEqual(
            self.store.snapshot().span(self.ids["u-a2"]).rewrite_text, f"summary of {self.ids['u-a2']}"
        )

    def test_accept_records_aux_provenance_and_agent_authority(self) -> None:
        proposal = self._proposal()
        staged = self.store.accept_proposal(
            proposal_id=proposal.proposal_id,
            proposal_sha256=proposal.proposal_sha256,
            base_revision=proposal.base_revision,
            txn_id="txn-accept",
        )
        payload = staged.staged_operations[-1]
        self.assertIs(payload.actor_origin, ActorOrigin.AGENT)
        self.assertEqual(payload.provenance["origin"], "aux")
        self.assertEqual(payload.provenance["proposal_id"], proposal.proposal_id)

    def test_hash_mismatch_is_refused(self) -> None:
        proposal = self._proposal()
        self.assertExceptionCode(
            "proposal_hash_mismatch",
            StaleProposalError,
            self.store.accept_proposal,
            proposal_id=proposal.proposal_id,
            proposal_sha256="0" * 64,
            base_revision=1,
            txn_id="txn-accept",
        )

    def test_base_mismatch_is_refused(self) -> None:
        proposal = self._proposal()
        self.assertExceptionCode(
            "proposal_base_mismatch",
            StaleProposalError,
            self.store.accept_proposal,
            proposal_id=proposal.proposal_id,
            proposal_sha256=proposal.proposal_sha256,
            base_revision=2,
            txn_id="txn-accept",
        )

    def test_stale_proposal_is_refused_after_head_moves(self) -> None:
        proposal = self._proposal()
        self.commit(self.store, [self.drop([self.ids["u-a1"], self.ids["u-t1"]])], "txn-new")
        self.assertExceptionCode(
            "stale_proposal",
            StaleProposalError,
            self.store.accept_proposal,
            proposal_id=proposal.proposal_id,
            proposal_sha256=proposal.proposal_sha256,
            base_revision=1,
            txn_id="txn-accept",
        )

    def test_unknown_proposal_is_refused(self) -> None:
        self.assertExceptionCode(
            "unknown_proposal",
            NotFoundError,
            self.store.accept_proposal,
            proposal_id="prop_nope",
            proposal_sha256="0" * 64,
            base_revision=1,
            txn_id="txn-accept",
        )

    def test_proposal_cannot_target_protected_spans(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self._proposal([self.ids["u-q1"]])
        self.assertEqual(getattr(ctx.exception, "code", None), "protected_span")

    def test_proposal_span_budget(self) -> None:
        store = self.open_store("aux-budget", config=StoreConfig(max_aux_spans=1))
        ids = self.ingest(store)
        ops = [
            EditOperation(kind=EditKind.PIN, span_ids=(ids[uid],), actor_origin=ActorOrigin.AUX)
            for uid in ("u-a1", "u-a2")
        ]
        with self.assertRaises(StoreBudgetError):
            store.create_proposal(ops, base_revision=1)

    def test_proposal_requires_aux_actor(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_proposal(
                [EditOperation(kind=EditKind.PIN, span_ids=(self.ids["u-a2"],), actor_origin="agent")],
                base_revision=1,
            )

    def test_proposal_rejects_future_base(self) -> None:
        with self.assertRaises(StaleProposalError):
            self._proposal(base=99)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
