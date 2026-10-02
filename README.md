# Colloid mutation data lake

This location holds **only** Colloid's mutation data lake: every mutation Colloid has verified
(L6 deep assurance + replicate measurement against baseline), stored as content-addressed
records in a hash-chained, append-only ledger.

- `records/<id[:2]>/<id>.json`: one record per file. `id = sha256("colloid.mutation/1" \0 kind
  \0 canonical-JSON(content))`. **gene** records describe a single change (locus, payload,
  explanation, provenance). **program** records describe a verified combination of genes
  and its evidence (effects with confidence intervals, holdout, attribution, noise floor,
  platform fingerprint, run, engine commit).
- `ledger.jsonl`: entry *n* = `{seq, record, kind, prev, recorded_at, entry_hash}`, with
  `prev` = entry *n-1*'s `entry_hash`. The head hash in `LAKE` commits to the whole history.
  Which mutation is older or newer is its `seq`, and the chain makes that order tamper-evident.

Verify everything: `colloid lake verify <location>`. Records are never edited or deleted. A
newer measurement of the same mutation is a new program record whose `derived_from` points at
the older one.
