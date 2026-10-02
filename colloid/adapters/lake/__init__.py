"""LakeStore adapters: where the mutation data lake lives.

* :class:`~colloid.adapters.lake.directory.DirectoryLake` - a plain directory. Works on every
  platform, needs nothing but a filesystem, and is what runs write to by default.
* :class:`~colloid.adapters.lake.gitbranch.GitBranchLake` - a dedicated git branch (default
  ``colloid/datalake``) that holds only the lake, written with git plumbing. It never touches
  the working tree or the real index, so it is safe to run inside a checkout with
  uncommitted work. Git adds a second hash chain (commits) on top of the ledger's own, and
  the branch can be pushed, mirrored and audited like any other.

Both share one on-disk layout::

    LAKE                       {"schema", "head", "entries"} - the current head, for quick checks
    README.md                  what this branch/directory is and how to verify it
    ledger.jsonl               one LedgerEntry per line, append-only, hash-chained
    records/<id[:2]>/<id>.json one Record per file, content-addressed

:func:`open_lake` picks a backend from a location string: ``git:<branch>`` (current repo),
``git:<repo-path>#<branch>``, or a directory path.
"""

from __future__ import annotations

from pathlib import Path

from colloid.ports import LakeStore

LAKE_README = """# Colloid mutation data lake

This location holds **only** Colloid's mutation data lake: every mutation Colloid has verified
(L6 deep assurance + replicate measurement against baseline), stored as content-addressed
records in a hash-chained, append-only ledger.

- `records/<id[:2]>/<id>.json`: one record per file. `id = sha256("colloid.mutation/1" \\0 kind
  \\0 canonical-JSON(content))`. **gene** records describe a single change (locus, payload,
  explanation, provenance). **program** records describe a verified combination of genes
  and its evidence (effects with confidence intervals, holdout, attribution, noise floor,
  platform fingerprint, run, engine commit).
- `ledger.jsonl`: entry *n* = `{seq, record, kind, prev, recorded_at, entry_hash}`, with
  `prev` = entry *n-1*'s `entry_hash`. The head hash in `LAKE` commits to the whole history.
  Which mutation is older or newer is its `seq`, and the chain makes that order tamper-evident.

Verify everything: `colloid lake verify <location>`. Records are never edited or deleted. A
newer measurement of the same mutation is a new program record whose `derived_from` points at
the older one.
"""


def open_lake(location: str) -> LakeStore:
    from colloid.adapters.lake.directory import DirectoryLake
    from colloid.adapters.lake.gitbranch import GitBranchLake

    if location.startswith("git:"):
        spec = location[4:]
        repo, _, branch = spec.rpartition("#") if "#" in spec else (".", "", spec)
        return GitBranchLake(Path(repo or "."), branch or "colloid/datalake")
    return DirectoryLake(Path(location))
