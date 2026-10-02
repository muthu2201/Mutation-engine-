"""A small async database helper on top of a psycopg connection pool."""

from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool


class Executor:
    """Query helpers shared by the pool-backed Database and a transaction."""

    async def _run(self, sql, params):
        raise NotImplementedError

    async def fetch(self, sql, params=()):
        cur = await self._run(sql, params)
        return await cur.fetchall()

    async def fetchrow(self, sql, params=()):
        cur = await self._run(sql, params)
        return await cur.fetchone()

    async def fetchval(self, sql, params=()):
        row = await self.fetchrow(sql, params)
        return None if row is None else row[0]

    async def execute(self, sql, params=()):
        cur = await self._run(sql, params)
        return cur.rowcount


class Transaction(Executor):
    def __init__(self, conn):
        self.conn = conn

    async def _run(self, sql, params):
        return await self.conn.execute(sql, params)


class Database(Executor):
    def __init__(self, dsn, *, min_size, max_size, prepare_threshold, options):
        kwargs = {"autocommit": True, "prepare_threshold": prepare_threshold}
        if options:
            kwargs["options"] = options
        self.pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, kwargs=kwargs, open=False)

    async def open(self):
        await self.pool.open(wait=True, timeout=30)

    async def close(self):
        await self.pool.close()

    async def _run(self, sql, params):
        async with self.pool.connection() as conn:
            cur = conn.cursor()
            await cur.execute(sql, params)
            # Materialise rows before the connection returns to the pool.
            rows = await cur.fetchall() if cur.description is not None else []
            return _Result(rows, cur.rowcount)

    @asynccontextmanager
    async def transaction(self):
        async with self.pool.connection() as conn, conn.transaction():
            yield Transaction(conn)


class _Result:
    def __init__(self, rows, rowcount):
        self._rows = rows
        self.rowcount = rowcount

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None
