"""One rewrite-size authority: store, engine, tools, commands, aux, projection.

The tool layer used to reject at 8000 bytes while the store accepted 16384, so a
valid store-level rewrite was refused before it ever reached the store. The
ceiling now lives in :data:`hermes_clm.models.MAX_REWRITE_BYTES` and every layer
derives from it.
"""

from __future__ import annotations

import pytest
from clm_fakes import FakeService

from hermes_clm import models
from hermes_clm.auxiliary import AuxLimits
from hermes_clm.commands import _MAX_REWRITE_BYTES
from hermes_clm.engine import EngineConfig
from hermes_clm.projection import DEFAULT_MAX_REWRITE_BYTES
from hermes_clm.store import StoreConfig
from hermes_clm.tools import ToolHandlers, ToolLimits


def test_the_shared_constant_is_the_documented_limit():
    assert models.MAX_REWRITE_BYTES == 16_384


def test_every_layer_derives_the_same_rewrite_ceiling():
    assert ToolLimits().max_rewrite_bytes == models.MAX_REWRITE_BYTES
    assert StoreConfig().max_rewrite_bytes == models.MAX_REWRITE_BYTES
    assert EngineConfig().max_rewrite_bytes == models.MAX_REWRITE_BYTES
    assert AuxLimits().max_rewrite_bytes == models.MAX_REWRITE_BYTES
    assert DEFAULT_MAX_REWRITE_BYTES == models.MAX_REWRITE_BYTES
    assert _MAX_REWRITE_BYTES == models.MAX_REWRITE_BYTES


def test_a_rewrite_the_store_accepts_is_not_refused_by_the_tool_layer():
    """10k bytes: previously a tool-layer refusal, now a normal staged operation."""

    replacement = "x" * 10_000
    service = FakeService(revision=1)
    handlers = ToolHandlers(lambda _session_id: service)

    import json

    payload = json.loads(
        handlers.stage(
            {
                "operations": [
                    {"kind": "rewrite", "span_ids": ["m-0001"], "replacement": replacement}
                ],
                "expected_revision": 1,
            }
        )
    )

    assert payload["ok"] is True, payload["errors"]
    staged = service.args_for("stage")[0]
    assert staged[0]["replacement"] == replacement


def test_a_rewrite_over_the_shared_ceiling_is_still_refused():
    import json

    service = FakeService(revision=1)
    handlers = ToolHandlers(lambda _session_id: service)
    payload = json.loads(
        handlers.stage(
            {
                "operations": [
                    {
                        "kind": "rewrite",
                        "span_ids": ["m-0001"],
                        "replacement": "x" * (models.MAX_REWRITE_BYTES + 1),
                    }
                ],
                "expected_revision": 1,
            }
        )
    )
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "limit_exceeded"


@pytest.mark.parametrize("bad", [0, -1, True, "nope"])
def test_configured_limits_still_override_the_default(bad):
    limits = ToolLimits.from_config({"clm.max_rewrite_bytes": bad})
    assert limits.max_rewrite_bytes == models.MAX_REWRITE_BYTES
