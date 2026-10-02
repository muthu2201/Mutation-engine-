"""Applying OS-layer knob genes (sysctls, transparent huge pages) with guaranteed restore.

Kernel knobs are global to the machine, so a crash in the middle of an evaluation must not
leave the host misconfigured. Before the first write, the original value of every key is
recorded in a *journal* file. :meth:`OsLayer.restore` writes the journal values back and
deletes the journal; it is called after each phase that changed something, on engine
shutdown, and - crucially - at the next start-up if a previous run died without restoring.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

THP_ROOT = Path("/sys/kernel/mm/transparent_hugepage")
SYSCTL_ROOT = Path("/proc/sys")


def _path(kind: str, key: str) -> Path:
    if kind == "sysctl":
        p = (SYSCTL_ROOT / key).resolve()
        if not str(p).startswith(str(SYSCTL_ROOT)):
            raise ValueError(f"sysctl key escapes /proc/sys: {key}")
        return p
    if kind == "thp":
        if key not in ("enabled", "defrag"):
            raise ValueError(f"unknown THP key {key}")
        return THP_ROOT / key
    raise ValueError(kind)


def _read(kind: str, key: str) -> str:
    text = _path(kind, key).read_text().strip()
    if kind == "thp" and "[" in text:
        return text[text.index("[") + 1 : text.index("]")]
    return text


class OsLayer:
    def __init__(self, journal: Path = Path("/opt/colloid/state/os_journal.json")) -> None:
        self.journal = journal
        self.journal.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, str]:
        if self.journal.exists():
            return dict(json.loads(self.journal.read_text()))
        return {}

    def _save(self, data: dict[str, str]) -> None:
        tmp = self.journal.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.journal)

    def current(self, os_cfg: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
        return {f"{kind}:{key}": _read(kind, key) for kind, entries in os_cfg.items() for key in entries}

    def apply(self, os_cfg: Mapping[str, Mapping[str, Any]]) -> list[str]:
        """Make the kernel match ``os_cfg`` for the keys it mentions and restore every other
        journaled key to its original value. Returns the keys that were written."""
        journal = self._load()
        wanted = {f"{kind}:{key}": str(value) for kind, entries in os_cfg.items() for key, value in entries.items()}
        written = []
        # Restore keys that are journaled but no longer wanted.
        for jkey, original in list(journal.items()):
            if jkey not in wanted:
                kind, key = jkey.split(":", 1)
                if _read(kind, key) != original:
                    _path(kind, key).write_text(original)
                    written.append(jkey)
                del journal[jkey]
        for jkey, value in wanted.items():
            kind, key = jkey.split(":", 1)
            cur = _read(kind, key)
            if jkey not in journal:
                journal[jkey] = cur
            if cur != value:
                self._save(journal)  # journal first, then write
                _path(kind, key).write_text(value)
                written.append(jkey)
        self._save(journal)
        if not journal:
            with contextlib.suppress(FileNotFoundError):
                self.journal.unlink()
        return written

    def restore(self) -> list[str]:
        journal = self._load()
        restored = []
        for jkey, original in journal.items():
            kind, key = jkey.split(":", 1)
            with contextlib.suppress(OSError):
                if _read(kind, key) != original:
                    _path(kind, key).write_text(original)
                    restored.append(jkey)
        with contextlib.suppress(FileNotFoundError):
            self.journal.unlink()
        return restored
