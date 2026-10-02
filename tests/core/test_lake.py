"""Mutation data lake (core): content addressing, the hash chain, and tamper detection."""

import dataclasses

import pytest

from colloid.core.lake import (
    GENESIS,
    ChainError,
    LedgerEntry,
    Record,
    append,
    head,
    order,
    record_id,
    sanitize,
    verify_chain,
)

T0, T1, T2 = "2026-10-01T09:00:00.000000Z", "2026-10-02T09:00:00.000000Z", "2026-10-03T09:00:00.000000Z"


def gene(n: int) -> Record:
    return Record.make("gene", {"target": "t", "locus": {"unit_id": f"u{n}", "surface": "code_region"}, "payload_kind": "value",
                                "payload": {"value": n}, "explain": f"gene {n}", "provenance": {"operator": "test"}})


def program(genes: list[Record], gain: float, derived: list[str] = ()) -> Record:
    return Record.make("program", {"target": "t", "genes": sorted(g.id for g in genes), "effects": {"cost": {"gain_pct": gain}},
                                   "status": "promoted", "platform": {"os": "linux"}, "derived_from": list(derived)})


def build():
    g1, g2 = gene(1), gene(2)
    p1 = program([g1], 10.0)
    p2 = program([g1, g2], 25.0, [p1.id])
    e1 = append([], [g1, p1], T0)
    e2 = e1 + append(e1, [g2, p2], T1)
    return e2, {r.id: r for r in (g1, g2, p1, p2)}, (g1, g2, p1, p2)


def test_content_addressing_is_canonical_and_kind_scoped():
    a = Record.make("gene", {"b": 1, "a": [1.0, (2, 3)], "explain": "x", "locus": {}, "payload_kind": "v", "payload": {}, "provenance": {}})
    b = Record.make("gene", {"payload": {}, "provenance": {}, "payload_kind": "v", "locus": {}, "explain": "x", "a": [1.0, [2, 3]], "b": 1})
    assert a.id == b.id  # key order and tuple-vs-list do not matter
    assert record_id("gene", {"x": 1}) != record_id("program", {"x": 1})
    assert sanitize({"x": float("nan"), 1: float("inf")}) == {"x": None, "1": None}
    assert Record.from_json(a.to_json()) == a


def test_missing_required_fields_rejected():
    with pytest.raises(ChainError, match="missing"):
        Record.make("program", {"target": "t"})


def test_chain_verifies_and_orders():
    entries, records, (g1, g2, p1, p2) = build()
    assert [e.seq for e in entries] == [0, 1, 2, 3]
    assert entries[0].prev == GENESIS and verify_chain(entries, records) == head(entries)
    assert order(entries, p1.id, p2.id) == -1 and order(entries, p2.id, p1.id) == 1 and order(entries, g1.id, g1.id) == 0


def test_append_is_idempotent_and_refuses_backwards_clock():
    entries, _, (g1, g2, p1, p2) = build()
    assert append(entries, [g1, p2], T2) == []
    with pytest.raises(ChainError, match="older than the head"):
        append(entries, [gene(9)], T0)


@pytest.mark.parametrize("attack", ["edit_record", "edit_entry", "reorder", "delete", "rewind_clock", "forward_ref", "swap_kind"])
def test_every_kind_of_tampering_is_detected(attack):
    entries, records, (g1, g2, p1, p2) = build()
    entries = list(entries)
    if attack == "edit_record":  # change a measured effect but keep the id
        records[p1.id] = Record("program", {**p1.content, "effects": {"cost": {"gain_pct": 99.0}}})
    elif attack == "edit_entry":
        entries[1] = dataclasses.replace(entries[1], recorded_at=T2)
    elif attack == "reorder":
        entries[2], entries[3] = entries[3], entries[2]
    elif attack == "delete":
        del entries[2]
    elif attack == "rewind_clock":  # re-hash consistently, but time runs backwards
        e = entries[3]
        entries[3] = LedgerEntry.make(e.seq, e.record, e.kind, e.prev, T0)
        entries[2] = LedgerEntry.make(entries[2].seq, entries[2].record, entries[2].kind, entries[2].prev, T2)
        entries[3] = LedgerEntry.make(3, e.record, e.kind, entries[2].entry_hash, T0)
    elif attack == "forward_ref":  # a program recorded before one of its genes
        g3 = gene(3)
        p3 = program([g3], 5.0)
        records[g3.id], records[p3.id] = g3, p3
        entries += append(entries, [p3, g3], T2)
    elif attack == "swap_kind":
        e = entries[0]
        entries[0] = LedgerEntry.make(0, e.record, "program", e.prev, e.recorded_at)
    with pytest.raises(ChainError):
        verify_chain(entries, records)
