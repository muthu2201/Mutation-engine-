"""ASGI application: routing, request parsing, error handling and lifecycle."""

import gc
import json
import re
import sys
import traceback
from urllib.parse import parse_qs

from shop import handlers, search, util
from shop.config import settings
from shop.db import Database

ROUTES = [
    ("GET", re.compile(r"^/healthz$"), "health"),
    ("GET", re.compile(r"^/products/search$"), "search"),
    ("GET", re.compile(r"^/products/(\d+)$"), "product"),
    ("GET", re.compile(r"^/customers/(\d+)/summary$"), "customer_summary"),
    ("GET", re.compile(r"^/customers/(\d+)/recommendations$"), "recommendations"),
    ("GET", re.compile(r"^/categories/(\d+)/top$"), "category_top"),
    ("GET", re.compile(r"^/reports/daily$"), "daily_report"),
    ("POST", re.compile(r"^/orders$"), "create_order"),
]

state = {"db": None}


def query_param(query, name, default=None):
    values = query.get(name)
    return values[0] if values else default


async def dispatch(route, match, query, body):
    db = state["db"]
    if route == "health":
        return {"ok": True}
    if route == "search":
        q = query_param(query, "q", "")
        limit = util.parse_int(query_param(query, "limit", "10"), "limit", 1, 50)
        return await search.search_products(db, q, limit)
    if route == "product":
        return await handlers.product_detail(db, int(match.group(1)))
    if route == "customer_summary":
        return await handlers.customer_summary(db, int(match.group(1)))
    if route == "recommendations":
        return await handlers.recommendations(db, int(match.group(1)))
    if route == "category_top":
        limit = util.parse_int(query_param(query, "limit", "10"), "limit", 1, 100)
        return await handlers.category_top(db, int(match.group(1)), limit)
    if route == "daily_report":
        as_of = util.parse_date(query_param(query, "as_of"), "as_of")
        days = util.parse_int(query_param(query, "days", "7"), "days", 1, 366)
        return await handlers.daily_report(db, as_of, days)
    if route == "create_order":
        try:
            payload = json.loads(body or b"null")
        except ValueError:
            raise util.HTTPError(400, "invalid JSON body") from None
        return await handlers.create_order(db, payload)
    raise util.HTTPError(404, "not found")


async def read_body(receive):
    chunks = []
    more = True
    while more:
        message = await receive()
        chunks.append(message.get("body", b""))
        more = message.get("more_body", False)
    return b"".join(chunks)


async def respond(send, status, payload):
    body = util.to_json(payload, settings.json_compact)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def lifespan(receive, send):
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                gc.set_threshold(settings.gc_gen0, 10, 10)
                db = Database(
                    settings.dsn,
                    min_size=settings.pool_min,
                    max_size=settings.pool_max,
                    prepare_threshold=settings.prepare_threshold,
                    options=settings.pg_options,
                )
                await db.open()
                state["db"] = db
                if settings.gc_freeze:
                    gc.collect()
                    gc.freeze()
            except Exception as exc:
                await send({"type": "lifespan.startup.failed", "message": repr(exc)})
                return
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            if state["db"] is not None:
                await state["db"].close()
            await send({"type": "lifespan.shutdown.complete"})
            return


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await lifespan(receive, send)
        return
    if scope["type"] != "http":
        return
    method, path = scope["method"], scope["path"]
    for route_method, pattern, route in ROUTES:
        match = pattern.match(path)
        if match and route_method == method:
            break
    else:
        await respond(send, 404, {"error": "not found"})
        return
    query = parse_qs(scope.get("query_string", b"").decode())
    body = await read_body(receive) if method == "POST" else b""
    try:
        payload = await dispatch(route, match, query, body)
    except util.HTTPError as exc:
        await respond(send, exc.status, {"error": exc.message})
        return
    except Exception:
        traceback.print_exc(file=sys.stderr)
        await respond(send, 500, {"error": "internal server error"})
        return
    await respond(send, 201 if route == "create_order" else 200, payload)
