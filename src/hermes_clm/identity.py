"""Row identity, protection policy and ordered-subsequence alignment.

Stdlib only: this module never imports Hermes and never constructs store
models. It reads host message dicts and store rows structurally, exactly as
``docs/integration-contract.md`` describes them.

Two jobs:

* **identity** - what a row *is* (durable ``message_uid``, absorbed aliases,
  tool-call structure) and whether that row may be edited at all.
* **alignment** - which request row corresponds to which durable history row.
  The host strips persistence-only fields (``message_uid`` included) from every
  provider-bound copy, so a request row carries no identity of its own; it is
  matched to the UID-bearing history the host hands the engine alongside it.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Durable identity fields, mirrored from the host's message_metadata contract.
MESSAGE_UID = "message_uid"
ABSORBED_MESSAGE_UIDS = "_absorbed_message_uids"
TOOL_CALL_UIDS = "_tool_call_uids"
TOOL_CALL_UID = "_tool_call_uid"

#: Roles the host owns. A projection never rewrites, offloads or drops one.
PROTECTED_ROLES = frozenset({"system", "developer", "user"})

#: The only content part type this package is willing to treat as text.
TEXT_PART_TYPES = frozenset({"text"})


def message_uid(message: Any) -> Optional[str]:
    """The row's durable id: a non-empty string, else ``None`` (never coerced)."""
    if not isinstance(message, Mapping):
        return None
    uid = message.get(MESSAGE_UID)
    return uid if isinstance(uid, str) and uid else None


def absorbed_uids(message: Any) -> Tuple[str, ...]:
    """Alias UIDs this row absorbed (a merged consecutive-user survivor)."""
    if not isinstance(message, Mapping):
        return ()
    raw = message.get(ABSORBED_MESSAGE_UIDS)
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: Dict[str, None] = {}
    for candidate in raw:
        if isinstance(candidate, str) and candidate:
            seen.setdefault(candidate, None)
    return tuple(seen)


def row_span_ids(message: Any) -> Tuple[str, ...]:
    """The row's own UID followed by its absorbed aliases."""
    uid = message_uid(message)
    if uid is None:
        return ()
    return (uid,) + absorbed_uids(message)


def role_of(message: Any) -> Optional[str]:
    """The row's provider role as a non-empty string, else ``None``."""
    if not isinstance(message, Mapping):
        return None
    role = message.get("role")
    return role if isinstance(role, str) and role else None


def tool_call_ids(message: Any) -> Tuple[str, ...]:
    """Provider tool-call ids declared by an assistant row, in order."""
    if not isinstance(message, Mapping):
        return ()
    calls = message.get("tool_calls")
    if not isinstance(calls, (list, tuple)):
        return ()
    ids: List[str] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            ids.append(call_id)
    return tuple(ids)


def tool_call_id_of(message: Any) -> Optional[str]:
    """The call id a tool-result row answers, else ``None``."""
    if not isinstance(message, Mapping):
        return None
    call_id = message.get("tool_call_id")
    return call_id if isinstance(call_id, str) and call_id else None


def canonical_content(content: Any) -> str:
    """A stable, order-insensitive canonical form of a message's ``content``.

    Used for signatures and content hashes only - never for sending or storing.
    """
    if isinstance(content, str):
        return "s:" + content
    if content is None:
        return "none:"
    if isinstance(content, Mapping):
        return "d:" + json.dumps(content, sort_keys=True, default=str, separators=(",", ":"))
    if isinstance(content, (list, tuple)):
        parts = [
            "d:" + json.dumps(part, sort_keys=True, default=str, separators=(",", ":"))
            if isinstance(part, Mapping)
            else "v:" + repr(part)
            for part in content
        ]
        return "l:[" + ",".join(parts) + "]"
    return "v:" + repr(content)


