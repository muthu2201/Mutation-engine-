"""Mutation data lake service: ingest verified mutations from a run, and seed new runs.

**Ingest** (``colloid lake ingest RUN``). Every program a run *promoted*, or *verified*
(passed L6 while promotion was held), becomes one program record. Each of its genes becomes
a gene record. The evidence comes from the strongest measurement the run made of that
program: the post-run replicate and holdout from ``colloid verify`` when present, otherwise
the L6 holdout estimate. It carries the attribution the run measured (Shapley values and the
leave-one-gene-out ablation), the A/A noise floor and the platform fingerprint. Programs that
merely scored well at L5 are *not* ingested: the lake holds verified knowledge only.

**Seeding** (``lake:`` in an experiment). At the start of a run, the lake's program records
for the same target are checked against the *current* stack. Every gene's locus must exist
in the current Atlas, and a source gene must have been written against the exact source that
is there now (``base_hash``). Applicable programs are injected in generation 1 as
``lake_seed`` proposals, and they go through the full cascade like any other candidate. The
lake is a prior, never a verdict: a mutation verified on another machine, data set or day must
re-earn its place here.
"""

from __future__ import annotations

import datetime
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from colloid.adapters.store.sql_store import open_store
from colloid.core.atlas import StackAtlas
from colloid.core.ids import sha256_hex
from colloid.core.lake import ChainError, LedgerEntry, Record, append, head, verify_chain
from colloid.core.models import Gene, PayloadKind, ProgramStatus, Provenance, Stage, Verdict
from colloid.core.objectives import gain_percent
from colloid.ports import LakeStore
from colloid.services.report import _store_url, explain_gene

TARGET = "stackzero"


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def engine_commit() -> str | None:
    res = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"], capture_output=True, check=False)
    return res.stdout.decode().strip() or None


# ---------------------------------------------------------------------- records
def gene_record(gene: Gene, atlas: StackAtlas, explain: str, target: str = TARGET) -> Record:
    locus = atlas.loci[gene.locus_id]
    unit = atlas.units[locus.unit_id]
    return Record.make("gene", {
        "target": target,
        "locus": {"unit_id": unit.id, "unit": unit.name, "symbol_path": unit.symbol_path, "surface": locus.surface.value,
                  "layer": unit.layer.value, "kind": unit.kind.value, "language": unit.tags.get("language")},
        "payload_kind": gene.payload_kind.value,
        "payload": gene.payload,
        "explain": explain,
        "provenance": gene.provenance.model_dump(),
    })


