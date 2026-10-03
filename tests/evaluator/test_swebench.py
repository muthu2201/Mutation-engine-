"""The SWE-bench judge's pure parts, and the rule that the search never sees the gold fields (ADR 0011)."""

import json
from pathlib import Path

import pytest

from colloid.adapters.lake.directory import DirectoryLake
from colloid.core.lake import verify_chain
from colloid.services import swebench as swe
from colloid_evaluator.swebench import policy, repos

PATCH = """diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -7,7 +7,7 @@
-    regex = r'^[\\w.@+-]+$'
+    regex = r'\\A[\\w.@+-]+\\Z'
"""


def test_patch_policy():
    assert policy.check_patch(PATCH) == []
    assert policy.check_patch("") == ["empty patch"]
    bad = PATCH.replace("django/contrib/auth/validators.py", "tests/auth_tests/test_validators.py")
    assert any("tests are the judge's" in v for v in policy.check_patch(bad))
    assert any("only Python" in v for v in policy.check_patch(PATCH.replace("validators.py", "README.rst")))
    deleted = "diff --git a/x.py b/x.py\ndeleted file mode 100644\n--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    assert any("deletes a file" in v for v in policy.check_patch(deleted))
    big = PATCH + "".join(f"+x{i} = {i}\n" for i in range(300))
    assert any("changed lines" in v for v in policy.check_patch(big))
    assert policy.is_test_path("sympy/core/tests/test_basic.py") and policy.is_test_path("conftest.py")
    assert not policy.is_test_path("sympy/core/basic.py")


def test_test_commands_and_selection(tmp_path: Path):
    assert repos.test_command("django/django", ["tests/auth_tests/test_validators.py"]).endswith("--parallel 1 auth_tests.test_validators")
    assert repos.test_command("pydata/xarray", ["xarray/tests/test_a.py"]) == "pytest -rA xarray/tests/test_a.py"
    (tmp_path / "tests/auth_tests").mkdir(parents=True)
    (tmp_path / "tests/auth_tests/test_validators.py").write_text("from django.contrib.auth import validators\n")
    (tmp_path / "tests/auth_tests/test_forms.py").write_text("import django.contrib.auth.forms\n")
    (tmp_path / "tests/other/").mkdir(parents=True)
    (tmp_path / "tests/other/test_misc.py").write_text("x = 'django.contrib.auth.validators'\n")
    picked = repos.select_tests(tmp_path, ["django/contrib/auth/validators.py"])
    assert picked[0] == "tests/auth_tests/test_validators.py" and "tests/auth_tests/test_forms.py" not in picked


def test_search_never_reads_gold_fields(tmp_path: Path):
    leaky = tmp_path / "tasks.jsonl"
    leaky.write_text(json.dumps({"instance_id": "x", "problem_statement": "p", "FAIL_TO_PASS": ["t"]}) + "\n")
    with pytest.raises(ValueError, match="gold fields"):
        swe.load_tasks(leaky)
    for mod in ("colloid/services/swebench.py", "colloid/core/repair.py"):
        src = (Path(__file__).resolve().parents[2] / mod).read_text()
        assert "open(gold" not in src and "gold.read" not in src and "gold.open" not in src, mod


def test_resolved_fixes_enter_the_lake_and_unresolved_ones_do_not(tmp_path: Path):
    run = tmp_path / "swebench-run"
    run.mkdir()
    sub = {"snippet": "py:django/contrib/auth/validators.py::ASCIIUsernameValidator.<L7-9>", "model": "qwen2.5-coder-3b",
           "template": "fix", "diff": PATCH, "votes": 1, "changed_lines": 2, "new_source": "regex = r'\\A[\\w.@+-]+\\Z'\n",
           "prompt_hash": "p", "response_hash": "r"}
    rows = [{"instance_id": "django__django-11099", "repo": "django/django", "base_commit": "abc", "image_digest": "img@sha256:1",
             "submission": sub, "grade": {"resolved": True, "fail_to_pass": {"success": 3, "failure": 0}}},
            {"instance_id": "django__django-1", "repo": "django/django", "base_commit": "def", "submission": sub, "grade": {"resolved": False}}]
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    lake = DirectoryLake(tmp_path / "lake")
    assert swe.ingest(run, lake, recorded_at="2026-10-02T00:00:00.000000Z") == 2
    records = lake.records()
    verify_chain(lake.entries(), records)
    prog = next(r for r in records.values() if r.kind == "program")
    assert prog.content["target"] == "swebench:django/django" and prog.content["effects"]["resolved"] is True
    assert swe.ingest(run, lake, recorded_at="2026-10-02T00:00:01.000000Z") == 0  # idempotent


def test_ingested_programs_carry_the_memorisation_verdict_of_the_solving_model(tmp_path: Path):
    run = tmp_path / "swebench-run"
    run.mkdir()
    sub = {"snippet": "py:django/contrib/auth/validators.py::ASCIIUsernameValidator.<L7-9>", "model": "qwen2.5-coder-7b",
           "template": "fix", "diff": PATCH, "votes": 0, "changed_lines": 2, "new_source": "x\n", "prompt_hash": "p", "response_hash": "r"}
    (run / "results.jsonl").write_text(json.dumps({"instance_id": "django__django-11099", "repo": "django/django", "base_commit": "abc",
                                                   "submission": sub, "grade": {"resolved": True}}) + "\n")
    probe = {"instance_id": "django__django-11099", "path_hit": True, "path_mentioned_in_issue": False, "task_id_overlap": 0.7,
             "task_id_exact_lines": 1, "verdict": "suspect"}
    probes = {"probes": [{**probe, "model": "qwen2.5-coder-3b", "verdict": "clean"}, {**probe, "model": "qwen2.5-coder-7b"}],
              "submissions": [{"instance_id": "django__django-11099", "overlap5": 1.0, "identical_added_lines": True}]}
    lake = DirectoryLake(tmp_path / "lake")
    assert swe.ingest(run, lake, probes=probes, recorded_at="2026-10-02T00:00:00.000000Z") == 2
    prog = next(r for r in lake.records().values() if r.kind == "program")
    mp = prog.content["memorisation_probe"]
    assert mp["verdict"] == "suspect" and mp["task_id_exact_lines"] == 1 and mp["submission_identical_to_gold"] is True
