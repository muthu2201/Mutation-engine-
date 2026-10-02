"""ProgramStore adapters: SQLite (single node, zero-ops) and PostgreSQL (shared, multi-worker).

Both adapters share one relational schema - a direct projection of the blueprint's data
model (section C) - and one implementation of every query; only the *dialect* differs
(placeholder style, JSON column type, upsert syntax, connection handling). The same
conformance kit (tests/adapters/test_store_conformance.py) runs against both.

Everything that matters for reproducibility is persisted:

* ``gene``        - content-addressed payloads + provenance (operator, model, prompt hash)
* ``program``     - ``hash(baseline, sorted gene ids)`` → genes, island, status
* ``lineage``     - child/parent edges with the operator and Δgenes
* ``evaluation``  - per-stage verdicts, metrics, objective estimates, env fingerprint
* ``attribution`` / ``epistasis`` - Shapley credits and measured ε_ij
* ``llm_call``    - model, params, prompt hash, response hash, tokens, cost
* ``alert``       - suspicion / canary / A-A / red-team events
* ``unit``, ``edge``, ``atlas_path``, ``locus``, ``leverage`` - the Stack Atlas
* ``archive_cell`` - MAP-Elites occupancy snapshots
* ``kv``          - engine state (bandit posteriors, run config, baseline metrics)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from colloid.core.atlas import StackAtlas
from colloid.core.models import (
    Alert,
    AtlasPath,
    AttributionRecord,
    Edge,
    EpistasisRecord,
    Evaluation,
    Gene,
    LeverageCurve,
    LLMCallRecord,
    Locus,
    Program,
    ProgramStatus,
    Unit,
)

TABLES = {
    "gene": "id TEXT PRIMARY KEY, locus_id TEXT NOT NULL, payload_kind TEXT NOT NULL, payload {J} NOT NULL, provenance {J} NOT NULL, created_at DOUBLE PRECISION NOT NULL",
    "program": "id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL, gene_ids {J} NOT NULL, island TEXT NOT NULL, generation INTEGER NOT NULL, parent_ids {J} NOT NULL, operator TEXT NOT NULL, status TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL",
    "lineage": "child_id TEXT NOT NULL, parent_id TEXT NOT NULL, op TEXT NOT NULL, delta_genes {J} NOT NULL, PRIMARY KEY (child_id, parent_id)",
    "evaluation": "id TEXT PRIMARY KEY, program_id TEXT NOT NULL, stage TEXT NOT NULL, protocol_id TEXT NOT NULL, verdict TEXT NOT NULL, reasons {J} NOT NULL, metrics {J} NOT NULL, objectives {J} NOT NULL, env_fingerprint {J} NOT NULL, raw_ref TEXT, cost_usd DOUBLE PRECISION NOT NULL, duration_s DOUBLE PRECISION NOT NULL, created_at DOUBLE PRECISION NOT NULL",
    "attribution": "program_id TEXT NOT NULL, gene_id TEXT NOT NULL, method TEXT NOT NULL, objective TEXT NOT NULL, value DOUBLE PRECISION NOT NULL, ci_lo DOUBLE PRECISION NOT NULL, ci_hi DOUBLE PRECISION NOT NULL, PRIMARY KEY (program_id, gene_id, method, objective)",
    "epistasis": "gene_a TEXT NOT NULL, gene_b TEXT NOT NULL, objective TEXT NOT NULL, epsilon DOUBLE PRECISION NOT NULL, ci_lo DOUBLE PRECISION NOT NULL, ci_hi DOUBLE PRECISION NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (gene_a, gene_b, objective)",
    "llm_call": "id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, template TEXT NOT NULL, params {J} NOT NULL, prompt_hash TEXT NOT NULL, response_hash TEXT NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, latency_s DOUBLE PRECISION NOT NULL, cost_usd DOUBLE PRECISION NOT NULL, ok BOOLEAN NOT NULL, error TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL",
    "alert": "id TEXT PRIMARY KEY, kind TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL, program_id TEXT, created_at DOUBLE PRECISION NOT NULL",
    "kv": "key TEXT PRIMARY KEY, value {J} NOT NULL, updated_at DOUBLE PRECISION NOT NULL",
    "signature": "program_id TEXT PRIMARY KEY, sig TEXT NOT NULL",
    "unit": "id TEXT PRIMARY KEY, kind TEXT NOT NULL, layer TEXT NOT NULL, name TEXT NOT NULL, parent_id TEXT, symbol_path TEXT NOT NULL, content_hash TEXT NOT NULL, adapter TEXT NOT NULL, tags {J} NOT NULL",
    "edge": "src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, weight DOUBLE PRECISION NOT NULL, source TEXT NOT NULL, observed_at DOUBLE PRECISION NOT NULL",
    "atlas_path": "id TEXT PRIMARY KEY, kind TEXT NOT NULL, unit_ids {J} NOT NULL, weight DOUBLE PRECISION NOT NULL, workload_id TEXT NOT NULL",
    "locus": "id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, surface TEXT NOT NULL, risk_class TEXT NOT NULL, mutability TEXT NOT NULL",
    "leverage": "unit_id TEXT NOT NULL, workload_id TEXT NOT NULL, curve {J} NOT NULL, ci {J} NOT NULL, PRIMARY KEY (unit_id, workload_id)",
    "unit_dynamic": "unit_id TEXT PRIMARY KEY, tags {J} NOT NULL",
    "archive_cell": "island TEXT NOT NULL, descriptor TEXT NOT NULL, program_id TEXT NOT NULL, fitness {J} NOT NULL, score DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (island, descriptor)",
}
INDEXES = [
    "CREATE INDEX IF NOT EXISTS evaluation_program ON evaluation (program_id, stage)",
    "CREATE INDEX IF NOT EXISTS program_island ON program (island, status)",
    "CREATE INDEX IF NOT EXISTS lineage_parent ON lineage (parent_id)",
    "CREATE INDEX IF NOT EXISTS edge_src ON edge (src)",
]


def _j(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=lambda o: o.model_dump(mode="json") if hasattr(o, "model_dump") else str(o))


class _SQLStore:
    """Dialect-independent query implementation. Subclasses provide ``_conn()``,
    ``ph`` (placeholder) and ``json_type``."""

    PORT_API = "1.0.0"
    ph = "?"
    json_type = "TEXT"

    # -- dialect hooks ---------------------------------------------------------------
    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        raise NotImplementedError

    def _load(self, value: Any) -> Any:
        return json.loads(value) if isinstance(value, (str, bytes)) else value

    def _q(self, sql: str) -> str:
        return sql.replace("?", self.ph)

    def _upsert(self, table: str, cols: Sequence[str], keys: Sequence[str]) -> str:
        placeholders = ", ".join(["?"] * len(cols))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in keys)
        conflict = f"ON CONFLICT ({', '.join(keys)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
        return self._q(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) {conflict}")

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._cursor() as cur:
            cur.execute(sql, tuple(params))

    def _many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._cursor() as cur:
            cur.executemany(sql, [tuple(r) for r in rows])

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._cursor() as cur:
            cur.execute(self._q(sql), tuple(params))
            return list(cur.fetchall())

    def init_schema(self) -> None:
        with self._cursor() as cur:
            for name, cols in TABLES.items():
                cur.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols.format(J=self.json_type)})")
            for idx in INDEXES:
                cur.execute(idx)

    # -- genes & programs -------------------------------------------------------------
    def put_gene(self, gene: Gene) -> None:
        sql = self._q(
            "INSERT INTO gene (id, locus_id, payload_kind, payload, provenance, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO NOTHING"
        )
        self._exec(sql, (gene.id, gene.locus_id, gene.payload_kind.value, _j(gene.payload), _j(gene.provenance.model_dump(mode="json")), time.time()))

    def get_gene(self, gene_id: str) -> Gene | None:
        rows = self._all("SELECT id, locus_id, payload_kind, payload, provenance FROM gene WHERE id = ?", (gene_id,))
        if not rows:
            return None
        r = rows[0]
        return Gene.model_validate({"id": r[0], "locus_id": r[1], "payload_kind": r[2], "payload": self._load(r[3]), "provenance": self._load(r[4])})

    def genes(self, gene_ids: Sequence[str]) -> list[Gene]:
        out = []
        for gid in gene_ids:
            g = self.get_gene(gid)
            if g is None:
                raise KeyError(f"gene {gid} not in store")
            out.append(g)
        return out

    def put_program(self, program: Program) -> None:
        cols = ("id", "baseline_id", "gene_ids", "island", "generation", "parent_ids", "operator", "status", "created_at")
        sql = self._q(
            f"INSERT INTO program ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))}) ON CONFLICT (id) DO NOTHING"
        )
        self._exec(
            sql,
            (
                program.id,
                program.baseline_id,
                _j(list(program.gene_ids)),
                program.island,
                program.generation,
                _j(list(program.parent_ids)),
                program.operator,
                program.status.value,
                program.created_at,
            ),
        )

    def _program(self, r: tuple[Any, ...]) -> Program:
        return Program(
            id=r[0],
            baseline_id=r[1],
            gene_ids=tuple(self._load(r[2])),
            island=r[3],
            generation=int(r[4]),
            parent_ids=tuple(self._load(r[5])),
            operator=r[6],
            status=ProgramStatus(r[7]),
            created_at=float(r[8]),
        )

    _PROGRAM_COLS = "id, baseline_id, gene_ids, island, generation, parent_ids, operator, status, created_at"

    def get_program(self, program_id: str) -> Program | None:
        rows = self._all(f"SELECT {self._PROGRAM_COLS} FROM program WHERE id = ?", (program_id,))
        return self._program(rows[0]) if rows else None

    def set_status(self, program_id: str, status: ProgramStatus) -> None:
        self._exec(self._q("UPDATE program SET status = ? WHERE id = ?"), (status.value, program_id))

    def programs(self, *, island: str | None = None, status: ProgramStatus | None = None, limit: int = 10_000) -> list[Program]:
        where, params = [], []
        if island is not None:
            where.append("island = ?")
            params.append(island)
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(f"SELECT {self._PROGRAM_COLS} FROM program {clause} ORDER BY created_at LIMIT {int(limit)}", params)
        return [self._program(r) for r in rows]

    def count_programs(self) -> dict[str, int]:
        return {r[0]: int(r[1]) for r in self._all("SELECT status, COUNT(*) FROM program GROUP BY status")}

    def put_lineage(self, child_id: str, parent_id: str, op: str, delta_genes: Sequence[str]) -> None:
        sql = self._q("INSERT INTO lineage (child_id, parent_id, op, delta_genes) VALUES (?, ?, ?, ?) ON CONFLICT (child_id, parent_id) DO NOTHING")
        self._exec(sql, (child_id, parent_id, op, _j(list(delta_genes))))

    def lineage(self, program_id: str) -> list[dict[str, Any]]:
        """Ancestry of ``program_id`` (walking parent edges back to the baseline)."""
        out, frontier, seen = [], [program_id], {program_id}
        while frontier:
            cur = frontier.pop()
            for child, parent, op, delta in self._all("SELECT child_id, parent_id, op, delta_genes FROM lineage WHERE child_id = ?", (cur,)):
                out.append({"child": child, "parent": parent, "op": op, "delta_genes": self._load(delta)})
                if parent not in seen:
                    seen.add(parent)
                    frontier.append(parent)
        return out

    def children(self, program_id: str) -> list[str]:
        return [r[0] for r in self._all("SELECT child_id FROM lineage WHERE parent_id = ?", (program_id,))]

    # -- evaluations ------------------------------------------------------------------
    def put_evaluation(self, ev: Evaluation) -> None:
        cols = ("id", "program_id", "stage", "protocol_id", "verdict", "reasons", "metrics", "objectives", "env_fingerprint", "raw_ref", "cost_usd", "duration_s", "created_at")
        self._exec(
            self._upsert("evaluation", cols, ("id",)),
            (
                ev.id,
                ev.program_id,
                ev.stage.value,
                ev.protocol_id,
                ev.verdict.value,
                _j(list(ev.reasons)),
                _j({k: v.model_dump(mode="json") for k, v in ev.metrics.items()}),
                _j([o.model_dump(mode="json") for o in ev.objectives]),
                _j(ev.env_fingerprint),
                ev.raw_ref,
                ev.cost_usd,
                ev.duration_s,
                ev.created_at,
            ),
        )

    def evaluations(self, program_id: str | None = None, *, stage: str | None = None, limit: int = 100_000) -> list[Evaluation]:
        where, params = [], []
        if program_id is not None:
            where.append("program_id = ?")
            params.append(program_id)
        if stage is not None:
            where.append("stage = ?")
            params.append(stage)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(
            "SELECT id, program_id, stage, protocol_id, verdict, reasons, metrics, objectives, env_fingerprint, raw_ref, cost_usd, duration_s, created_at "
            f"FROM evaluation {clause} ORDER BY created_at LIMIT {int(limit)}",
            params,
        )
        out = []
        for r in rows:
            out.append(
                Evaluation.model_validate(
                    {
                        "id": r[0],
                        "program_id": r[1],
                        "stage": r[2],
                        "protocol_id": r[3],
                        "verdict": r[4],
                        "reasons": self._load(r[5]),
                        "metrics": self._load(r[6]),
                        "objectives": self._load(r[7]),
                        "env_fingerprint": self._load(r[8]),
                        "raw_ref": r[9],
                        "cost_usd": r[10],
                        "duration_s": r[11],
                        "created_at": r[12],
                    }
                )
            )
        return out

    # -- attribution / epistasis ---------------------------------------------------------
    def put_attribution(self, rec: AttributionRecord) -> None:
        cols = ("program_id", "gene_id", "method", "objective", "value", "ci_lo", "ci_hi")
        self._exec(self._upsert("attribution", cols, cols[:4]), (rec.program_id, rec.gene_id, rec.method, rec.objective, rec.value, rec.ci_lo, rec.ci_hi))

    def attributions(self, program_id: str | None = None) -> list[AttributionRecord]:
        sql = "SELECT program_id, gene_id, method, objective, value, ci_lo, ci_hi FROM attribution"
        rows = self._all(sql + (" WHERE program_id = ?" if program_id else ""), (program_id,) if program_id else ())
        return [AttributionRecord(program_id=r[0], gene_id=r[1], method=r[2], objective=r[3], value=r[4], ci_lo=r[5], ci_hi=r[6]) for r in rows]

    def put_epistasis(self, rec: EpistasisRecord) -> None:
        cols = ("gene_a", "gene_b", "objective", "epsilon", "ci_lo", "ci_hi", "n")
        a, b = sorted((rec.gene_a, rec.gene_b))
        self._exec(self._upsert("epistasis", cols, cols[:3]), (a, b, rec.objective, rec.epsilon, rec.ci_lo, rec.ci_hi, rec.n))

    def epistasis(self) -> list[EpistasisRecord]:
        rows = self._all("SELECT gene_a, gene_b, objective, epsilon, ci_lo, ci_hi, n FROM epistasis")
        return [EpistasisRecord(gene_a=r[0], gene_b=r[1], objective=r[2], epsilon=r[3], ci_lo=r[4], ci_hi=r[5], n=r[6]) for r in rows]

    # -- llm calls, alerts, kv -------------------------------------------------------------
    def put_llm_call(self, rec: LLMCallRecord) -> None:
        cols = ("id", "provider", "model", "template", "params", "prompt_hash", "response_hash", "tokens_in", "tokens_out", "latency_s", "cost_usd", "ok", "error", "created_at")
        self._exec(
            self._upsert("llm_call", cols, ("id",)),
            (rec.id, rec.provider, rec.model, rec.template, _j(rec.params), rec.prompt_hash, rec.response_hash, rec.tokens_in, rec.tokens_out, rec.latency_s, rec.cost_usd, rec.ok, rec.error, rec.created_at),
        )

    def llm_calls(self, limit: int = 10_000) -> list[LLMCallRecord]:
        rows = self._all(
            "SELECT id, provider, model, template, params, prompt_hash, response_hash, tokens_in, tokens_out, latency_s, cost_usd, ok, error, created_at "
            f"FROM llm_call ORDER BY created_at LIMIT {int(limit)}"
        )
        return [
            LLMCallRecord(
                id=r[0], provider=r[1], model=r[2], template=r[3], params=self._load(r[4]), prompt_hash=r[5], response_hash=r[6],
                tokens_in=r[7], tokens_out=r[8], latency_s=r[9], cost_usd=r[10], ok=bool(r[11]), error=r[12], created_at=r[13],
            )
            for r in rows
        ]

    def put_alert(self, alert: Alert) -> None:
        cols = ("id", "kind", "severity", "message", "program_id", "created_at")
        self._exec(self._upsert("alert", cols, ("id",)), (alert.id, alert.kind, alert.severity, alert.message, alert.program_id, alert.created_at))

    def alerts(self, limit: int = 1000) -> list[Alert]:
        rows = self._all(f"SELECT id, kind, severity, message, program_id, created_at FROM alert ORDER BY created_at DESC LIMIT {int(limit)}")
        return [Alert(id=r[0], kind=r[1], severity=r[2], message=r[3], program_id=r[4], created_at=r[5]) for r in rows]

    def kv_get(self, key: str) -> Any:
        rows = self._all("SELECT value FROM kv WHERE key = ?", (key,))
        return self._load(rows[0][0]) if rows else None

    def kv_set(self, key: str, value: Any) -> None:
        self._exec(self._upsert("kv", ("key", "value", "updated_at"), ("key",)), (key, _j(value), time.time()))

    def put_signature(self, program_id: str, signature_hex: str) -> None:
        self._exec(self._upsert("signature", ("program_id", "sig"), ("program_id",)), (program_id, signature_hex))

    def signatures(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self._all("SELECT program_id, sig FROM signature")]

    # -- atlas ---------------------------------------------------------------------------
    def put_atlas(self, atlas: StackAtlas) -> None:
        with self._cursor() as cur:
            for t in ("unit", "edge", "atlas_path", "locus", "leverage", "unit_dynamic"):
                cur.execute(f"DELETE FROM {t}")
        now = time.time()
        self._many(
            self._q("INSERT INTO unit (id, kind, layer, name, parent_id, symbol_path, content_hash, adapter, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"),
            [(u.id, u.kind.value, u.layer.value, u.name, u.parent_id, u.symbol_path, u.content_hash, u.adapter, _j(u.tags)) for u in atlas.units.values()],
        )
        self._many(
            self._q("INSERT INTO edge (src, dst, kind, weight, source, observed_at) VALUES (?, ?, ?, ?, ?, ?)"),
            [(e.src, e.dst, e.kind.value, e.weight, e.source.value, now) for e in atlas.edges],
        )
        self._many(
            self._q("INSERT INTO atlas_path (id, kind, unit_ids, weight, workload_id) VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING"),
            [(p.id, p.kind.value, _j(list(p.unit_ids)), p.weight, p.workload_id) for p in atlas.paths],
        )
        self._many(
            self._q("INSERT INTO locus (id, unit_id, surface, risk_class, mutability) VALUES (?, ?, ?, ?, ?)"),
            [(lc.id, lc.unit_id, lc.surface.value, lc.risk_class.value, lc.mutability.value) for lc in atlas.loci.values()],
        )
        self._many(
            self._q("INSERT INTO leverage (unit_id, workload_id, curve, ci) VALUES (?, ?, ?, ?)"),
            [(c.unit_id, c.workload_id, _j({"points": c.points, "slope": c.slope}), _j(list(c.slope_ci))) for c in atlas.leverage.values()],
        )
        self._many(
            self._q("INSERT INTO unit_dynamic (unit_id, tags) VALUES (?, ?)"),
            [(uid, _j(dict(tags))) for uid, tags in atlas.dynamic.items() if tags],
        )

    def get_atlas(self) -> StackAtlas | None:
        units = self._all("SELECT id, kind, layer, name, parent_id, symbol_path, content_hash, adapter, tags FROM unit")
        if not units:
            return None
        atlas = StackAtlas()
        for r in units:
            atlas.units[r[0]] = Unit(id=r[0], kind=r[1], layer=r[2], name=r[3], parent_id=r[4], symbol_path=r[5], content_hash=r[6], adapter=r[7], tags=self._load(r[8]))
        atlas.edges = [Edge(src=r[0], dst=r[1], kind=r[2], weight=r[3], source=r[4]) for r in self._all("SELECT src, dst, kind, weight, source FROM edge")]
        for r in self._all("SELECT id, kind, unit_ids, weight, workload_id FROM atlas_path"):
            atlas.paths.append(AtlasPath(id=r[0], kind=r[1], unit_ids=tuple(self._load(r[2])), weight=r[3], workload_id=r[4]))
        for r in self._all("SELECT id, unit_id, surface, risk_class, mutability FROM locus"):
            atlas.loci[r[0]] = Locus(id=r[0], unit_id=r[1], surface=r[2], risk_class=r[3], mutability=r[4])
        for r in self._all("SELECT unit_id, workload_id, curve, ci FROM leverage"):
            curve = self._load(r[2])
            atlas.leverage[r[0]] = LeverageCurve(
                unit_id=r[0], workload_id=r[1], points=tuple(tuple(p) for p in curve["points"]), slope=curve["slope"], slope_ci=tuple(self._load(r[3]))
            )
        for r in self._all("SELECT unit_id, tags FROM unit_dynamic"):
            atlas.dynamic[r[0]].update(self._load(r[1]))
        return atlas

    # -- archive -----------------------------------------------------------------------
    def put_archive_cell(self, island: str, descriptor: str, program_id: str, fitness: dict[str, Any], score: float) -> None:
        cols = ("island", "descriptor", "program_id", "fitness", "score", "updated_at")
        self._exec(self._upsert("archive_cell", cols, ("island", "descriptor")), (island, descriptor, program_id, _j(fitness), score, time.time()))

    def archive_cells(self, island: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT island, descriptor, program_id, fitness, score FROM archive_cell"
        rows = self._all(sql + (" WHERE island = ?" if island else ""), (island,) if island else ())
        return [{"island": r[0], "descriptor": r[1], "program_id": r[2], "fitness": self._load(r[3]), "score": r[4]} for r in rows]

    def close(self) -> None:
        pass


class SQLiteStore(_SQLStore):
    """Single-file store in WAL mode. Thread-safe (one connection guarded by a lock) and
    multi-process-safe (WAL + busy timeout)."""

    ph = "?"
    json_type = "TEXT"

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=60.0, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=60000")
        self._db.execute("PRAGMA foreign_keys=OFF")
        self._lock = threading.RLock()
        self.init_schema()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        with self._lock:
            cur = self._db.cursor()
            for attempt in range(20):
                try:
                    cur.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == 19:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            try:
                yield cur
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()


class PostgresStore(_SQLStore):
    """PostgreSQL store (JSONB columns, connection pool). Use for multi-worker deployments."""

    ph = "%s"
    json_type = "JSONB"

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 8) -> None:
        from psycopg_pool import ConnectionPool

        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True, kwargs={"autocommit": False})
        self.init_schema()

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            yield cur

    def _exec(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._cursor() as cur:
            cur.execute(sql, tuple(params))

    def close(self) -> None:
        self._pool.close()


def open_store(url: str) -> _SQLStore:
    """``sqlite:///path/to/file.db`` or ``postgresql://...``."""
    if url.startswith("sqlite:///"):
        # sqlite:///relative/path.db  or  sqlite:////absolute/path.db
        return SQLiteStore(url[len("sqlite:///") :])
    if url.startswith(("postgresql://", "postgres://")):
        return PostgresStore(url)
    raise ValueError(f"unsupported store url {url!r}")
