"""The mutation data lake: a content-addressed, hash-chained ledger of verified mutations.

Every mutation Colloid has *verified*, meaning it passed L6 and was replicate-measured
against baseline, is worth more than the run that found it. The lake keeps that knowledge
across runs, days, machines and targets, so each new run starts from what earlier runs proved
instead of from zero.

Two kinds of record:

* **gene** - one change at one locus: the locus in language- and platform-neutral terms
  (unit path, surface, layer, language), the payload (new source text or knob value), the
  hash of the source it was written against, a human-readable explanation, and provenance
  (operator, model, prompt hash). A gene record holds only the change itself, so the same
  mutation found by two runs on two machines is one record.
* **program** - a verified combination of genes (by gene-record id) with its *evidence*: the
  measured effect on every objective with confidence intervals and the protocol that produced
  them, holdout persistence, attribution (Shapley / ablation), the A/A noise floor in force,
  the platform fingerprint, the run and engine commit, and links to earlier program records
  it extends (``derived_from``).

**Addressing.** A record's id is ``sha256(schema || canonical JSON of its content)``.
Canonical JSON means sorted keys, no whitespace, UTF-8, and no NaN/Infinity, so the id is a
pure function of the content on any platform and any Python version. Re-ingesting the same
knowledge is a no-op.

**Ordering ("which is older, which is newer").** Records enter the lake through an
append-only *ledger*. Entry *n* carries ``seq = n``, the id of the record, a UTC timestamp,
and ``prev`` = the hash of entry *n-1*. Its own ``entry_hash`` covers all of those. This is
a hash chain like a transparency log: the head hash commits to the entire history, and
reordering, inserting, deleting or editing any entry or record breaks verification at that
point. :func:`verify_chain` checks all of it.

Everything here is pure (no I/O). Storage backends (a directory, a git branch) live in
``colloid.adapters.lake``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA = "colloid.mutation/1"
GENESIS = "0" * 64
KINDS = ("gene", "program")
REQUIRED: dict[str, tuple[str, ...]] = {
    "gene": ("locus", "payload_kind", "payload", "explain", "provenance"),
    "program": ("target", "genes", "effects", "status", "platform"),
}


class ChainError(ValueError):
    """The ledger or a record failed verification (the message says exactly where)."""


def sanitize(obj: Any) -> Any:
    """Make a value canonical-JSON-safe: tuples become lists, non-finite floats become None,
    and mapping keys become strings. Applied before hashing so ids are stable."""
    if isinstance(obj, Mapping):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def record_id(kind: str, content: Mapping[str, Any]) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown record kind {kind!r}")
    return hashlib.sha256(SCHEMA.encode() + b"\0" + kind.encode() + b"\0" + canonical(content)).hexdigest()


def validate(kind: str, content: Mapping[str, Any]) -> None:
    missing = [k for k in REQUIRED[kind] if k not in content]
    if missing:
        raise ChainError(f"{kind} record is missing {missing}")
    canonical(content)  # raises on NaN / non-JSON values


@dataclass(frozen=True, slots=True)
class Record:
    kind: str
    content: dict[str, Any]

    @property
    def id(self) -> str:
        return record_id(self.kind, self.content)

    @staticmethod
    def make(kind: str, content: Mapping[str, Any]) -> Record:
        clean = sanitize(content)
        validate(kind, clean)
        return Record(kind, clean)

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "kind": self.kind, "id": self.id, "content": self.content}

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> Record:
        if data.get("schema") != SCHEMA:
            raise ChainError(f"unsupported schema {data.get('schema')!r}")
        rec = Record(str(data["kind"]), dict(data["content"]))
        if rec.id != data.get("id"):
            raise ChainError(f"record content does not hash to its id {str(data.get('id'))[:12]}")
        return rec


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    seq: int
    record: str  # record id
    kind: str
    prev: str  # entry_hash of seq-1 (GENESIS for seq 0)
    recorded_at: str  # ISO-8601 UTC, e.g. 2026-10-02T11:43:21Z
    entry_hash: str

    @staticmethod
    def digest(seq: int, record: str, kind: str, prev: str, recorded_at: str) -> str:
        body = {"seq": seq, "record": record, "kind": kind, "prev": prev, "recorded_at": recorded_at}
        return hashlib.sha256(b"colloid.ledger/1\0" + canonical(body)).hexdigest()

    @staticmethod
    def make(seq: int, record: str, kind: str, prev: str, recorded_at: str) -> LedgerEntry:
        return LedgerEntry(seq, record, kind, prev, recorded_at, LedgerEntry.digest(seq, record, kind, prev, recorded_at))

    def to_json(self) -> dict[str, Any]:
        return {"seq": self.seq, "record": self.record, "kind": self.kind, "prev": self.prev,
                "recorded_at": self.recorded_at, "entry_hash": self.entry_hash}

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> LedgerEntry:
        return LedgerEntry(int(data["seq"]), str(data["record"]), str(data["kind"]), str(data["prev"]),
                           str(data["recorded_at"]), str(data["entry_hash"]))


def head(entries: Sequence[LedgerEntry]) -> str:
    return entries[-1].entry_hash if entries else GENESIS


def append(entries: Sequence[LedgerEntry], records: Iterable[Record], recorded_at: str) -> list[LedgerEntry]:
    """Ledger entries for the records not already in the chain, in the given order. Records
    already present (by id) are skipped, which makes ingestion idempotent."""
    if entries and recorded_at < entries[-1].recorded_at:
        raise ChainError(f"timestamp {recorded_at} is older than the head ({entries[-1].recorded_at}); clocks must not run backwards")
    known = {e.record for e in entries}
    out: list[LedgerEntry] = []
    prev, seq = head(entries), len(entries)
    for rec in records:
        if rec.id in known:
            continue
        e = LedgerEntry.make(seq, rec.id, rec.kind, prev, recorded_at)
        out.append(e)
        known.add(rec.id)
        prev, seq = e.entry_hash, seq + 1
    return out


def verify_chain(entries: Sequence[LedgerEntry], records: Mapping[str, Record]) -> str:
    """Verify the whole lake and return its head hash. Checks, for every entry: contiguous
    ``seq``; ``prev`` equals the previous entry's hash; ``entry_hash`` recomputes; timestamps
    never decrease; the record exists, is of the declared kind and hashes to its id; and every
    program's gene references point at gene records recorded *earlier* in the chain."""
    prev = GENESIS
    last_time = ""
    position: dict[str, int] = {}
    for i, e in enumerate(entries):
        where = f"ledger entry {i}"
        if e.seq != i:
            raise ChainError(f"{where}: seq {e.seq} (expected {i}): entries missing or reordered")
        if e.prev != prev:
            raise ChainError(f"{where}: prev {e.prev[:12]} does not match the previous entry {prev[:12]}")
        if LedgerEntry.digest(e.seq, e.record, e.kind, e.prev, e.recorded_at) != e.entry_hash:
            raise ChainError(f"{where}: entry_hash does not recompute (entry edited)")
        if e.recorded_at < last_time:
            raise ChainError(f"{where}: timestamp {e.recorded_at} is older than its predecessor {last_time}")
        rec = records.get(e.record)
        if rec is None:
            raise ChainError(f"{where}: record {e.record[:12]} is missing from the lake")
        if rec.kind != e.kind or rec.id != e.record:
            raise ChainError(f"{where}: record {e.record[:12]} does not match the entry (kind or content changed)")
        if rec.kind == "program":
            for gid in rec.content.get("genes", []):
                if position.get(gid) is None:
                    raise ChainError(f"{where}: program references gene record {str(gid)[:12]} not recorded before it")
            for pid in rec.content.get("derived_from", []):
                if position.get(pid) is None:
                    raise ChainError(f"{where}: program derives from {str(pid)[:12]}, which is not recorded before it")
        position[e.record] = i
        prev, last_time = e.entry_hash, e.recorded_at
    return prev


def order(entries: Sequence[LedgerEntry], a: str, b: str) -> int:
    """-1 if record ``a`` entered the lake before ``b``, 1 if after, 0 if same; KeyError if absent."""
    pos = {e.record: e.seq for e in entries}
    return (pos[a] > pos[b]) - (pos[a] < pos[b])
