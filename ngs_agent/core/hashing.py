"""Deterministic hashing primitives.

Two hash families are used, and the difference matters for reproducibility:

``canonical_json_sha256``
    Content hash of a Python mapping. Keys are sorted, floats are rejected
    (they are not stable across serializers), and the output is the SHA-256 of
    a UTF-8 JSON string. Used for input files, adapter request/response bodies
    and audit records.

``ga4gh_digest``
    The GA4GH ``sha512t24`` URL-safe digest used by VRS for computed
    identifiers. Used for variant identity and evidence-record identity so that
    our identifiers are structurally compatible with VRS digests even where a
    full ``ga4gh:SQ.`` sequence digest is not available.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ngs_agent.core.errors import NonCanonicalValueError

_CHUNK_SIZE = 1024 * 1024  # 1 MiB: keeps memory flat for arbitrarily large inputs.


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to a byte-stable JSON string.

    Floats are rejected on purpose. A float's shortest-round-trip
    representation is an implementation detail of the serializer, so two builds
    of two Python versions could hash the same logical value differently. Call
    that must carry a real number encodes it as a string (e.g. ``"0.00002"``)
    or as an exact integer pair (``{"numerator": 2, "denominator": 100000}``).
    """
    assert_canonical_value(value, path="$")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def assert_canonical_value(value: Any, path: str = "$") -> None:
    """Raise :class:`NonCanonicalValueError` if ``value`` cannot be hashed stably."""
    _assert_hashable_type(value, path=path)


def _assert_hashable_type(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        raise NonCanonicalValueError(
            f"{path}: floats are not permitted in canonical hashes because their "
            "serialization is not guaranteed stable across Python builds; encode "
            "the value as a string or an exact integer ratio instead."
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValueError(
                    f"{path}: mapping keys must be strings, got {type(key)}")
            _assert_hashable_type(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _assert_hashable_type(item, path=f"{path}[{index}]")
        return
    raise NonCanonicalValueError(
        f"{path}: unsupported type {type(value).__name__} in canonical hash")


def canonical_json_sha256(value: Any) -> str:
    """SHA-256 hex digest of the canonical JSON form of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_file(path: Path, chunk_size: int = _CHUNK_SIZE) -> str:
    """Stream-compute the SHA-256 of ``path`` with constant memory.

    There is deliberately no size cutoff that could degrade the digest to
    ``None``: a checksum that is sometimes missing is worse than no checksum.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def ga4gh_digest(payload: str) -> str:
    """GA4GH ``sha512t24`` URL-safe digest (the VRS computed-identifier scheme).

    ``sha512t24`` is the first 24 bytes of SHA-512, base64url encoded without
    padding. This is exactly the digest function VRS specifies for
    ``LiteralSequenceExpression`` and ``SimpleInterval`` identifiers.
    """
    raw = hashlib.sha512(payload.encode("utf-8")).digest()[:24]
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
