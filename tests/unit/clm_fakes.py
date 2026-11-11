"""In-memory fakes for the CLM tools/auxiliary/commands tests.

No SQLite, no Hermes, no network: the fakes implement exactly the scoped service
seam that tools and commands call, and record every call so tests can assert that
nothing was written when it should not have been.
"""

from __future__ import annotations

import json
from typing import Any


class FakeService:
    """Recording stand-in for a per-session store service."""

    def __init__(self, revision: int = 1) -> None:
        self.revision = revision
        self.spans: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.stage_result: dict[str, Any] | None = None
        self.commit_result: dict[str, Any] | None = None
        self.revert_result: dict[str, Any] | None = None
        self.status_result: dict[str, Any] | None = None
        self.proposal_result: dict[str, Any] | None = None
        self.proposals: list[dict[str, Any]] = []
        self.raise_on: set[str] = set()

    # -- helpers ----------------------------------------------------------- #

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.raise_on:
            raise RuntimeError(f"{name} exploded at /private/path/store.sqlite3")

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def kwargs_for(self, name: str) -> dict[str, Any]:
        for call_name, _, kwargs in self.calls:
            if call_name == name:
                return kwargs
        raise AssertionError(f"no call to {name}; calls={self.names()}")

    def args_for(self, name: str) -> tuple[Any, ...]:
        for call_name, args, _ in self.calls:
            if call_name == name:
                return args
        raise AssertionError(f"no call to {name}; calls={self.names()}")

    def add_span(self, span_id: str, content: str, *, protected: bool = False) -> None:
        self.spans[span_id] = {
            "ok": True,
            "span_id": span_id,
            "message": {"role": "tool", "content": content},
            "role": "tool",
            "origin": "host",
            "protected": protected,
            "content_sha256": "0" * 64,
        }

    # -- service surface --------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        self._record("status", (), {})
        if self.status_result is not None:
            return dict(self.status_result)
        return {
            "ok": True,
            "revision": self.revision,
            "head_hash": "abc123def4567890",
            "active": 4,
            "offloaded": 2,
            "pinned": 1,
            "estimated_tokens": 64000,
            "context_length": 128000,
            "reserve_tokens": 2048,
            "pressure": 0.5,
            "store_health": "ok",
            "last_error": None,
        }

    def inspect(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        self._record("inspect", (), {"cursor": cursor, "limit": limit})
        spans = [
            {
                "span_id": span_id,
                "role": record.get("role"),
                "origin": record.get("origin"),
                "protected": bool(record.get("protected")),
                "estimated_tokens": 12,
            }
            for span_id, record in list(self.spans.items())[:limit]
        ]
        return {
            "ok": True,
            "revision": self.revision,
            "spans": spans,
            "cursor": None,
            "limit": limit,
        }

    def stage(
        self, operations: Any, *, expected_revision: int, txn_id: str, actor_origin: str
    ) -> dict[str, Any]:
        self._record(
            "stage",
            (operations,),
            {
                "expected_revision": expected_revision,
                "txn_id": txn_id,
                "actor_origin": actor_origin,
            },
        )
        if self.stage_result is not None:
            return dict(self.stage_result)
        return {
            "ok": True,
            "revision": expected_revision,
            "applied": len(operations),
            "inert": 0,
            "errors": [],
            "receipts": [],
        }

    def commit(self, *, txn_id: str, expected_revision: int) -> dict[str, Any]:
        self._record("commit", (), {"txn_id": txn_id, "expected_revision": expected_revision})
        if self.commit_result is not None:
            return dict(self.commit_result)
        self.revision = expected_revision + 1
        return {"ok": True, "revision": self.revision, "applied": 1, "errors": [], "receipts": []}

    def discard(self, *, txn_id: str) -> None:
        self._record("discard", (), {"txn_id": txn_id})

    def revert(
        self, *, target_revision: int, expected_revision: int, txn_id: str, actor_origin: str
    ) -> dict[str, Any]:
        self._record(
            "revert",
            (),
            {
                "target_revision": target_revision,
                "expected_revision": expected_revision,
                "txn_id": txn_id,
                "actor_origin": actor_origin,
            },
        )
        if self.revert_result is not None:
            return dict(self.revert_result)
        self.revision = expected_revision + 1
        return {"ok": True, "revision": self.revision, "errors": [], "receipts": []}

    def search(self, query: str, *, cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        self._record("search", (query,), {"cursor": cursor, "limit": limit})
        return {"ok": True, "results": [], "cursor": None, "limit": limit}

    def retrieve(self, span_id: str) -> dict[str, Any]:
        self._record("retrieve", (span_id,), {})
        return self.spans.get(span_id, {"ok": False, "errors": [{"code": "span_not_found"}]})

    def create_proposal(self, operations: Any, *, base_revision: int) -> dict[str, Any]:
        self._record("create_proposal", (operations,), {"base_revision": base_revision})
        if self.proposal_result is not None:
            return dict(self.proposal_result)
        from hermes_clm.auxiliary import canonical_proposal_sha256

        proposal = {
            "proposal_id": f"prop-{len(self.proposals) + 1}",
            "proposal_sha256": canonical_proposal_sha256(operations, base_revision=base_revision),
            "base_revision": base_revision,
            "origin": "aux",
            "operations": [dict(operation) for operation in operations],
        }
        self.proposals.append(proposal)
        return dict(proposal)

    def accept_proposal(
        self, *, proposal_id: str, proposal_sha256: str, base_revision: int, txn_id: str
    ) -> dict[str, Any]:
        self._record(
            "accept_proposal",
            (),
            {
                "proposal_id": proposal_id,
                "proposal_sha256": proposal_sha256,
                "base_revision": base_revision,
                "txn_id": txn_id,
            },
        )
        if base_revision != self.revision:
            return {
                "ok": False,
                "revision": self.revision,
                "errors": [
                    {
                        "code": "stale_revision",
                        "message": f"proposal base {base_revision} != head {self.revision}",
                    }
                ],
            }
        known = {proposal["proposal_id"]: proposal for proposal in self.proposals}
        proposal = known.get(proposal_id)
        if proposal is None:
            return {
                "ok": False,
                "errors": [{"code": "unknown_proposal", "message": "no such proposal"}],
            }
        if proposal["proposal_sha256"] != proposal_sha256:
            return {
                "ok": False,
                "errors": [{"code": "proposal_hash_mismatch", "message": "hash does not match"}],
            }
        return {"ok": True, "revision": base_revision, "applied": 1, "errors": [], "receipts": []}

    def unpin(
        self, span_ids: Any, *, expected_revision: int, txn_id: str, actor_origin: str
    ) -> dict[str, Any]:
        self._record(
            "unpin",
            (span_ids,),
            {
                "expected_revision": expected_revision,
                "txn_id": txn_id,
                "actor_origin": actor_origin,
            },
        )
        self.revision = expected_revision + 1
        return {"ok": True, "revision": self.revision, "errors": [], "receipts": []}


class FakeAuxResponse:
    """Minimal OpenAI-shaped auxiliary response."""

    def __init__(self, content: str | None, *, usage: dict[str, Any] | None = None) -> None:
        message = type("Message", (), {"content": content})()
        choice = type("Choice", (), {"message": message})()
        self.choices = [choice]
        self.usage = usage


def aux_json(payload: Any) -> FakeAuxResponse:
    return FakeAuxResponse(json.dumps(payload), usage={"prompt_tokens": 11, "completion_tokens": 7})


class RecordingAuxClient:
    """Callable standing in for ``agent.auxiliary_client.call_llm``."""

    def __init__(self, response: Any = None, *, raises: BaseException | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.response
