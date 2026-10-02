"""Materialised stacks: a lake record becomes a deployable artifact with an evidence manifest;
carrying-only drops hitchhikers; publishing writes a data-only branch without touching the checkout."""

import json
import subprocess

from colloid.adapters.lake.directory import DirectoryLake
from colloid.adapters.store.sql_store import open_store
from colloid.core.models import AttributionRecord
from colloid.services import lake as svc
from colloid.services import stack
from tests.adapters.test_lake_ingest import (  # noqa: F401  (fixture)
    RATING,
    code_gene,
    knob_gene,
    make_run,
    target,
)


def lake_with_attribution(tmp_path, atlas):
    kg = knob_gene(atlas, "db.idx_reviews_product", True)
    cg = code_gene(atlas, RATING, lambda s: s.replace("async def", "async  def", 1))
    run, (pid,) = make_run(tmp_path, "r", atlas, [[kg, cg]])
    st = open_store(f"sqlite:///{tmp_path / 'r' / 'colloid.db'}")
    st.put_attribution(AttributionRecord(program_id=pid, gene_id=kg.id, method="shapley_exact", objective="cost", value=0.32, ci_lo=0.27, ci_hi=0.37))
    st.put_attribution(AttributionRecord(program_id=pid, gene_id=cg.id, method="shapley_exact", objective="cost", value=0.0, ci_lo=-0.07, ci_hi=0.07))
    st.close()
    lake = DirectoryLake(tmp_path / "lake")
    rep = svc.ingest_run(run, lake, log=lambda m: None)
    return lake, rep.programs[0]["record"]


def test_materialize_full_and_carrying_only(tmp_path, target):  # noqa: F811
    _, atlas = target
    lake, rid = lake_with_attribution(tmp_path, atlas)
    full = stack.materialize(lake, rid[:12], tmp_path / "full")
    assert len(full["genes"]) == 2 and full["dropped_hitchhikers"] == []
    assert "async  def rating_summary" in (tmp_path / "full" / "service" / "shop" / "search.py").read_text()  # source gene applied
    lean = stack.materialize(lake, rid[:12], tmp_path / "lean", carrying_only=True)
    assert [g["explain"] for g in lean["genes"]] == ["db.idx_reviews_product = True [knob_sample]"]
    assert len(lean["dropped_hitchhikers"]) == 1 and "not a separate L6 run" in lean["evidence"]["this_artifact"]
    assert "async  def" not in (tmp_path / "lean" / "service" / "shop" / "search.py").read_text()  # hitchhiker left out
    sql = (tmp_path / "lean" / "db" / "migrations" / "0001_colloid.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS colloid_idx_reviews_product ON reviews (product_id);" in sql
    diff = json.loads((tmp_path / "lean" / "runtime" / "launch.diff.json").read_text())
    assert set(diff) == {"indexes"}  # nothing else differs from the baseline
    m = json.loads((tmp_path / "lean" / "MANIFEST.json").read_text())
    assert m["record"] == rid and m["lake"]["head"] and "colloid stack materialize" in m["reproduce"]


def test_publish_is_a_data_only_branch(tmp_path, target):  # noqa: F811
    _, atlas = target
    lake, rid = lake_with_attribution(tmp_path, atlas)
    stack.materialize(lake, rid[:12], tmp_path / "lean", carrying_only=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (repo / "x.py").write_text("x = 1\n")
    g("add", "x.py")
    g("commit", "-q", "-m", "code")
    before = (g("rev-parse", "HEAD"), g("status", "--porcelain"))
    c1 = stack.publish(tmp_path / "lean", "stack/stackzero-verified", repo)
    c2 = stack.publish(tmp_path / "lean", "stack/stackzero-verified", repo)  # a re-materialisation is a new commit on top
    assert (g("rev-parse", "HEAD"), g("status", "--porcelain")) == before
    assert g("rev-parse", "stack/stackzero-verified^") == c1 and g("rev-parse", "stack/stackzero-verified") == c2
    files = set(g("ls-tree", "-r", "--name-only", "stack/stackzero-verified").splitlines())
    assert {"MANIFEST.json", "README.md", "db/migrations/0001_colloid.sql"} <= files and "x.py" not in files
