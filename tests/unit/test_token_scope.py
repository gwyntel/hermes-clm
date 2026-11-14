"""The store-wide span total is not the live request total."""

from __future__ import annotations

import store_testkit as kit


class TokenScopeTests(kit.StoreTestCase):
    def test_store_status_uses_the_all_spans_name(self) -> None:
        store = self.open_store()
        store.ingest(kit.sample_messages())
        status = store.status()
        assert "est_tokens" not in status
        assert status["all_spans_est_tokens"] > 0
        assert status["estimator"] == "chars/4"

    def test_store_total_is_not_the_live_request_total(self) -> None:
        store = self.open_store()
        messages = kit.sample_messages()
        store.ingest(messages)
        before = store.status()["all_spans_est_tokens"]
        store.ingest([messages[0]])
        after = store.status()
        assert after["counts"]["spans"] == len(messages)
        assert after["all_spans_est_tokens"] == before
