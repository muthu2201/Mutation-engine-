"""PostgreSQL cluster management for the StackZero target.

The cluster is part of the *target* (Colloid mutates its configuration and index set), so
it is managed with the same care as a candidate:

* It runs as the ``postgres`` OS user inside its own cgroups (CPU accounting for the
  database's share of "CPU per request"; PSS sampling for memory) and its own empty network
  namespace: ``listen_addresses = ''`` and a Unix socket only.
* Authentication separates the evaluator from candidates: the superuser needs a SCRAM
  password stored in a root-only file; the unprivileged ``shop`` role (the only role a
  candidate can use) owns nothing and can only read/write the shop tables.
* Data is synthetic (``seed.sql``). A *template* database is built once; every benchmark
  comparison and every correctness check runs on a throwaway clone
  (``CREATE DATABASE ... TEMPLATE``), so candidates never share mutable state and writes
  cannot leak between evaluations (risk class C isolation).
* Postmaster-level GUCs (``shared_buffers``) are applied by restarting the cluster;
  session-level GUCs are passed per connection by the service and need no restart.
* ``autovacuum`` is off on the benchmark cluster: a background vacuum kicking in during one
  arm of an A/B comparison is measurement noise, not a property of the candidate.
* **One owner.** A cluster belongs to one process at a time (an exclusive lock next to its
  data directory). Inside that process every target shares one cluster object
  (:meth:`PostgresCluster.shared`). A second process that tries to start the cluster fails
  with the owner's pid instead of stopping it, so an experiment cannot be killed by a test,
  a bake-off or another run that happens to use the same state directory.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg

from colloid.adapters.sandbox.linux import LinuxProcess, LinuxSandbox
from colloid.ports import SandboxSpec

PG_BIN = Path("/usr/lib/postgresql/16/bin")
TEMPLATE_DB = "shop_template"


class PostgresError(RuntimeError):
    pass


_SHARED: dict[Path, PostgresCluster] = {}
_OWNER_LOCKS: dict[Path, int] = {}


def _take_ownership(root: Path) -> None:
    """Hold the cluster's owner lock for the rest of this process's life (released by exit)."""
    key = root.resolve()
    if key in _OWNER_LOCKS or not sys.platform.startswith("linux"):
        return
    import fcntl

    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / "owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner = os.pread(fd, 64, 0).decode(errors="replace").strip() or "unknown"
        os.close(fd)
        raise PostgresError(f"the evaluation cluster at {root} is owned by another Colloid process (pid {owner}); "
                            "wait for it to finish - starting it here would stop that process's database") from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    _OWNER_LOCKS[key] = fd


class PostgresCluster:
    @classmethod
    def shared(cls, sandbox: LinuxSandbox, root: Path) -> PostgresCluster:
        """The process-wide cluster object for ``root`` (every target of the process uses it)."""
        key = Path(root).resolve()
        if key not in _SHARED:
            _SHARED[key] = cls(sandbox, root)
        return _SHARED[key]

    def __init__(self, sandbox: LinuxSandbox, root: Path = Path("/opt/colloid/state/pg"), schema_dir: Path | None = None) -> None:
        self.sandbox = sandbox
        self.root = root
        self.data = root / "data"
        self.socket_dir = root / "sock"
        self.pw_file = root / "superuser.pw"
        self.schema_dir = schema_dir
        self.proc: LinuxProcess | None = None
        self.current_gucs: dict[str, Any] = {}
        self.current_cpus: str | None = None
        self._ids: tuple[int, int] | None = None

    @property
    def uid(self) -> int:
        return self._postgres_ids()[0]

    @property
    def gid(self) -> int:
        return self._postgres_ids()[1]

    def _postgres_ids(self) -> tuple[int, int]:
        """The ``postgres`` OS account, resolved when the cluster is first used, so the target
        (Atlas, knobs, regions) can be constructed on hosts without it (macOS, Windows, CI)."""
        if self._ids is None:
            import pwd  # POSIX-only: imported where used

            pw = pwd.getpwnam("postgres")
            self._ids = (pw.pw_uid, pw.pw_gid)
        return self._ids

    # ------------------------------------------------------------------ setup
    def _as_postgres(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        def demote() -> None:
            os.setgid(self.gid)
            os.setuid(self.uid)

        return subprocess.run(argv, preexec_fn=demote, capture_output=True, text=True, check=False, cwd="/tmp")

    def initialized(self) -> bool:
        return (self.data / "PG_VERSION").exists()

    def init(self) -> None:
        if self.initialized():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o755)
        password = secrets.token_urlsafe(24)
        self.pw_file.write_text(password)
        os.chmod(self.pw_file, 0o600)
        tmp_pw = self.root / ".initpw"
        tmp_pw.write_text(password)
        os.chown(tmp_pw, self.uid, self.gid)
        self.data.mkdir(parents=True, exist_ok=True)
        os.chown(self.data, self.uid, self.gid)
        proc = self._as_postgres(
            [str(PG_BIN / "initdb"), "-D", str(self.data), "-U", "postgres", "--auth-local=scram-sha-256", f"--pwfile={tmp_pw}", "-E", "UTF8", "--locale=C.UTF-8"]
        )
        tmp_pw.unlink()
        if proc.returncode != 0:
            raise PostgresError(f"initdb failed: {proc.stderr}")
        self.socket_dir.mkdir(exist_ok=True)
        os.chown(self.socket_dir, self.uid, self.gid)
        os.chmod(self.socket_dir, 0o755)
        conf = self.data / "postgresql.conf"
        with open(conf, "a") as fh:
            fh.write(
                "\n# --- colloid ---\n"
                "listen_addresses = ''\n"
                f"unix_socket_directories = '{self.socket_dir}'\n"
                "unix_socket_permissions = 0777\n"
                "max_connections = 200\n"
                "shared_preload_libraries = 'pg_stat_statements'\n"
                "autovacuum = off\n"
                "timezone = 'UTC'\n"
                "log_min_messages = warning\n"
                "logging_collector = off\n"
                "jit = on\n"
            )
        (self.data / "pg_hba.conf").write_text(
            "# colloid: superuser needs a password; the unprivileged shop role is trusted on the local socket\n"
            "local all postgres scram-sha-256\n"
            "local all shop trust\n"
        )
        os.chown(self.data / "pg_hba.conf", self.uid, self.gid)

    # ------------------------------------------------------------------ lifecycle
    def running(self) -> bool:
        return self.proc is not None and self.proc.alive()

    def start(self, gucs: Mapping[str, Any] | None = None, cpus: str | None = None, timeout: float = 60.0) -> None:
        if self.running():
            return
        _take_ownership(self.root)
        self._stop_orphan()
        args = [str(PG_BIN / "postgres"), "-D", str(self.data)]
        for k, v in (gucs or {}).items():
            args += ["-c", f"{k}={v}"]
        log = self.root / "postgres.log"
        log.touch()
        os.chown(log, self.uid, self.gid)
        spec = SandboxSpec(
            argv=tuple(args),
            cwd=str(self.data),
            env={"LANG": "C.UTF-8"},
            risk_class="A",
            network=False,
            memory_limit_mb=8192,
            pids_limit=512,
            cpu_seconds=10**8,
            wall_seconds=0,
            file_size_mb=1 << 20,
            open_files=65536,
            cpus=cpus,
            run_as_sandbox_user=False,
            user="postgres",
            stdout_path=str(log),
            label="pg",
        )
        self.proc = self.sandbox.spawn(spec)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.proc.alive():
                raise PostgresError(f"postgres exited early: {log.read_text()[-3000:]}")
            try:
                with self.superuser("postgres") as conn:
                    conn.execute("SELECT 1")
                self.current_gucs = dict(gucs or {})
                self.current_cpus = cpus
                return
            except psycopg.OperationalError:
                time.sleep(0.2)
        raise PostgresError("postgres did not become ready")

    def _stop_orphan(self) -> None:
        pid_file = self.data / "postmaster.pid"
        if not pid_file.exists():
            return
        try:
            pid = int(pid_file.read_text().split()[0])
            os.kill(pid, 2)  # SIGINT = fast shutdown
            for _ in range(100):
                os.kill(pid, 0)
                time.sleep(0.1)
        except (ProcessLookupError, ValueError):
            pass

    def stop(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.proc.pid, 2)
        self.proc.wait(timeout=30)
        self.proc.kill()
        self.proc = None

    def ensure(self, gucs: Mapping[str, Any], cpus: str | None) -> bool:
        """Make the running cluster match ``gucs`` / ``cpus``; restart only when needed.
        Returns True if a restart happened."""
        wanted = dict(gucs)
        if self.running() and wanted == self.current_gucs and cpus == self.current_cpus:
            return False
        self.stop()
        self.start(wanted, cpus)
        return True

    # ------------------------------------------------------------------ connections
    def superuser(self, dbname: str) -> psycopg.Connection[Any]:
        password = self.pw_file.read_text().strip()
        return psycopg.connect(host=str(self.socket_dir), user="postgres", password=password, dbname=dbname, autocommit=True, connect_timeout=5)

    def dsn(self, dbname: str) -> str:
        return f"host={self.socket_dir} dbname={dbname} user=shop"

    def cpuacct_path(self) -> str:
        assert self.proc is not None
        return self.proc.cpuacct_path()

    def pids(self) -> list[int]:
        return self.proc.pids() if self.proc is not None else []

    # ------------------------------------------------------------------ databases
    def has_template(self) -> bool:
        with self.superuser("postgres") as conn:
            return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEMPLATE_DB,)).fetchone() is not None

    def build_template(self, schema_sql: str, seed_sql: str) -> float:
        """Create and seed the template database. Returns seconds taken."""
        t0 = time.monotonic()
        with self.superuser("postgres") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE_DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEMPLATE_DB}")
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'shop'").fetchone() is None:
                conn.execute("CREATE ROLE shop LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        with self.superuser(TEMPLATE_DB) as conn:
            conn.execute(schema_sql)
            conn.execute(seed_sql)
            conn.execute("VACUUM (FREEZE, ANALYZE)")
            conn.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO shop")
            conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO shop")
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        return time.monotonic() - t0

    def clone(self, name: str) -> None:
        with self.superuser("postgres") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE_DB}")
            # No temporary tables for candidates: on a pooled connection they outlive the request
            # (a cross-request cache). Database ACLs are not copied from the template.
            conn.execute(f"REVOKE TEMPORARY ON DATABASE {name} FROM PUBLIC")

    def drop(self, name: str) -> None:
        with self.superuser("postgres") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")

    def list_databases(self, prefix: str) -> list[str]:
        with self.superuser("postgres") as conn:
            rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (prefix + "%",)).fetchall()
        return [r[0] for r in rows]

    def apply_indexes(self, dbname: str, wanted: Mapping[str, str]) -> dict[str, list[str]]:
        """Reconcile the ``colloid_*`` indexes of ``dbname`` with ``wanted`` (name → DDL)."""
        with self.superuser(dbname) as conn:
            have = {r[0] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname LIKE 'colloid_%'").fetchall()}
            created, dropped = [], []
            for name in sorted(have - set(wanted)):
                conn.execute(f"DROP INDEX {name}")
                dropped.append(name)
            for name in sorted(set(wanted) - have):
                conn.execute(wanted[name])
                created.append(name)
            if created:
                conn.execute("ANALYZE")
        return {"created": created, "dropped": dropped}

    def reset_stat_statements(self, dbname: str) -> None:
        with self.superuser(dbname) as conn:
            conn.execute("SELECT pg_stat_statements_reset()")

    def stat_statements(self, dbname: str) -> list[dict[str, Any]]:
        with self.superuser(dbname) as conn:
            rows = conn.execute(
                "SELECT query, calls, total_exec_time, rows FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid "
                "WHERE d.datname = %s ORDER BY total_exec_time DESC",
                (dbname,),
            ).fetchall()
        return [{"query": r[0], "calls": int(r[1]), "total_ms": float(r[2]), "rows": int(r[3])} for r in rows]

    def destroy(self) -> None:
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)
