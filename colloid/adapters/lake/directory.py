"""Directory-backed mutation data lake (portable: Linux, macOS, Windows).

Atomicity comes from write-to-temp + ``os.replace``, which is atomic on POSIX and on Windows
(same volume). Record files are content-addressed, so writing one twice is harmless. The
ledger is replaced as a whole file, never appended in place, so a crash leaves either the old
ledger or the new one and never a torn line. Writers serialise on a lock file created with
``O_CREAT | O_EXCL``, the one exclusive-create primitive every OS supports. The head is
re-checked under the lock, so a concurrent writer is a clean :class:`LakeConflict`, never a
fork of the chain.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from colloid.adapters.lake import LAKE_README
from colloid.core.lake import SCHEMA, ChainError, LedgerEntry, Record, head
from colloid.ports import LakeConflict


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class DirectoryLake:
    PORT_API = "1.0.0"

    def __init__(self, root: Path, lock_timeout_s: float = 30.0, stale_lock_s: float = 600.0) -> None:
        self.root = Path(root)
        self.location = str(self.root)
        self.lock_timeout_s = lock_timeout_s
        self.stale_lock_s = stale_lock_s

    # ------------------------------------------------------------------ read
    def entries(self) -> list[LedgerEntry]:
        p = self.root / "ledger.jsonl"
        if not p.exists():
            return []
        out = []
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            if line.strip():
                try:
                    out.append(LedgerEntry.from_json(json.loads(line)))
                except (ValueError, KeyError) as exc:
                    raise ChainError(f"ledger line {n}: unreadable ({exc})") from exc
        return out

    def records(self) -> dict[str, Record]:
        out: dict[str, Record] = {}
        base = self.root / "records"
        if not base.exists():
            return out
        for f in sorted(base.glob("*/*.json")):
            rec = Record.from_json(json.loads(f.read_text(encoding="utf-8")))
            if f.stem != rec.id:
                raise ChainError(f"{f.name}: file name does not match the record id {rec.id[:12]}")
            out[rec.id] = rec
        return out

    # ------------------------------------------------------------------ write
    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".lock"
        deadline = time.monotonic() + self.lock_timeout_s
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > self.stale_lock_s:
                        lock.unlink()  # a writer died holding it
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise LakeConflict(f"lake {self.root} is locked by another writer") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def commit(self, records: Sequence[Record], entries: Sequence[LedgerEntry], expected_head: str, message: str) -> str:
        with self._lock():
            current = self.entries()
            if head(current) != expected_head:
                raise LakeConflict(f"lake head moved: expected {expected_head[:12]}, found {head(current)[:12]}")
            for rec in records:
                _atomic_write(self.root / "records" / rec.id[:2] / f"{rec.id}.json",
                              json.dumps(rec.to_json(), sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8"))
            all_entries = [*current, *entries]
            ledger = "".join(json.dumps(e.to_json(), sort_keys=True, separators=(",", ":")) + "\n" for e in all_entries)
            _atomic_write(self.root / "ledger.jsonl", ledger.encode("utf-8"))
            new_head = head(all_entries)
            _atomic_write(self.root / "LAKE", json.dumps({"schema": SCHEMA, "head": new_head, "entries": len(all_entries),
                                                          "last_message": message}, indent=1).encode("utf-8"))
            if not (self.root / "README.md").exists():
                _atomic_write(self.root / "README.md", LAKE_README.encode("utf-8"))
            return new_head