def _effects_from_verification(v: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for obj, e in (v.get("replicate") or {}).items():
        out[obj] = {"gain_pct": e["gain_pct"], "ci_pct": e["ci_pct"], "p": e["p"]}
    return out


def _effects_from_evaluation(ev: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for est in ev.objectives:
        if est.reference == "baseline":
            out[est.objective] = {"gain_pct": round(gain_percent(est.log_ratio), 2),
                                  "ci_pct": [round(gain_percent(est.ci_lo), 2), round(gain_percent(est.ci_hi), 2)], "p": round(est.p_value, 5)}
    return out


@dataclass
class IngestReport:
    location: str
    programs: list[dict[str, Any]] = field(default_factory=list)
    new_records: int = 0
    new_entries: int = 0
    head: str = ""
    skipped: list[str] = field(default_factory=list)


def ingest_run(run: str, lake: LakeStore, *, recorded_at: str | None = None, log: Callable[[str], None] = print) -> IngestReport:
    store = open_store(_store_url(run))
    rep = IngestReport(lake.location)
    try:
        atlas = store.get_atlas()
        if atlas is None:
            raise ChainError(f"run {run} has no stored Atlas; nothing to ingest")
        setup = store.kv_get("setup") or {}
        aa = store.kv_get("aa_test") or {}
        verification = {r["program"]: r for r in (store.kv_get("verification") or {}).get("programs", [])}
        ablation = store.kv_get("ablation") or {}
        shapley = [a for a in store.attributions() if a.method.startswith("shapley")]
        baseline = next(p for p in store.programs(island="baseline"))
        existing = lake.records()
        entries = lake.entries()
        verify_chain(entries, existing)  # never append to a lake that does not verify
        new: dict[str, Record] = {}
        order: list[Record] = []
        programs = [*store.programs(status=ProgramStatus.PROMOTED), *store.programs(status=ProgramStatus.VERIFIED)]
        for prog in sorted(programs, key=lambda p: p.id):
            genes = store.genes(prog.gene_ids)
            v = verification.get(prog.id)
            l6 = [e for e in store.evaluations(prog.id) if e.stage == Stage.L6 and e.verdict == Verdict.PASS]
            if v and v.get("replicate"):
                effects, protocol = _effects_from_verification(v), f"replicate-x{(store.kv_get('verification') or {}).get('cycles')}"
                holdout = v.get("holdout")
            elif l6:
                effects, protocol, holdout = _effects_from_evaluation(l6[-1]), "L6-holdout", None
            else:
                rep.skipped.append(f"{prog.id[:10]}: no verified measurement")
                continue
            gene_ids: list[str] = []
            gene_rec_of: dict[str, str] = {}
            for g in genes:
                gr = gene_record(g, atlas, explain_gene(store, g.id, atlas))
                gene_ids.append(gr.id)
                gene_rec_of[g.id] = gr.id
                if gr.id not in existing and gr.id not in new:
                    new[gr.id] = gr
                    order.append(gr)
            attribution = [{"gene": gene_rec_of[a.gene_id], "method": a.method, "value": a.value, "ci": [a.ci_lo, a.ci_hi]}
                           for a in shapley if a.program_id == prog.id and a.gene_id in gene_rec_of]
            if ablation.get("program") == prog.id:
                by_short = {g.id[:8]: gene_rec_of[g.id] for g in genes}
                for row in ablation.get("genes", []):
                    if "contribution_log" in row and row["gene"] in by_short:
                        attribution.append({"gene": by_short[row["gene"]], "method": "leave_one_out", "value": row["contribution_log"],
                                            "ci": row["contribution_ci_log"]})
            gset = set(gene_ids)
            derived = sorted(rid for rid, r in {**existing, **new}.items()
                             if r.kind == "program" and r.content.get("target") == TARGET and set(r.content["genes"]) < gset)
            content = {
                "target": TARGET, "baseline_id": baseline.id, "program_id": prog.id, "genes": sorted(gene_ids),
                "status": prog.status.value, "island": prog.island, "operator": prog.operator,
                "effects": effects, "protocol": protocol, "holdout": holdout, "attribution": attribution,
                "noise_floor": {"per_cycle": aa.get("noise_floor_per_cycle"), "gate": aa.get("gate")},
                "platform": setup.get("fingerprint") or {}, "rate_rps": setup.get("rate_rps"),
                "run": Path(run).name, "engine_commit_at_ingest": engine_commit(), "derived_from": derived,
            }
            pr = Record.make("program", content)
            rep.programs.append({"record": pr.id, "program": prog.id, "status": prog.status.value,
                                 "cost_gain_pct": (effects.get("cost") or {}).get("gain_pct"), "new": pr.id not in existing})
            if pr.id not in existing and pr.id not in new:
                new[pr.id] = pr
                order.append(pr)
        new_entries = append(entries, order, recorded_at or utc_now())
        if new_entries:
            message = f"lake: +{len(new_entries)} records from run {Path(run).name} (seq {new_entries[0].seq}..{new_entries[-1].seq})"
            rep.head = lake.commit(order, new_entries, head(entries), message)
        else:
            rep.head = head(entries)
        rep.new_records, rep.new_entries = len(order), len(new_entries)
        log(f"lake {lake.location}: +{rep.new_entries} entries, head {rep.head[:16]}")
        return rep
    finally:
        store.close()


# ---------------------------------------------------------------------- verification / listing
def verify(lake: LakeStore) -> dict[str, Any]:
    entries, records = lake.entries(), lake.records()
    h = verify_chain(entries, records)
    orphans = sorted(set(records) - {e.record for e in entries})
    if orphans:
        raise ChainError(f"{len(orphans)} record(s) present but never entered in the ledger, e.g. {orphans[0][:12]}")
    return {"location": lake.location, "head": h, "entries": len(entries), "genes": sum(1 for e in entries if e.kind == "gene"),
            "programs": sum(1 for e in entries if e.kind == "program"),
            "oldest": entries[0].recorded_at if entries else None, "newest": entries[-1].recorded_at if entries else None}


def listing(lake: LakeStore) -> list[dict[str, Any]]:
    records = lake.records()
    rows = []
    for e in lake.entries():
        r = records[e.record]
        row: dict[str, Any] = {"seq": e.seq, "recorded_at": e.recorded_at, "kind": e.kind, "id": e.record, "entry_hash": e.entry_hash}
        if e.kind == "gene":
            row["what"] = r.content["explain"]
        else:
            cost = r.content["effects"].get("cost") or {}
            row["what"] = (f"{len(r.content['genes'])} genes, {r.content['status']}, cost {cost.get('gain_pct')}% "
                           f"CI {cost.get('ci_pct')} ({r.content.get('protocol')}), run {r.content.get('run')}")
            row["derived_from"] = r.content.get("derived_from", [])
        rows.append(row)
    return rows


# ---------------------------------------------------------------------- seeding
@dataclass
class Seed:
    record: str
    genes: list[Gene]
    cost_gain_pct: float | None
    recorded_at: str


def applicable_gene(content: dict[str, Any], atlas: StackAtlas, knob_of_locus: dict[str, str]) -> tuple[Gene | None, str]:
    """Rebuild a lake gene on the *current* Atlas, or say why it no longer applies."""
    loc = content["locus"]
    locus = next((lc for lc in atlas.loci.values() if lc.unit_id == loc["unit_id"] and lc.surface.value == loc["surface"]), None)
    if locus is None:
        return None, f"locus {loc['unit']} ({loc['surface']}) no longer exists"
    kind = PayloadKind(content["payload_kind"])
    payload = dict(content["payload"])
    if kind == PayloadKind.SOURCE:
        current = str(atlas.units[locus.unit_id].tags.get("baseline_source", ""))
        if payload.get("base_hash") != sha256_hex(current)[:16]:
            return None, f"{loc['unit']}: source changed since the mutation was verified (base_hash mismatch)"
    elif locus.id not in knob_of_locus:
        return None, f"{loc['unit']}: knob not present on this target"
    return Gene.make(locus.id, kind, payload, Provenance(**content["provenance"])), ""


def seeds(lake: LakeStore, atlas: StackAtlas, knob_of_locus: dict[str, str], *, top: int = 4, target: str = TARGET) -> tuple[list[Seed], list[str]]:
    """The lake's best verified programs for this target that apply to the current stack,
    newest evidence first among equals, best measured cost gain first."""
    records = lake.records()
    entries = lake.entries()
    verify_chain(entries, records)
    when = {e.record: e.recorded_at for e in entries}
    out: list[Seed] = []
    skipped: list[str] = []
    seen_sets: set[frozenset[str]] = set()
    for e in reversed(entries):  # newest first, so a re-measured program supersedes its older record
        r = records[e.record]
        if r.kind != "program" or r.content.get("target") != target:
            continue
        key = frozenset(r.content["genes"])
        if key in seen_sets:
            continue
        seen_sets.add(key)
        genes: list[Gene] = []
        why = ""
        for gid in r.content["genes"]:
            g, why = applicable_gene(records[gid].content, atlas, knob_of_locus)
            if g is None:
                break
            genes.append(g)
        if why:
            skipped.append(f"{e.record[:12]}: {why}")
            continue
        out.append(Seed(e.record, genes, (r.content["effects"].get("cost") or {}).get("gain_pct"), when[e.record]))
    out.sort(key=lambda s: -(s.cost_gain_pct if s.cost_gain_pct is not None else -1e9))
    return out[:top], skipped


__all__ = ["IngestReport", "LedgerEntry", "Seed", "ingest_run", "listing", "seeds", "verify"]
