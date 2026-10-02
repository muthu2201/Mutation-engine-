"""Git-branch-backed mutation data lake.

The lake lives on its own branch (default ``colloid/datalake``) whose tree contains only the
lake files: no code, so it can be shared and mirrored without the engine. Every ingest is one
commit, so ``git log colloid/datalake`` is the day-by-day history of what Colloid learned.

Only git plumbing is used (``colloid.adapters.gitref``), so the caller's checkout is never
touched: blobs, a temporary index, ``commit-tree`` and a compare-and-swap ``update-ref``.
The first commit has no parent, so the branch shares no history with the code. If anyone
else moved the branch in between, git refuses and we raise :class:`LakeConflict`.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from colloid.adapters.gitref import RefMoved, commit_files, push
from colloid.adapters.lake import LAKE_README
from colloid.core.lake import SCHEMA, ChainError, LedgerEntry, Record, head
from colloid.ports import LakeConflict


class GitBranchLake:
    PORT_API = "1.0.0"

    def __init__(self, repo: Path, branch: str = "colloid/datalake") -> None:
        self.repo = Path(repo).resolve()
        self.branch = branch
        self.ref = f"refs/heads/{branch}"
        self.location = f"git:{self.repo}#{branch}"

    # ------------------------------------------------------------------ git
    def _git(self, *args: str, data: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
        res = subprocess.run(["git", "-C", str(self.repo), *args], input=data, capture_output=True,
                             env={**os.environ, **(env or {})}, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {res.stderr.decode(errors='replace').strip()[:400]}")
        return res.stdout

    def tip(self) -> str | None:
        res = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "-q", self.ref], capture_output=True, check=False)
        return res.stdout.decode().strip() or None

    def _blob(self, tip: str, path: str) -> bytes | None:
        res = subprocess.run(["git", "-C", str(self.repo), "cat-file", "blob", f"{tip}:{path}"], capture_output=True, check=False)
        return res.stdout if res.returncode == 0 else None

    # ------------------------------------------------------------------ read
    def entries(self) -> list[LedgerEntry]:
        tip = self.tip()
        raw = self._blob(tip, "ledger.jsonl") if tip else None
        if not raw:
            return []
        out = []
        for n, line in enumerate(raw.decode("utf-8").splitlines()):
            if line.strip():
                try:
                    out.append(LedgerEntry.from_json(json.loads(line)))
                except (ValueError, KeyError) as exc:
                    raise ChainError(f"ledger line {n}: unreadable ({exc})") from exc
        return out

    def records(self) -> dict[str, Record]:
        tip = self.tip()
        if not tip:
            return {}
        listing = self._git("ls-tree", "-r", "--format=%(objectname) %(path)", tip, "--", "records").decode().splitlines()
        if not listing:
            return {}
        # one `cat-file --batch` process for all blobs instead of one process per record
        oids = [line.split(" ", 1) for line in listing if line.strip()]
        out_raw = self._git("cat-file", "--batch", data="".join(f"{oid}\n" for oid, _ in oids).encode())
        out: dict[str, Record] = {}
        pos = 0
        for oid, path in oids:
            header_end = out_raw.index(b"\n", pos)
            _, _, size = out_raw[pos:header_end].decode().split(" ")
            body = out_raw[header_end + 1 : header_end + 1 + int(size)]
            pos = header_end + 1 + int(size) + 1
            rec = Record.from_json(json.loads(body))
            if Path(path).stem != rec.id:
                raise ChainError(f"{path}: file name does not match the record id {rec.id[:12]} (blob {oid[:10]})")
            out[rec.id] = rec
        return out

    # ------------------------------------------------------------------ write
    def commit(self, records: Sequence[Record], entries: Sequence[LedgerEntry], expected_head: str, message: str) -> str:
        tip = self.tip()
        current = self.entries()
        if head(current) != expected_head:
            raise LakeConflict(f"lake head moved: expected {expected_head[:12]}, found {head(current)[:12]}")
        all_entries = [*current, *entries]
        new_head = head(all_entries)
        files: dict[str, bytes] = {
            f"records/{r.id[:2]}/{r.id}.json": json.dumps(r.to_json(), sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8")
            for r in records
        }
        files["ledger.jsonl"] = "".join(json.dumps(e.to_json(), sort_keys=True, separators=(",", ":")) + "\n" for e in all_entries).encode()
        files["LAKE"] = json.dumps({"schema": SCHEMA, "head": new_head, "entries": len(all_entries), "last_message": message}, indent=1).encode()
        files["README.md"] = LAKE_README.encode()
        try:
            commit_files(self.repo, self.branch, files, f"{message}\n\nlake-head: {new_head}\nentries: {len(all_entries)}", expected_tip=tip)
        except RefMoved as exc:
            raise LakeConflict(str(exc)) from exc
        return new_head

    def push(self, remote: str = "origin") -> str:
        return push(self.repo, self.branch, remote)
