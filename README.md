# stackzero: verified by Colloid

Materialised from lake record `fc1bc599367a2d44` (program `58c5d609e82eef78`, run `stackzero`), lake head `5df73c4e8a76f7d4`. Regenerate with `colloid stack materialize fc1bc599367a2d44 --lake git:/home/user/Mutation-engine-#colloid/datalake --carrying-only`.

## What changed

- db.idx_reviews_product = True [knob_sample] (carries measured gain)
- db.idx_orders_customer_placed = True [knob_perturb] (carries measured gain)

Left out (measured contribution indistinguishable from zero):

- shop_score_batch: code rewrite (llm_rewrite/optimize) optimize
- Executor.fetchrow: code rewrite (gi_edit) copy line 3 before line 3
- score: code rewrite (llm_rewrite/optimize) optimize

## Evidence

- Verified program: cost per request **30.95%** lower, 95% CI [27.27, 34.45] (replicate-x6), p50 46.93%, p95 40.43%, memory -0.03%.
- This artifact: the genes whose measured contribution CI lies above zero. The full program they come from was L6-verified; this subset's own measurement is the leave-one-gene-out ablation (oracle-checked, replicate-measured), not a separate L6 run. Measured on its own: cost 31.33% lower, 95% CI [28.15, 34.11] (L2 oracle + replicate, 6 cycles).
- Per-gene attribution (log-ratio, 95% CI) is in MANIFEST.json.

## Deploy

1. `db/migrations/0001_colloid.sql`: index DDL, idempotent.
2. `db/postgresql.colloid.conf`: server settings. (none)
3. `db/session.colloid.sql`: per-database settings. (none)
4. `service/`, `native/`: application code with the verified source changes applied; `runtime/launch.diff.json` lists every runtime/allocator/compiler setting that differs from the baseline.

Measured on the platform recorded in MANIFEST.json. Re-verify on your own hardware (`colloid run` with `lake:` pointing at the lake re-evaluates these genes from scratch).
