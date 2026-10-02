"""Content addressing.

Every durable object in Colloid (units, loci, genes, programs) is identified by a hash of
its *content*, not by an auto-increment counter. Two consequences matter for the engine:

1. Deduplication is free. If two operators independently propose the same knob value at
   the same locus, they produce the same gene id, and the evaluator never pays twice.
2. Reproducibility. A program id is ``hash(baseline_id, sorted gene ids)``; given the
   baseline commit and the gene payloads (stored in the program DB), anyone can rebuild
   exactly the same variant and verify the id.

Hashes are SHA-256 over a canonical JSON encoding (sorted keys, no whitespace, UTF-8), so
dict ordering or float formatting differences cannot change an id.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialise ``value`` deterministically (sorted keys, compact separators)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def _default(obj: Any) -> Any:
    # pydantic models and enums are the only non-JSON values we hash.
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "value"):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"cannot canonicalise {type(obj).__name__}")


def content_hash(*parts: Any, length: int = 16) -> str:
    """Return a short hex SHA-256 digest of the canonical encoding of ``parts``.

    16 hex characters = 64 bits. With the number of objects a run produces (≤10^7) the
    birthday-collision probability is ~10^-6, which is acceptable for ids; artifacts that
    need cryptographic strength (build outputs) use :func:`sha256_hex` instead.
    """
    digest = hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
    return digest[:length]


def sha256_hex(data: bytes | str) -> str:
    """Full SHA-256 of raw bytes or text (used for source and artifact hashes)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
