"""Redaction and append-only archive helpers.

Two responsibilities:

1. **Redaction at ingestion.**  Raw means *unsummarized*, not *secret
   preserving*.  Every archived payload passes through a deterministic pattern
   scrubber first (``clm.archive_redact`` defaults to ``True``); the opt-out is
   documented as sensitive in ``docs/store-format.md``.
2. **Append-only archive records with a hash chain.**  Each record carries
   ``prev_hash``/``entry_hash`` so interruption or tampering is *detected*.
   There is no tamper-proof claim against same-UID shell access.

Nothing here mutates caller input: redaction always builds fresh structures.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .errors import ValidationError
from .models import canonical_json, canonical_sha256, sha256_hex

__all__ = [
    "canonical_json",
    "canonical_sha256",
    "sha256_hex",
    "RedactionRule",
    "DEFAULT_RULES",
    "Redactor",
    "RedactionOutcome",
    "redact_message",
    "chain_entry_hash",
    "GENESIS_HASH",
    "verify_chain",
]

#: Genesis predecessor for the archive hash chain (64 zero hex chars).
GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """One deterministic scrubber rule."""

    name: str
    pattern: re.Pattern[str]
    replacement: str
    group: int | None = None

    def apply(self, text: str) -> tuple[str, int]:
        if self.group is None:
            new_text, count = self.pattern.subn(self.replacement, text)
        else:
            count = 0
            group: int = self.group

            def _sub(match: re.Match[str]) -> str:
                nonlocal count
                count += 1
                start, end = match.span(group)
                offset = match.start()
                whole = match.group(0)
                return whole[: start - offset] + self.replacement + whole[end - offset :]

            new_text = self.pattern.sub(_sub, text)
        return new_text, count


def _rule(
    name: str, pattern: str, replacement: str, group: int | None = None, flags: int = 0
) -> RedactionRule:
    return RedactionRule(
        name=name, pattern=re.compile(pattern, flags), replacement=replacement, group=group
    )


#: Ordered, conservative secret scrubbers.  Order matters: compound rules run
#: before generic assignment rules so the more specific label wins.
DEFAULT_RULES: tuple[RedactionRule, ...] = (
    _rule(
        "private_key",
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        "[REDACTED:private_key]",
        flags=re.DOTALL,
    ),
    _rule("anthropic_key", r"sk-ant-[A-Za-z0-9_\-]{16,}", "[REDACTED:anthropic_key]"),
    _rule("openai_key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}", "[REDACTED:openai_key]"),
    _rule("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}", "[REDACTED:github_token]"),
    _rule("github_pat", r"\bgithub_pat_[A-Za-z0-9_]{20,}", "[REDACTED:github_pat]"),
    _rule("slack_token", r"\bxox[abpros]-[A-Za-z0-9\-]{10,}", "[REDACTED:slack_token]"),
    _rule("aws_access_key_id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[REDACTED:aws_access_key_id]"),
    _rule("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b", "[REDACTED:google_api_key]"),
    _rule(
        "jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", "[REDACTED:jwt]"
    ),
    _rule("bearer", r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{16,}", r"\1 [REDACTED:bearer]"),
    _rule(
        "conn_string",
        r"(?i)\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/]+:)[^\s@/]+@",
        r"\1[REDACTED:conn_string]@",
    ),
    _rule(
        "assigned_secret",
        r"(?i)\b(api[_-]?key|secret|client[_-]?secret|password|passwd|access[_-]?token|auth[_-]?token|refresh[_-]?token)\b(\s*[:=]\s*)([\"']?)([^\s\"',;}\]]{6,})\3",
        r"\1\2\3[REDACTED:assigned_secret]\3",
    ),
)


@dataclass(frozen=True, slots=True)
class RedactionOutcome:
    message: dict[str, Any]
    count: int = 0
    rules: tuple[str, ...] = ()

    @property
    def redacted(self) -> bool:
        return self.count > 0


def _scrub_text(text: str, rules: Iterable[RedactionRule]) -> tuple[str, list[str]]:
    hits: list[str] = []
    out = text
    for rule in rules:
        out, count = rule.apply(out)
        if count:
            hits.extend([rule.name] * count)
    return out, hits


def _scrub_value(value: Any, rules: tuple[RedactionRule, ...], hits: list[str]) -> Any:
    if isinstance(value, str):
        new_text, found = _scrub_text(value, rules)
        hits.extend(found)
        return new_text
    if isinstance(value, Mapping):
        return {key: _scrub_value(item, rules, hits) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, rules, hits) for item in value]
    if isinstance(value, tuple):
        return [_scrub_value(item, rules, hits) for item in value]
    return value


def redact_message(
    message: Mapping[str, Any],
    *,
    enabled: bool = True,
    extra_rules: Iterable[RedactionRule] = (),
) -> RedactionOutcome:
    """Return a redacted deep copy of ``message`` plus redaction metadata.

    ``enabled=False`` is the documented sensitive opt-out: the payload is still
    deep-copied, so the caller's object is never aliased into the archive.
    """

    if not isinstance(message, Mapping):
        raise ValidationError("archived message must be a mapping", code="invalid_message")
    copied = copy.deepcopy(dict(message))
    if not enabled:
        return RedactionOutcome(message=copied, count=0, rules=())
    rules = tuple(DEFAULT_RULES) + tuple(extra_rules)
    hits: list[str] = []
    redacted = _scrub_value(copied, rules, hits)
    if not isinstance(redacted, dict):  # pragma: no cover - defensive
        redacted = dict(copied)
    return RedactionOutcome(message=redacted, count=len(hits), rules=tuple(dict.fromkeys(hits)))


class Redactor:
    """Configurable, reusable scrubber bound to a store handle."""

    __slots__ = ("enabled", "_rules")

    def __init__(self, *, enabled: bool = True, extra_rules: Iterable[RedactionRule] = ()) -> None:
        self.enabled = bool(enabled)
        self._rules = tuple(DEFAULT_RULES) + tuple(extra_rules)

    def __call__(self, message: Mapping[str, Any]) -> RedactionOutcome:
        if not isinstance(message, Mapping):
            raise ValidationError("archived message must be a mapping", code="invalid_message")
        copied = copy.deepcopy(dict(message))
        if not self.enabled:
            return RedactionOutcome(message=copied, count=0, rules=())
        hits: list[str] = []
        redacted = _scrub_value(copied, self._rules, hits)
        if not isinstance(redacted, dict):  # pragma: no cover - defensive
            redacted = dict(copied)
        return RedactionOutcome(message=redacted, count=len(hits), rules=tuple(dict.fromkeys(hits)))


# ---------------------------------------------------------------------------
# hash chain
# ---------------------------------------------------------------------------


def chain_entry_hash(
    *,
    prev_hash: str,
    span_id: str,
    content_sha256: str,
    payload_json: str,
    revision: int,
) -> str:
    """Deterministic link hash for one archive record."""

    return sha256_hex(
        canonical_json(
            {
                "prev": prev_hash,
                "span_id": span_id,
                "content_sha256": content_sha256,
                "payload_sha256": sha256_hex(payload_json),
                "revision": revision,
            }
        )
    )


def verify_chain(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify an archive hash chain.

    ``rows`` must be ordered by ``archive_id`` and expose the columns
    ``archive_id``, ``span_id``, ``content_sha256``, ``payload_json``,
    ``prev_hash``, ``entry_hash`` and ``revision``.
    """

    expected_prev = GENESIS_HASH
    count = 0
    problems: list[dict[str, Any]] = []
    for row in rows:
        count += 1
        if row["prev_hash"] != expected_prev:
            problems.append(
                {
                    "archive_id": row["archive_id"],
                    "code": "broken_link",
                    "expected_prev": expected_prev,
                    "found_prev": row["prev_hash"],
                }
            )
        recomputed = chain_entry_hash(
            prev_hash=row["prev_hash"],
            span_id=row["span_id"],
            content_sha256=row["content_sha256"],
            payload_json=row["payload_json"],
            revision=row["revision"],
        )
        if recomputed != row["entry_hash"]:
            problems.append(
                {
                    "archive_id": row["archive_id"],
                    "code": "entry_hash_mismatch",
                    "expected": recomputed,
                    "found": row["entry_hash"],
                }
            )
        if sha256_hex(row["payload_json"]) != canonical_sha256(_loads(row["payload_json"])):
            # payload self-consistency: canonical re-encoding must match the stored bytes
            problems.append({"archive_id": row["archive_id"], "code": "payload_not_canonical"})
        expected_prev = row["entry_hash"]
    return {"ok": not problems, "count": count, "problems": problems, "head_hash": expected_prev}


def _loads(payload_json: str) -> Any:
    import json

    return json.loads(payload_json)
