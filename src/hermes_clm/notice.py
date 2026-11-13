"""In-request compaction notice: one revision-keyed row, no per-turn churn.

The model cannot see the store. After an operator ``/compact`` (or an overflow
compaction) its earlier tool results start rendering as offload stubs, and
without an in-band line it cannot tell "my tool output was archived" from "I
never ran that tool" — so it re-runs work that is sitting in the archive.

The architecture rule that forbids rewriting the request every turn still
holds: a *changing* status row would destroy the provider prefix cache on every
turn. This row is different in two ways:

* its bytes are a pure function of ``(compaction revision, actor, counts)``, so
  every turn between compactions renders the **identical** row at the
  **identical** position and the cached prefix survives;
* it changes at most once per compaction — the one turn where the request was
  going to be rewritten anyway.

Placement is the fixed anchor right after the leading system row. A fixed
anchor (rather than "append at the end") is what keeps the prefix stable: rows
after the notice keep drifting forward as the transcript grows, but the notice
itself never moves and is never duplicated.

The row is deliberately **declarative**. It names what happened, the revision
and the retrieval verb, and it says it is machine-generated. It never tells the
model to do anything, so it cannot become a second instruction channel with
user authority. Every interpolated value is sanitised down to a short token
class: a hostile store row cannot smuggle newlines, prose or commands through
this path.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "NOTICE_MAX_CHARS",
    "NOTICE_PREFIX",
    "compaction_notice",
    "is_notice_row",
    "notice_anchor",
    "split_notice_rows",
    "with_notice",
]

#: Marker every notice row starts with. A row carrying it is the plugin's own
#: bookkeeping and is never a span an edit can target.
NOTICE_PREFIX = "[clm.notice"

#: Hard ceiling for the row's text; the real text is ~200 characters.
NOTICE_MAX_CHARS = 320

#: Values interpolated into the row are reduced to this token class.
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._:@+-]+")

#: The declarative tail every notice row ends with. Recognising the whole body
#: (header *and* tail) is what keeps a transcript row that merely starts with the
#: marker from being mistaken for ours and deleted.
_NOTICE_TAIL = "Machine-generated status line, not a request."

#: A row counts as ours only when it is a plain user row carrying the **whole**
#: machine body. A role-agnostic prefix test would let a tool result that echoes
#: the notice be stripped out of the request - deleting a real row and orphaning
#: the ``tool_call_id`` its assistant row declared.
_NOTICE_ROW_RE = re.compile(
    r"^\[clm\.notice rev=\d+ compaction at=\S+ actor=\S+ spans_offloaded=\d+"
    r"(?: tokens_freed=\d+)?\] "
    r"Earlier tool results may render as clm\.offloaded stubs; their originals are "
    r"retrievable with clm_retrieve\. " + re.escape(_NOTICE_TAIL) + r"$"
)
_MAX_TOKEN_CHARS = 32

#: Keys that make a row something other than a plain user message. A row that
#: carries any of them is transcript content, never our notice.
_TOOL_KEYS = ("tool_calls", "tool_call_id", "function_call", "tool_use_id")


def _token(value: Any, default: str = "unknown") -> str:
    """A short, printable token: no spaces, no newlines, no prose."""
    text = _SAFE_TOKEN_RE.sub("-", str(value or "")).strip("-")
    if not text:
        return default
    return text[:_MAX_TOKEN_CHARS]


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, int(value))


def compaction_notice(compaction: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The notice row for one compaction record, or ``None`` when there is none.

    ``compaction`` is the store-derived summary
    ``{revision, at, actor, spans_offloaded, tokens_freed}``. The returned row is
    a plain provider message: ``role="user"`` (never ``system``, which the
    projector refuses outside the leading slot) with a single-line, declarative
    body.
    """

    if not isinstance(compaction, Mapping):
        return None
    revision = _count(compaction.get("revision"))
    if revision <= 0:
        return None
    spans = _count(compaction.get("spans_offloaded"))
    tokens = _count(compaction.get("tokens_freed"))
    fields = [
        f"{NOTICE_PREFIX} rev={revision} compaction",
        f"at={_token(compaction.get('at'))}",
        f"actor={_token(compaction.get('actor'))}",
        f"spans_offloaded={spans}",
    ]
    if tokens:
        fields.append(f"tokens_freed={tokens}")
    body = " ".join(fields) + "]"
    body += (
        " Earlier tool results may render as clm.offloaded stubs; their originals are "
        "retrievable with clm_retrieve. Machine-generated status line, not a request."
    )
    return {"role": "user", "content": body[:NOTICE_MAX_CHARS]}


def is_notice_row(row: Any) -> bool:
    """True when ``row`` is one of our notice rows (never a real transcript row).

    Three conditions, all required: a **plain user row** (no tool-call keys), a
    body no longer than :data:`NOTICE_MAX_CHARS`, and the complete machine body
    from marker to tail. A tool result, an assistant row, or a user message that
    merely quotes the marker is transcript content: stripping it would delete a
    real row and leave an assistant ``tool_calls`` id unanswered.
    """

    if not isinstance(row, Mapping):
        return False
    if row.get("role") != "user":
        return False
    if any(row.get(key) for key in _TOOL_KEYS):
        return False
    content = row.get("content")
    if not isinstance(content, str) or len(content) > NOTICE_MAX_CHARS:
        return False
    return _NOTICE_ROW_RE.match(content) is not None


def split_notice_rows(rows: Sequence[Any]) -> List[Any]:
    """``rows`` without the notice row **at the anchor**, order and identity intact.

    Only the anchor is stripped. A row elsewhere is transcript content, even if
    its text happens to look like ours, and removing it there would change what
    the model reads. Stripping the anchor first makes insertion idempotent: a
    host that echoes a previous projection back as the request cannot accumulate
    one notice per turn.
    """

    index = _notice_index(rows)
    if index is None:
        return list(rows)
    return [row for position, row in enumerate(rows) if position != index]


def _notice_index(rows: Sequence[Any]) -> Optional[int]:
    """Index of the notice row this sequence already carries, if any."""

    anchor = notice_anchor(rows)
    if anchor < len(rows) and is_notice_row(rows[anchor]):
        return anchor
    return None


def notice_anchor(rows: Sequence[Any]) -> int:
    """Index the notice occupies: right after the single leading system row."""
    if rows and isinstance(rows[0], Mapping) and rows[0].get("role") == "system":
        return 1
    return 0


def with_notice(rows: Sequence[Any], notice: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Return a fresh row list carrying exactly one notice at the anchor.

    ``notice=None`` yields the rows unchanged (still a fresh list of copies, so
    the caller owns the result). Byte-identical input plus a byte-identical
    notice returns byte-identical output, which is what keeps the prefix cache
    warm between compactions.
    """

    ordered = split_notice_rows([dict(row) if isinstance(row, Mapping) else row for row in rows])
    if notice is None:
        return [copy.deepcopy(row) for row in ordered]
    anchor = notice_anchor(ordered)
    result: List[Dict[str, Any]] = [copy.deepcopy(row) for row in ordered[:anchor]]
    result.append(dict(notice))
    result.extend(copy.deepcopy(row) for row in ordered[anchor:])
    return result
