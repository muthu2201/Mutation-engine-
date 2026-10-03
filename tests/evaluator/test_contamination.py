"""Post-hoc memorisation probes (ADR 0011 addendum): scoring and verdicts, with a fake model."""

import json

from colloid_evaluator.swebench import contamination as c

GOLD = """diff --git a/django/contrib/auth/validators.py b/django/contrib/auth/validators.py
--- a/django/contrib/auth/validators.py
+++ b/django/contrib/auth/validators.py
@@ -7,7 +7,7 @@
-    regex = r'^[\\w.@+-]+$'
+    regex = r'\\A[\\w.@+-]+\\Z'
"""
TASK = {"instance_id": "django__django-11099", "repo": "django/django",
        "problem_statement": "UsernameValidator allows a trailing newline in usernames. The regex uses $."}


def test_scoring_helpers():
    assert c.added_lines(GOLD) == ["regex = r'\\A[\\w.@+-]+\\Z'"]
    gold = c.tokens(["a = compute(x, y) + 1"])
    assert c.overlap5(gold, gold) == 1.0 and c.overlap5(c.tokens(["b = other()"]), gold) == 0.0 and c.overlap5([], gold) == 0.0
    assert c.files_of(GOLD) == ["django/contrib/auth/validators.py"]
    assert c.predicted_path("```\ndjango/contrib/auth/validators.py\n```") == "django/contrib/auth/validators.py"
    assert c.mentioned("see django.contrib.auth.validators", "django/contrib/auth/validators.py")
    assert not c.mentioned(TASK["problem_statement"], "django/contrib/auth/validators.py")


def test_a_model_that_recalls_the_gold_patch_is_suspect():
    def ask(model, prompt):
        return "```\ndjango/contrib/auth/validators.py\n```" if "file path" in prompt else "```diff\n" + GOLD + "```"

    p = c.probe(TASK, {"patch": GOLD}, "m", ask)
    assert p.path_hit and not p.path_mentioned_in_issue and p.task_id_exact_lines == 1 and p.verdict == "suspect"


def test_naming_the_file_without_recalling_the_patch_is_path_only_and_a_miss_is_clean():
    def path_only(model, prompt):
        return "```\ndjango/contrib/auth/validators.py\n```" if "file path" in prompt else "```diff\n+print('hello world')\n```"

    assert c.probe(TASK, {"patch": GOLD}, "m", path_only).verdict == "path-only"

    def miss(model, prompt):
        return "```\ndjango/core/validators.py\n```" if "file path" in prompt else "I don't know."

    assert c.probe(TASK, {"patch": GOLD}, "m", miss).verdict == "clean"


def test_submission_overlap_needs_no_model():
    s = c.submission_overlap(GOLD, GOLD)
    assert s["identical_added_lines"] and s["gold_added_lines"] == 1
    other = GOLD.replace("\\A[\\w.@+-]+\\Z", "^[\\w.@+-]+\\Z")
    assert not c.submission_overlap(other, GOLD)["identical_added_lines"]


def test_run_paces_each_instance_and_scores_graded_submissions(tmp_path):
    rec = tmp_path / TASK["instance_id"] / "record.json"
    rec.parent.mkdir()
    rec.write_text(json.dumps({"submission": {"diff": GOLD, "model": "m"}, "grade": {"resolved": True}}))
    paced: list[int] = []

    def ask(model, prompt):
        return "I don't know."

    out = c.run([TASK["instance_id"]], {TASK["instance_id"]: TASK}, {TASK["instance_id"]: {"patch": GOLD}}, tmp_path, ask,
                ["m", "n"], log=lambda _: None, pace=paced.append)
    assert paced == [4] and [p["verdict"] for p in out["probes"]] == ["clean", "clean"]
    assert out["submissions"] == [{"instance_id": TASK["instance_id"], "resolved": True, "model": "m", "overlap5": 1.0,
                                   "identical_added_lines": True, "gold_added_lines": 1, "submission_added_lines": 1}]


def test_a_fix_spelled_out_in_the_issue_is_detected_without_a_model():
    patch = "diff --git a/m.py b/m.py\n+        if username is None or password is None:\n+            return\n"
    told = "My suggestion is to shortcut with:\n\t\tif username is None or password is None:\n\t\t\treturn"
    assert c.issue_states_fix(told, patch) == {"gold_lines_in_issue": 1, "gold_nontrivial_lines": 1, "issue_overlap5": 1.0}
    assert c.issue_states_fix("It crashes when the username is missing.", patch)["gold_lines_in_issue"] == 0
