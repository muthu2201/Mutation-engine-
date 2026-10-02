"""Telemetry adapter: structured JSON-lines events and spans.

Every engine phase emits events (``generation.start``, ``cascade.stage``, ``llm.call``,
``island.migrate``...) and spans with wall-clock durations to an append-only JSONL file.
The dashboard tails this file for its live view, and the file is the raw material for the
run report. Records carry a run id and monotonically increasing sequence numbers so a
crash-truncated file is still parseable up to the last complete line.

A Prometheus text exposition of counters is available via :meth:`prometheus` for scraping.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class JsonlTelemetry:
    PORT_API = "1.0.0"

    def __init__(self, path: str | Path, run_id: str | None = None, echo: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.echo = echo
        self._lock = threading.Lock()
        self._seq = 0
        self.counters: Counter[str] = Counter()
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115 - long-lived handle

    def emit(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self._seq += 1
            rec = {"t": time.time(), "run": self.run_id, "seq": self._seq, "kind": kind, **fields}
            line = json.dumps(rec, default=str, separators=(",", ":"))
            self._fh.write(line + "\n")
            self.counters[kind] += 1
        if self.echo:
            short = {k: v for k, v in fields.items() if not isinstance(v, (dict, list)) or len(str(v)) < 120}
            print(f"[{time.strftime('%H:%M:%S')}] {kind} {short}", flush=True)

    @contextmanager
    def span(self, name: str, **fields: Any) -> Iterator[dict[str, Any]]:
        span_id = uuid.uuid4().hex[:8]
        start = time.monotonic()
        extra: dict[str, Any] = {}
        self.emit(f"{name}.start", span=span_id, **fields)
        try:
            yield extra
        except BaseException as exc:
            self.emit(f"{name}.error", span=span_id, duration_s=round(time.monotonic() - start, 4), error=repr(exc)[:500], **fields)
            raise
        self.emit(f"{name}.end", span=span_id, duration_s=round(time.monotonic() - start, 4), **fields, **extra)

    def prometheus(self) -> str:
        lines = ["# TYPE colloid_events_total counter"]
        for kind, n in sorted(self.counters.items()):
            lines.append(f'colloid_events_total{{kind="{kind}"}} {n}')
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def read_events(path: str | Path, kinds: set[str] | None = None) -> list[dict[str, Any]]:
    out = []
    p = Path(path)
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kinds is None or rec.get("kind") in kinds:
                out.append(rec)
    return out
