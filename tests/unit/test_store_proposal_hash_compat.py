"""Pinned compatibility: store proposal hashes match the auxiliary helper.

The auxiliary compressor computes the hash *before* calling the store and
compares it to ``Proposal.proposal_sha256``; a mismatch aborts the proposal as
corruption.  The store therefore hashes exactly

    sha256(canonical JSON of {"base_revision", "operations"[raw], "origin"})

with sorted keys and compact separators, over the caller's raw operation
payloads (no store-normalized authority/provenance fields).
"""

from __future__ import annotations

import hashlib
import json
import unittest

import store_testkit as kit

from hermes_clm.auxiliary import AuxCompressor, canonical_proposal_sha256

_FIXED_OPERATIONS = [
    {"kind": "rewrite", "span_ids": ["sp_example"], "replacement": "short summary"}
]
_FIXED_BASE = 7
#: Golden value for the fixed payload above.  Changing either implementation
#: must keep agreeing with this digest (sorted compact canonical JSON).
_FIXED_DIGEST = "d98d94f406ed95b0fda717435badc1a6a9af69c0b1442d0aab3c7ab6605bb23f"


class AuxiliaryHashPinTests(unittest.TestCase):
    def test_fixed_payload_digest_is_pinned(self) -> None:
        self.assertEqual(
            canonical_proposal_sha256(_FIXED_OPERATIONS, base_revision=_FIXED_BASE),
            _FIXED_DIGEST,
        )

    def test_canonical_body_uses_base_revision_key(self) -> None:
        body = {
            "base_revision": _FIXED_BASE,
            "operations": _FIXED_OPERATIONS,
            "origin": "aux",
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(hashlib.sha256(encoded.encode("utf-8")).hexdigest(), _FIXED_DIGEST)
        legacy = {
            "base": _FIXED_BASE,
            "operations": _FIXED_OPERATIONS,
            "origin": "aux",
        }
        legacy_encoded = json.dumps(
            legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        self.assertNotEqual(
            hashlib.sha256(legacy_encoded.encode("utf-8")).hexdigest(), _FIXED_DIGEST
        )


class StoreProposalHashCompatibilityTests(kit.StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.ids = self.ingest(self.store)

    def _raw_operations(self, span_ids):
        # Shape produced by AuxCompressor.propose: no actor_origin, no provenance.
        return [
            {"kind": "rewrite", "span_ids": [span_id], "replacement": f"summary of {span_id}"}
            for span_id in span_ids
        ]

    def test_store_hash_equals_auxiliary_hash_for_raw_payloads(self) -> None:
        operations = self._raw_operations([self.ids["u-a2"]])
        proposal = self.store.create_proposal(operations, base_revision=1)
        self.assertEqual(
            proposal.proposal_sha256,
            canonical_proposal_sha256(operations, base_revision=1),
        )
        self.assertEqual(
            proposal.proposal_id,
            "prop_" + canonical_proposal_sha256(operations, base_revision=1)[:24],
        )

    def test_store_hash_matches_for_multiple_operations_and_base(self) -> None:
        operations = self._raw_operations([self.ids["u-a1"], self.ids["u-a2"]])
        proposal = self.store.create_proposal(operations, base_revision=1)
        self.assertEqual(
            proposal.proposal_sha256,
            canonical_proposal_sha256(operations, base_revision=1),
        )

    def test_missing_actor_defaults_to_aux_and_preserves_hash(self) -> None:
        # Aux does not send actor_origin; the store must accept that raw shape.
        operations = self._raw_operations([self.ids["u-a2"]])
        self.assertNotIn("actor_origin", operations[0])
        proposal = self.store.create_proposal(operations, base_revision=1)
        self.assertEqual(
            proposal.proposal_sha256, canonical_proposal_sha256(operations, base_revision=1)
        )
        self.assertEqual(proposal.operations[0].actor_origin, kit.ActorOrigin.AUX)

    def test_explicit_non_aux_actor_is_refused(self) -> None:
        operations = [
            {
                "kind": "rewrite",
                "span_ids": [self.ids["u-a2"]],
                "replacement": "x",
                "actor_origin": "agent",
            }
        ]
        with self.assertRaises(Exception) as ctx:
            self.store.create_proposal(operations, base_revision=1)
        self.assertEqual(getattr(ctx.exception, "code", None), "actor_mismatch")

    def test_accepted_proposal_keeps_the_auxiliary_hash(self) -> None:
        operations = self._raw_operations([self.ids["u-a2"]])
        expected = canonical_proposal_sha256(operations, base_revision=1)
        proposal = self.store.create_proposal(operations, base_revision=1)
        staged = self.store.accept_proposal(
            proposal_id=proposal.proposal_id,
            proposal_sha256=expected,
            base_revision=1,
            txn_id="txn-accept",
        )
        self.assertTrue(staged.ok)
        committed = self.store.commit(txn_id="txn-accept", expected_revision=1)
        self.assertEqual(committed.revision, 2)
        self.assertEqual(
            self.store.snapshot().span(self.ids["u-a2"]).rewrite_text, operations[0]["replacement"]
        )

    def test_auxiliary_compressor_end_to_end_hash_agrees_with_store(self) -> None:
        span_id = self.ids["u-a2"]
        replacement = "shorter summary"

        def fake_call(**_kwargs):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"rewrites": [{"span_id": span_id, "text": replacement}]}
                            )
                        }
                    }
                ]
            }

        class _Service:
            """Minimal aux service seam; proposals are handed over as mappings."""

            def __init__(self, store) -> None:
                self.store = store

            def status(self):
                return self.store.status()

            def retrieve(self, span_id):
                return self.store.retrieve(span_id)

            def create_proposal(self, operations, *, base_revision):
                return self.store.create_proposal(operations, base_revision=base_revision).to_dict()

        compressor = AuxCompressor(call=fake_call)
        result = compressor.propose(_Service(self.store), span_ids=[span_id])
        self.assertTrue(result["ok"], result)
        proposal = result["proposal"]
        self.assertEqual(
            proposal["proposal_sha256"],
            canonical_proposal_sha256(
                [{"kind": "rewrite", "span_ids": [span_id], "replacement": replacement}],
                base_revision=1,
            ),
        )
        self.store.accept_proposal(
            proposal_id=proposal["proposal_id"],
            proposal_sha256=proposal["proposal_sha256"],
            base_revision=1,
            txn_id="txn-aux-e2e",
        )
        self.store.commit(txn_id="txn-aux-e2e", expected_revision=1)
        self.assertEqual(self.store.snapshot().span(span_id).rewrite_text, replacement)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
