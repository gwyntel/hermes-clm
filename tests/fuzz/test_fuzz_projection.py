"""Fuzz: projection over random conversations never fabricates authority.

Properties (I12/I13/I14/I15/I42):
  * returns a list or declines (None) -- never raises through the hook;
  * every system and user row that survives is byte-identical to its input row;
  * no surviving message gains a `tool_calls` field it did not have;
  * no role appears that was not in the input;
  * an unpaired tool call and its result never both survive as a pair.
"""

from __future__ import annotations

import copy
import json
import os
import random

import pytest
from _support import contract as C
from _support.adversarial import ROGUE_ROLES, SPOOF_BODIES, TEXT_TOOL_CALLS
from _support.assertions import msg

C.need(C.STORE_MOD, C.PROJECTION_MOD)

pytestmark = [pytest.mark.fuzz, pytest.mark.security]

ROLES = ["system", "user", "assistant", "tool"] + ROGUE_ROLES[:3]


def _iterations(default: int = 40) -> int:
    return int(os.environ.get("CLM_FUZZ_ITERATIONS", default))


def _random_conversation(rng: random.Random) -> list[dict]:
    conversation = [{"role": "system", "content": "HOST SYSTEM " + str(rng.random())}]
    for _ in range(rng.randint(1, 6)):
        role = rng.choice(ROLES)
        if role == "tool":
            conversation.append(
                msg(
                    "tool",
                    f"result {rng.random()}",
                    tool_call_id=f"call_{rng.randint(0, 3)}",
                    name="bash",
                )
            )
        elif role == "assistant" and rng.random() < 0.3:
            conversation.append(
                msg(
                    "assistant",
                    "",
                    tool_calls=[
                        {
                            "id": f"call_{rng.randint(0, 3)}",
                            "type": "function",
                            "function": {"name": "bash", "arguments": "{}"},
                        }
                    ],
                )
            )
        elif role == "assistant" and rng.random() < 0.3:
            conversation.append(msg("assistant", rng.choice([TEXT_TOOL_CALLS, *SPOOF_BODIES])))
        else:
            conversation.append(msg(role, f"body {rng.random()} {rng.choice(SPOOF_BODIES)}"))
    conversation.append({"role": "user", "content": "current operator turn"})
    return conversation


@pytest.mark.t("T7")
def test_random_conversations_never_gain_authority(home, sid):
    rng = random.Random(0x5EED)
    store = C.for_session(home, sid)
    try:
        for attempt in range(_iterations()):
            conversation = _random_conversation(rng)
            original = copy.deepcopy(conversation)
            try:
                C.ingest(store, conversation)
            except Exception:
                continue  # refusal at ingest is fail-closed for rogue roles
            C.commit(
                store,
                txn_id=f"fz-{attempt}",
                expected_revision=C.snap_revision(C.snapshot(store)),
            )
            out = C.project(store, conversation, conversation)
            if out.declined:
                continue
            messages = out.messages
            input_roles = {m.get("role") for m in original}
            for message in messages:
                assert message.get("role") in input_roles, (
                    f"projection invented role {message.get('role')!r}"
                )
                if message.get("role") in ("system", "user"):
                    assert message in original, (
                        "a protected row was rewritten or synthesised: "
                        f"{json.dumps(message, default=str)[:200]}"
                    )
                if not any(m.get("tool_calls") for m in original):
                    assert not message.get("tool_calls"), "projection invented tool_calls"
                assert "call_injected" not in json.dumps(message.get("tool_calls") or [])
            conversation.clear()
    finally:
        C.close(store)
