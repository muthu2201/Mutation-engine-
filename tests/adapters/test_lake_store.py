"""LakeStore conformance: directory and git-branch backends behave identically, are atomic,
refuse a concurrent writer, and the git backend never touches the caller's checkout."""

import subprocess

import pytest

from colloid.adapters.lake.directory import DirectoryLake
from colloid.adapters.lake.gitbranch import GitBranchLake
from colloid.core.lake import Record, append, head, verify_chain
from colloid.ports import LakeConflict, LakeStore

T0, T1 = "2026-10-01T00:00:00.000000Z", "2026-10-02T00:00:00.000000Z"


def gene(n):
    return Record.make("gene", {"target": "t", "locus": {"unit_id": f"u{n}"}, "payload_kind": "value", "payload": {"value": n},
                                "explain": f"g{n}", "provenance": {"operator": "test"}})


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "code.py").write_text("print('engine')\n")
    git(r, "add", "code.py")
    git(r, "commit", "-q", "-m", "code")
    (r / "wip.py").write_text("uncommitted work\n")  # must survive untouched
    (r / "code.py").write_text("print('edited, not staged')\n")
    return r


@pytest.fixture(params=["directory", "git"])
def lake(request, tmp_path, repo):
    return DirectoryLake(tmp_path / "lake") if request.param == "directory" else GitBranchLake(repo, "colloid/datalake")


def commit_two_batches(lake):
    g1, g2, g3 = gene(1), gene(2), gene(3)
    e1 = append([], [g1, g2], T0)
    lake.commit([g1, g2], e1, head([]), "batch 1")
    e2 = append(e1, [g3], T1)
    h = lake.commit([g3], e2, head(e1), "batch 2")
    return h, [g1, g2, g3]


def test_roundtrip_and_chain(lake):
    assert isinstance(lake, LakeStore)
    h, genes = commit_two_batches(lake)
    entries, records = lake.entries(), lake.records()
    assert [e.seq for e in entries] == [0, 1, 2] and set(records) == {g.id for g in genes}
    assert verify_chain(entries, records) == h


def test_concurrent_writer_is_refused(lake):
    commit_two_batches(lake)
    stale = head([])  # a writer that read the lake before anything was committed
    g9 = gene(9)
    with pytest.raises(LakeConflict):
        lake.commit([g9], append([], [g9], T1), stale, "stale writer")
    assert len(lake.entries()) == 3  # nothing partial was written


def test_git_backend_leaves_the_checkout_untouched(repo):
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"), git(repo, "symbolic-ref", "HEAD"), git(repo, "diff"))
    lake = GitBranchLake(repo, "colloid/datalake")
    commit_two_batches(lake)
    after = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"), git(repo, "symbolic-ref", "HEAD"), git(repo, "diff"))
    assert before == after  # same commit, same branch, same uncommitted and unstaged work
    log = git(repo, "log", "--format=%H %P %s", "colloid/datalake").splitlines()
    assert len(log) == 2 and log[0].endswith("batch 2") and len(log[1].split(" ")[1]) != 40  # root commit: no parent
    files = set(git(repo, "ls-tree", "-r", "--name-only", "colloid/datalake").splitlines())
    assert {"LAKE", "README.md", "ledger.jsonl"} <= files and all(f in {"LAKE", "README.md", "ledger.jsonl"} or f.startswith("records/") for f in files)
    assert "code.py" not in files  # the data branch holds no engine code
    no_common = subprocess.run(["git", "-C", str(repo), "merge-base", "main", "colloid/datalake"], capture_output=True, check=False)
    assert no_common.returncode == 1  # the data branch shares no history with the code


def test_git_backend_detects_a_moved_branch(repo):
    lake = GitBranchLake(repo, "colloid/datalake")
    commit_two_batches(lake)
    entries = lake.entries()
    other = GitBranchLake(repo, "colloid/datalake")  # a second writer appends first
    g7 = gene(7)
    other.commit([g7], append(entries, [g7], T1), head(entries), "other writer")
    g8 = gene(8)
    with pytest.raises(LakeConflict):
        lake.commit([g8], append(entries, [g8], T1), head(entries), "loser")
