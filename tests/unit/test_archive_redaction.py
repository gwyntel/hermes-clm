"""Redaction rules and archive hash-chain integrity."""

from __future__ import annotations

import copy
import unittest

import store_testkit as kit

from hermes_clm.archive import (
    DEFAULT_RULES,
    GENESIS_HASH,
    RedactionRule,
    Redactor,
    canonical_json,
    chain_entry_hash,
    redact_message,
    verify_chain,
)
from hermes_clm.errors import ValidationError


class RedactionTests(unittest.TestCase):
    def assertRedacts(self, text: str, label: str) -> None:
        outcome = redact_message({"role": "user", "content": text})
        self.assertGreater(outcome.count, 0, f"nothing redacted for {label}: {text!r}")
        self.assertIn(f"[REDACTED:{label}]", outcome.message["content"])

    def test_known_secret_shapes(self) -> None:
        cases = {
            "openai_key": "key sk-abcdefghijklmnopqrstuvwxyz123456",
            "anthropic_key": "key sk-ant-api03-abcdefghijklmnop",
            "github_token": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "github_pat": "token github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
            "slack_token": "token xoxb-1234567890-abcdefghij",
            "aws_access_key_id": "id AKIAIOSFODNN7EXAMPLE",
            "google_api_key": "key AIzaSyA1234567890abcdefghijklmnopqrstuv",
            "jwt": "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        }
        for label, text in cases.items():
            self.assertRedacts(text, label)

    def test_private_key_block(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        self.assertRedacts(text, "private_key")

    def test_bearer_and_connection_strings(self) -> None:
        outcome = redact_message(
            {
                "role": "assistant",
                "content": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            }
        )
        self.assertIn("[REDACTED:bearer]", outcome.message["content"])
        outcome = redact_message(
            {"role": "assistant", "content": "postgres://user:supersecret@db.local:5432/app"}
        )
        self.assertIn("[REDACTED:conn_string]", outcome.message["content"])
        self.assertNotIn("supersecret", outcome.message["content"])

    def test_assigned_secret(self) -> None:
        outcome = redact_message({"role": "user", "content": 'api_key = "abcdef123456"'})
        self.assertIn("[REDACTED:assigned_secret]", outcome.message["content"])

    def test_nested_structures_are_scrubbed(self) -> None:
        message = {
            "role": "tool",
            "content": [{"type": "text", "text": "sk-abcdefghijklmnopqrstuvwxyz"}],
            "meta": {"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
        }
        outcome = redact_message(message)
        self.assertGreaterEqual(outcome.count, 2)
        self.assertNotIn("sk-abcdefghij", canonical_json(outcome.message))
        self.assertNotIn("ghp_abcdefghij", canonical_json(outcome.message))

    def test_input_is_not_mutated(self) -> None:
        message = {"role": "user", "content": "sk-abcdefghijklmnopqrstuvwx"}
        original = copy.deepcopy(message)
        redact_message(message)
        self.assertEqual(message, original)

    def test_clean_text_is_unchanged_and_cheap(self) -> None:
        message = {
            "role": "assistant",
            "content": "nothing secret here",
            "tool_calls": [{"id": "a"}],
        }
        outcome = redact_message(message)
        self.assertEqual(outcome.count, 0)
        self.assertEqual(outcome.message, message)
        self.assertIsNot(outcome.message, message)

    def test_opt_out_returns_deep_copy(self) -> None:
        message = {"role": "user", "content": "sk-abcdefghijklmnopqrstuvwx"}
        outcome = redact_message(message, enabled=False)
        self.assertEqual(outcome.count, 0)
        self.assertIn("sk-abcdefghij", outcome.message["content"])
        outcome.message["content"] = "changed"
        self.assertIn("sk-abcdefghij", message["content"])

    def test_extra_rules(self) -> None:
        rule = RedactionRule(
            name="internal_id",
            pattern=__import__("re").compile(r"INT-\d{6}"),
            replacement="[REDACTED:internal_id]",
        )
        outcome = redact_message({"role": "user", "content": "case INT-123456"}, extra_rules=[rule])
        self.assertIn("[REDACTED:internal_id]", outcome.message["content"])

    def test_redactor_is_reusable_and_deterministic(self) -> None:
        redactor = Redactor(enabled=True)
        message = {"role": "user", "content": "sk-abcdefghijklmnopqrstuvwx"}
        first = redactor(message)
        second = redactor(message)
        self.assertEqual(first.message, second.message)
        self.assertEqual(first.count, second.count)

    def test_non_mapping_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            redact_message(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_every_default_rule_has_a_label(self) -> None:
        for rule in DEFAULT_RULES:
            self.assertIn(f"[REDACTED:{rule.name}]", rule.replacement)


class HashChainTests(unittest.TestCase):
    def _rows(self, count: int = 3) -> list[dict]:
        rows = []
        prev = GENESIS_HASH
        for index in range(count):
            payload = {"role": "assistant", "content": f"message {index}"}
            payload_json = canonical_json(payload)
            entry = chain_entry_hash(
                prev_hash=prev,
                span_id=f"sp_{index}",
                content_sha256="a" * 64,
                payload_json=payload_json,
                revision=1,
            )
            rows.append(
                {
                    "archive_id": index + 1,
                    "span_id": f"sp_{index}",
                    "content_sha256": "a" * 64,
                    "payload_json": payload_json,
                    "prev_hash": prev,
                    "entry_hash": entry,
                    "revision": 1,
                }
            )
            prev = entry
        return rows

    def test_intact_chain_verifies(self) -> None:
        report = verify_chain(self._rows())
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["count"], 3)

    def test_payload_tamper_detected(self) -> None:
        rows = self._rows()
        rows[1]["payload_json"] = canonical_json({"role": "assistant", "content": "swapped"})
        report = verify_chain(rows)
        self.assertFalse(report["ok"])
        self.assertIn("entry_hash_mismatch", {problem["code"] for problem in report["problems"]})

    def test_broken_link_detected(self) -> None:
        rows = self._rows()
        del rows[1]
        report = verify_chain(rows)
        self.assertFalse(report["ok"])
        self.assertIn("broken_link", {problem["code"] for problem in report["problems"]})

    def test_truncated_chain_still_links(self) -> None:
        rows = self._rows()[:2]
        self.assertTrue(verify_chain(rows)["ok"])

    def test_empty_chain_is_valid(self) -> None:
        report = verify_chain([])
        self.assertTrue(report["ok"])
        self.assertEqual(report["head_hash"], GENESIS_HASH)

    def test_rehashed_forgery_is_detected_by_link(self) -> None:
        rows = self._rows()
        forged_payload = canonical_json({"role": "assistant", "content": "forged"})
        rows[1]["payload_json"] = forged_payload
        rows[1]["entry_hash"] = chain_entry_hash(
            prev_hash=rows[1]["prev_hash"],
            span_id=rows[1]["span_id"],
            content_sha256=rows[1]["content_sha256"],
            payload_json=forged_payload,
            revision=rows[1]["revision"],
        )
        report = verify_chain(rows)
        self.assertFalse(report["ok"], "re-hashing one link breaks the next link")
        self.assertIn("broken_link", {problem["code"] for problem in report["problems"]})


class StoreArchiveIntegrationTests(kit.StoreTestCase):
    def test_store_archive_chain_verifies_after_many_ingests(self) -> None:
        store = self.open_store()
        for batch in range(5):
            store.ingest(
                [
                    {
                        "message_uid": f"u-{batch}-{i}",
                        "role": "assistant",
                        "content": f"body {batch}-{i}",
                    }
                    for i in range(3)
                ]
            )
        raw = self.raw_connect()
        rows = [
            dict(row)
            for row in raw.execute(
                "SELECT archive_id, span_id, content_sha256, payload_json, prev_hash, entry_hash, revision "
                "FROM archive ORDER BY archive_id"
            )
        ]
        report = verify_chain(rows)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["count"], 15)
        self.assertEqual(store.verify()["ok"], True)

    def test_archive_payloads_match_span_hashes(self) -> None:
        store = self.open_store()
        self.ingest(store)
        raw = self.raw_connect()
        for row in raw.execute(
            "SELECT a.content_sha256 AS ac, s.content_sha256 AS sc FROM archive a JOIN spans s USING (span_id)"
        ):
            self.assertEqual(row["ac"], row["sc"])

    def test_archive_rows_are_never_rewritten_across_versions(self) -> None:
        store = self.open_store()
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v1"}])
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v2"}])
        raw = self.raw_connect()
        count = raw.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        store.ingest([{"message_uid": "u-1", "role": "assistant", "content": "v2"}])
        self.assertEqual(raw.execute("SELECT COUNT(*) FROM archive").fetchone()[0], count)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