def content_sha256(content: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_content`."""
    return hashlib.sha256(canonical_content(content).encode("utf-8")).hexdigest()


def is_text_parts(content: Any) -> bool:
    """True for a list whose every part is a ``{"type": "text"}`` dict."""
    if not isinstance(content, (list, tuple)):
        return False
    if not content:
        return False
    return all(
        isinstance(part, Mapping) and part.get("type") in TEXT_PART_TYPES for part in content
    )


def is_multimodal_content(content: Any) -> bool:
    """True when ``content`` carries non-text or unknown content parts."""
    if not isinstance(content, (list, tuple)):
        return False
    if not content:
        return False
    return not is_text_parts(content)


def is_editable_content(content: Any) -> bool:
    """True when ``content`` may be replaced by plain text.

    Only a string (or an absent/``None`` content) is replaceable. Multimodal
    and unknown content-part structures are never flattened - see ADR 0001 (6).
    """
    return content is None or isinstance(content, str)


def is_protected_message(
    message: Any,
    *,
    pinned: bool = False,
    newest_tool_result: bool = False,
    incoming: bool = False,
) -> bool:
    """Whether a row is host-owned and therefore never edited by projection.

    Protection is structural: role, pinned state, position (newest tool result),
    and content shape. Nothing here looks at message *text*.
    """
    if not isinstance(message, Mapping):
        return True
    role = role_of(message)
    if role is None:
        return True
    if role in PROTECTED_ROLES:
        return True
    if pinned or incoming:
        return True
    if newest_tool_result and role == "tool":
        return True
    if is_multimodal_content(message.get("content")):
        return True
    return False


def row_signature(message: Any) -> Optional[Tuple[Any, ...]]:
    """The comparable identity signature of a row, ignoring bytes the host rewrites.

    Tool structure wins over text because it is stable across the host's
    ``api_content`` replay rewriting. Text rows fall back to a role-qualified
    content digest. ``None`` means the row cannot participate in alignment.
    """
    if not isinstance(message, Mapping):
        return None
    role = role_of(message)
    if role is None:
        return None
    call_id = tool_call_id_of(message)
    if call_id is not None:
        return ("tool", role, call_id)
    ids = tool_call_ids(message)
    if ids:
        return ("calls", role, ids)
    return ("text", role, content_sha256(message.get("content")))


def alignment_signature(message: Any) -> Optional[Tuple[Any, ...]]:
    """Signature used for alignment; requires a durable id to be matchable."""
    signature = row_signature(message)
    if signature is None or message_uid(message) is None:
        return None
    return signature


@dataclass(eq=False)
class Alignment:
    """Ordered mapping between request rows and UID-bearing history rows.

    ``pairs`` is monotone: both the request index and the history index increase.
    A pair is *ambiguous* when another history row with the same signature sits
    inside the window its neighbours leave open, so editing it could hit the
    wrong row.
    """

    pairs: Tuple[Tuple[int, int], ...] = ()
    request_to_uid: Dict[int, str] = field(default_factory=dict)
    uid_to_request: Dict[str, int] = field(default_factory=dict)
    ambiguous_request_indices: frozenset = frozenset()
    ambiguous_uids: frozenset = frozenset()
    unmatched_request_indices: Tuple[int, ...] = ()

    def uid_for(self, request_index: int) -> Optional[str]:
        """The durable UID aligned to a request index, else ``None``."""
        return self.request_to_uid.get(request_index)

    @property
    def present_uids(self) -> frozenset:
        """UIDs this request is believed to show, ambiguous candidates included."""
        return frozenset(self.uid_to_request) | self.ambiguous_uids

    def is_ambiguous(self, uid: Optional[str]) -> bool:
        """True when an alignment for ``uid`` was not unique."""
        return uid is not None and uid in self.ambiguous_uids

    def summary(self) -> Dict[str, Any]:
        """JSON-serializable alignment diagnostics."""
        return {
            "matched": len(self.pairs),
            "ambiguous_uids": sorted(self.ambiguous_uids),
            "unmatched_request_indices": list(self.unmatched_request_indices),
        }


def align(request_messages: Sequence[Any], history_messages: Sequence[Any]) -> Alignment:
    """Align request rows to history rows as an ordered, unique subsequence.

    The request is the assembled provider request (system prompt, prefill,
    injected user content, history, newest tool output); the history is the
    UID-bearing conversation list. Rows the host synthesized (system prompt,
    prefill, injection-only user rows, unpersisted tail) simply find no match
    and pass through untouched.
    """
    requests = list(request_messages or ())
    history = list(history_messages or ())
    history_signatures = [alignment_signature(row) for row in history]
    history_uids = [message_uid(row) for row in history]

    # Signature -> sorted positions, so both passes are logarithmic rather than a
    # nested scan (a long session must not make every request quadratic).
    positions_by_signature: Dict[Any, List[int]] = {}
    for position, signature in enumerate(history_signatures):
        if signature is not None:
            positions_by_signature.setdefault(signature, []).append(position)

    def earliest_after(signature: Any, cursor: int) -> Optional[int]:
        positions = positions_by_signature.get(signature)
        if not positions:
            return None
        index = bisect.bisect_right(positions, cursor)
        return positions[index] if index < len(positions) else None

    def latest_before(signature: Any, limit: int) -> Optional[int]:
        positions = positions_by_signature.get(signature)
        if not positions:
            return None
        index = bisect.bisect_left(positions, limit)
        return positions[index - 1] if index else None

    pairs: List[Tuple[int, int]] = []
    unmatched: List[int] = []
    cursor = -1
    for request_index, row in enumerate(requests):
        signature = row_signature(row)
        if signature is None:
            unmatched.append(request_index)
            continue
        match = earliest_after(signature, cursor)
        if match is None:
            unmatched.append(request_index)
            continue
        pairs.append((request_index, match))
        cursor = match

    ambiguous_requests: set = set()
    ambiguous_uids: set = set()
    if pairs:
        # Every position a row could occupy in *some* monotone alignment. The
        # earliest is the greedy match; the latest comes from a backward pass.
        # When they differ, byte-identical duplicates mean the request does not
        # determine which durable row it is showing - and an edit could land on
        # the wrong one.
        latest: Dict[int, int] = {}
        limit = len(history)
        for request_index in range(len(requests) - 1, -1, -1):
            signature = row_signature(requests[request_index])
            if signature is None:
                continue
            candidate_position = latest_before(signature, limit)
            if candidate_position is None:
                continue
            latest[request_index] = candidate_position
            limit = candidate_position
        for request_index, first in pairs:
            last = latest.get(request_index)
            if last is None or last <= first:
                continue
            signature = history_signatures[first]
            positions = positions_by_signature.get(signature) or []
            low = bisect.bisect_left(positions, first)
            high = bisect.bisect_right(positions, last)
            if high - low <= 1:
                continue
            ambiguous_requests.add(request_index)
            for position in positions[low:high]:
                uid = history_uids[position]
                if uid is not None:
                    ambiguous_uids.add(uid)

    request_to_uid: Dict[int, str] = {}
    uid_to_request: Dict[str, int] = {}
    for request_index, history_index in pairs:
        uid = history_uids[history_index]
        if uid is None:  # pragma: no cover - alignable rows always carry a uid
            continue
        request_to_uid[request_index] = uid
        uid_to_request.setdefault(uid, request_index)

    return Alignment(
        pairs=tuple(pairs),
        request_to_uid=request_to_uid,
        uid_to_request=uid_to_request,
        ambiguous_request_indices=frozenset(ambiguous_requests),
        ambiguous_uids=frozenset(ambiguous_uids),
        unmatched_request_indices=tuple(unmatched),
    )


@dataclass(eq=False)
class ToolGroups:
    """Tool-call groups: an assistant row plus the results that answer it."""

    uid_to_group: Dict[str, str] = field(default_factory=dict)
    members: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def group_of(self, uid: Optional[str]) -> Optional[str]:
        """The group id containing ``uid``, else ``None``."""
        return None if uid is None else self.uid_to_group.get(uid)

    def members_of(self, uid: Optional[str]) -> Tuple[str, ...]:
        """Every UID in ``uid``'s group, in order (just ``uid`` when ungrouped)."""
        group = self.group_of(uid)
        if group is None:
            return () if uid is None else (uid,)
        return self.members.get(group, ())


def tool_groups(messages: Sequence[Any]) -> ToolGroups:
    """Group assistant tool calls with their contiguous results.

    Grouping is structural and uses only call ids, never text: a group is one
    assistant row with ``tool_calls`` plus the following ``tool`` rows whose
    ``tool_call_id`` it declared. Rows without a UID cannot be edited and so
    cannot join a group.
    """
    rows = list(messages or ())
    uid_to_group: Dict[str, str] = {}
    members: Dict[str, Tuple[str, ...]] = {}
    index = 0
    while index < len(rows):
        row = rows[index]
        call_ids = tool_call_ids(row)
        if role_of(row) != "assistant" or not call_ids:
            index += 1
            continue
        assistant_uid = message_uid(row)
        group_uids: List[str] = [assistant_uid] if assistant_uid else []
        cursor = index + 1
        declared = set(call_ids)
        while cursor < len(rows):
            candidate = rows[cursor]
            if role_of(candidate) != "tool" or tool_call_id_of(candidate) not in declared:
                break
            candidate_uid = message_uid(candidate)
            if candidate_uid:
                group_uids.append(candidate_uid)
            cursor += 1
        if assistant_uid and group_uids:
            group_id = "group:" + assistant_uid
            members[group_id] = tuple(group_uids)
            for uid in group_uids:
                uid_to_group[uid] = group_id
        index = max(cursor, index + 1)
    return ToolGroups(uid_to_group=uid_to_group, members=members)


def group_covered(members: Iterable[str], selected: Iterable[str]) -> bool:
    """True when ``selected`` hits a group either entirely or not at all."""
    member_tuple = tuple(members)
    if not member_tuple:
        return True
    hits = [uid for uid in member_tuple if uid in set(selected)]
    return len(hits) in (0, len(member_tuple))
