"""Fuzz: hostile session-id shapes must never escape the sessions root.

Property: for any string, `ContextStore.for_session` either refuses or lands exactly on
`HERMES_HOME/clm/sessions/<id>` (I1/I2/I4). Seeds are fixed and the run is bounded, so
this is deterministic in CI; raise CLM_FUZZ_ITERATIONS for a longer sweep.
"""

from __future__ import annotations

import os
import random
import string

import pytest

from _support import contract as C
from _support.assertions import expect_refusal

C.need(C.STORE_MOD)

pytestmark = [pytest.mark.fuzz, pytest.mark.security]

ALPHABET = string.ascii_letters + string.digits + "._-/\\ \x00:\u200b\u00e9%$'\"" + "~"


def _iterations(default: int = 120) -> int:
    return int(os.environ.get("CLM_FUZZ_ITERATIONS", default))


@pytest.mark.t("T1")
def test_random_session_ids_cannot_escape_the_store_root(home):
    rng = random.Random(0xC1A5)
    sessions_root = (home / "clm" / "sessions").resolve()
    accepted = 0
    for _ in range(_iterations()):
        length = rng.randint(0, 90)
        candidate = "".join(rng.choice(ALPHABET) for _ in range(length))
        if candidate.endswith("/") or candidate.endswith(os.sep):
            continue
        try:
            store = C.for_session(home, candidate)
        except Exception:
            continue
        accepted += 1
        directory = C.session_dir(home, candidate).resolve()
        C.close(store)
        assert os.path.commonpath([str(directory), str(sessions_root)]) == str(sessions_root), (
            f"accepted id {candidate!r} resolved outside the sessions root: {directory}"
        )
        assert directory.name == candidate, (
            f"accepted id {candidate!r} was silently sanitised to {directory.name!r}"
        )
    # a corpus of pure alphabet strings will accept some; that is fine. What matters is
    # that none escaped. Sanity-check that the loop was not a no-op.
    assert accepted or _iterations() == 0
