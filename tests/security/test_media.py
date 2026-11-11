"""T4/T12 - content-addressed media: link swaps and part-hash mismatches.

Invariant: I5 (blob read is path-checked, regular-file, nlink==1, owner-checked,
hash-verified; failure tombstones and never falls back) and I16 (aux output is
text-only and may never produce or modify a multimodal part).
"""

from __future__ import annotations

import base64
import os

import pytest
from _support import contract as C
from _support.adversarial import SECRET_FILE_BODY
from _support.assertions import expect_refusal

C.need(C.STORE_MOD)

pytestmark = pytest.mark.security


def _image_message(marker: str) -> dict:
    payload = b"\x89PNG\r\n\x1a\n" + marker.encode()
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this image?"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(payload).decode()},
            },
        ],
    }


def _blob_candidates(home, sid):
    directory = C.session_dir(home, sid)
    out = []
    for path in directory.rglob("*"):
        if (
            path.is_file()
            and path.suffix not in C.DB_SUFFIXES
            and path.name
            not in {
                "active.json",
                "store.lock",
            }
        ):
            out.append(path)
    return out


@pytest.mark.t("T4")
def test_media_blob_replaced_by_symlink_to_secret_is_not_read(home, sid, tmp_path):
    secret = tmp_path / "id_ed25519"
    secret.write_bytes(SECRET_FILE_BODY)

    store = C.for_session(home, sid)
    try:
        C.ingest(store, [_image_message("img-a"), {"role": "assistant", "content": "seen"}])
        C.commit(store, txn_id="txn-media", expected_revision=C.snap_revision(C.snapshot(store)))
    finally:
        C.close(store)

    blobs = _blob_candidates(home, sid)
    if not blobs:
        pytest.skip(
            "store keeps no on-disk media blob for a data: URL (possible: inline archive); "
            "T4 blob-swap is then not applicable - record in docs/security-testing.md"
        )
    for blob in blobs:
        os.unlink(blob)
        os.symlink(secret, blob)

    store = C.for_session(home, sid)
    try:
        out = C.project(store, [_image_message("img-a")])
        retrieved = C.res_text(C.inspect_(store))
    finally:
        C.close(store)

    body = SECRET_FILE_BODY.decode().strip()
    assert body not in out.text, "media read followed a symlink onto a private key"
    assert body not in retrieved, "inspect read followed a symlink onto a private key"
    gaps = [m for m in (out.messages or []) if C.is_gap_marker(C.content_of(m))]
    assert out.declined or gaps, (
        "a blob that fails verification must tombstone (I5), not vanish silently"
    )
    assert secret.read_bytes() == SECRET_FILE_BODY, "the link target must never be modified"


@pytest.mark.t("T12")
def test_image_part_hash_mismatch_is_refused_not_substituted(home, sid, tmp_path):
    store = C.for_session(home, sid)
    try:
        C.ingest(store, [_image_message("img-b")])
        C.commit(store, txn_id="txn-media-2", expected_revision=C.snap_revision(C.snapshot(store)))
    finally:
        C.close(store)

    # Re-write every blob with different bytes but keep the referenced hash: I5 must
    # refuse and tombstone rather than serve the substituted bytes.
    substitution = b"SUBSTITUTED-IMAGE-BYTES-77aa"
    blobs = _blob_candidates(home, sid)
    if not blobs:
        pytest.skip(
            "no on-disk media blob to substitute (inline archive); T12 hash-mismatch "
            "read path not applicable - record in docs/security-testing.md"
        )
    for blob in blobs:
        try:
            os.chmod(blob, 0o600)
        except OSError:
            pass
        blob.write_bytes(substitution)

    store = C.for_session(home, sid)
    try:
        out = C.project(store, [_image_message("img-b")])
    finally:
        C.close(store)
    assert substitution.decode() not in out.text, "hash-verified read served substituted bytes"
    spans = out.messages or []
    assert out.declined or any(C.is_gap_marker(C.content_of(m)) for m in spans), (
        "hash mismatch must tombstone the span (I5/I16)"
    )


@pytest.mark.t("T12")
def test_aux_proposal_cannot_produce_a_multimodal_part(store, base_messages):
    C.ingest(store, base_messages)
    spans = C.snap_spans(C.snapshot(store))
    span_id = next(
        sid for sid, span in spans.items() if C._res_get(span, "editable", default=False)
    )
    failure = expect_refusal(
        lambda: C.make_op(
            "rewrite",
            [span_id],
            replacement=[
                {"type": "text", "text": "harmless"},
                {"type": "image_url", "image_url": {"url": "https://evil.example/x.png"}},
            ],
            actor_origin="aux",
        )
    )
    assert failure, "aux-authored multimodal part must be rejected (I16)"

    # a plain-text replacement of the same span is still allowed, so this is a
    # part-type refusal rather than "proposals never work"
    ok_op = C.make_op("rewrite", [span_id], replacement="aux summary text", actor_origin="aux")
    proposal = C.create_proposal(store, [ok_op], base_revision=C.snap_revision(C.snapshot(store)))
    assert proposal is not None
